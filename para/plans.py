"""What each plan includes. Prices are charged by Stripe (prices looked up by lookup_key)."""
import time

TRIAL_DAYS = 14
PLANS = {
    "trial": {"label": "Free trial", "price_usd": 0, "checks": 20, "seats": 3},
    "solo": {"label": "Solo", "price_usd": 20, "checks": 150, "seats": 1, "lookup_key": "para_solo_monthly"},
    "firm": {"label": "Firm", "price_usd": 150, "checks": 1000, "seats": 10, "lookup_key": "para_firm_monthly"},
}
PAID = ("solo", "firm")
# Stripe subscription statuses that keep a paid plan usable (past_due: Stripe is still retrying the card).
ACTIVE_STATUSES = ("active", "trialing", "past_due")


def entitlement(firm, now=None):
    """{plan, active, reason, checks, seats, period_key, period_end} for a firm row (dict)."""
    now = now or time.time()
    plan = firm["plan"] if firm["plan"] in PLANS else "trial"
    spec = PLANS[plan]
    if plan == "trial":
        ends = firm.get("trial_ends") or 0
        active = now < ends
        return {"plan": plan, "label": spec["label"], "active": active, "checks": spec["checks"], "seats": spec["seats"],
                "period_key": "trial", "period_end": ends,
                "reason": None if active else "Your free trial has ended. Choose a plan to keep checking memos."}
    start = int(firm.get("period_start") or 0)
    if firm.get("status") == "manual":  # paid outside Stripe (e.g. e-Transfer), granted until period_end
        ends = firm.get("period_end") or 0
        active = now < ends
        return {"plan": plan, "label": spec["label"], "active": active, "checks": spec["checks"], "seats": spec["seats"],
                "period_key": f"p{start}", "period_end": ends,
                "reason": None if active else "Your access period has ended. Choose a plan, or contact us to renew."}
    active = firm.get("status") in ACTIVE_STATUSES
    reason = None
    if not active:
        reason = "Your subscription is not active. Update your billing details or choose a plan."
    elif firm.get("status") == "past_due":
        reason = "Your last payment failed; Stripe will retry. Update your card under Manage billing."
    return {"plan": plan, "label": spec["label"], "active": active, "checks": spec["checks"], "seats": spec["seats"],
            "period_key": f"p{start}", "period_end": firm.get("period_end"), "reason": reason}
