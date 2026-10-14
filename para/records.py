"""Audit log and the gated action. Both are append-only JSON lines under data/."""
import json
import threading
from datetime import datetime, timezone

from . import config, tenant

_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append(name, entry):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock, (config.DATA_DIR / name).open("a") as f:
        f.write(json.dumps(entry) + "\n")


def audit(entry):
    entry = {"timestamp": _now(), **entry}
    if config.HOSTED:  # per-firm audit in the database
        from . import store
        return store.add_audit(tenant.require(), entry)
    _append("audit_log.jsonl", entry)


def submit_memo_to_partner(memo, check_id, reason="", subject=""):
    """The gated action. Emails the partner when email is set up; otherwise it is logged only.

    Returns {channel, to, sent, error}. Every attempt, failed or not, goes to actions.log, so a blocked
    memo is visible by its absence and a failed delivery is never mistaken for a sent one.
    """
    from . import mailer
    delivery = {**mailer.describe(), "sent": False, "error": None}
    if delivery["channel"] == "email":
        try:
            mailer.send_memo(memo, check_id, reason, subject)
            delivery["sent"] = True
        except mailer.MailError as e:
            delivery["error"] = str(e)
    else:
        delivery["sent"] = True  # logged-only delivery: the log entry below is the action
    if not config.HOSTED:  # hosted: the firm's audit entry for the decision records the delivery
        _append("actions.log", {"time": _now(), "action": "submit_memo_to_partner", "check_id": check_id, "memo": memo,
                                "delivery": delivery})
    return delivery


def read_audit(limit=50):
    if config.HOSTED:
        from . import store
        return store.read_audit(tenant.require(), limit)
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
