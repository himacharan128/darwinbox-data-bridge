"""Interruption, resumption and the outcomes nobody learns the answer to.

The architecture makes most of this structural: only human decisions and destination
acceptances are durable, everything else is replayed from them. These tests exist to
prove that claim rather than assume it — especially the uncertain delivery, where the
write lands and the caller never finds out.
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
def env(tmp_path, monkeypatch):
    """The API, plus a handle to rebuild it as a fresh process would after a crash."""
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("DBX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MOCK_TARGET_DB", str(tmp_path / "target.db"))

    import importlib

    import dbx_mock_target.main as target_main

    importlib.reload(target_main)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(target_main.app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)

    def boot() -> TestClient:
        """Rebuild the API from disk — what a restarted process sees."""
        import dbx_api.main as api_main
        import dbx_api.store as api_store

        importlib.reload(api_store)
        importlib.reload(api_main)
        api_main.destination.base_url = f"http://127.0.0.1:{port}"
        return TestClient(api_main.app)

    try:
        yield boot, TestClient(target_main.app)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def upload(client, folder: str = "run1") -> str:
    """Files only. Nothing is processed or delivered until a schema is approved."""
    return client.post("/api/runs/from-fixtures", json={"folder": folder}).json()["run_id"]


def approve(client, run_id: str) -> None:
    """Approving is what starts processing — and delivery now rides along with it."""
    body = (ROOT / "tests" / "fixtures" / "schemas" / "target_schema.yaml").read_text()
    version = client.post(f"/api/runs/{run_id}/schema", json={"body": body}).json()["version"]
    client.post(f"/api/runs/{run_id}/schema/{version}/approve")


def start_run(client, folder: str = "run1") -> str:
    run_id = upload(client, folder)
    approve(client, run_id)
    # Pushing is a step somebody takes. These tests are about what happens to a
    # record after it has been sent, so they take it once and leave sending on.
    keep_sending(client, run_id)
    return run_id


def keep_sending(client, run_id: str) -> None:
    client.get(f"/api/runs/{run_id}?wait=true")
    client.post(f"/api/runs/{run_id}/deliver", json={"keep_sending": True})


def _answer(client, run, needle, value, action="correct"):
    state = client.get(f"/api/runs/{run}?wait=true").json()
    case = next((c for c in state["cases"] if needle in c["headline"]), None)
    assert case is not None, f"no open case matching {needle!r}"
    client.post(
        f"/api/runs/{run}/cases/{case['key']}/decide",
        json={"action": action, "value": value},
    )


def test_a_restart_preserves_decisions_and_recomputes_the_same_queue(env):
    boot, _ = env
    client = boot()
    run = start_run(client)
    _answer(client, run, "no column for status", "constant:ACTIVE")
    before = client.get(f"/api/runs/{run}?wait=true").json()["counts"]

    restarted = boot()  # nothing in memory survives; only the database does
    after = restarted.get(f"/api/runs/{run}?wait=true").json()["counts"]

    assert after == before, "reopening a run must recompute exactly the same state"


def test_a_restart_does_not_resend_what_already_landed(env):
    boot, target = env
    client = boot()
    run = start_run(client)
    _answer(client, run, "no column for status", "constant:ACTIVE")
    client.get(f"/api/runs/{run}?wait=true")

    delivered = len(target.get("/records", params={"run_id": run}).json())
    assert delivered > 0, "records ready with no open case deliver on their own"

    restarted = boot()
    again = restarted.post(f"/api/runs/{run}/deliver").json()
    assert sum(again["sent"].values()) == 0, "a resumed run must not resend"

    stored = target.get("/records", params={"run_id": run}).json()
    assert len(stored) == delivered, "the destination must hold each record exactly once"


def test_an_uncertain_outcome_is_reconciled_rather_than_blindly_retried(env):
    """The dangerous failure: the write lands, the response never arrives.

    Retrying blind turns a possible success into a certain duplicate. The client must
    ask the destination what it holds before deciding.
    """
    boot, target = env
    client = boot()
    # The failure has to be armed before the work starts, because delivery is part of
    # processing now rather than a button pressed afterwards.
    target.post("/admin/failure-mode", json={"mode": "uncertain", "remaining": 1})
    run = start_run(client)
    _answer(client, run, "no column for status", "constant:ACTIVE")
    client.get(f"/api/runs/{run}?wait=true")

    stored = target.get("/records", params={"run_id": run}).json()
    keys = [r["natural_key"] for r in stored]
    assert len(keys) == len(set(keys)), "reconciliation must prevent a duplicate record"

    attempts = client.get(f"/api/runs/{run}/destination").json()["attempts"]
    assert any(a["outcome"] == "uncertain" for a in attempts), "the uncertainty is audited"
    assert len(stored) > 0, "the record is present exactly once, not lost and not doubled"


def test_a_permanent_rejection_is_never_retried(env):
    boot, _ = env
    client = boot()
    run = start_run(client)
    _answer(client, run, "no column for status", "constant:ACTIVE")
    client.post(f"/api/runs/{run}/deliver")

    attempts = client.get(f"/api/runs/{run}/destination").json()["attempts"]
    for key in {a["natural_key"] for a in attempts}:
        theirs = [a for a in attempts if a["natural_key"] == key]
        if any(a["outcome"] == "rejected" for a in theirs):
            assert len(theirs) == 1, "a permanent rejection must not be retried"


def test_retry_recovers_from_a_transient_failure_without_duplicating(env):
    boot, target = env
    client = boot()
    target.post("/admin/failure-mode", json={"mode": "transient", "remaining": 2})
    run = start_run(client)
    _answer(client, run, "no column for status", "constant:ACTIVE")
    client.get(f"/api/runs/{run}?wait=true")

    stored = target.get("/records", params={"run_id": run}).json()
    keys = [r["natural_key"] for r in stored]
    assert len(keys) == len(set(keys))

    attempts = client.get(f"/api/runs/{run}/destination").json()["attempts"]
    outcomes = {a["outcome"] for a in attempts}
    assert "transient_failed" in outcomes
    assert "accepted" in outcomes


def test_rolling_back_one_run_leaves_another_untouched(env):
    """Run isolation: fixing one migration must not disturb a different one."""
    boot, target = env
    client = boot()
    runs = []
    for _ in range(2):
        run = start_run(client)
        _answer(client, run, "no column for status", "constant:ACTIVE")
        client.get(f"/api/runs/{run}?wait=true")
        runs.append(run)

    held = {r: len(target.get("/records", params={"run_id": r}).json()) for r in runs}
    assert all(v > 0 for v in held.values())

    client.post(f"/api/runs/{runs[0]}/rollback")

    assert len(target.get("/records", params={"run_id": runs[0]}).json()) == 0
    assert len(target.get("/records", params={"run_id": runs[1]}).json()) == held[runs[1]]
    assert client.get(f"/api/runs/{runs[1]}?wait=true").json()["counts"]["delivered"] == held[runs[1]]


def test_the_same_employee_in_two_runs_is_processed_independently(env):
    """Decision 2: runs are independent processing scopes."""
    boot, target = env
    client = boot()
    first = start_run(client)
    _answer(client, first, "no column for status", "constant:ACTIVE")
    client.get(f"/api/runs/{first}?wait=true")

    second = start_run(client, folder="run2")
    state = client.get(f"/api/runs/{second}?wait=true").json()
    assert state["counts"]["records"] > 0, "run 2 must process on its own terms"

    shared = target.get("/records", params={"natural_key": "EMP-00001"}).json()
    assert len({r["run_id"] for r in shared}) >= 1


def test_a_failure_can_be_arranged_so_the_recovery_can_be_seen(env):
    """The retry path exists in code and could not be demonstrated.

    A destination that always accepts never shows retry, reconciliation or
    rollback working, which left half of an acceptance criterion invisible.
    """
    boot, _ = env
    client = boot()
    run = start_run(client)
    client.get(f"/api/runs/{run}?wait=true")

    armed = client.post("/api/destination/rehearse",
                        json={"run_id": run, "mode": "transient", "remaining": 2})
    assert armed.status_code == 200, armed.text
    assert armed.json()["destination"]["mode"] == "transient"

    _answer(client, run, "no column for status", "constant:ACTIVE")
    client.get(f"/api/runs/{run}?wait=true")
    client.post(f"/api/runs/{run}/deliver", json={"keep_sending": False})

    # Whatever the destination dropped is retryable, and pressing it again lands it.
    client.post("/api/destination/rehearse",
                json={"run_id": run, "mode": "none", "remaining": 0})
    client.post(f"/api/runs/{run}/deliver", json={"keep_sending": False})
    stored = client.get(f"/api/runs/{run}/destination").json()["records"]
    keys = [r["natural_key"] for r in stored]

    assert keys, "a retry after the fault clears must land the records"
    assert len(keys) == len(set(keys)), "retrying must never duplicate a record"


def test_an_unknown_failure_mode_is_refused(env):
    boot, _ = env
    client = boot()
    run = start_run(client)
    assert client.post("/api/destination/rehearse",
                       json={"run_id": run, "mode": "explode", "remaining": 1}).status_code == 422
