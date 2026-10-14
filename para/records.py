"""Audit log and the gated action. Both are append-only JSON lines under data/."""
import json
import threading
from datetime import datetime, timezone

from . import config

_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append(name, entry):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock, (config.DATA_DIR / name).open("a") as f:
        f.write(json.dumps(entry) + "\n")


def audit(entry):
    _append("audit_log.jsonl", {"timestamp": _now(), **entry})


def submit_memo_to_partner(memo, check_id):
    """The gated action, mocked: it logs instead of emailing, so a blocked memo is visible by its absence."""
    _append("actions.log", {"time": _now(), "action": "submit_memo_to_partner", "check_id": check_id, "memo": memo})


def read_audit(limit=50):
    try:
        lines = (config.DATA_DIR / "audit_log.jsonl").read_text().splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
