"""Both ways a target schema gets established, and what approval does to a run.

Mode A is a client who knows their destination. Mode B is the more common
engagement: a pile of exports and no target defined yet. Either way the schema is a
human-approved contract, and approving a new one must never rewrite what was already
sent under the old one.
"""
from __future__ import annotations

import json
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
    return client.post("/api/runs/from-fixtures", json={"folder": "run1"}).json()["run_id"]


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


def test_nothing_is_processed_until_a_schema_is_approved(client, run):
    """The gate. A migration against a schema nobody agreed to is unaccountable."""
    state = client.get(f"/api/runs/{run}?wait=true").json()
    assert state["status"] == "awaiting_schema"
    assert state["counts"]["records"] == 0
    assert state["cases"] == []

    # A proposal is not an approval.
    proposal = client.post(f"/api/runs/{run}/schema/recommend").json()
    assert client.get(f"/api/runs/{run}").json()["status"] == "awaiting_schema"

    result = client.post(f"/api/runs/{run}/schema/{proposal['version']}/approve").json()
    assert result["started"] is True
    after = client.get(f"/api/runs/{run}?wait=true").json()
    assert after["status"] != "awaiting_schema"
    assert after["counts"]["records"] > 0


def test_uploaded_files_are_reported_before_any_schema_is_chosen(client, run):
    """A consultant should see their files were read before making decisions."""
    files = client.get(f"/api/runs/{run}/files").json()
    assert files["total_rows"] > 0
    assert files["total_columns"] > 50
    assert all("name" in f and "kind" in f for f in files["files"])
    assert any(f["kind"] == "pdf" for f in files["files"])


def test_a_fresh_proposal_is_visible_immediately(client, run):
    """The editor has to have something to show, or the agent looks like it did nothing.

    Regression: `active` was derived only from an *approved* schema, so a draft the
    agent had just written rendered as an empty editor.
    """
    before = client.get(f"/api/runs/{run}/schema").json()
    assert before["active"] is None and before["draft_version"] is None

    client.post(f"/api/runs/{run}/schema/recommend")

    after = client.get(f"/api/runs/{run}/schema").json()
    assert after["active"] is not None, "a proposal must render without being approved"
    assert after["showing_state"] == "draft"
    assert after["origin"] == "recommended"
    assert after["approved_version"] is None
    assert len(after["active"]["fields"]) >= 8


def test_the_editor_can_post_json_and_have_it_accepted(client, run):
    """What the field editor sends is JSON, not YAML. Both must normalise the same."""
    client.post(f"/api/runs/{run}/schema/recommend")
    proposal = client.get(f"/api/runs/{run}/schema").json()["active"]

    existing = {f["name"] for f in proposal["fields"]}
    new_field = next(
        n for n in ("secondment_note", "payroll_ref", "badge_number") if n not in existing
    )
    edited = {**proposal, "fields": [*proposal["fields"],
                                     {"name": new_field, "type": "string"}]}
    saved = client.post(
        f"/api/runs/{run}/schema", json={"body": json.dumps(edited), "origin": "edited"}
    )
    assert saved.status_code == 200, saved.json()
    assert saved.json()["fields"] == len(proposal["fields"]) + 1

    shown = client.get(f"/api/runs/{run}/schema").json()
    assert new_field in {f["name"] for f in shown["active"]["fields"]}


def test_a_duplicate_field_name_is_refused(client, run):
    """Two fields called the same thing is a schema that cannot be satisfied."""
    client.post(f"/api/runs/{run}/schema/recommend")
    proposal = client.get(f"/api/runs/{run}/schema").json()["active"]
    doubled = {**proposal, "fields": [*proposal["fields"], proposal["fields"][0]]}

    response = client.post(f"/api/runs/{run}/schema", json={"body": json.dumps(doubled)})
    assert response.status_code == 422
    assert "duplicate" in response.json()["detail"].lower()


def test_delivered_records_keep_the_version_they_were_sent_under(client, run):
    """The property that makes the audit trail truthful.

    A payload was shaped by a particular schema. Editing that schema afterwards must
    not make the history describe something that was never sent.
    """
    version = client.post(
        f"/api/runs/{run}/schema", json={"body": SCHEMA_YAML.read_text()}
    ).json()["version"]
    client.post(f"/api/runs/{run}/schema/{version}/approve")
    state = client.get(f"/api/runs/{run}?wait=true").json()
    case = next(c for c in state["cases"] if "no column for status" in c["headline"])
    client.post(
        f"/api/runs/{run}/cases/{case['key']}/decide",
        json={"action": "correct", "value": "constant:ACTIVE"},
    )
    client.get(f"/api/runs/{run}?wait=true")
    before = client.get(f"/api/runs/{run}/destination").json()["records"]
    assert before, "records with no open case deliver on their own"
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


def test_a_proposed_schema_can_actually_reconcile(client, run):
    """A schema with nothing unique has no blocking keys, so nothing ever merges.

    The model names fields well and rarely marks one unique, which quietly turned
    multi-file reconciliation off: the same person arrived once per file with no
    duplicate ever detected.
    """
    client.post(f"/api/runs/{run}/schema/recommend")
    proposal = client.get(f"/api/runs/{run}/schema").json()
    unique = [f["name"] for f in proposal["active"]["fields"] if f.get("unique")]
    assert unique, "something has to identify a person"

    # …but only things that actually identify one.
    assert not any(n in ("first_name", "last_name", "designation", "department_code")
                   for n in unique), f"over-marked: {unique}"

    client.post(f"/api/runs/{run}/schema/{proposal['draft_version']}/approve")
    counts = client.get(f"/api/runs/{run}?wait=true").json()["counts"]
    assert counts["records"] < counts["rows_read"], (
        "the same people appear across these files and should have been merged"
    )
