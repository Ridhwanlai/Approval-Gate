"""Your wrapper. Start with check_source_of_truth() and policy.json.

Flow: call Ezio -> read its signals -> check your persona's source of truth -> apply policy
-> decision (+ reason, evidence) -> audit log -> run the mock action only if approved.

Run one input:  python3 wrapper.py customer_support "Your 30% loyalty discount applies to the Pro plan."
Score yourself: python3 evaluate.py customer_support
"""
import csv
import json
import math
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import audit
import citation_check
import mock_actions
from ezio_client import EzioError, analyze

HERE = Path(__file__).resolve().parent
PERSONAS = HERE.parent / "personas"
POLICY = json.loads((HERE / "policy.json").read_text())

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
class Finding:
    level: str
    message: str
    evidence: dict = None


@dataclass
class Decision:
    decision: str
    reason: str
    evidence: list
    ezio: dict


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


@lru_cache(maxsize=None)
def load_source_of_truth(persona):
    """Every .json and .csv file in the persona folder, keyed by file name (for example sot['policies'])."""
    data = {}
    for f in sorted((PERSONAS / persona).iterdir()):
        if f.stem in ("practice_set", "business_rules"):
            continue
        if f.suffix == ".json":
            data[f.stem] = json.loads(f.read_text())
        elif f.suffix == ".csv":
            with f.open() as handle:
                data[f.stem] = list(csv.DictReader(handle))
    return data


def check_source_of_truth(text, context, sot):
    """Paralegal: every case in the memo against trusted_citations.json and known_fabricated.json.

    Fabricated or misstated (reporter, year, holding) -> BLOCK; unknown case or advocacy language -> HOLD.
    Verified cases come back as APPROVE-level findings so they appear as evidence.
    See citation_check.py and personas/paralegal/business_rules.md.
    """
    return [Finding(level or APPROVE, message, ev) for level, message, ev in citation_check.check(text, sot)]


def apply_policy(signals, findings, policy):
    decision, reasons = APPROVE, []

    def raise_to(level, reason):
        nonlocal decision
        reasons.append((level, reason))
        if RANK[level] > RANK[decision]:
            decision = level

    for f in findings:
        if f.level != APPROVE:
            raise_to(f.level, f.message)
    if signals is None:
        raise_to(policy["on_ezio_failure"], "Ezio could not give a usable answer, so only the firm's citation lists were checked")
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


def explain(decision, reasons, findings, signals):
    verified = [f for f in findings if f.level == APPROVE]
    if decision == APPROVE:
        what = f"{len(verified)} case citation(s) match the firm's trusted list" if verified else "the memo cites no cases, so there is nothing to cite-check"
        ezio = f"Ezio scored it {signals.tier} {signals.score:g}" if signals else "Ezio"
        return f"Approved: {what}, and {ezio} with no contradicted claims or weak dimensions. Sent to the partner."
    top = [r for lvl, r in reasons if lvl == decision][:3]
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


def decide(text, context=None, persona="customer_support", scenario=None, audit_log=True):
    context = context or {}
    signals, response, error = None, None, None
    try:
        response = analyze(text, scenario=scenario)
        signals = parse_signals(response)
    except EzioError as e:
        error, response = str(e), e.response
    try:
        findings = check_source_of_truth(text, context, load_source_of_truth(persona))
    except Exception as e:  # a broken check must fail closed, never open
        findings = [Finding(HOLD, f"the citation check hit an internal error ({type(e).__name__})")]
    reasons, decision = apply_policy(signals, findings, POLICY)
    evidence = [f.evidence or {"finding": f.message} for f in findings]
    evidence.append(ezio_evidence(signals, response, error))
    result = Decision(decision, explain(decision, reasons, findings, signals), evidence,
                      response if error is None else {"error": error, "response": response})
    if audit_log:
        audit.record(persona, text, context, result)
    return result


def gate(text, context=None, persona="customer_support"):
    result = decide(text, context, persona)
    print(f"{result.decision}: {result.reason}")
    if result.decision == APPROVE:
        mock_actions.run_for_persona(persona, text)
    else:
        print("[GATE] action not executed")
    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit('usage: python3 wrapper.py <persona> "<text>" [context-json]')
    gate(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}, sys.argv[1])
