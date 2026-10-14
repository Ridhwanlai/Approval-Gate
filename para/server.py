"""Para's product server: the persona page, the gate, the review queue and the agent routes.

Browser-facing contract (the browser never sees a key, harness id or session id):
  GET  /health                      gate and agent configuration status
  GET  /api/samples                 practice memos
  POST /api/checks                  {text, context}       -> 202 {id}; the check runs in the background
  GET  /api/checks/<id>             progress per stage, then the decision
  POST /api/checks/<id>/review      {outcome, note, reviewer}   attorney resolves a HOLD
  POST /api/checks/<id>/revision    -> 202 {id}; the drafter harness proposes a fix (never sent)
  GET  /api/revisions/<id>          progress, then the revised draft and change list
  GET  /api/trace/<job>/<feature>   sanitized trace of one harness run
  GET  /api/models                  each agent's default model, the user's choice and the models offered
  PUT  /api/models                  {feature, model}; model null goes back to the harness default
  GET  /audit                       recent audit records
  POST /decide                      synchronous check, kept for the challenge's gate.py contract
"""
import json
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import agents, config, pipeline, records, settings
from .hr_client import HRError

UI_DIR = config.ROOT / "ui"
MAX_TEXT = 20000
SERVER_ONLY = ("session_id", "response_id", "harness_runs", "ezio")


def public_audit(rec):
    return {k: v for k, v in rec.items() if k not in SERVER_ONLY}


class Handler(BaseHTTPRequestHandler):
    server_version = "Para"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, code, body):
        raw = json.dumps(body, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 1_000_000:
            raise ValueError("bad length")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data

    def memo_from(self, data):
        text, context = data.get("text"), data.get("context", {})
        if isinstance(context, str):
            context = {"re": context} if context.strip() else {}
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or not isinstance(context, dict):
            raise ValueError("bad memo")
        return text, context

    # ------------------------------------------------------------ GET

    def do_GET(self):
        route = self.path.partition("?")[0]
        if route == "/health":
            return self.send_json(200, {"status": "ok", "agents": agents.status()})
        if route == "/api/samples":
            items = json.loads((config.FIRM_DIR / "practice_set.json").read_text())["items"]
            return self.send_json(200, {"samples": [{"id": i["id"], "text": i["text"], "context": i["context"]} for i in items]})
        if route == "/api/models":
            try:
                return self.send_json(200, {"agents": [agents.model_options(f) for f in agents.HARNESS_NAMES if f in config.registry()]})
            except HRError as e:
                return self.send_json(502, {"error": str(e)})
        if route == "/audit":
            return self.send_json(200, {"records": [public_audit(r) for r in records.read_audit()]})
        m = re.fullmatch(r"/api/(checks|revisions)/([a-z]{3}_[0-9a-f]{12})", route)
        if m:
            job = pipeline.get(m.group(2))
            if job is None or job.kind != m.group(1)[:-1]:
                return self.send_json(404, {"error": "Unknown id"})
            return self.send_json(200, job.public())
        m = re.fullmatch(r"/api/trace/([a-z]{3}_[0-9a-f]{12})/([a-z_]+)", route)
        if m:
            try:
                t = pipeline.trace(m.group(1), m.group(2))
            except HRError as e:
                return self.send_json(502, {"error": str(e)})
            return self.send_json(200, t) if t else self.send_json(404, {"error": "No trace for that run"})
        return self.static(route)

    def static(self, route):
        name = "persona.html" if route in ("/", "") else route.lstrip("/")
        target = (UI_DIR / name).resolve()
        if UI_DIR.resolve() not in target.parents or not target.is_file():
            return self.send_json(404, {"error": "Unknown endpoint"})
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # ------------------------------------------------------------ POST

    def do_PUT(self):
        if self.path.partition("?")[0] != "/api/models":
            return self.send_json(404, {"error": "Unknown endpoint"})
        try:
            data = self.body()
            feature, model = data.get("feature"), data.get("model") or None
            if feature not in agents.HARNESS_NAMES:
                raise ValueError
            if model is not None and model not in {o["id"] for o in agents.model_options(feature)["options"]}:
                return self.send_json(400, {"error": f"{model} is not offered for this agent"})
        except (ValueError, UnicodeDecodeError, OSError):
            return self.send_json(400, {"error": "Send {feature, model}; model null resets to the default"})
        except HRError as e:
            return self.send_json(502, {"error": str(e)})
        settings.set_model(feature, model)
        return self.send_json(200, agents.model_options(feature))

    def do_POST(self):
        route = self.path.partition("?")[0]
        try:
            data = self.body()
        except (ValueError, UnicodeDecodeError, OSError):
            return self.send_json(400, {"error": "Send a JSON object"})
        if route in ("/api/checks", "/decide"):
            try:
                text, context = self.memo_from(data)
            except ValueError:
                return self.send_json(400, {"error": f"text must be a non-empty string of at most {MAX_TEXT} characters"})
            if route == "/api/checks":
                return self.send_json(202, {"id": pipeline.start_check(text, context).id})
            job = pipeline.start_check(text, context, background=False)
            return self.send_json(200, job.result)
        m = re.fullmatch(r"/api/checks/(chk_[0-9a-f]{12})/(review|revision)", route)
        if m and m.group(2) == "review":
            outcome, note = data.get("outcome"), str(data.get("note") or "").strip()
            if outcome not in ("proceed", "reject") or not note:
                return self.send_json(400, {"error": "Review needs outcome (proceed or reject) and a reason"})
            code, body = pipeline.review(m.group(1), outcome, note[:2000], str(data.get("reviewer") or "").strip()[:120])
            return self.send_json(code, body)
        if m and m.group(2) == "revision":
            job, err = pipeline.start_revision(m.group(1))
            return self.send_json(202, {"id": job.id}) if job else self.send_json(409, {"error": err})
        return self.send_json(404, {"error": "Unknown endpoint"})


def serve(host=None, port=None):
    host, port = host or os.getenv("HOST", "127.0.0.1"), int(port or os.getenv("PORT", "9000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Para on http://{host}:{port}/  (agents: {agents.status()})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
