"""Stripe billing for the hosted version, over Stripe's REST API with the standard library.

Para never sees a card: customers pay on Stripe Checkout and manage their plan in the Stripe customer
portal. The server needs STRIPE_SECRET_KEY and, for notifications, STRIPE_WEBHOOK_SECRET in its
environment. Prices are found by lookup key (plans.PLANS[...]["lookup_key"]); tools/stripe_setup.py
creates them.
"""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, plans, store

API = os.getenv("STRIPE_API_BASE", "https://api.stripe.com").rstrip("/")
_price_cache = {}


class BillingError(Exception):
    """A billing problem whose message is safe to show the firm's owner."""


def secret_key():
    return os.getenv("STRIPE_SECRET_KEY", "")


def configured():
    return bool(secret_key())


def _encode(params, prefix=""):
    """Stripe's form encoding: nested dicts and lists become key[sub][0]=value."""
    pairs = []
    for k, v in params.items():
        key = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            pairs += _encode(v, key)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                pairs += _encode(item, f"{key}[{i}]") if isinstance(item, dict) else [(f"{key}[{i}]", item)]
        elif v is not None:
            pairs.append((key, "true" if v is True else "false" if v is False else str(v)))
    return pairs


def call(method, path, params=None):
    if not configured():
        raise BillingError("Payments are not set up on this server yet.")
    data = urllib.parse.urlencode(_encode(params or {})).encode() if method == "POST" else None
    url = API + path + ("?" + urllib.parse.urlencode(_encode(params or {})) if method == "GET" and params else "")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + secret_key(), "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read()).get("error", {}).get("message", "")
        except ValueError:
            message = ""
        raise BillingError(f"Stripe refused the request ({e.code}). {message}".strip()) from None
    except OSError:
        raise BillingError("Stripe could not be reached. Try again in a moment.") from None


def price_id(plan):
    spec = plans.PLANS.get(plan)
    if plan not in plans.PAID or not spec:
        raise BillingError("Choose the Solo or Firm plan.")
    if plan not in _price_cache:
        found = call("GET", "/v1/prices", {"lookup_keys": [spec["lookup_key"]], "active": True, "limit": 1}).get("data") or []
        if not found:
            raise BillingError("This plan's price is not set up in Stripe yet (run tools/stripe_setup.py).")
        _price_cache[plan] = found[0]["id"]
    return _price_cache[plan]


def _customer(firm_id, email):
    firm = store.firm(firm_id)
    if firm["stripe_customer"]:
        return firm["stripe_customer"]
    cust = call("POST", "/v1/customers", {"email": email, "name": firm["name"], "metadata": {"firm_id": firm_id}})
    store.update_firm(firm_id, stripe_customer=cust["id"])
    return cust["id"]


def checkout_url(firm_id, plan, email):
    """A Stripe Checkout page for a monthly subscription to `plan`."""
    firm = store.firm(firm_id)
    if firm["plan"] in plans.PAID and firm["status"] in plans.ACTIVE_STATUSES:
        raise BillingError("You already have a subscription. Use Manage billing to change or cancel it.")
    session = call("POST", "/v1/checkout/sessions", {
        "mode": "subscription", "customer": _customer(firm_id, email), "client_reference_id": firm_id,
        "line_items": [{"price": price_id(plan), "quantity": 1}],
        "subscription_data": {"metadata": {"firm_id": firm_id, "plan": plan}},
        "allow_promotion_codes": True,
        "success_url": f"{config.PUBLIC_URL}/billing/return?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{config.PUBLIC_URL}/account",
    })
    return session["url"]


def portal_url(firm_id):
    firm = store.firm(firm_id)
    if not firm["stripe_customer"]:
        raise BillingError("There is no billing account yet. Choose a plan first.")
    return call("POST", "/v1/billing_portal/sessions",
                {"customer": firm["stripe_customer"], "return_url": f"{config.PUBLIC_URL}/account"})["url"]


