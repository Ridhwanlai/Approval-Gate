#!/usr/bin/env python3
"""Create Para's product and monthly prices in your Stripe account (safe to run again).

  STRIPE_SECRET_KEY=sk_test_... python3 tools/stripe_setup.py
  STRIPE_SECRET_KEY=sk_test_... python3 tools/stripe_setup.py --webhook https://YOUR-APP.onrender.com/stripe/webhook

Use a test-mode key (sk_test_...) first; run it again with the live key when you launch. Prices are
created with lookup keys (para_solo_monthly, para_firm_monthly), which is how the app finds them.
With --webhook it also registers the notification endpoint and prints its signing secret once: put
that in STRIPE_WEBHOOK_SECRET on the server, and keep it out of chat, email and the repository.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from para import billing, plans  # noqa: E402

EVENTS = ["checkout.session.completed", "customer.subscription.created", "customer.subscription.updated",
          "customer.subscription.deleted", "invoice.payment_failed"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--webhook", help="public URL of /stripe/webhook to register")
    args = ap.parse_args()
    if not billing.configured():
        raise SystemExit("Set STRIPE_SECRET_KEY in this terminal's environment first (never paste it into a file in the repo).")
    mode = "TEST" if billing.secret_key().startswith(("sk_test_", "rk_test_")) else "LIVE"
    print(f"Stripe {mode} mode")
    products = billing.call("GET", "/v1/products", {"limit": 100, "active": True}).get("data", [])
    product = next((p for p in products if (p.get("metadata") or {}).get("app") == "para"), None)
    if not product:
        product = billing.call("POST", "/v1/products", {"name": "Para", "description": "Approval gate for AI-drafted legal research memos",
                                                        "metadata": {"app": "para"}})
        print(f"Created product {product['id']}")
    else:
        print(f"Using product {product['id']}")
    for name in plans.PAID:
        spec = plans.PLANS[name]
        found = billing.call("GET", "/v1/prices", {"lookup_keys": [spec["lookup_key"]], "active": True, "limit": 1}).get("data", [])
        amount = spec["price_usd"] * 100
        if found and found[0].get("unit_amount") == amount:
            print(f"{spec['label']}: ${spec['price_usd']}/month already set up ({found[0]['id']})")
            continue
        price = billing.call("POST", "/v1/prices", {
            "product": product["id"], "currency": "usd", "unit_amount": amount, "recurring": {"interval": "month"},
            "lookup_key": spec["lookup_key"], "transfer_lookup_key": True, "nickname": f"Para {spec['label']} monthly",
            "metadata": {"plan": name, "checks": spec["checks"], "seats": spec["seats"]}})
        print(f"{spec['label']}: created ${spec['price_usd']}/month ({price['id']})")
    if args.webhook:
        hooks = billing.call("GET", "/v1/webhook_endpoints", {"limit": 100}).get("data", [])
        if any(h.get("url") == args.webhook for h in hooks):
            print(f"Webhook already registered for {args.webhook} (its signing secret is shown in the Stripe dashboard).")
        else:
            hook = billing.call("POST", "/v1/webhook_endpoints", {"url": args.webhook, "enabled_events": EVENTS,
                                                                  "description": "Para subscriptions"})
            print(f"Registered webhook {hook['id']}. Signing secret (set as STRIPE_WEBHOOK_SECRET on the server, then clear this terminal):")
            print(hook["secret"])
    print("Also turn on the customer portal once: Stripe dashboard > Settings > Billing > Customer portal.")


if __name__ == "__main__":
    main()
