"""The whole thing, over HTTP, from upload to destination and back.

The mock destination runs in-process behind an ASGI transport, so the real delivery
client — retries, idempotency, reconciliation — is what gets exercised.
"""
from __future__ import annotations

import socket
import threading
import time
import warnings
from collections import Counter
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """Real HTTP to a real destination process, so the delivery client is exercised."""
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("DBX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MOCK_TARGET_DB", str(tmp_path / "target.db"))

    import importlib

    import dbx_api.main as api_main
    import dbx_api.store as api_store
    import dbx_mock_target.main as target_main

    importlib.reload(target_main)
    importlib.reload(api_store)
    importlib.reload(api_main)

    port = _free_port()
    config = uvicorn.Config(target_main.app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)

    api_main.destination.base_url = f"http://127.0.0.1:{port}"
    try:
        yield TestClient(api_main.app), TestClient(target_main.app)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def start_run(client, folder: str = "run1", schema: str | None = None) -> str:
    """Upload files and approve a schema — the two steps before anything is processed."""
    run_id = client.post(
        "/api/runs/from-fixtures", json={"folder": folder}
    ).json()["run_id"]
    body = schema or (ROOT / "tests" / "fixtures" / "schemas" / "target_schema.yaml").read_text()
    version = client.post(f"/api/runs/{run_id}/schema", json={"body": body}).json()["version"]
    client.post(f"/api/runs/{run_id}/schema/{version}/approve")
    return run_id


def keep_sending(client, run_id: str) -> str:
    """Press the push button once and leave sending on, as a consultant would."""
    client.get(f"/api/runs/{run_id}?wait=true")
    client.post(f"/api/runs/{run_id}/deliver", json={"keep_sending": True})
    return run_id


def _answer(client, run, needle, value, action="correct"):
    state = client.get(f"/api/runs/{run}?wait=true").json()
    case = next((c for c in state["cases"] if needle in c["headline"]), None)
    assert case is not None, f"no open case matching {needle!r}"
    return client.post(
        f"/api/runs/{run}/cases/{case['key']}/decide",
        json={"action": action, "value": value},
    ).json()


@pytest.fixture
def run(stack):
    client, _ = stack
    return client, keep_sending(client, start_run(client))


def test_agent_processes_without_asking_about_everything(run):
    client, rid = run
    state = client.get(f"/api/runs/{rid}?wait=true").json()
    applied = [m for m in state["mappings"] if m["decision"] == "auto_apply"]
    assert len(applied) >= 40, "the agent should map most columns unaided"
    assert state["counts"]["records"] >= 35
    # A boundary, not a queue: far fewer cases than records, across five source files.
    assert 0 < state["counts"]["open_cases"] < state["counts"]["records"]


def test_one_answer_unblocks_many_records(run):
    client, rid = run
    assert client.get(f"/api/runs/{rid}?wait=true").json()["counts"]["ready"] == 0
    result = _answer(client, rid, "no column for status", "constant:ACTIVE")
    assert result["ready"] >= 10, "a field-level answer must release every record it blocked"


def test_pushing_sends_what_is_ready_and_nothing_else(run):
    """Delivery follows readiness once a person has asked for it.

    Pushing to the target is the one action with a consequence outside this tool,
    so it starts as a button. After the first push the consultant can leave sending
    on, and from then on readiness is enough.
    """
    client, rid = run
    before = client.get(f"/api/runs/{rid}?wait=true").json()["counts"]["delivered"]
    _answer(client, rid, "no column for status", "constant:ACTIVE")
    after = client.get(f"/api/runs/{rid}?wait=true").json()["counts"]

    assert after["delivered"] > before, "answering one case should send what it released"
    stored = client.get(f"/api/runs/{rid}/destination").json()["records"]
    assert len(stored) == after["delivered"], "the destination is the source of truth"

    again = client.post(f"/api/runs/{rid}/deliver").json()
    assert sum(again["sent"].values()) == 0, "nothing outstanding means nothing resent"


