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
    return client, client.post("/api/runs/from-fixtures", json={"folder": "run1"}).json()["run_id"]


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


def test_delivery_is_idempotent_and_destination_is_the_truth(run):
    client, rid = run
    _answer(client, rid, "no column for status", "constant:ACTIVE")

    first = client.post(f"/api/runs/{rid}/deliver").json()
    assert first["sent"]["accepted"] > 0

    stored = client.get(f"/api/runs/{rid}/destination").json()
    assert len(stored["records"]) == first["sent"]["accepted"]

    again = client.post(f"/api/runs/{rid}/deliver").json()
    assert sum(again["sent"].values()) == 0, "resuming a run must not resend"


def test_rollback_returns_records_without_erasing_history(run):
    client, rid = run
    _answer(client, rid, "no column for status", "constant:ACTIVE")
    client.post(f"/api/runs/{rid}/deliver")

    before = client.get(f"/api/runs/{rid}?wait=true").json()["counts"]["records"]
    result = client.post(f"/api/runs/{rid}/rollback").json()
    assert result["succeeded"] == result["attempted"] > 0
    assert result["partial"] is False

    after = client.get(f"/api/runs/{rid}?wait=true").json()
    assert after["counts"]["delivered"] == 0
    assert after["counts"]["records"] == before, "rollback must not touch source data"
    assert len(client.get(f"/api/runs/{rid}/audit").json()) > 0


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
