"""Para's agent features. Each feature_key maps to one HarnessRouter harness through harnesses/registry.json.

The harnesses only produce evidence (or, for the drafter, a draft). Nothing here decides; gate.py does.
Every agent output is treated as untrusted: quotes must be verbatim substrings of the memo and list
entries must exist on the firm's lists, or the finding is rejected and logged.
"""
import json
import re
import threading

from . import config
from .hr_client import Client, HRError

CITE_AUDIT, STYLE_REVIEW, REVISION = "memo_cite_audit", "memo_style_review", "memo_revision"
HARNESS_NAMES = {CITE_AUDIT: "para-cite-auditor", STYLE_REVIEW: "para-memo-reviewer", REVISION: "para-revision-drafter"}
LABELS = {CITE_AUDIT: "AI cite-audit", STYLE_REVIEW: "AI memo review", REVISION: "AI revision"}
AUDIT_STATUSES = {"verified", "fabricated", "misstated", "unverified"}
STYLE_KINDS = {"overconfidence", "emotional", "one_sided"}
SKIP_WORDS = {"the", "and", "co.", "inc.", "corp.", "company", "of", "v.", "vs.", "railroad", "board", "education"}

_client = None
_slots = threading.BoundedSemaphore(config.HR_MAX_CONCURRENT)


def client():
    global _client
    if _client is None:
        _client = Client()
    return _client


def set_client(c):
    """Tests swap in a client pointed at a fake HarnessRouter."""
    global _client
    _client = c


def status():
    reg = config.registry()
    if not client().configured():
        return "no_key"
    if not all(k in reg for k in HARNESS_NAMES):
        return "not_provisioned"
    return "configured"


def run_feature(feature_key, task_input, on_event=None, deadline_s=240, queue_s=600):
    harness_id = config.registry().get(feature_key)  # server-side map only; never from the browser
    if not harness_id:
        raise HRError("not_configured", f"No harness is configured for {feature_key}. Run harnesses/provision.py.")
    # The account's plan caps simultaneous tasks; queue here instead of colliding with the cap.
    # Time spent queued does not count against the agent's own deadline.
    if not _slots.acquire(blocking=False):
        (on_event or (lambda *_: None))("queued", {})
        if not _slots.acquire(timeout=queue_s):
            raise HRError("rate_limited", "Too many AI checks are waiting; try again shortly.")
    try:
        return client().run(harness_id, task_input, on_event=on_event, deadline_s=deadline_s)
    finally:
        _slots.release()


# ---------------------------------------------------------------- shared helpers

def _firm_lists(sot):
    trusted = sot.get("trusted_citations", {}).get("cases", [])
    fabricated = sot.get("known_fabricated", {}).get("cases", [])
    return trusted, fabricated


def _blocks(memo, sot, extra=""):
    trusted, fabricated = _firm_lists(sot)
    return (f"<memo>\n{memo}\n</memo>\n{extra}<trusted>\n{json.dumps(trusted, indent=1)}\n</trusted>\n"
            f"<fabricated>\n{json.dumps(fabricated, indent=1)}\n</fabricated>")