def test_rollback_returns_records_without_erasing_history(run):
    client, rid = run
    _answer(client, rid, "no column for status", "constant:ACTIVE")
    client.get(f"/api/runs/{rid}?wait=true")

    before = client.get(f"/api/runs/{rid}?wait=true").json()["counts"]["records"]
    result = client.post(f"/api/runs/{rid}/rollback").json()
    assert result["succeeded"] == result["attempted"] > 0
    assert result["partial"] is False

    after = client.get(f"/api/runs/{rid}?wait=true").json()
    assert after["counts"]["delivered"] == 0
    assert after["counts"]["records"] == before, "rollback must not touch source data"
    assert len(client.get(f"/api/runs/{rid}/audit").json()) > 0

    # An undo that the next processing pass undoes is not an undo.
    assert after["delivery_paused"] is True
    assert client.get(f"/api/runs/{rid}?wait=true").json()["counts"]["delivered"] == 0

    resumed = client.post(f"/api/runs/{rid}/deliver").json()
    assert sum(resumed["sent"].values()) > 0, "and resuming sends them again"
    assert client.get(f"/api/runs/{rid}?wait=true").json()["delivery_paused"] is False


def test_state_is_stable_across_replays(run):
    client, rid = run
    _answer(client, rid, "no column for status", "constant:ACTIVE")
    snapshots = [client.get(f"/api/runs/{rid}?wait=true").json()["counts"] for _ in range(3)]
    assert snapshots[0] == snapshots[1] == snapshots[2]


def test_rejecting_excludes_without_deleting_source(run):
    client, rid = run
    _answer(client, rid, "no column for status", "constant:ACTIVE")
    before = client.get(f"/api/runs/{rid}?wait=true").json()["counts"]["records"]
    _answer(client, rid, "is not a valid email address", None, action="reject")
    after = client.get(f"/api/runs/{rid}?wait=true").json()
    assert after["counts"]["records"] == before
    assert after["counts"]["excluded"] >= 1


def test_approval_is_not_offered_as_a_way_past_a_failed_check(run):
    client, rid = run
    state = client.get(f"/api/runs/{rid}?wait=true").json()
    case = next(c for c in state["cases"] if c["class"] == "MISSING_REQUIRED")
    response = client.post(
        f"/api/runs/{rid}/cases/{case['key']}/decide", json={"action": "approve"}
    )
    assert response.status_code == 422


def test_destination_records_every_attempt_including_failures(stack, run):
    client, rid = run
    _, target = stack
    target.post("/admin/failure-mode", json={"mode": "transient", "remaining": 1})
    _answer(client, rid, "no column for status", "constant:ACTIVE")
    client.post(f"/api/runs/{rid}/deliver")

    attempts = client.get(f"/api/runs/{rid}/destination").json()["attempts"]
    outcomes = {a["outcome"] for a in attempts}
    assert "transient_failed" in outcomes, "a failed attempt must be recorded, not hidden"
    assert "accepted" in outcomes, "and the retry must be recorded as succeeding"


def test_a_record_is_not_sent_before_the_record_it_points_at(run):
    """Delivery order, not validation.

    A manager reference to a record that exists but is blocked passes validation — the
    record is there. Sending the report first would still leave a reference the
    destination cannot resolve.
    """
    client, rid = run
    _answer(client, rid, "no column for status", "constant:ACTIVE")
    state = client.get(f"/api/runs/{rid}?wait=true").json()

    waiting = [r for r in state["records"] if r.get("waiting_on_another")]
    assert waiting, "at least one record should be held behind its manager"

    delivered = {r["key"] for r in state["records"] if r["state"] == "delivered"}
    by_key = {r["key"]: r for r in state["records"]}
    for record in waiting:
        assert record["key"] not in delivered
        manager = record["values"].get("manager_employee_id")
        if manager and manager in by_key:
            assert by_key[manager]["state"] != "delivered", (
                "a record is only held when the one it points at is not there yet"
            )


