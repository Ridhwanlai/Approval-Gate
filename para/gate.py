"""The decision. Only this module turns signals into APPROVE, HOLD_FOR_REVIEW or BLOCK.

Signals, in order of authority:
  1. The firm's citation lists, checked deterministically (citation_check.py).
  2. Ezio's scores and fact-check (ezio_client.py).
  3. HarnessRouter agents (agents.py): evidence only. They can raise a decision, never lower it.
"""
import json
import math
import re
from dataclasses import dataclass, field

from . import agents, citation_check, config
from .ezio_client import EzioError

POLICY = json.loads((config.ROOT / "para" / "policy.json").read_text())
APPROVE, HOLD, BLOCK = "APPROVE", "HOLD_FOR_REVIEW", "BLOCK"
RANK = {APPROVE: 0, HOLD: 1, BLOCK: 2}


@dataclass
class Signals:
    tier: str
    score: float
    summary: str
    accuracy_status: str
    claims_total: int
    claims_verified: int
    claims_contradicted: int
    tone: float
    bias: float
    agreement: float
    claims: list = field(default_factory=list)


@dataclass
class AgentResult:
    """What one harness contributed to a check. Session and response ids stay on the server."""
    feature: str
    ok: bool
    error: str = None
    accepted: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    notes: str = ""
    elapsed: float = None
    model: str = None
    session_id: str = None
    response_id: str = None

    def evidence(self):
        return {"signal": "agent", "feature": self.feature, "label": agents.LABELS[self.feature],
                "harness": agents.HARNESS_NAMES[self.feature], "ok": self.ok, "error": self.error,
                "findings": self.accepted, "rejected": self.rejected, "notes": self.notes,
                "elapsed": self.elapsed, "model": self.model, "trace": bool(self.session_id)}


