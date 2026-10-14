"""Para's product server: the persona page, the gate, the review queue and the agent routes.

Browser-facing contract (the browser never sees a key, harness id or session id):
  GET  /health                      gate and agent configuration status
  GET  /api/samples                 practice memos
  POST /api/checks                  {text, context}       -> 202 {id}; the check runs in the background
  GET  /api/checks/<id>             progress per stage, then the decision
  POST /api/checks/<id>/review      {outcome, note, reviewer}   attorney resolves a HOLD
  POST /api/checks/<id>/revision    -> 202 {id}; the drafter harness proposes a fix (never sent)
  GET  /api/revisions/<id>          progress, then the revised draft and change list
  POST /api/drafts                  {question, matter}  -> 202 {id}; the drafter harness writes a first draft (never sent)
  GET  /api/drafts/<id>             progress, then the draft memo
  GET  /api/trace/<job>/<feature>   sanitized trace of one harness run
  GET  /api/models                  each agent's default model, the user's choice and the models offered
  PUT  /api/models                  {feature, model}; model null goes back to the harness default
  GET  /api/me                      mode, and in hosted mode the user, firm, plan, usage and CSRF token
  GET  /audit                       recent audit records
  POST /decide                      synchronous check, kept for the challenge's gate.py contract (local only)

Hosted mode adds sign-in and the firm's account (see the HOSTED section below). Every route except the
public ones needs a session; every write needs the session's CSRF token and a same-origin request.
"""
import hmac
import json
import mimetypes
import os
import re
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import agents, config, pipeline, records, settings, tenant
from .hr_client import HRError

UI_DIR = config.ROOT / "ui"
MAX_TEXT = 20000
SERVER_ONLY = ("session_id", "response_id", "harness_runs", "ezio")
COOKIE = "para_session"
# Hosted routes reachable without signing in.
PUBLIC_GET = {"/health", "/login", "/signup", "/join", "/login.html", "/terms", "/privacy"}
PUBLIC_POST = {"/auth/signup", "/auth/login", "/auth/accept-invite", "/stripe/webhook"}
PAGES = {"/": "persona.html", "/account": "account.html", "/login": "login.html", "/signup": "login.html", "/join": "login.html",
         "/terms": "terms.html", "/privacy": "privacy.html"}


def public_audit(rec):
    return {k: v for k, v in rec.items() if k not in SERVER_ONLY}