def test_a_held_record_rides_on_its_neighbours_case(run):
    """It has nothing of its own to decide, so it must not become its own question."""
    client, rid = run
    _answer(client, rid, "no column for status", "constant:ACTIVE")
    state = client.get(f"/api/runs/{rid}?wait=true").json()

    carrying = [c for c in state["cases"] if c["children"]]
    assert carrying, "the blocking case should carry its dependents"

    waiting = {r["key"] for r in state["records"] if r.get("waiting_on_another")}
    own_cases = {c["record"] for c in state["cases"] if c["record"]}
    assert not (waiting & own_cases), "a dependent must not also raise its own case"

    for case in carrying:
        assert case["blocks"] >= case["children"] + 1, (
            "the effect shown to a reviewer includes what rides along"
        )


def test_resolving_the_parent_releases_what_was_riding_on_it(run):
    """Answering one question sends the record and everything held behind it."""
    client, rid = run
    _answer(client, rid, "no column for status", "constant:ACTIVE")
    before = client.get(f"/api/runs/{rid}?wait=true").json()
    held = {r["key"] for r in before["records"] if r.get("waiting_on_another")}
    assert held

    parent = next(c for c in before["cases"] if c["children"])
    client.post(f"/api/runs/{rid}/cases/{parent['key']}/decide",
                json={"action": "correct", "value": "FULL_TIME"})

    after = client.get(f"/api/runs/{rid}?wait=true").json()
    assert after["counts"]["delivered"] > before["counts"]["delivered"], (
        "the parent and its dependents should go together"
    )
    still_held = {r["key"] for r in after["records"] if r.get("waiting_on_another")}
    assert still_held < held or not still_held


def test_opening_an_untouched_run_does_not_reprocess_it(run):
    """A finished migration is served from store, not recomputed.

    The replay cache lives in process memory, so a restart used to mean every old
    run re-ran its whole pipeline the next time somebody opened it.
    """
    client, rid = run
    import dbx_api.main as api_main

    first = client.get(f"/api/runs/{rid}?wait=true").json()

    # Exactly what losing the process does: the in-memory result is gone.
    api_main.jobs.invalidate(rid)
    api_main.jobs._results.clear()

    calls = []
    original = api_main.replay
    api_main.replay = lambda *a, **k: (calls.append(1), original(*a, **k))[1]
    try:
        again = client.get(f"/api/runs/{rid}").json()
    finally:
        api_main.replay = original

    assert calls == [], "opening an unchanged run must not replay the pipeline"
    assert again["counts"] == first["counts"]
    assert again["status"] == first["status"]


def test_a_decision_invalidates_the_stored_answer(run):
    """The snapshot must never outlive the thing it was computed from."""
    client, rid = run
    before = client.get(f"/api/runs/{rid}?wait=true").json()["counts"]
    _answer(client, rid, "no column for status", "constant:ACTIVE")
    after = client.get(f"/api/runs/{rid}?wait=true").json()["counts"]
    assert after["ready"] != before["ready"] or after["delivered"] != before["delivered"], (
        "a decision must produce a fresh answer, not the stored one"
    )


def test_the_agent_looks_at_the_data_before_it_asks(run):
    """An agent investigates; a form just asks.

    Every case it raises about something checkable should arrive with the looking
    already done, so a consultant reads a finding rather than starting from scratch.
    """
    client, rid = run
    state = client.get(f"/api/runs/{rid}?wait=true").json()
    cases = state["cases"]
    assert cases, "the fixtures raise cases"

    looked_into = [c for c in cases if c.get("checked")]
    assert looked_into, "no case shows any sign the agent checked anything"

    for case in looked_into:
        for step in case["checked"]:
            assert step["looked_at"], "a look with no description is not auditable"
            assert step["found"], "a look that found nothing should not be recorded"

    # A conclusion may be missing - the model sometimes spends its looks and then
    # fails to commit - and the looks are still worth showing. What must never
    # happen is the reverse: a conclusion with nothing behind it.
    for case in cases:
        if case.get("found"):
            assert case.get("checked"), (
                f"{case['headline']!r} concluded something without looking at anything"
            )


def test_a_suggestion_is_only_ever_offered_never_applied(run):
    """The model may propose an answer. It may not be the one who gives it."""
    client, rid = run
    before = client.get(f"/api/runs/{rid}?wait=true").json()
    suggested = [
        (c["key"], o["value"])
        for c in before["cases"]
        for o in c["options"] if o.get("recommended")
    ]
    if not suggested:
        pytest.skip("no suggestion was produced for these fixtures")

    # Every case carrying a suggestion is still open and still blocking its records.
    for key, _ in suggested:
        case = next(c for c in before["cases"] if c["key"] == key)
        assert case["state"] == "open", "a suggestion must not resolve the case"


