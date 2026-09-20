#!/usr/bin/env python
"""Run every sample set through the application and report what each produced.

A quick way to see the boundary behave across clean, messy, conflicting, malformed
and hostile input without clicking through five runs.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

SAMPLES = ROOT / "samples"
FIXTURES = ROOT / "tests" / "fixtures"


def main() -> int:
    os.environ.setdefault("DBX_DATA_DIR", str(ROOT / ".artifacts" / "samples"))
    os.environ.setdefault("MOCK_TARGET_DB", str(ROOT / ".artifacts" / "samples" / "m.db"))
    port = 8079
    os.environ["MOCK_TARGET_URL"] = f"http://127.0.0.1:{port}"

    import uvicorn
    from dbx_mock_target import app as target
    from fastapi.testclient import TestClient

    threading.Thread(
        target=lambda: uvicorn.run(target, host="127.0.0.1", port=port, log_level="error"),
        daemon=True,
    ).start()
    time.sleep(1.5)

    from dbx_api import app

    client = TestClient(app)
    folders = sorted(d for d in SAMPLES.iterdir() if d.is_dir())

    print(f"\n{'set':18s} {'records':>8} {'ready':>6} {'cases':>6}  status")
    print("=" * 72)
    for folder in folders:
        staged = FIXTURES / f"sample-{folder.name}"
        if staged.exists():
            shutil.rmtree(staged)
        shutil.copytree(folder, staged)
        try:
            run_id = client.post(
                "/api/runs/from-fixtures", json={"folder": staged.name}
            ).json()["run_id"]
            state = client.get(f"/api/runs/{run_id}?wait=true").json()
        except Exception as exc:  # noqa: BLE001 - the report is the point
            print(f"{folder.name:18s}  FAILED: {type(exc).__name__}: {exc}")
            continue
        finally:
            shutil.rmtree(staged, ignore_errors=True)

        counts = state["counts"]
        print(f"{folder.name:18s} {counts['records']:>8} {counts['ready']:>6} "
              f"{counts['open_cases']:>6}  {state['status']}")
        classes: dict[str, int] = {}
        for case in state["cases"]:
            classes[case["class"]] = classes.get(case["class"], 0) + 1
        for name, n in sorted(classes.items(), key=lambda kv: -kv[1]):
            print(f"{'':20s} {n:>3}  {name}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
