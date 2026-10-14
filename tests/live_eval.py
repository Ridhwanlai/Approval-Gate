#!/usr/bin/env python3
"""Run the practice set through the full live pipeline: real HarnessRouter harnesses, mock Ezio.

  python3 tests/live_eval.py            # all 16 practice memos, 4 at a time
  python3 tests/live_eval.py pl-07 pl-09

Costs real HarnessRouter usage (two harness runs per memo). Writes data/live_eval.json.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k != "HR_API_KEY"}
    mock = subprocess.Popen([sys.executable, str(ROOT / "mock_ezio.py")], env={**env, "PORT": str(port), "LATENCY": "0.5-1.5"},
                            stdout=subprocess.DEVNULL)
    os.environ["EZIO_URL"] = f"http://127.0.0.1:{port}"
    os.environ.setdefault("PARA_DATA_DIR", str(ROOT / "data" / "live_eval"))
    sys.path.insert(0, str(ROOT))
    from para import agents, pipeline
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    if agents.status() != "configured":
        raise SystemExit(f"HarnessRouter is not ready ({agents.status()})")
    items = json.loads((ROOT / "firm" / "practice_set.json").read_text())["items"]
    if sys.argv[1:]:
        items = [i for i in items if i["id"] in sys.argv[1:]]

    def one(item):
        t0 = time.monotonic()
        job = pipeline.start_check(item["text"], item["context"], background=False)
        r = job.result
        ags = {e["feature"]: e for e in r["evidence"] if e.get("signal") == "agent"}
        return {"id": item["id"], "expected": item["expected_decision"], "got": r["decision"], "seconds": round(time.monotonic() - t0, 1),
                "reason": r["reason"], "agents": {k: {"ok": v["ok"], "error": v["error"], "findings": v["findings"],
                                                      "rejected": v["rejected"], "elapsed": v["elapsed"]} for k, v in ags.items()}}

    try:
        with ThreadPoolExecutor(4) as pool:
            rows = list(pool.map(one, items))
    finally:
        mock.terminate()
    exact = sum(r["expected"] == r["got"] for r in rows)
    bad = [r for r in rows if r["expected"] != "APPROVE"]
    caught = sum(r["got"] != "APPROVE" for r in bad)
    false_blocks = sum(r["expected"] == "APPROVE" and r["got"] != "APPROVE" for r in rows)
    for r in rows:
        flags = ", ".join(f"{k.split('_')[1]}:{'ok' if v['ok'] else 'FAIL'}({len(v['findings'])})" for k, v in r["agents"].items())
        print(f"{'OK ' if r['expected'] == r['got'] else 'XX '}{r['id']}  expected {r['expected']:<16} got {r['got']:<16} {r['seconds']:>5}s  {flags}")
        if r["expected"] != r["got"]:
            print(f"     {r['reason']}")
    print(f"\nexact {exact}/{len(rows)}, bad caught {caught}/{len(bad)}, good memos stopped {false_blocks}/{len(rows) - len(bad)}")
    out = ROOT / "data" / "live_eval.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))
    print(f"Wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
