"""The whole thing, over HTTP, from upload to destination and back.

The mock destination runs in-process behind an ASGI transport, so the real delivery
client — retries, idempotency, reconciliation — is what gets exercised.
"""
from __future__ import annotations

import socket
import threading
import time
import warnings
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
    return client, start_run(client)


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


def test_records_deliver_themselves_once_nothing_is_blocking_them(run):
    """Delivery follows readiness. A click before the fact only delayed safe work."""
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
