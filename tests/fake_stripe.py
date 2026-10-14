"""A fake Stripe API for tests: prices by lookup key, customers, Checkout, subscriptions and the portal."""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeStripe:
    def __init__(self):
        self.calls, self.sessions, self.subscriptions, self.customers = [], {}, {}, {}
        self.prices = {"para_solo_monthly": "price_solo", "para_firm_monthly": "price_firm"}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def params(self, raw):
                return dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))

            def do_GET(self):
                path, _, query = self.path.partition("?")
                q = self.params(query)
                fake.calls.append(("GET", path, q, self.headers.get("Authorization")))
                if path == "/v1/prices":
                    key = q.get("lookup_keys[0]")
                    pid = fake.prices.get(key)
                    return self.reply(200, {"data": [{"id": pid, "lookup_key": key}] if pid else []})
                if path.startswith("/v1/checkout/sessions/"):
                    s = fake.sessions.get(path.rsplit("/", 1)[1])
                    if not s:
                        return self.reply(404, {"error": {"message": "No such session"}})
                    return self.reply(200, {**s, "subscription": fake.subscriptions.get(s.get("subscription")) or s.get("subscription")})
                if path.startswith("/v1/subscriptions/"):
                    sub = fake.subscriptions.get(path.rsplit("/", 1)[1])
                    return self.reply(200, sub) if sub else self.reply(404, {"error": {"message": "No such subscription"}})
                self.reply(404, {"error": {"message": "unknown"}})

            def do_POST(self):
                body = self.params(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
                fake.calls.append(("POST", self.path, body, self.headers.get("Authorization")))
                if self.path == "/v1/customers":
                    cid = f"cus_{len(fake.customers) + 1}"
                    fake.customers[cid] = body
                    return self.reply(200, {"id": cid})
                if self.path == "/v1/checkout/sessions":
                    sid = f"cs_test_{len(fake.sessions) + 1}"
                    fake.sessions[sid] = {"id": sid, "url": f"https://checkout.stripe.test/{sid}", "mode": "subscription",
                                          "client_reference_id": body.get("client_reference_id"), "customer": body.get("customer"),
                                          "price": body.get("line_items[0][price]"), "subscription": None}
                    return self.reply(200, fake.sessions[sid])
                if self.path == "/v1/billing_portal/sessions":
                    return self.reply(200, {"url": f"https://billing.stripe.test/{body.get('customer')}"})
                self.reply(404, {"error": {"message": "unknown"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def complete(self, session_id, plan="solo", status="active", start=1_800_000_000):
        """Simulate the customer paying: the session gains an active subscription."""
        s = self.sessions[session_id]
        sub_id = f"sub_{len(self.subscriptions) + 1}"
        lookup = {"solo": "para_solo_monthly", "firm": "para_firm_monthly"}[plan]
        self.subscriptions[sub_id] = {
            "id": sub_id, "status": status, "customer": s["customer"],
            "metadata": {"firm_id": s["client_reference_id"], "plan": plan},
            "items": {"data": [{"price": {"id": self.prices[lookup], "lookup_key": lookup},
                                "current_period_start": start, "current_period_end": start + 30 * 86400}]}}
        s["subscription"] = sub_id
        return self.subscriptions[sub_id]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