def test_nothing_reaches_the_target_until_somebody_pushes(stack):
    """The one action with a consequence outside this tool stays a decision.

    Everything else the agent does is reversible inside the console. Writing to the
    client's system is not, so readiness alone is not permission.
    """
    client, _ = stack
    rid = start_run(client)
    # One field-level answer is what makes these records sendable at all.
    _answer(client, rid, "no column for status", "constant:ACTIVE")
    state = client.get(f"/api/runs/{rid}?wait=true").json()

    assert state["counts"]["ready"] > 0, "the fixtures should produce sendable records"
    assert state["counts"]["delivered"] == 0, "nothing may be sent before it is asked for"
    assert client.get(f"/api/runs/{rid}/destination").json()["records"] == []

    sent = client.post(f"/api/runs/{rid}/deliver", json={"keep_sending": False}).json()
    assert sum(sent["sent"].values()) > 0
    after = client.get(f"/api/runs/{rid}?wait=true").json()
    assert after["counts"]["delivered"] > 0
    assert after["auto_send"] is False, "one push is one push, not a standing instruction"


def test_one_authority_rule_settles_every_disagreement_between_two_files(run):
    """A rule, not an answer. This is the delta the human supplies.

    Two exports disagreeing about the same person is not a per-field judgement -
    it is "the HR system is right and payroll is stale", said once.
    """
    client, rid = run
    state = client.get(f"/api/runs/{rid}?wait=true").json()
    conflicts = [c for c in state["cases"] if c["class"] == "CONFLICTING_FACTS"]
    if len(conflicts) < 2:
        pytest.skip("these fixtures do not produce enough disagreements")

    # The rule a consultant reaches for is the one that covers the most cases -
    # here, believing the real PDF over an OCR of the same roster.
    offered = Counter(
        o["value"] for c in conflicts for o in c["options"]
        if (o["value"] or "").startswith("authority:")
    )
    best = offered.most_common(1)[0][0]
    target = next(
        c for c in conflicts if any(o["value"] == best for o in c["options"])
    )

    client.post(
        f"/api/runs/{rid}/cases/{target['key']}/decide",
        json={"action": "correct", "value": best},
    )
    after = client.get(f"/api/runs/{rid}?wait=true").json()
    left = [c for c in after["cases"] if c["class"] == "CONFLICTING_FACTS"]

    assert len(left) <= len(conflicts) // 2, (
        f"one rule should settle most of the disagreements it covers, "
        f"but {len(conflicts)} became {len(left)}"
    )


def test_an_undecidable_date_column_is_one_question_not_one_per_row(run):
    """Whether a column is day-first is a fact about the column.

    Asked per record it produced five identical questions on the same column, each
    blocking one employee, and answering one told you nothing about the next.
    """
    client, rid = run
    # These fixtures block every record on a missing status until it is supplied,
    # which would mask what answering the date column releases.
    _answer(client, rid, "no column for status", "constant:ACTIVE")
    state = client.get(f"/api/runs/{rid}?wait=true").json()
    dates = [c for c in state["cases"] if c["headline"].startswith("How should dates")]
    if not dates:
        pytest.skip("these fixtures have no undecidable date column")

    case = dates[0]
    assert case["blocks"] > 1, "if it only blocks one row it is not a column question"
    assert len(case["options"]) >= 2, "a reading has to be choosable"
    # The options name readings, not the two values of whichever row asked first.
    assert all((o["value"] or "").startswith("%") for o in case["options"])
    assert any("Day first" in o["label"] for o in case["options"])

    held = {case["key"]}
    client.post(
        f"/api/runs/{rid}/cases/{case['key']}/decide",
        json={"action": "correct", "value": case["options"][0]["value"]},
    )
    after = client.get(f"/api/runs/{rid}?wait=true").json()

    assert not [c for c in after["cases"] if c["headline"].startswith("How should dates")], (
        "one answer should settle the whole column"
    )
    # And it does not come back as five separate questions about the same column.
    per_row = [
        c for c in after["cases"]
        if c["field"] == case["field"] and c["class"] == "AMBIGUOUS_VALUE"
        and c["key"] not in held
    ]
    assert not per_row, (
        f"answering the column produced {len(per_row)} row-level questions about it"
    )


