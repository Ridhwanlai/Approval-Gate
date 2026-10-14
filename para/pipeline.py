"""Runs a memo through every signal, then lets gate.py decide. Also runs the revision drafter.

The product server, not any agent, decides which harness runs and when:
  check:    list check (instant) + Ezio + memo_cite_audit + memo_style_review, in parallel -> gate.decide
  revision: only on the paralegal's request, only for a HOLD or BLOCK, and its draft is never sent
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import agents, gate, records, tenant
from .ezio_client import EzioError, analyze
from .hr_client import HRError

_jobs, _lock = {}, threading.Lock()
ID_PREFIX = {"check": "chk", "revision": "rev", "draft": "drf"}
STAGE_LABELS = {"lists": "Firm citation lists", "ezio": "Ezio fact-check", agents.CITE_AUDIT: "AI cite-audit (Hermes)",
                agents.STYLE_REVIEW: "AI memo review (Hermes)", agents.REVISION: "AI revision drafter (Hermes)",
                agents.DRAFT: "AI memo drafter (Hermes)"}


class Job:
    def __init__(self, kind, stages, **extra):
        self.id = f"{ID_PREFIX[kind]}_{uuid.uuid4().hex[:12]}"  # server.py routes match these prefixes
        self.kind, self.state, self.result, self.error = kind, "running", None, None
        self.stages = {s: {"label": STAGE_LABELS[s], "state": "pending", "detail": ""} for s in stages}
        self.sessions = {}  # feature -> (session_id, response_id); server-side only
        self.firm_id = tenant.current()  # hosted: only this firm may see or act on the job
        self.started = time.monotonic()
        self.__dict__.update(extra)
        with _lock:
            _jobs[self.id] = self

    def stage(self, name, state, detail=""):
        with _lock:
            self.stages[name].update(state=state, detail=detail)

    def public(self):
        with _lock:
            return {"id": self.id, "kind": self.kind, "state": self.state, "error": self.error,
                    "seconds": round(time.monotonic() - self.started, 1),
                    "stages": [{"key": k, **v} for k, v in self.stages.items()], "result": self.result}


def get(job_id):
    """The job, only if it belongs to the current firm (hosted) or this install (local)."""
    with _lock:
        job = _jobs.get(job_id)
    return job if job is not None and job.firm_id == tenant.current() else None


# ---------------------------------------------------------------- one harness run

PROGRESS = {"queued": "Queued: waiting for a free HarnessRouter slot", "waiting": "Sent to HarnessRouter; waiting for a sandbox", "started": "Agent is working",
            "activity": "Agent is working", "retry": "Retrying after a transient error",
            "recovering": "Connection dropped; recovering the result without resubmitting"}


def run_agent(job, feature, task_input, parse, deadline_s):
    def on_event(kind, data):
        if kind == "started":
            job.sessions[feature] = (data.get("session_id"), data.get("response_id"))
        job.stage(feature, "running", PROGRESS.get(kind, "Agent is working"))

    job.stage(feature, "running", "Starting")
    try:
        run = agents.run_feature(feature, task_input, on_event=on_event, deadline_s=deadline_s)
        job.sessions[feature] = (run.session_id, run.response_id)
        accepted, rejected, notes = parse(run)
        result = gate.AgentResult(feature, True, accepted=accepted, rejected=rejected, notes=notes, elapsed=run.elapsed,
                                  model=run.model, session_id=run.session_id, response_id=run.response_id)
        job.stage(feature, "done", f"{len(accepted)} finding(s) in {run.elapsed:g} s" + (f", {len(rejected)} rejected" if rejected else ""))
    except HRError as e:
        sid, rid = job.sessions.get(feature, (e.session_id, e.response_id))
        result = gate.AgentResult(feature, False, error=str(e), session_id=sid, response_id=rid)
        job.stage(feature, "failed", str(e))
    except (ValueError, KeyError, TypeError) as e:
        sid, rid = job.sessions.get(feature, (None, None))
        result = gate.AgentResult(feature, False, error=f"the reply could not be used: {e}", session_id=sid, response_id=rid)
        job.stage(feature, "failed", "The agent's reply could not be used")
    return result


# ---------------------------------------------------------------- check

def start_check(text, context, scenario=None, background=True):
    features = list(gate.POLICY["agents"]["run_on_check"])
    job = Job("check", ["lists", "ezio", *features], text=text, context=context)
    if background:
        tenant.thread(_run_check, job, scenario, features).start()
    else:
        _run_check(job, scenario, features)
    return job


def _run_check(job, scenario, features):
    try:
        text, sot = job.text, gate.load_source_of_truth()
        deadline = gate.POLICY["agents"]["deadline_seconds"]
        job.stage("lists", "running")
        list_findings = gate.check_lists(text, sot)
        flagged = sum(1 for lvl, _, _ in list_findings if lvl != gate.APPROVE)
        job.stage("lists", "done", f"{len(list_findings)} finding(s), {flagged} flagged")

        def ezio():
            job.stage("ezio", "running", "Scoring")
            try:
                resp = analyze(text, scenario=scenario)
                sig = gate.parse_signals(resp)
                job.stage("ezio", "done", f"{sig.tier} {sig.score:g}")
                return sig, resp, None
            except EzioError as e:
                job.stage("ezio", "failed", str(e))
                return None, e.response, str(e)

        def audit_parse(run):
            accepted, rejected = agents.parse_audit(run.text, text, sot)
            return accepted, rejected, ""

        def review_parse(run):
            res, rejected = agents.parse_review(run.text, text)
            return res["issues"], rejected, res["notes"]

        jobs = {agents.CITE_AUDIT: (agents.audit_input(text, sot), audit_parse),
                agents.STYLE_REVIEW: (agents.review_input(text), review_parse)}
        with ThreadPoolExecutor(max_workers=1 + len(features)) as pool:
            f_ezio = tenant.submit(pool, ezio)
            f_agents = [tenant.submit(pool, run_agent, job, f, *jobs[f], deadline) for f in features]
            signals, response, error = f_ezio.result()
            results = [f.result() for f in f_agents]

        out = gate.decide(text, signals, response, error, list_findings, results)
        delivery = None
        if out["decision"] == gate.APPROVE:
            delivery = records.submit_memo_to_partner(text, job.id, out["reason"], (job.context or {}).get("re", ""))
        executed = bool(delivery and delivery["sent"])
        records.audit({"type": "decision", "check_id": job.id, "input": text, "context": job.context,
                       "decision": out["decision"], "reason": out["reason"], "evidence": out["evidence"],
                       "ezio": out["ezio"], "action_executed": executed, "delivery": delivery,
                       "harness_runs": [{"feature": r.feature, "harness": agents.HARNESS_NAMES[r.feature], "ok": r.ok,
                                         "error": r.error, "session_id": r.session_id, "response_id": r.response_id,
                                         "elapsed": r.elapsed} for r in results]})
        job.decision = out["decision"]
        job.result = {"check_id": job.id, "decision": out["decision"], "reason": out["reason"],
                      "evidence": out["evidence"], "action_executed": executed, "delivery": delivery,
                      "can_revise": out["decision"] != gate.APPROVE}
        job.state = "done"
    except Exception as e:  # never crash open: an unexpected error records a HOLD and sends nothing
        job.decision = gate.HOLD
        job.result = {"check_id": job.id, "decision": gate.HOLD, "action_executed": False, "evidence": [], "can_revise": False,
                      "reason": f"Held for attorney review: the gate hit an internal error ({type(e).__name__}). Not sent."}
        records.audit({"type": "decision", "check_id": job.id, "input": job.text, "decision": gate.HOLD,
                       "reason": job.result["reason"], "action_executed": False})
        job.state = "done"


# ---------------------------------------------------------------- review (attorney resolves a HOLD)

def review(check_id, outcome, note, reviewer):
    job = get(check_id)
    if job is None or job.kind != "check" or job.state != "done":
        return 404, {"error": "Unknown or unfinished check"}
    if getattr(job, "decision", None) != gate.HOLD or getattr(job, "reviewed", False):
        return 409, {"error": "Only a memo the gate held, and not yet reviewed, can be resolved here; blocked memos must be fixed and resent"}
    job.reviewed = True
    delivery = None
    if outcome == "proceed":
        delivery = records.submit_memo_to_partner(job.text, check_id, f"Held, then approved by {reviewer or 'an attorney'}: {note}",
                                                  (job.context or {}).get("re", ""))
    executed = bool(delivery and delivery["sent"])
    records.audit({"type": "review", "check_id": check_id, "input": job.text, "outcome": outcome,
                   "reviewer": reviewer or "unnamed reviewer", "note": note, "action_executed": executed, "delivery": delivery})
    return 200, {"logged": True, "action_executed": executed, "delivery": delivery}


# ---------------------------------------------------------------- revision (drafter harness)

def start_revision(check_id):
    check = get(check_id)
    if check is None or check.kind != "check" or check.state != "done" or not check.result:
        return None, "Unknown or unfinished check"
    if check.result["decision"] == gate.APPROVE:
        return None, "Approved memos are already with the partner; there is nothing to revise"
    job = Job("revision", [agents.REVISION], check_id=check_id)
    tenant.thread(_run_revision, job, check).start()
    return job, None


def _run_revision(job, check):
    sot = gate.load_source_of_truth()
    out = {}

    def parse(run):
        files = {}
        try:
            for f in agents.client().changed_files(run.session_id):
                path = f.get("path") or f.get("name") or ""
                if path.split("/")[-1] in ("revised_memo.md", "changes.md"):
                    files[path.split("/")[-1]] = agents.client().read_file(run.session_id, path)
        except HRError:
            files = {}  # fall back to the copy in the JSON reply
        out.update(agents.parse_revision(run.text, files))
        return [], [], ""

    task = agents.revision_input(check.text, check.result, sot)
    result = run_agent(job, agents.REVISION, task, parse, deadline_s=300)
    records.audit({"type": "revision", "check_id": check.id, "revision_id": job.id, "ok": result.ok, "error": result.error,
                   "harness": agents.HARNESS_NAMES[agents.REVISION], "session_id": result.session_id,
                   "response_id": result.response_id, "changes": out.get("changes"), "action_executed": False})
    job.result = {"revision_id": job.id, "ok": result.ok, "error": result.error, **out,
                  "note": "This draft has not been sent. Load it, read it, and run it through the gate again."}
    job.state = "done"


# ---------------------------------------------------------------- draft (drafter harness, from the paralegal's question)

def start_draft(question, matter=""):
    job = Job("draft", [agents.DRAFT], question=question, matter=matter)
    tenant.thread(_run_draft, job).start()
    return job


def _run_draft(job):
    out = {}

    def parse(run):
        out.update(agents.parse_draft(run.text))
        return [], [], ""

    task = agents.draft_input(job.question, gate.load_source_of_truth(), job.matter)
    result = run_agent(job, agents.DRAFT, task, parse, deadline_s=240)
    records.audit({"type": "draft", "draft_id": job.id, "question": job.question, "ok": result.ok, "error": result.error,
                   "harness": agents.HARNESS_NAMES[agents.DRAFT], "model": result.model, "session_id": result.session_id,
                   "response_id": result.response_id, "action_executed": False})
    job.result = {"draft_id": job.id, "ok": result.ok, "error": result.error, "model": result.model, **out,
                  "note": "A draft only. Nothing was sent. Read it, edit it, then run it through the gate."}
    job.state = "done"


# ---------------------------------------------------------------- trace

def trace(job_id, feature):
    """A sanitized view of one harness run, for a person working out why the gate did what it did."""
    job = get(job_id)
    if job is None or feature not in job.sessions:
        return None
    session_id, _ = job.sessions[feature]
    if not session_id:
        return None
    steps = []
    for t in agents.client().turns(session_id):
        steps.append({"status": t.get("status"), "elapsed": t.get("elapsed"), "model": t.get("served_model"),
                      "tools": [x.get("name") or x.get("tool") or x.get("type") for x in t.get("tools") or [] if isinstance(x, dict)],
                      "files": [f.get("path") or f.get("name") for f in t.get("files") or [] if isinstance(f, dict)],
                      "error": t.get("error") or t.get("incomplete_reason"),
                      "output": (t.get("assistant") or "")[:6000]})
    return {"feature": feature, "label": STAGE_LABELS[feature], "harness": agents.HARNESS_NAMES[feature], "turns": steps}
