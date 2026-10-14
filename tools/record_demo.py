#!/usr/bin/env python3
"""Record the website's replay demo from real runs: docs/assets/demo-data.json.

  python3 tools/record_demo.py --audit data/live_eval/audit_log.jsonl --revise pl-07 pl-12

--audit    an audit log from tests/live_eval.py: one recorded decision per practice memo
--revise   practice ids to run through the live revision drafter, then re-check the draft

Live steps use your HarnessRouter key and credits. The output holds only practice-set text and
the decisions Para made on it: no keys, harness ids, session ids or raw Ezio payloads.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "assets" / "demo-data.json"
KEEP_AGENT = ("feature", "label", "harness", "ok", "error", "findings", "notes", "elapsed", "model")


def clean_evidence(evidence):
    out = []
    for e in evidence:
        if e.get("signal") == "agent":
            out.append({k: e.get(k) for k in KEEP_AGENT} | {"signal": "agent"})
        else:
            out.append(e)
    return out


def start_stack():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k != "HR_API_KEY"}
    mock = subprocess.Popen([sys.executable, str(ROOT / "mock_ezio.py")], env={**env, "PORT": str(port), "LATENCY": "0.5-1"},
                            stdout=subprocess.DEVNULL)
    os.environ["EZIO_URL"] = f"http://127.0.0.1:{port}"
    os.environ.setdefault("PARA_DATA_DIR", str(ROOT / "data" / "demo_record"))
    time.sleep(1)
    return mock


def revise(ids, items):
    from para import pipeline
    out = {}
    for pid in ids:
        chk = pipeline.start_check(items[pid]["text"], {}, background=False)
        job, err = pipeline.start_revision(chk.id)
        if err:
            print(f"{pid}: no revision ({err})")
            continue
        while job.state != "done":
            time.sleep(1)
        r = job.result
        if not r["ok"]:
            print(f"{pid}: revision failed ({r['error']})")
            continue
        again = pipeline.start_check(r["revised_memo"], {}, background=False).result
        print(f"{pid}: {chk.result['decision']} -> revised -> {again['decision']}")
        out[pid] = {"revised_memo": r["revised_memo"], "changes": r["changes"],
                    "recheck": {"decision": again["decision"], "reason": again["reason"], "evidence": clean_evidence(again["evidence"])}}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audit", type=Path, required=True)
    ap.add_argument("--revise", nargs="*", default=[])
    args = ap.parse_args()
    items = {i["id"]: i for i in json.loads((ROOT / "firm" / "practice_set.json").read_text())["items"]}
    by_text = {i["text"]: i for i in items.values()}
    memos = []
    for line in args.audit.read_text().splitlines():
        rec = json.loads(line)
        item = by_text.get(rec.get("input"))
        if rec.get("type") != "decision" or not item:
            continue
        memos.append({"id": item["id"], "label": item["label"], "expected": item["expected_decision"], "text": item["text"],
                      "decision": rec["decision"], "reason": rec["reason"], "sent": rec["action_executed"],
                      "evidence": clean_evidence(rec["evidence"]), "recorded": rec["timestamp"]})
    memos.sort(key=lambda m: m["id"])
    revisions = {}
    if args.revise:
        sys.path.insert(0, str(ROOT))
        mock = start_stack()
        try:
            revisions = revise(args.revise, items)
        finally:
            mock.terminate()
    for m in memos:
        if m["id"] in revisions:
            m["revision"] = revisions[m["id"]]
    data = {"_about": "Recorded from live Para runs (Hermes agents on HarnessRouter, mock Ezio). Replayed by the site; nothing calls an API.",
            "exported": datetime.now(timezone.utc).isoformat(timespec="seconds"), "memos": memos}
    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(memos)} memos, {len(revisions)} with revisions")


if __name__ == "__main__":
    main()