def test_a_run_can_be_renamed_and_the_rename_is_recorded(stack):
    client, _ = stack
    rid = client.post("/api/runs/from-fixtures", json={"folder": "run1"}).json()["run_id"]

    renamed = client.patch(f"/api/runs/{rid}", json={"label": "  Acme   payroll_v2 "})
    assert renamed.status_code == 200
    assert renamed.json()["label"] == "Acme payroll_v2", "spacing tidied, text kept as typed"

    listed = next(r for r in client.get("/api/runs").json() if r["id"] == rid)
    assert listed["label"] == "Acme payroll_v2"

    renames = [e for e in client.get(f"/api/runs/{rid}/audit").json()
               if e["action"] == "run.renamed"]
    assert len(renames) == 1 and "Acme payroll_v2" in renames[0]["summary"]

    # The same name again is no change, so it is not a second entry in the history.
    client.patch(f"/api/runs/{rid}", json={"label": "Acme payroll_v2"})
    assert len([e for e in client.get(f"/api/runs/{rid}/audit").json()
                if e["action"] == "run.renamed"]) == 1

    assert client.patch(f"/api/runs/{rid}", json={"label": "   "}).status_code == 422
    assert client.patch(f"/api/runs/{rid}", json={"label": "x" * 121}).status_code == 422
    assert client.patch("/api/runs/run-nope", json={"label": "x"}).status_code == 404


def test_choosing_a_reading_the_other_files_contradict_is_recorded_as_such(run):
    client, rid = run
    state = client.get(f"/api/runs/{rid}?wait=true").json()
    case = next((c for c in state["cases"] if c["headline"].startswith("How should dates")), None)
    if case is None or "cross_check" not in case["evidence"]:
        pytest.skip("these fixtures have no date column shared with another file")

    recommended = [o for o in case["options"] if o["recommended"]]
    cautioned = [o for o in case["options"] if o.get("caution")]
    assert len(recommended) == 1 and cautioned, "the measured reading is suggested, the other flagged"

    client.post(
        f"/api/runs/{rid}/cases/{case['key']}/decide",
        json={"action": "correct", "value": cautioned[0]["value"]},
    )
    audit = client.get(f"/api/runs/{rid}/audit").json()
    assert any(e["action"] == "review.against_evidence" for e in audit), (
        "overriding the evidence is allowed, but the history has to say it happened"
    )


def _held(target, rid):
    """What the destination actually holds for a run - active records only."""
    return target.get("/records", params={"run_id": rid}).json()


def test_undoing_a_delivery_then_pushing_again_really_sends_it_again(stack, run):
    client, rid = run
    _, target = stack
    _answer(client, rid, "no column for status", "constant:ACTIVE")
    client.get(f"/api/runs/{rid}?wait=true")
    client.post(f"/api/runs/{rid}/deliver")
    first = {r["natural_key"] for r in _held(target, rid)}
    assert first

    client.post(f"/api/runs/{rid}/rollback")
    assert _held(target, rid) == []

    again = client.post(f"/api/runs/{rid}/deliver").json()["sent"]
    assert again["accepted"] == len(first) and again["duplicate"] == 0, (
        "after an undo, 'already there' means the tombstone answered, not the record"
    )
    held = _held(target, rid)
    assert {r["natural_key"] for r in held} == first
    assert all(r["generation"] == 2 for r in held)
    assert client.get(f"/api/runs/{rid}?wait=true").json()["counts"]["delivered"] == len(first)

    second = client.post(f"/api/runs/{rid}/rollback").json()
    assert second["attempted"] == second["succeeded"] == len(first), (
        "one undo per record the destination holds, not one per time it was ever sent"
    )


