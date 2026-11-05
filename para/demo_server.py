"""Para's free public demo: paste a memo on the website and it runs through the real gate and agents.

  POST /api/demo/check          {text}  -> 202 {id}
  GET  /api/demo/checks/<id>    progress per stage, then the decision
  GET  /health

No accounts, no review queue, no drafting, nothing stored and nothing sent: the audit log and the send
action are switched off in demo mode (config.DEMO). Limits keep the operator's HarnessRouter credits
safe: checks per visitor per day, a daily total, memo length, and how many checks run at once. Only
the website's own origin (DEMO_ORIGINS) may call it from a browser.
"""
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import agents, pipeline

MAX_CHARS = int(os.getenv("DEMO_MAX_CHARS", "3000"))
PER_VISITOR = int(os.getenv("DEMO_PER_VISITOR", "5"))      # checks per visitor per day
PER_DAY = int(os.getenv("DEMO_PER_DAY", "100"))             # checks per day in total
AT_ONCE = int(os.getenv("DEMO_AT_ONCE", "2"))               # checks running at the same time
ORIGINS = {o.strip().rstrip("/") for o in os.getenv("DEMO_ORIGINS", "https://ridhwanlai.github.io").split(",") if o.strip()}

_lock = threading.Lock()
_day = {"date": None, "total": 0, "visitors": {}}


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def take_quota(visitor):
    """Count one check for this visitor today, or return a message saying why not."""
    with _lock:
        if _day["date"] != _today():
            _day.update(date=_today(), total=0, visitors={})
        if _day["total"] >= PER_DAY:
            return "The demo has reached today's limit. Please try again tomorrow, or download Para to run it yourself."
        if _day["visitors"].get(visitor, 0) >= PER_VISITOR:
            return f"You've used today's {PER_VISITOR} demo checks. Download Para to keep checking memos for free."
        _day["total"] += 1
        _day["visitors"][visitor] = _day["visitors"].get(visitor, 0) + 1
        return None


def give_back(visitor):
    with _lock:
        _day["total"] = max(0, _day["total"] - 1)
        if visitor in _day["visitors"]:
            _day["visitors"][visitor] = max(0, _day["visitors"][visitor] - 1)


def public_result(job):
    """What the website may show: the decision, reason and evidence. No session or harness ids."""
    data = job.public()
    return {k: data[k] for k in ("id", "state", "seconds", "stages", "result")}


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "Para-demo"

    def log_message(self, fmt, *args):
        pass  # visitors' memos and addresses are not logged

    def cors(self):
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if origin in ORIGINS:
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        return {}

    def send_json(self, code, body):
        raw = json.dumps(body, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in self.cors().items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def visitor(self):
        forwarded = [p.strip() for p in (self.headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
        return forwarded[-1] if forwarded else self.client_address[0]

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in self.cors().items():
            self.send_header(k, v)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        route = self.path.partition("?")[0]
        if route == "/health":
            return self.send_json(200, {"status": "ok", "agents": agents.status()})
        m = re.fullmatch(r"/api/demo/checks/(chk_[0-9a-f]{12})", route)
        if m:
            job = pipeline.get(m.group(1))
            return self.send_json(200, public_result(job)) if job else self.send_json(404, {"error": "That check has expired. Run it again."})
        return self.send_json(404, {"error": "Unknown endpoint"})

    def do_POST(self):
        if self.path.partition("?")[0] != "/api/demo/check":
            return self.send_json(404, {"error": "Unknown endpoint"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 100_000:
                raise ValueError
            data = json.loads(self.rfile.read(length))
            text = data.get("text") if isinstance(data, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise ValueError
        except (ValueError, UnicodeDecodeError, OSError):
            return self.send_json(400, {"error": "Paste a memo to check."})
        if len(text) > MAX_CHARS:
            return self.send_json(400, {"error": f"The demo checks memos up to {MAX_CHARS:,} characters. Trim it, or download Para for longer memos."})
        pipeline.prune()
        if pipeline.running_total() >= AT_ONCE:
            return self.send_json(429, {"error": "Para is checking other memos right now. Try again in a minute."})
        refused = take_quota(self.visitor())
        if refused:
            return self.send_json(429, {"error": refused})
        try:
            job = pipeline.start_check(text.strip(), {})
        except Exception:
            give_back(self.visitor())
            return self.send_json(500, {"error": "The check could not start. Try again in a minute."})
        return self.send_json(202, {"id": job.id})


def serve(host, port):
    server = ThreadingHTTPServer((host, port), DemoHandler)
    print(f"Para demo on http://{host}:{port}/  (agents: {agents.status()}; origins: {', '.join(sorted(ORIGINS))})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
