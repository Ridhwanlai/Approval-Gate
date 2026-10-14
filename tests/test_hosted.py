"""Hosted-version tests: accounts, plans, usage and (later) billing. No network.

  python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from para import config, plans, store  # noqa: E402


class StoreBase(unittest.TestCase):
    def setUp(self):
        self._db = config.DB_FILE
        config.DB_FILE = Path(tempfile.mkdtemp(prefix="para-db-")) / "para.db"
        store.reset_for_tests()

    def tearDown(self):
        store.reset_for_tests()
        config.DB_FILE = self._db

    def new_firm(self, email="owner@firm.test", name="Doe & Roe LLP"):
        return store.create_firm_with_owner(name, "Dana Owner", email, "correct horse battery")


class AccountTests(StoreBase):
    def test_signup_creates_trial_firm_and_owner(self):
        firm_id, user_id = self.new_firm()
        f = store.firm(firm_id)
        self.assertEqual((f["plan"], f["status"]), ("trial", "trialing"))
        self.assertEqual(store.membership(user_id), {"firm_id": firm_id, "role": "owner"})
        raw = store._one("select pw_hash from users where id = ?", (user_id,))["pw_hash"]
        self.assertNotIn("correct horse", raw)
        self.assertTrue(raw.startswith("pbkdf2_sha256$"))

    def test_duplicate_email_and_weak_password_refused(self):
        self.new_firm()
        with self.assertRaises(store.StoreError):
            self.new_firm(email="OWNER@firm.test")
        with self.assertRaises(store.StoreError):
            store.create_firm_with_owner("X", "Y", "y@firm.test", "short")

    def test_sign_in_and_session(self):
        _, user_id = self.new_firm()
        self.assertIsNone(store.authenticate("owner@firm.test", "wrong password!!", "1.2.3.4"))
        self.assertEqual(store.authenticate("Owner@Firm.test", "correct horse battery", "1.2.3.4"), user_id)
        token = store.create_session(user_id)
        s = store.session(token)
        self.assertEqual((s["user_id"], s["role"]), (user_id, "owner"))
        self.assertIsNone(store.session("not-a-token"))
        stored = store._one("select token_hash from sessions")["token_hash"]
        self.assertNotEqual(stored, token)
        store.end_session(token)
        self.assertIsNone(store.session(token))

    def test_repeated_failures_lock_out(self):
        self.new_firm()
        for _ in range(10):
            store.authenticate("owner@firm.test", "wrong password!!", "9.9.9.9")
        with self.assertRaises(store.StoreError):
            store.authenticate("owner@firm.test", "correct horse battery", "9.9.9.9")

    def test_invites_respect_seats_and_owner_stays(self):
        firm_id, owner = self.new_firm()
        store.update_firm(firm_id, plan="solo", status="active")
        with self.assertRaises(store.StoreError):
            store.create_invite(firm_id)  # Solo: 1 seat, already used by the owner
        store.update_firm(firm_id, plan="firm")
        token = store.create_invite(firm_id)
        joined_firm, member = store.accept_invite(token, "Pat", "pat@firm.test", "another long password")
        self.assertEqual(joined_firm, firm_id)
        with self.assertRaises(store.StoreError):
            store.accept_invite(token, "Again", "again@firm.test", "another long password")
        with self.assertRaises(store.StoreError):
            store.remove_member(firm_id, owner)
        store.remove_member(firm_id, member)
        self.assertEqual([m["role"] for m in store.members(firm_id)], ["owner"])


class UsageTests(StoreBase):
    def test_trial_allows_twenty_checks_then_stops(self):
        firm_id, _ = self.new_firm()
        for _ in range(plans.PLANS["trial"]["checks"]):
            store.consume(firm_id)
        with self.assertRaises(store.StoreError) as e:
            store.consume(firm_id)
        self.assertIn("Choose a plan", str(e.exception))

    def test_expired_trial_blocks(self):
        firm_id, _ = self.new_firm()
        later = time.time() + (plans.TRIAL_DAYS + 1) * 86400
        with self.assertRaises(store.StoreError) as e:
            store.consume(firm_id, now=later)
        self.assertIn("trial has ended", str(e.exception))

    def test_paid_plan_counts_per_period_and_per_firm(self):
        a, _ = self.new_firm()
        b, _ = self.new_firm(email="other@firm.test", name="Other LLP")
        store.update_firm(a, plan="solo", status="active", period_start=1000)
        store.consume(a)
        store.consume(a)
        self.assertEqual(store.usage(a)["used"], 2)
        self.assertEqual(store.usage(b)["used"], 0)
        store.update_firm(a, period_start=2000)  # a new billing period starts a fresh count
        self.assertEqual(store.usage(a)["used"], 0)
        store.update_firm(a, status="canceled")
        with self.assertRaises(store.StoreError):
            store.consume(a)

    def test_manual_grant_for_e_transfer_lapses_on_time(self):
        firm_id, _ = self.new_firm()
        now = time.time()
        store.grant("OWNER@firm.test", "solo", 31, now=now)
        u = store.usage(firm_id, now=now + 86400)
        self.assertEqual((u["plan"], u["active"], u["checks"]), ("solo", True, 150))
        store.consume(firm_id, now=now + 86400)
        self.assertFalse(store.usage(firm_id, now=now + 32 * 86400)["active"])
        with self.assertRaises(store.StoreError):
            store.consume(firm_id, now=now + 32 * 86400)
        store.revoke("owner@firm.test")
        self.assertFalse(store.usage(firm_id)["active"])
        with self.assertRaises(store.StoreError):
            store.grant("nobody@firm.test", "solo", 31)

    def test_audit_is_kept_per_firm(self):
        a, _ = self.new_firm()
        b, _ = self.new_firm(email="other@firm.test", name="Other LLP")
        store.add_audit(a, {"decision": "BLOCK"})
        self.assertEqual(store.read_audit(a), [{"decision": "BLOCK"}])
        self.assertEqual(store.read_audit(b), [])


class TenancyTests(StoreBase):
    """In hosted mode every piece of firm data is scoped to the firm in context, and nothing runs without one."""

    def setUp(self):
        super().setUp()
        self._hosted = config.HOSTED
        config.HOSTED = True
        self.a, _ = self.new_firm()
        self.b, _ = self.new_firm(email="other@firm.test", name="Other LLP")

    def tearDown(self):
        from para import tenant
        tenant.set_firm(None)
        config.HOSTED = self._hosted
        super().tearDown()

    def as_firm(self, firm_id):
        from para import tenant
        tenant.set_firm(firm_id)

    def test_settings_audit_and_lists_are_per_firm(self):
        from para import gate, records, settings
        self.as_firm(self.a)
        settings.set_model("memo_cite_audit", "gpt-5.4-mini")
        records.audit({"decision": "BLOCK"})
        store.save_firm_lists(self.a, {"trusted_citations": {"cases": []}, "known_fabricated": {"cases": []}})
        self.as_firm(self.b)
        self.assertIsNone(settings.model_for("memo_cite_audit"))
        self.assertEqual(records.read_audit(), [])
        self.assertTrue(gate.load_source_of_truth()["trusted_citations"]["cases"])  # B still has the sample lists
        self.as_firm(self.a)
        self.assertEqual(settings.model_for("memo_cite_audit"), "gpt-5.4-mini")
        self.assertEqual(gate.load_source_of_truth()["trusted_citations"]["cases"], [])

    def test_jobs_are_visible_only_to_their_firm(self):
        from para import pipeline
        self.as_firm(self.a)
        job = pipeline.Job("check", ["lists"], text="memo", context={})
        self.assertIs(pipeline.get(job.id), job)
        self.as_firm(self.b)
        self.assertIsNone(pipeline.get(job.id))

    def test_no_firm_fails_closed(self):
        from para import records, settings
        self.as_firm(None)
        with self.assertRaises(RuntimeError):
            settings.load()
        with self.assertRaises(RuntimeError):
            records.read_audit()

    def test_firm_carries_into_job_threads(self):
        from para import tenant
        self.as_firm(self.a)
        seen = []
        t = tenant.thread(lambda: seen.append(tenant.current()))
        t.start()
        t.join()
        self.assertEqual(seen, [self.a])


class HostedHttpTests(StoreBase):
    """The hosted server over HTTP: sign-in, CSRF, tenancy, owner-only changes, allowance and Stripe billing."""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        sys.path.insert(0, str(ROOT / "tests"))
        from fake_stripe import FakeStripe
        from para import billing, server
        cls.stripe = FakeStripe()
        cls._api, billing.API = billing.API, cls.stripe.url
        cls.web = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.web.server_address[1]}"
        threading.Thread(target=cls.web.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        from para import billing
        cls.web.shutdown()
        cls.web.server_close()
        cls.stripe.close()
        billing.API = cls._api

    def setUp(self):
        super().setUp()
        from para import billing
        billing._price_cache.clear()
        self._saved = (config.HOSTED, config.PUBLIC_URL)
        config.HOSTED, config.PUBLIC_URL = True, self.base
        os.environ.update(STRIPE_SECRET_KEY="sk_test_fake", STRIPE_WEBHOOK_SECRET="whsec_test")

    def tearDown(self):
        from para import tenant
        tenant.set_firm(None)
        config.HOSTED, config.PUBLIC_URL = self._saved
        for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"):
            os.environ.pop(k, None)
        super().tearDown()

    # -- a tiny browser: one cookie jar per "person"

    def client(self):
        import http.cookiejar
        import urllib.request
        jar = http.cookiejar.CookieJar()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect())
        state = {"csrf": None}
        base = self.base

        def req(path, body=None, method=None, origin=True, csrf=True, raw=None, headers=None):
            import urllib.error
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
            h = {"Content-Type": "application/json", **(headers or {})}
            if origin:
                h["Origin"] = base
            if csrf and state["csrf"]:
                h["X-CSRF-Token"] = state["csrf"]
            r = urllib.request.Request(base + path, data=data, method=method or ("POST" if data is not None else "GET"), headers=h)
            try:
                with opener.open(r, timeout=20) as f:
                    code, text, loc = f.status, f.read(), f.headers.get("Location")
            except urllib.error.HTTPError as e:
                code, text, loc = e.code, e.read(), e.headers.get("Location")
            try:
                out = json.loads(text) if text else {}
            except ValueError:
                out = {"_html": True}
            if path == "/api/me" and code == 200:
                state["csrf"] = out.get("csrf")
            return code, out, loc

        req.state = state
        return req

    def signup(self, email="owner@firm.test", firm="Doe & Roe LLP"):
        c = self.client()
        code, body, _ = c("/auth/signup", {"firm": firm, "name": "Dana", "email": email, "password": "correct horse battery"})
        self.assertEqual(code, 200, body)
        c("/api/me")
        return c

    def firm_id_of(self, email):
        return store.membership(store._one("select id from users where email = ?", (email,))["id"])["firm_id"]

    def test_signed_out_visitors_are_sent_to_sign_in(self):
        c = self.client()
        self.assertEqual(c("/")[0:3:2], (303, "/login"))
        self.assertEqual(c("/api/me")[0], 401)
        self.assertEqual(c("/login")[0], 200)
        self.assertEqual(c("/health")[0], 200)

    def test_signup_sign_in_and_out(self):
        c = self.signup()
        code, me, _ = c("/api/me")
        self.assertEqual((code, me["mode"], me["role"], me["usage"]["plan"], me["usage"]["checks"]), (200, "hosted", "owner", "trial", 20))
        self.assertEqual(c("/auth/logout", {})[0], 200)
        self.assertEqual(c("/api/me")[0], 401)
        d = self.client()
        self.assertEqual(d("/auth/login", {"email": "owner@firm.test", "password": "nope nope nope"})[0], 401)
        self.assertEqual(d("/auth/login", {"email": "owner@firm.test", "password": "correct horse battery"})[0], 200)
        self.assertEqual(d("/api/me")[0], 200)

    def test_writes_need_csrf_and_same_origin(self):
        c = self.signup()
        body = {"partner_email": "partner@firm.test"}
        self.assertEqual(c("/api/firm/settings", body, method="PUT", csrf=False)[0], 403)
        self.assertEqual(c("/api/firm/settings", body, method="PUT", origin=False)[0], 403)
        self.assertEqual(c("/api/firm/settings", body, method="PUT", origin=False, headers={"Origin": "https://evil.test"})[0], 403)
        self.assertEqual(c("/api/firm/settings", body, method="PUT")[0], 200)
        self.assertEqual(c("/api/me")[1]["firm"]["partner_email"], "partner@firm.test")

    def test_one_firm_cannot_see_another_firms_work(self):
        from para import pipeline, tenant
        a = self.signup()
        b = self.signup(email="b@other.test", firm="Other LLP")
        tenant.set_firm(self.firm_id_of("owner@firm.test"))
        job = pipeline.Job("check", ["lists"], text="secret memo", context={})
        job.state = "done"
        tenant.set_firm(None)
        self.assertEqual(a(f"/api/checks/{job.id}")[0], 200)
        self.assertEqual(b(f"/api/checks/{job.id}")[0], 404)
        self.assertEqual(b(f"/api/checks/{job.id}/review", {"outcome": "proceed", "note": "x"})[0], 404)
        store.add_audit(self.firm_id_of("owner@firm.test"), {"decision": "BLOCK", "input": "secret memo"})
        self.assertNotIn("secret memo", json.dumps(b("/audit")[1]))

    def test_used_up_allowance_asks_to_upgrade(self):
        c = self.signup()
        firm_id = self.firm_id_of("owner@firm.test")
        for _ in range(20):
            store.consume(firm_id)
        code, body, _ = c("/api/drafts", {"question": "Must the state appoint counsel?"})
        self.assertEqual((code, body.get("upgrade")), (402, True))

    def test_signups_per_network_are_limited(self):
        for i in range(5):
            self.signup(email=f"user{i}@firm{i}.test", firm=f"Firm {i}")
        c = self.client()
        code, body, _ = c("/auth/signup", {"firm": "Sixth", "name": "S", "email": "six@firm6.test", "password": "correct horse battery"})
        self.assertEqual(code, 429)

    def test_one_firm_cannot_hold_every_ai_slot(self):
        from para import pipeline, tenant
        c = self.signup()
        tenant.set_firm(self.firm_id_of("owner@firm.test"))
        busy = pipeline.Job("check", ["lists"], text="x", context={})  # still running
        tenant.set_firm(None)
        code, body, _ = c("/api/drafts", {"question": "Must the state appoint counsel?"})
        self.assertEqual((code, body.get("busy")), (429, True))
        self.assertEqual(store.usage(self.firm_id_of("owner@firm.test"))["used"], 0)  # nothing was charged
        busy.state = "done"

    def test_team_invite_join_and_owner_only_changes(self):
        owner = self.signup()
        code, inv, _ = owner("/api/firm/invites", {})
        self.assertEqual(code, 200)
        token = inv["link"].split("#", 1)[1]
        member = self.client()
        self.assertEqual(member("/auth/accept-invite", {"token": token, "name": "Pat", "email": "pat@firm.test",
                                                       "password": "another long password"})[0], 200)
        member("/api/me")
        self.assertEqual(member("/api/me")[1]["role"], "member")
        self.assertEqual(member("/api/firm/settings", {"partner_email": "x@y.test"}, method="PUT")[0], 403)
        self.assertEqual(member("/api/billing/checkout", {"plan": "solo"})[0], 403)
        self.assertEqual(len(owner("/api/firm")[1]["members"]), 2)

    def test_firm_lists_upload_is_validated(self):
        c = self.signup()
        bad = {"trusted_citations": {"cases": [{"case": "X v. Y"}]}, "known_fabricated": {"cases": []}}
        self.assertEqual(c("/api/firm/lists", bad, method="PUT")[0], 400)
        good = {"trusted_citations": {"cases": [{"case": "X v. Y", "citation": "1 U.S. 1", "year": 1800, "holding": "A rule."}]},
                "known_fabricated": {"cases": [{"case": "Fake v. Case"}]}}
        self.assertEqual(c("/api/firm/lists", good, method="PUT")[1], {"trusted": 1, "fabricated": 1})
        self.assertTrue(c("/api/me")[1]["firm"]["custom_lists"])

    def test_checkout_return_activates_the_paid_plan(self):
        c = self.signup()
        firm_id = self.firm_id_of("owner@firm.test")
        code, body, _ = c("/api/billing/checkout", {"plan": "firm"})
        self.assertEqual(code, 200, body)
        session_id = body["url"].rsplit("/", 1)[1]
        self.assertEqual(self.stripe.sessions[session_id]["price"], "price_firm")
        self.assertEqual(self.stripe.sessions[session_id]["client_reference_id"], firm_id)
        self.assertTrue(all(call[3] == "Bearer sk_test_fake" for call in self.stripe.calls))
        self.stripe.complete(session_id, plan="firm")
        code, _, loc = c(f"/billing/return?session_id={session_id}")
        self.assertEqual((code, loc), (303, "/account?subscribed=1"))
        usage = c("/api/me")[1]["usage"]
        self.assertEqual((usage["plan"], usage["checks"], usage["active"]), ("firm", 1000, True))
        self.assertEqual(c("/api/billing/checkout", {"plan": "solo"})[0], 400)  # already subscribed
        self.assertTrue(c("/api/billing/portal", {})[1]["url"].startswith("https://billing.stripe.test/"))

    def test_return_for_another_firms_session_is_refused(self):
        a = self.signup()
        b = self.signup(email="b@other.test", firm="Other LLP")
        session_id = a("/api/billing/checkout", {"plan": "solo"})[1]["url"].rsplit("/", 1)[1]
        self.stripe.complete(session_id)
        self.assertEqual(b(f"/billing/return?session_id={session_id}")[2], "/account?billing_error=1")
        self.assertEqual(b("/api/me")[1]["usage"]["plan"], "trial")

    def webhook(self, event, secret="whsec_test", ts=None):
        import hashlib
        import hmac
        payload = json.dumps(event).encode()
        ts = ts or int(time.time())
        sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
        c = self.client()
        return c("/stripe/webhook", raw=payload, origin=False, csrf=False, headers={"Stripe-Signature": f"t={ts},v1={sig}"})[0]

    def test_webhook_updates_plan_and_rejects_forgeries(self):
        c = self.signup()
        firm_id = self.firm_id_of("owner@firm.test")
        session_id = c("/api/billing/checkout", {"plan": "solo"})[1]["url"].rsplit("/", 1)[1]
        sub = self.stripe.complete(session_id, plan="solo")
        event = {"id": "evt_1", "type": "customer.subscription.updated", "data": {"object": sub}}
        self.assertEqual(self.webhook(event, secret="whsec_wrong"), 400)
        self.assertEqual(self.webhook(event, ts=int(time.time()) - 3600), 400)
        self.assertEqual(store.firm(firm_id)["plan"], "trial")
        self.assertEqual(self.webhook(event), 200)
        self.assertEqual((store.firm(firm_id)["plan"], store.firm(firm_id)["status"]), ("solo", "active"))
        cancelled = {**sub, "status": "canceled"}
        self.assertEqual(self.webhook({"id": "evt_2", "type": "customer.subscription.deleted", "data": {"object": cancelled}}), 200)
        self.assertFalse(store.usage(firm_id)["active"])
        self.assertEqual(self.webhook({"id": "evt_1", "type": "customer.subscription.updated", "data": {"object": sub}}), 200)
        self.assertEqual(store.firm(firm_id)["status"], "canceled")  # a replayed event is not applied twice


if __name__ == "__main__":
    unittest.main()