def test_a_value_changed_after_an_undo_is_the_value_delivered(stack, run):
    client, rid = run
    _, target = stack
    state = client.get(f"/api/runs/{rid}?wait=true").json()
    case = next(c for c in state["cases"] if "no column for status" in c["headline"])
    decide = f"/api/runs/{rid}/cases/{case['key']}/decide"
    client.post(decide, json={"action": "correct", "value": "constant:ACTIVE"})
    client.get(f"/api/runs/{rid}?wait=true")
    client.post(f"/api/runs/{rid}/deliver")
    client.post(f"/api/runs/{rid}/rollback")

    # The reason to undo is usually to fix something and send it again.
    client.post(decide, json={"action": "correct", "value": "constant:ON_LEAVE"})
    client.get(f"/api/runs/{rid}?wait=true")
    client.post(f"/api/runs/{rid}/deliver")

    after = client.get(f"/api/runs/{rid}?wait=true").json()
    shown = {r["key"]: r["values"].get("status") for r in after["records"]
             if r["state"] == "delivered"}
    held = {r["natural_key"]: r["payload"].get("status") for r in _held(target, rid)}
    assert held == shown, "the destination must hold what the console says was sent"
    assert "ON_LEAVE" in held.values()
    assert not after["failures"]


def test_deliveries_carry_the_approved_version_and_another_run_cannot_change_its_rules(stack):
    client, target = stack
    first = start_run(client)
    _answer(client, first, "no column for status", "constant:ACTIVE")
    client.get(f"/api/runs/{first}?wait=true")

    # A second migration approves a schema of its own, whose status allows only
    # EXITED. Registered as "version 1" at the destination, it used to replace the
    # first run's rules, and the first run's valid records came back refused.
    strict = (ROOT / "tests" / "fixtures" / "schemas" / "target_schema.yaml").read_text()
    strict = strict.replace("allowed: [ACTIVE, ON_LEAVE, EXITED]", "allowed: [EXITED]")
    start_run(client, schema=strict)

    sent = client.post(f"/api/runs/{first}/deliver").json()["sent"]
    assert sent["accepted"] > 0 and sent["rejected"] == 0
    assert {r["schema_version"] for r in _held(target, first)} == {1}

    # Approving an edit is a new version, and what is sent after it says so.
    v2 = client.post(f"/api/runs/{first}/schema", json={"body": strict.replace(
        "allowed: [EXITED]", "allowed: [ACTIVE, ON_LEAVE, EXITED]")}).json()["version"]
    client.post(f"/api/runs/{first}/schema/{v2}/approve")
    client.get(f"/api/runs/{first}?wait=true")
    client.post(f"/api/runs/{first}/rollback")
    client.post(f"/api/runs/{first}/deliver")
    assert {r["schema_version"] for r in _held(target, first)} == {v2}


def test_a_rehearsed_failure_only_touches_the_run_it_was_asked_for(stack):
    client, _ = stack
    mine, theirs = start_run(client), start_run(client)
    for rid in (mine, theirs):
        _answer(client, rid, "no column for status", "constant:ACTIVE")
        client.get(f"/api/runs/{rid}?wait=true")

    armed = client.post("/api/destination/rehearse",
                        json={"run_id": mine, "mode": "transient", "remaining": 3})
    assert armed.status_code == 200
    client.post(f"/api/runs/{theirs}/deliver")
    outcomes = {a["outcome"] for a in client.get(f"/api/runs/{theirs}/destination")
                .json()["attempts"]}
    assert "transient_failed" not in outcomes, "someone else's rehearsal dropped this run"

    client.post(f"/api/runs/{mine}/deliver")
    outcomes = {a["outcome"] for a in client.get(f"/api/runs/{mine}/destination")
                .json()["attempts"]}
    assert "transient_failed" in outcomes


def test_a_correction_reads_as_one_in_the_history(run):
    client, rid = run
    _answer(client, rid, "no column for status", "constant:ACTIVE")
    summaries = [e["summary"] for e in client.get(f"/api/runs/{rid}/audit").json()]
    assert any(s.startswith("Corrected: ") for s in summaries)
    assert not any("Correctd" in s for s in summaries)
