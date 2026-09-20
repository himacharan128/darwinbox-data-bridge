"""Both ways a target schema gets established, and what approval does to a run.

Mode A is a client who knows their destination. Mode B is the more common
engagement: a pile of exports and no target defined yet. Either way the schema is a
human-approved contract, and approving a new one must never rewrite what was already
sent under the old one.
"""
from __future__ import annotations

import socket
import threading
import time
import warnings
from pathlib import Path

import pytest
import uvicorn
import yaml
from fastapi.testclient import TestClient

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[2]
SCHEMA_YAML = ROOT / "tests" / "fixtures" / "schemas" / "target_schema.yaml"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
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
    server = uvicorn.Server(
        uvicorn.Config(target_main.app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    api_main.destination.base_url = f"http://127.0.0.1:{port}"
    try:
        yield TestClient(api_main.app)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def run(client):
    rid = client.post("/api/runs/from-fixtures", json={"folder": "run1"}).json()["run_id"]
    client.get(f"/api/runs/{rid}?wait=true")
    return rid


def test_mode_a_accepts_yaml_and_json_into_one_representation(client, run):
    as_yaml = client.post(
        f"/api/runs/{run}/schema", json={"body": SCHEMA_YAML.read_text()}
    ).json()
    parsed = yaml.safe_load(SCHEMA_YAML.read_text())
    as_object = client.post(
        f"/api/runs/{run}/schema", json={"schema_obj": parsed}
    ).json()
    # YAML text and a parsed object must normalise to the same internal schema.
    assert as_yaml["fields"] == as_object["fields"]

    versions = client.get(f"/api/runs/{run}/schema").json()["versions"]
    assert [v["state"] for v in versions] == ["draft", "draft"]


def test_an_unusable_schema_is_refused_with_a_readable_reason(client, run):
    response = client.post(
        f"/api/runs/{run}/schema",
        json={"body": "entity: employee\nfields:\n  - name: x\n    type: enum\n"},
    )
    assert response.status_code == 422
    assert "enum" in response.json()["detail"]


def test_malformed_text_is_refused_rather_than_half_parsed(client, run):
    response = client.post(f"/api/runs/{run}/schema", json={"body": "fields: [{{{"})
    assert response.status_code == 422


def test_mode_b_proposes_a_schema_from_the_data_alone(client, run):
    proposal = client.post(f"/api/runs/{run}/schema/recommend").json()
    fields = {f["name"] for f in proposal["schema"]["fields"]}
    assert proposal["schema"]["entity"] == "employee"
    assert len(fields) >= 8
    # Five files spell the identifier five ways; the proposal must be one field.
    assert sum(1 for f in fields if "employee" in f and "id" in f) == 1
    assert proposal["state"] == "draft", "a proposal is never self-approving"


def test_approving_supersedes_the_previous_version(client, run):
    first = client.post(f"/api/runs/{run}/schema", json={"body": SCHEMA_YAML.read_text()})
    second = client.post(f"/api/runs/{run}/schema", json={"body": SCHEMA_YAML.read_text()})
    client.post(f"/api/runs/{run}/schema/{first.json()['version']}/approve")
    client.post(f"/api/runs/{run}/schema/{second.json()['version']}/approve")

    states = {
        v["version"]: v["state"] for v in client.get(f"/api/runs/{run}/schema").json()["versions"]
    }
    assert states[first.json()["version"]] == "superseded"
    assert states[second.json()["version"]] == "approved"


def test_delivered_records_keep_the_version_they_were_sent_under(client, run):
    """The property that makes the audit trail truthful.

    A payload was shaped by a particular schema. Editing that schema afterwards must
    not make the history describe something that was never sent.
    """
    state = client.get(f"/api/runs/{run}?wait=true").json()
    case = next(c for c in state["cases"] if "no column for status" in c["headline"])
    client.post(
        f"/api/runs/{run}/cases/{case['key']}/decide",
        json={"action": "correct", "value": "constant:ACTIVE"},
    )
    sent = client.post(f"/api/runs/{run}/deliver").json()
    assert sent["sent"]["accepted"] > 0

    before = client.get(f"/api/runs/{run}/destination").json()["records"]
    versions_before = {r["natural_key"]: r["schema_version"] for r in before}

    edited = yaml.safe_load(SCHEMA_YAML.read_text())
    edited["fields"].append({"name": "cost_centre", "type": "string", "required": False})
    new_version = client.post(f"/api/runs/{run}/schema", json={"schema_obj": edited}).json()
    result = client.post(f"/api/runs/{run}/schema/{new_version['version']}/approve").json()

    assert result["delivered_unchanged"] == len(before)
    after = client.get(f"/api/runs/{run}/destination").json()["records"]
    assert {r["natural_key"]: r["schema_version"] for r in after} == versions_before
    for record in after:
        assert "cost_centre" not in record["payload"], (
            "a delivered payload must not gain a field it was never sent with"
        )