def validate_lists(data):
    """A firm's uploaded citation lists, normalized, or ValueError with a message the owner can act on."""
    out = {}
    for key, needs in (("trusted_citations", ("case", "citation", "year", "holding")), ("known_fabricated", ("case",))):
        block = data.get(key)
        cases = block.get("cases") if isinstance(block, dict) else block
        if not isinstance(cases, list) or len(cases) > 5000:
            raise ValueError(f"{key} must be a list of cases (at most 5,000)")
        clean = []
        for i, c in enumerate(cases):
            if not isinstance(c, dict) or not all(str(c.get(f) or "").strip() for f in needs):
                raise ValueError(f"{key} entry {i + 1} needs: {', '.join(needs)}")
            clean.append({k: (str(v)[:500] if k != "year" else v) for k, v in c.items()
                          if k in ("case", "citation", "court", "year", "holding", "note")})
        out[key] = {"cases": clean}
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "Para"

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------ responses

    def send_json(self, code, body, headers=None):
        raw = json.dumps(body, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def redirect(self, location, headers=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

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

    # ------------------------------------------------------------ hosted: who is asking

    def session_token(self):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except Exception:
            return None
        return jar[COOKIE].value if COOKIE in jar else None

    def cookie(self, token, max_age):
        secure = "; Secure" if config.PUBLIC_URL.startswith("https://") else ""
        return {"Set-Cookie": f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"}

    def same_origin(self):
        """Writes must come from Para's own pages (Origin, or Referer as a fallback)."""
        source = self.headers.get("Origin") or self.headers.get("Referer")
        if not source:
            return False
        want, got = urlparse(config.PUBLIC_URL), urlparse(source)
        return (got.scheme, got.netloc) == (want.scheme, want.netloc)

    def client_ip(self):
        """The visitor's address. Behind a proxy (Render) the last X-Forwarded-For entry is the one the proxy saw;
        earlier entries can be set by the visitor, so they are not trusted."""
        forwarded = [p.strip() for p in (self.headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
        return forwarded[-1] if forwarded else self.client_address[0]

    def gate_request(self, method, route):
        """Hosted mode: attach the session and firm, or answer the request. Returns True when handled."""
        self.auth = None
        tenant.set_firm(None)
        if not config.HOSTED:
            return False
        from . import store
        self.auth = store.session(self.session_token())
        if self.auth:
            tenant.set_firm(self.auth["firm_id"])
        public = route in (PUBLIC_GET if method == "GET" else PUBLIC_POST)
        if method != "GET" and route != "/stripe/webhook":
            if not self.same_origin():
                self.send_json(403, {"error": "Requests must come from Para's own pages."})
                return True
            if not public and not hmac.compare_digest(self.headers.get("X-CSRF-Token") or "", (self.auth or {}).get("csrf") or "-"):
                self.send_json(403, {"error": "Your session expired or this request was not from Para. Reload the page."})
                return True
        if public or self.auth:
            return False
        if method == "GET" and (route in PAGES or route == "/billing/return"):
            self.redirect("/login")
        else:
            self.send_json(401, {"error": "Sign in to continue."})
        return True

    def owner_only(self):
        if config.HOSTED and (self.auth or {}).get("role") != "owner":
            self.send_json(403, {"error": "Only the firm's owner can change this."})
            return True
        return False

    def use_allowance(self):
        """Hosted: take one AI check from the firm's allowance. Returns an error response if there is none."""
        if not config.HOSTED:
            return None
        from . import plans, store
        firm_id = self.auth["firm_id"]
        limit = plans.PLANS[store.usage(firm_id)["plan"]].get("concurrent", 1)
        if pipeline.running(firm_id) >= limit:
            return {"error": f"Your firm already has {limit} AI job{'s' if limit != 1 else ''} running. Start this one when one finishes.",
                    "busy": True}
        try:
            store.consume(firm_id)
        except store.StoreError as e:
            return {"error": str(e), "upgrade": True}
        return None

    def refund_allowance(self):
        if config.HOSTED:
            from . import store
            store.refund(self.auth["firm_id"])

    def me(self):
        from . import mailer
        if not config.HOSTED:
            return {"mode": "local", "delivery": mailer.describe()}
        from . import billing, store
        firm = store.firm(self.auth["firm_id"])
        user = store.user(self.auth["user_id"])
        return {"mode": "hosted", "csrf": self.auth["csrf"], "role": self.auth["role"],
                "user": {"email": user["email"], "name": user["name"]},
                "firm": {"name": firm["name"], "partner_email": firm["settings"].get("partner_email", ""),
                         "custom_lists": bool(store.firm_lists(firm["id"]))},
                "usage": store.usage(firm["id"]), "delivery": mailer.describe(),
                "billing": {"available": billing.configured(), "customer": bool(firm["stripe_customer"]),
                            "status": firm["status"]}}

    # ------------------------------------------------------------ GET

    def do_GET(self):
        route = self.path.partition("?")[0]
        if self.gate_request("GET", route):
            return
        if route == "/health":
            if config.HOSTED:
                return self.send_json(200, {"status": "ok", "agents": agents.status()})
            from . import mailer
            return self.send_json(200, {"status": "ok", "agents": agents.status(), "delivery": mailer.describe()})
        if route == "/api/me":
            return self.send_json(200, self.me())
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
        if config.HOSTED and route == "/api/firm":
            from . import store
            firm_id = self.auth["firm_id"]
            return self.send_json(200, {"members": store.members(firm_id), "lists": store.firm_lists(firm_id)})
        if config.HOSTED and route == "/billing/return":
            return self.billing_return()
        m = re.fullmatch(r"/api/(checks|revisions|drafts)/([a-z]{3}_[0-9a-f]{12})", route)
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
        if not config.HOSTED and route in ("/account", "/login", "/signup", "/join"):
            return self.redirect("/")
        name = PAGES.get(route) or route.lstrip("/")
        target = (UI_DIR / name).resolve()
        if UI_DIR.resolve() not in target.parents or not target.is_file():
            return self.send_json(404, {"error": "Unknown endpoint"})
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(raw)

    # ------------------------------------------------------------ PUT / DELETE

    def do_PUT(self):
        route = self.path.partition("?")[0]
        if self.gate_request("PUT", route):
            return
        try:
            data = self.body()
        except (ValueError, UnicodeDecodeError, OSError):
            return self.send_json(400, {"error": "Send a JSON object"})
        if route == "/api/models":
            if self.owner_only():
                return
            try:
                feature, model = data.get("feature"), data.get("model") or None
                if feature not in agents.HARNESS_NAMES:
                    raise ValueError
                if model is not None and model not in {o["id"] for o in agents.model_options(feature)["options"]}:
                    return self.send_json(400, {"error": f"{model} is not offered for this agent"})
            except ValueError:
                return self.send_json(400, {"error": "Send {feature, model}; model null resets to the default"})
            except HRError as e:
                return self.send_json(502, {"error": str(e)})
            settings.set_model(feature, model)
            return self.send_json(200, agents.model_options(feature))
        if config.HOSTED and route == "/api/firm/settings":
            if self.owner_only():
                return
            from . import store
            email = str(data.get("partner_email") or "").strip()
            if email:
                try:
                    email = store.normal_email(email)
                except store.StoreError as e:
                    return self.send_json(400, {"error": str(e)})
            current = settings.load()
            current["partner_email"] = email
            settings.save(current)
            return self.send_json(200, {"partner_email": email})
        if config.HOSTED and route == "/api/firm/lists":
            if self.owner_only():
                return
            from . import store
            try:
                lists = validate_lists(data)
            except ValueError as e:
                return self.send_json(400, {"error": str(e)})
            store.save_firm_lists(self.auth["firm_id"], lists)
            return self.send_json(200, {"trusted": len(lists["trusted_citations"]["cases"]),
                                        "fabricated": len(lists["known_fabricated"]["cases"])})
        return self.send_json(404, {"error": "Unknown endpoint"})

    def do_DELETE(self):
        route = self.path.partition("?")[0]
        if self.gate_request("DELETE", route):
            return
        if config.HOSTED and route == "/api/firm/lists":
            if self.owner_only():
                return
            from . import store
            store.save_firm_lists(self.auth["firm_id"], None)
            return self.send_json(200, {"lists": "sample"})
        m = re.fullmatch(r"/api/firm/members/(user_[0-9a-f]{16})", route)
        if config.HOSTED and m:
            if self.owner_only():
                return
            from . import store
            try:
                store.remove_member(self.auth["firm_id"], m.group(1))
            except store.StoreError as e:
                return self.send_json(400, {"error": str(e)})
            return self.send_json(200, {"removed": True})
        return self.send_json(404, {"error": "Unknown endpoint"})

    # ------------------------------------------------------------ POST

    def do_POST(self):
        route = self.path.partition("?")[0]
        if self.gate_request("POST", route):
            return
        if config.HOSTED and route == "/stripe/webhook":
            return self.stripe_webhook()
        try:
            data = self.body()
        except (ValueError, UnicodeDecodeError, OSError):
            return self.send_json(400, {"error": "Send a JSON object"})
        if config.HOSTED and route.startswith("/auth/"):
            return self.auth_route(route, data)
        if config.HOSTED and route.startswith(("/api/billing/", "/api/firm/")):
            return self.account_route(route, data)
        if route == "/decide" and config.HOSTED:
            return self.send_json(404, {"error": "Unknown endpoint"})
        if route in ("/api/checks", "/decide"):
            try:
                text, context = self.memo_from(data)
            except ValueError:
                return self.send_json(400, {"error": f"text must be a non-empty string of at most {MAX_TEXT} characters"})
            refused = self.use_allowance()
            if refused:
                return self.send_json(429 if refused.get("busy") else 402, refused)
            if route == "/api/checks":
                return self.send_json(202, {"id": pipeline.start_check(text, context).id})
            job = pipeline.start_check(text, context, background=False)
            return self.send_json(200, job.result)
        if route == "/api/drafts":
            question, matter = data.get("question"), data.get("matter") or ""
            if not isinstance(question, str) or not question.strip() or len(question) > agents.MAX_QUESTION or not isinstance(matter, str):
                return self.send_json(400, {"error": f"question must be a non-empty string of at most {agents.MAX_QUESTION} characters"})
            refused = self.use_allowance()
            if refused:
                return self.send_json(429 if refused.get("busy") else 402, refused)
            return self.send_json(202, {"id": pipeline.start_draft(question.strip(), matter.strip()[:1000]).id})
        m = re.fullmatch(r"/api/checks/(chk_[0-9a-f]{12})/(review|revision)", route)
        if m and m.group(2) == "review":
            outcome, note = data.get("outcome"), str(data.get("note") or "").strip()
            if outcome not in ("proceed", "reject") or not note:
                return self.send_json(400, {"error": "Review needs outcome (proceed or reject) and a reason"})
            reviewer = str(data.get("reviewer") or "").strip()[:120]
            if config.HOSTED:  # the signed-in attorney is the reviewer of record
                from . import store
                reviewer = store.user(self.auth["user_id"])["email"]
            code, body = pipeline.review(m.group(1), outcome, note[:2000], reviewer)
            return self.send_json(code, body)
        if m and m.group(2) == "revision":
            refused = self.use_allowance()
            if refused:
                return self.send_json(429 if refused.get("busy") else 402, refused)
            job, err = pipeline.start_revision(m.group(1))
            if not job:
                self.refund_allowance()
                return self.send_json(409, {"error": err})
            return self.send_json(202, {"id": job.id})
        return self.send_json(404, {"error": "Unknown endpoint"})

    # ------------------------------------------------------------ hosted: sign-in

    def auth_route(self, route, data):
        from . import store
        if route in ("/auth/signup", "/auth/accept-invite") and data.get("accept_terms") is not True:
            return self.send_json(400, {"error": "Agree to the Terms of Service and Privacy Policy to continue."})
        try:
            if route == "/auth/signup":
                if not store.signup_allowed(self.client_ip()):
                    return self.send_json(429, {"error": "Too many new accounts from this network today. Try again tomorrow, or sign in."})
                _, user_id = store.create_firm_with_owner(data.get("firm"), data.get("name"), data.get("email"), data.get("password"))
                store.record_signup(self.client_ip())
            elif route == "/auth/accept-invite":
                _, user_id = store.accept_invite(data.get("token"), data.get("name"), data.get("email"), data.get("password"))
            elif route == "/auth/login":
                user_id = store.authenticate(data.get("email"), data.get("password"), self.client_ip())
                if not user_id:
                    return self.send_json(401, {"error": "That email and password don't match an account."})
            elif route == "/auth/logout":
                store.end_session(self.session_token())
                return self.send_json(200, {"signed_out": True}, self.cookie("", 0))
            else:
                return self.send_json(404, {"error": "Unknown endpoint"})
            token = store.create_session(user_id)
        except store.StoreError as e:
            return self.send_json(400, {"error": str(e)})
        return self.send_json(200, {"signed_in": True}, self.cookie(token, store.SESSION_DAYS * 86400))

    # ------------------------------------------------------------ hosted: the firm's account and billing

    def account_route(self, route, data):
        from . import billing, store
        firm_id = self.auth["firm_id"]
        if route == "/api/firm/invites":
            if self.owner_only():
                return
            try:
                token = store.create_invite(firm_id)
            except store.StoreError as e:
                return self.send_json(400, {"error": str(e)})
            return self.send_json(200, {"link": f"{config.PUBLIC_URL}/join#{token}", "expires_days": 7})
        if route == "/api/billing/checkout":
            if self.owner_only():
                return
            try:
                url = billing.checkout_url(firm_id, data.get("plan"), store.user(self.auth["user_id"])["email"])
            except billing.BillingError as e:
                return self.send_json(400, {"error": str(e)})
            return self.send_json(200, {"url": url})
        if route == "/api/billing/portal":
            if self.owner_only():
                return
            try:
                url = billing.portal_url(firm_id)
            except billing.BillingError as e:
                return self.send_json(400, {"error": str(e)})
            return self.send_json(200, {"url": url})
        return self.send_json(404, {"error": "Unknown endpoint"})

    def billing_return(self):
        """Stripe Checkout sends the owner back here; confirm the subscription with Stripe, then show the account."""
        from urllib.parse import parse_qs
        from . import billing
        session_id = (parse_qs(urlparse(self.path).query).get("session_id") or [""])[0]
        try:
            billing.sync_checkout(session_id, self.auth["firm_id"])
            return self.redirect("/account?subscribed=1")
        except billing.BillingError:
            return self.redirect("/account?billing_error=1")

    def stripe_webhook(self):
        from . import billing
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 1_000_000:
            return self.send_json(400, {"error": "bad payload"})
        payload = self.rfile.read(length)
        try:
            event = billing.verify_webhook(payload, self.headers.get("Stripe-Signature") or "")
        except billing.BillingError:
            return self.send_json(400, {"error": "bad signature"})
        try:
            billing.handle_event(event)
        except billing.BillingError:
            return self.send_json(500, {"error": "could not apply event"})  # Stripe retries
        return self.send_json(200, {"received": True})


def serve(host=None, port=None):
    host, port = host or os.getenv("HOST", "127.0.0.1"), int(port or os.getenv("PORT", "9000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Para on http://{host}:{port}/  (agents: {agents.status()}, mode: {config.MODE})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