def extract_json(text):
    """The first JSON object in the reply, tolerating a code fence or stray prose around it."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the agent's reply")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError("the agent's reply is not a JSON object")
    if obj.get("error") == "role_mismatch":
        raise ValueError("the harness answered role_mismatch")
    return obj


def _distinct_tokens(name):
    return {w.strip(",.").lower() for w in name.split() if len(w.strip(",.")) >= 4 and w.lower() not in SKIP_WORDS}


def _linked(quote, entry):
    """A list entry is only accepted when the quoted text plausibly names it (a party word or its reporter cite)."""
    q = quote.lower()
    if any(t in q for t in _distinct_tokens(entry.get("case", ""))):
        return True
    volume = re.findall(r"\d+", str(entry.get("citation") or ""))[:1]
    return bool(volume) and volume[0] in re.findall(r"\d+", quote)


# ---------------------------------------------------------------- memo_cite_audit

def audit_input(memo, sot):
    return "Audit the case citations in this research memo against the firm's lists.\n" + _blocks(memo, sot)


def parse_audit(reply, memo, sot):
    """-> (accepted, rejected). Accepted entries carry the firm entry they were validated against."""
    obj = extract_json(reply)
    trusted, fabricated = _firm_lists(sot)
    lists = {**{c["case"]: ("trusted", c) for c in trusted}, **{c["case"]: ("fabricated", c) for c in fabricated}}
    accepted, rejected = [], []
    for a in obj.get("authorities") or []:
        if not isinstance(a, dict):
            continue
        quote, status, entry_name = str(a.get("quote") or "").strip(), a.get("status"), a.get("firm_entry")
        problems = [str(p) for p in a.get("problems") or [] if p][:4]
        item = {"quote": quote, "status": status, "firm_entry": entry_name, "problems": problems}
        if not quote or quote not in memo:
            rejected.append({**item, "why": "quote is not verbatim in the memo"})
            continue
        if status not in AUDIT_STATUSES:
            rejected.append({**item, "why": "unknown status"})
            continue
        if entry_name:
            kind_entry = lists.get(entry_name)
            if not kind_entry:
                rejected.append({**item, "why": "firm_entry is not on either firm list"})
                continue
            kind, entry = kind_entry
            if not _linked(quote, entry):
                # The agent tied the quote to a list entry it does not name: keep the authority, drop the link.
                item.update(firm_entry=None, status="unverified", problems=problems + ["the agent's list match could not be confirmed"])
            elif (status == "fabricated") != (kind == "fabricated"):
                rejected.append({**item, "why": f"status {status} contradicts the {kind} list"})
                continue
        elif status in ("verified", "misstated", "fabricated"):
            rejected.append({**item, "why": f"status {status} without a firm_entry"})
            continue
        accepted.append(item)
    return accepted, rejected


# ---------------------------------------------------------------- memo_style_review

def review_input(memo):
    return f"Review the style of this research memo.\n<memo>\n{memo}\n</memo>"


def parse_review(reply, memo):
    obj = extract_json(reply)
    accepted, rejected = [], []
    for i in obj.get("issues") or []:
        if not isinstance(i, dict):
            continue
        item = {"quote": str(i.get("quote") or "").strip(), "kind": i.get("kind"), "why": str(i.get("why") or "")[:300]}
        if not item["quote"] or item["quote"] not in memo:
            rejected.append({**item, "reject": "quote is not verbatim in the memo"})
        elif item["kind"] not in STYLE_KINDS:
            rejected.append({**item, "reject": "unknown kind"})
        else:
            accepted.append(item)
    return {"reads_as": obj.get("reads_as"), "issues": accepted, "notes": str(obj.get("notes") or "")[:400]}, rejected


# ---------------------------------------------------------------- memo_revision

def revision_input(memo, decision, sot):
    findings = {"decision": decision.get("decision"), "reason": decision.get("reason"),
                "evidence": [e for e in decision.get("evidence", []) if e.get("cited") or e.get("signal") in ("rule_check", "agent")]}
    return ("Revise this held or blocked research memo so it can be resubmitted through the gate.\n"
            + _blocks(memo, sot, f"<findings>\n{json.dumps(findings, indent=1)}\n</findings>\n"))


def parse_revision(reply, files_text=None):
    obj = extract_json(reply)
    revised = (files_text or {}).get("revised_memo.md") or obj.get("revised_memo") or ""
    changes_md = (files_text or {}).get("changes.md")
    changes = [re.sub(r"^\s*[-*]\s*", "", ln).strip() for ln in changes_md.splitlines() if ln.strip()] if changes_md \
        else [str(c) for c in obj.get("changes") or []]
    if not revised.strip():
        raise ValueError("the drafter returned no revised memo")
    return {"revised_memo": revised.strip(), "changes": [c for c in changes if c][:30]}