def _plan_of(subscription):
    meta_plan = (subscription.get("metadata") or {}).get("plan")
    for item in (subscription.get("items") or {}).get("data") or []:
        key = (item.get("price") or {}).get("lookup_key")
        for name in plans.PAID:
            if plans.PLANS[name]["lookup_key"] == key:
                return name
    return meta_plan if meta_plan in plans.PAID else None


def _period(subscription):
    """Current period start and end; newer Stripe API versions keep them on the subscription item."""
    start, end = subscription.get("current_period_start"), subscription.get("current_period_end")
    items = (subscription.get("items") or {}).get("data") or []
    if (start is None or end is None) and items:
        start, end = items[0].get("current_period_start"), items[0].get("current_period_end")
    return start, end


def apply_subscription(subscription, firm_id=None):
    """Copy a Stripe subscription's plan, status and billing period onto its firm."""
    firm_id = firm_id or (subscription.get("metadata") or {}).get("firm_id")
    firm = store.firm(firm_id) if firm_id else store.firm_by_customer(subscription.get("customer"))
    if not firm:
        raise BillingError("subscription for an unknown firm")
    if firm["stripe_customer"] and subscription.get("customer") != firm["stripe_customer"]:
        raise BillingError("subscription customer does not match the firm")
    plan = _plan_of(subscription)
    if not plan:
        raise BillingError("subscription for an unknown price")
    start, end = _period(subscription)
    status = subscription.get("status")
    store.update_firm(firm["id"], plan=plan, status=status, stripe_subscription=subscription.get("id"),
                      stripe_customer=subscription.get("customer"), period_start=start, period_end=end)
    return firm["id"]


def sync_checkout(session_id, firm_id):
    """After Checkout: confirm with Stripe (not the browser) that this firm's session produced a subscription."""
    if not session_id or not session_id.startswith("cs_"):
        raise BillingError("missing checkout session")
    session = call("GET", f"/v1/checkout/sessions/{urllib.parse.quote(session_id)}", {"expand": ["subscription"]})
    if session.get("client_reference_id") != firm_id:
        raise BillingError("checkout session belongs to another firm")
    sub = session.get("subscription")
    if isinstance(sub, str):
        sub = call("GET", f"/v1/subscriptions/{urllib.parse.quote(sub)}")
    if not sub:
        raise BillingError("checkout has not finished")
    return apply_subscription(sub, firm_id)


def verify_webhook(payload, signature_header, secret=None, tolerance=300, now=None):
    """Check a Stripe-Signature header (HMAC-SHA256 over "timestamp.payload") and return the event."""
    secret = secret or os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise BillingError("webhook secret not set")
    parts = {}
    for item in signature_header.split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        timestamp = int(parts["t"][0])
    except (KeyError, ValueError, IndexError):
        raise BillingError("no timestamp") from None
    if abs((now or time.time()) - timestamp) > tolerance:
        raise BillingError("stale signature")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
        raise BillingError("signature mismatch")
    try:
        return json.loads(payload)
    except ValueError:
        raise BillingError("bad payload") from None


def handle_event(event):
    """Apply one verified Stripe event. Each event id is applied once, and recorded only after it succeeds."""
    event_id = event.get("id", "")
    if store.seen_event(event_id):
        return "duplicate"
    result = _apply_event(event)
    store.record_event(event_id)
    return result


def _apply_event(event):
    kind, obj = event.get("type", ""), (event.get("data") or {}).get("object") or {}
    if kind == "checkout.session.completed" and obj.get("mode") == "subscription":
        sub = obj.get("subscription")
        if isinstance(sub, str):
            sub = call("GET", f"/v1/subscriptions/{urllib.parse.quote(sub)}")
        if sub and obj.get("client_reference_id"):
            apply_subscription(sub, obj["client_reference_id"])
        return kind
    if kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        apply_subscription(obj)
        return kind
    if kind == "invoice.payment_failed" and obj.get("subscription"):
        apply_subscription(call("GET", f"/v1/subscriptions/{urllib.parse.quote(obj['subscription'])}"))
        return kind
    return "ignored"