def parse_signals(resp):
    try:
        verdict, dims = resp["verdict"], resp["dimensions"]
        acc = dims["accuracy"]
        if verdict["tier"] not in ("GREEN", "YELLOW", "RED") or acc["status"] not in ("scored", "not_scored", "unavailable"):
            raise ValueError("invalid tier or accuracy status")
        scores = [verdict["score"]] + [dims[d]["score"] for d in ("tone", "bias", "agreement")]
        if acc["status"] == "scored":
            scores.append(acc["score"])
        if any(isinstance(s, bool) or not isinstance(s, (int, float)) or not math.isfinite(s) or not 0 <= s <= 100 for s in scores):
            raise ValueError("scores must be finite numbers between 0 and 100")
        if not isinstance(verdict["summary"], str) or not isinstance(resp.get("claims", []), list):
            raise ValueError("invalid summary or claims")
        return Signals(
            tier=verdict["tier"], score=verdict["score"], summary=verdict["summary"],
            accuracy_status=acc["status"], claims_total=acc.get("claims_total", 0),
            claims_verified=acc.get("claims_verified", 0), claims_contradicted=acc.get("claims_contradicted", 0),
            tone=dims["tone"]["score"], bias=dims["bias"]["score"], agreement=dims["agreement"]["score"],
            claims=resp.get("claims", []),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise EzioError("malformed mock response", resp) from e


def load_source_of_truth():
    return {stem: json.loads((config.FIRM_DIR / f"{stem}.json").read_text())
            for stem in ("trusted_citations", "known_fabricated")}


PLACEHOLDER_RE = re.compile(r"\[(?:AUTHORITY NEEDED|CITE-CHECK NEEDED|CITATION NEEDED|TODO|TK)\b[^\]]*\]", re.I)


def check_placeholders(text, policy=POLICY):
    """Drafting placeholders (for example from the revision drafter) mean the memo is unfinished."""
    found = PLACEHOLDER_RE.findall(text)
    if not found:
        return []
    shown = ", ".join(f"\"{p if len(p) <= 60 else p[:57] + '…]'}\"" for p in found[:2])
    return [(policy["drafting_placeholders"]["decision"], f"the memo still has {len(found)} drafting placeholder(s) to resolve: {shown}",
             {"signal": "rule_check", "rule": "Unresolved drafting placeholders", "matches": found[:5]})]


def check_lists(text, sot):
    """Deterministic checks -> [(level, message, evidence)]; a crash fails closed to HOLD."""
    try:
        return [(level or APPROVE, msg, ev) for level, msg, ev in citation_check.check(text, sot)] + check_placeholders(text)
    except Exception as e:  # a broken check must never approve
        return [(HOLD, f"the citation check hit an internal error ({type(e).__name__})", {"finding": "citation check error"})]


def _span(text, s):
    s = (s or "").replace(" (short form)", "")
    i = text.find(s) if s else -1
    return (i, i + len(s)) if i >= 0 else None


def _overlapping(text, quote, list_findings):
    q = _span(text, quote)
    out = []
    for level, _, ev in list_findings:
        c = _span(text, (ev or {}).get("cited"))
        if q and c and q[0] < c[1] and c[0] < q[1]:
            out.append((level, ev))
    return out


def agent_reasons(text, list_findings, results, policy):
    """Turn validated agent findings into (level, reason) pairs. Never returns APPROVE-lowering reasons."""
    rules = policy["agents"]
    out = []
    for r in results:
        label = agents.LABELS[r.feature]
        if not r.ok:
            out.append((rules["on_failure"], f"the {label} could not finish ({r.error}), so a person reads it first"))
            continue
        if r.feature == agents.CITE_AUDIT:
            for a in r.accepted:
                near = _overlapping(text, a["quote"], list_findings)
                listed = {(ev or {}).get("status") for _, ev in near}
                why = "; ".join(a["problems"]) or a["status"]
                if a["status"] == "fabricated":
                    if "fabricated" not in listed:
                        out.append((rules["fabricated"], f"the {label} matched \"{a['quote']}\" to the firm's fabricated case {a['firm_entry']}"))
                elif a["status"] == "misstated":
                    if not listed & {"near_miss", "fabricated"}:
                        out.append((rules["misstated"], f"the {label} reads \"{a['quote']}\" as misstating {a['firm_entry']}: {why}"))
                elif a["status"] == "unverified":
                    if not listed:
                        out.append((rules["unverified"], f"the {label} found an authority the list check missed, \"{a['quote']}\", on neither firm list"))
        elif r.feature == agents.STYLE_REVIEW and r.accepted:
            shown = ", ".join(f"{i['kind'].replace('_', '-')} (\"{i['quote']}\")" for i in r.accepted[:3])
            out.append((rules["advocacy"], f"the {label} says it reads as advocacy: {shown}"))
    return out


def apply_policy(signals, list_findings, agent_pairs, policy):
    decision, reasons = APPROVE, []

    def raise_to(level, reason):
        nonlocal decision
        reasons.append((level, reason))
        if RANK[level] > RANK[decision]:
            decision = level

    for level, msg, _ in list_findings:
        if level != APPROVE:
            raise_to(level, msg)
    for level, msg in agent_pairs:
        raise_to(level, msg)
    if signals is None:
        raise_to(policy["on_ezio_failure"], "Ezio could not give a usable answer, so only the firm's lists and the AI checks were applied")
        return reasons, decision
    contradicted = [c for c in signals.claims if isinstance(c, dict) and str(c.get("verdict", "")).lower() == "contradicted"]
    n = max(signals.claims_contradicted or 0, len(contradicted))
    if n > policy["contradicted_claims"]["block_above"]:
        first = contradicted[0].get("text") if contradicted else None
        raise_to(BLOCK, f"Ezio's fact-check contradicted {n} claim(s)" + (f": \"{first}\"" if first else ""))
    for dim, rule in policy["dimension_minimums"].items():
        score = getattr(signals, dim)
        if score < rule["hold_below"]:
            raise_to(HOLD, f"Ezio's {dim} score is {score:g}, below the {rule['hold_below']} floor, even though the overall tier is {signals.tier}")
    if signals.accuracy_status == "not_scored" and policy["when_accuracy_not_scored"] != "ignore":
        raise_to(policy["when_accuracy_not_scored"], "Ezio checked no claims")
    if signals.accuracy_status == "unavailable":
        raise_to(policy["when_accuracy_unavailable"], "Ezio's fact-check was unavailable, so statements beyond the citation lists are unchecked")
    tier_decision = policy["tier_decisions"][signals.tier]
    if tier_decision != APPROVE:
        raise_to(tier_decision, f"Ezio's overall tier is {signals.tier} ({signals.score:g})")
    return reasons, decision


def explain(decision, reasons, list_findings, signals, results):
    verified = [1 for level, _, ev in list_findings if level == APPROVE and (ev or {}).get("status") == "verified"]
    if decision == APPROVE:
        what = f"{len(verified)} case citation(s) match the firm's trusted list" if verified else "the memo cites no cases, so there is nothing to cite-check"
        ezio = f"Ezio scored it {signals.tier} {signals.score:g}" if signals else "Ezio"
        ai = " The AI cite-audit and memo review raised nothing." if results and all(r.ok for r in results) else ""
        return f"Approved: {what}, and {ezio} with no contradicted claims or weak dimensions.{ai} Sent to the partner."
    top = list(dict.fromkeys(r for lvl, r in reasons if lvl == decision))[:3]
    if decision == BLOCK:
        return "Blocked before sending: " + "; ".join(top) + ". The partner never received this memo."
    return "Held for attorney review: " + "; ".join(top) + ". Not sent until a reviewer approves it."


def ezio_evidence(signals, response, error=None):
    if signals is None:
        return {"signal": "ezio", "reachable": False, "error": error, "malformed": bool(response)}
    return {"signal": "ezio", "reachable": True, "tier": signals.tier, "score": signals.score,
            "accuracy_status": signals.accuracy_status, "claims_total": signals.claims_total,
            "claims_verified": signals.claims_verified, "claims_contradicted": signals.claims_contradicted,
            "tone": signals.tone, "bias": signals.bias, "agreement": signals.agreement,
            "contradicted_claims": [{"claim": c.get("text"), "source": c.get("source")} for c in signals.claims
                                    if isinstance(c, dict) and str(c.get("verdict", "")).lower() == "contradicted"],
            "supported_claims": [c.get("text") for c in signals.claims
                                 if isinstance(c, dict) and str(c.get("verdict", "")).lower() == "supported"],
            "summary": signals.summary}


def decide(text, signals, ezio_response, ezio_error, list_findings, results, policy=POLICY):
    agent_pairs = agent_reasons(text, list_findings, results, policy)
    reasons, decision = apply_policy(signals, list_findings, agent_pairs, policy)
    evidence = [ev or {"finding": msg} for _, msg, ev in list_findings]
    evidence.append(ezio_evidence(signals, ezio_response, ezio_error))
    evidence.extend(r.evidence() for r in results)
    return {"decision": decision, "reason": explain(decision, reasons, list_findings, signals, results),
            "evidence": evidence, "ezio": ezio_response if ezio_error is None else {"error": ezio_error, "response": ezio_response}}
