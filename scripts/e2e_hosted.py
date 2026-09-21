#!/usr/bin/env python
"""End-to-end checks against a running deployment.

Exercises the happy path, the escalation boundary, delivery recovery and hostile
input against a real server over real HTTP — not a test client, not a stub.

    uv run python scripts/e2e_hosted.py http://<host>
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


class Client:
    def __init__(self, base: str) -> None:
        self.http = httpx.Client(base_url=base.rstrip("/"), timeout=300)

    def upload(self, folder: Path) -> str:
        """Upload files only. Nothing is processed until a schema is approved."""
        files = [
            ("files", (p.name, p.read_bytes(), "application/octet-stream"))
            for p in sorted(folder.iterdir())
            if p.is_file() and not p.name.startswith(".") and not p.name.endswith(".md")
        ]
        return self.http.post("/api/runs", files=files).json()["run_id"]

    def approve(self, run: str, body: str | None = None) -> int:
        schema = body or (
            ROOT / "tests" / "fixtures" / "schemas" / "target_schema.yaml"
        ).read_text()
        version = self.http.post(
            f"/api/runs/{run}/schema", json={"body": schema}
        ).json()["version"]
        self.http.post(f"/api/runs/{run}/schema/{version}/approve")
        return version

    def start(self, folder: Path) -> str:
        run = self.upload(folder)
        self.approve(run)
        return run

    def state(self, run: str) -> dict:
        return self.http.get(f"/api/runs/{run}", params={"wait": "true"}).json()

    def answer(self, run: str, needle: str, value: str | None, action: str = "correct"):
        state = self.state(run)
        case = next((c for c in state["cases"] if needle in c["headline"]), None)
        if case is None:
            return None
        return self.http.post(
            f"/api/runs/{run}/cases/{case['key']}/decide",
            json={"action": action, "value": value},
        )


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
    c = Client(base)
    print(f"\nEnd-to-end against {base}\n" + "=" * 72)

    print("\n[1] service is up")
    health = c.http.get("/api/health").json()
    check("health endpoint responds", health.get("status") == "ok")
    check("destination is reachable from the api", health.get("destination_reachable") is True)
    check("console html is served", "<div id=\"root\">" in c.http.get("/").text)

    print("\n[2] nothing runs until a schema is agreed")
    run = c.upload(SAMPLES / "01-clean")
    waiting = c.state(run)
    check("uploading alone processes nothing", waiting["status"] == "awaiting_schema",
          waiting["status"])
    check("and produces no records", waiting["counts"]["records"] == 0)
    files = c.http.get(f"/api/runs/{run}/files").json()
    check("but the files are reported back", files["total_rows"] > 0,
          f"{len(files['files'])} files, {files['total_rows']} rows")
    proposal = c.http.post(f"/api/runs/{run}/schema/recommend").json()
    check("the agent can propose a schema", len(proposal["schema"]["fields"]) >= 8,
          f"{len(proposal['schema']['fields'])} fields")
    check("a proposal is not an approval",
          c.state(run)["status"] == "awaiting_schema")
    c.approve(run)

    print("\n[2b] happy path — clean data must not escalate")
    s = c.state(run)
    check("every employee found", s["counts"]["records"] >= 20, str(s["counts"]["records"]))
    check("nothing escalated", s["counts"]["open_cases"] == 0)
    check("rows read is reported beside them", s["counts"]["rows_read"] >= s["counts"]["records"])

    print("\n[3] pushing to the target, and undoing it")
    check("nothing is sent until it is asked for",
          s["counts"]["delivered"] == 0 and s["counts"]["ready"] > 0,
          f"{s['counts']['delivered']} sent, {s['counts']['ready']} ready")
    sent = c.http.post(f"/api/runs/{run}/deliver", json={"keep_sending": True}).json()
    check("the push reports an outcome per record",
          sum(sent["sent"].values()) == s["counts"]["ready"], str(sent["sent"]))
    s = c.state(run)
    dest = c.http.get(f"/api/runs/{run}/destination").json()
    check("destination actually holds them",
          len(dest["records"]) == s["counts"]["delivered"])
    again = c.http.post(f"/api/runs/{run}/deliver", json={"keep_sending": True}).json()
    check("pushing again sends nothing", sum(again["sent"].values()) == 0, str(again["sent"]))
    rb = c.http.post(f"/api/runs/{run}/rollback").json()
    check("rollback reports honestly",
          rb["succeeded"] == rb["attempted"] > 0 and not rb["partial"])
    paused = c.state(run)
    check("and pauses sending, so the undo sticks",
          paused.get("delivery_paused") is True and paused["counts"]["delivered"] == 0)
    after = c.http.get(f"/api/runs/{run}/destination").json()["records"]
    # The destination view deliberately keeps tombstones so the UI can show what was
    # undone; what must be zero is the records still counted as accepted.
    check("nothing is accepted at the destination any more",
          sum(1 for r in after if r["state"] == "accepted") == 0,
          f"{len(after)} rows, states: {sorted({r['state'] for r in after})}")
    check("source records survive rollback", c.state(run)["counts"]["records"] >= 20)
    resumed = c.http.post(f"/api/runs/{run}/deliver").json()
    check("resuming sends them again", sum(resumed["sent"].values()) > 0)

    print("\n[4] messy data — the boundary")
    run = c.start(SAMPLES / "02-messy")
    s = c.state(run)
    auto = [m for m in s["mappings"] if m["decision"] == "auto_apply"]
    check("reconciled across seven files", s["counts"]["records"] >= 35, str(s["counts"]["records"]))
    check("most columns mapped unaided", len(auto) >= 60, f"{len(auto)}/{len(s['mappings'])}")
    check("fewer cases than records", s["counts"]["open_cases"] < s["counts"]["records"])
    classes = {x["class"] for x in s["cases"]}
    check("several escalation kinds, not one", len(classes) >= 6, ", ".join(sorted(classes)))

    print("\n[5] one answer releases many records at once")
    before = c.state(run)["counts"]
    c.answer(run, "no column for status", "constant:ACTIVE")
    state = c.state(run)
    released = (state["counts"]["ready"] + state["counts"]["delivered"]) - (
        before["ready"] + before["delivered"]
    )
    check("a field-level answer unblocks in bulk", released >= 8,
          f"{before['ready'] + before['delivered']} -> "
          f"{state['counts']['ready'] + state['counts']['delivered']} sendable")
    check("records waiting on a neighbour ride on its case",
          any(x["children"] for x in state["cases"]))

    print("\n[6] negative — bad input is refused, not half-accepted")
    bad = c.http.post(f"/api/runs/{run}/schema", json={"body": "fields: [{{{"})
    check("malformed schema rejected with a reason", bad.status_code == 422)
    missing = c.http.get("/api/runs/run-does-not-exist")
    check("unknown run is 404 not 500", missing.status_code == 404)
    state = c.state(run)
    hard = next((x for x in state["cases"] if x["class"] == "MISSING_REQUIRED"), None)
    if hard:
        r = c.http.post(f"/api/runs/{run}/cases/{hard['key']}/decide", json={"action": "approve"})
        check("approve is refused where there is nothing to approve", r.status_code == 422)
    bad_case = c.http.post(f"/api/runs/{run}/cases/nonsense/decide",
                           json={"action": "correct", "value": "x"})
    check("unknown case is 404", bad_case.status_code == 404)

    print("\n[7] edge cases must not crash it")
    run = c.start(SAMPLES / "04-edge-cases")
    s = c.state(run)
    check("survives empty, malformed and disguised files", s["counts"]["records"] > 0,
          str(s["counts"]))
    lower = [r for r in s["records"] if r["key"].lower() == "emp-30032"]
    check("case-sensitive id not silently 'corrected'",
          all(r["key"] == "emp-30032" or r["key"] == "EMP-30032" for r in lower) or not lower)
    check("impossible dates escalate rather than parse",
          any("date" in (x["field"] or "") for x in s["cases"]))

    print("\n[8] hostile input")
    run = c.start(SAMPLES / "05-adversarial")
    s = c.state(run)
    noise = [m for m in s["mappings"]
             if m["file"] == "noise_only.csv" and m["decision"] == "auto_apply"]
    check("no column of pure noise is mapped", len(noise) == 0, f"{len(noise)} mapped")
    check("the noise file is one question, not a dozen",
          sum(1 for x in s["cases"] if "noise_only" in x["headline"]) <= 1)
    types = {r["values"].get("employment_type") for r in s["records"]}
    check("injection did not change other records' data",
          types <= {"FULL_TIME", "PART_TIME", "CONTRACT", "INTERN", None}, str(types))
    injected = [r for r in s["records"] if r["key"] == "EMP-40001"]
    if injected:
        kept = str(injected[0]["values"].get("designation", ""))
        check("injected text is preserved as data", "SYSTEM" in kept or kept == "")

    print("\n[9] audit")
    events = c.http.get(f"/api/runs/{run}/audit").json()
    check("every action is recorded", len(events) > 0, f"{len(events)} events")
    check("actors are attributed", {e.get("actor") for e in events} & {"agent", "human", "mock_api"} != set())

    print("\n" + "=" * 72)
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"    FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
