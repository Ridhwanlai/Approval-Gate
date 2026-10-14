# Launching hosted Para

Hosted Para is the paid, no-install version: firms sign up on the web, start a 14-day trial
(20 AI checks, no card), and subscribe through Stripe. It runs the same code as the free download
with `python3 run.py serve-hosted`, on [Render](https://render.com), using your HarnessRouter
account for every firm's AI checks.

| Plan | Price | AI checks a month | Users |
|---|---|---|---|
| Free trial | $0 for 14 days | 20 | 3 |
| Solo | $20 / month | 150 | 1 |
| Firm | $150 / month | 1,000 | 10 |

Each memo check, AI draft and AI fix uses one AI check. Change prices in `para/plans.py` and run
`tools/stripe_setup.py` again.

## What you need

- A **Stripe** account (stripe.com). Start in test mode; no business details are needed for that.
- A **Render** account (render.com), connected to this GitHub repository. The Starter instance plus a
  1 GB disk costs about $7–8 a month.
- Your **HarnessRouter** API key. The Free plan runs 2 AI tasks at a time across all firms; move to
  a paid plan before you have several firms checking memos at once, then raise `HR_MAX_CONCURRENT`.
- Optional: an outgoing mail account for emailing approved memos to partners (SMTP host, username,
  password, from-address). Without it, approved memos are logged and shown as not emailed.

Keep every key in the Render dashboard or your own terminal. Never put one in the repository,
an issue, chat or email.

## 1. Stripe (test mode first)

1. In the Stripe dashboard, switch on **Test mode** and copy the **secret key** (`sk_test_...`).
2. In your own terminal, from this repository:

   ```bash
   STRIPE_SECRET_KEY=sk_test_... python3 tools/stripe_setup.py
   ```

   It creates the Para product with the Solo and Firm monthly prices.
3. Turn on the customer portal: Settings → Billing → Customer portal → Activate. This is where
   firms update cards and cancel.

## 2. Render

1. Click **[Deploy to Render](https://render.com/deploy?repo=https://github.com/Ridhwanlai/Approval-Gate)**
   (or Render dashboard → New → Blueprint → this repository). Render reads `render.yaml`.
2. Fill in the values it asks for:
   - `PUBLIC_URL`: the service's address, e.g. `https://para-memo-gate.onrender.com`. Render shows it
     after the first deploy; set it, then redeploy.
   - `HR_API_KEY`: your HarnessRouter key.
   - `STRIPE_SECRET_KEY`: the `sk_test_...` key.
   - `STRIPE_WEBHOOK_SECRET`: from step 3 below (leave it blank for now and come back).
   - The `PARA_SMTP_*` values and `SMTP_PASSWORD`, if you are sending email.
3. Register the webhook so Stripe tells Para about renewals, failed payments and cancellations:

   ```bash
   STRIPE_SECRET_KEY=sk_test_... python3 tools/stripe_setup.py --webhook https://YOUR-SERVICE.onrender.com/stripe/webhook
   ```

   Put the printed `whsec_...` value in `STRIPE_WEBHOOK_SECRET` on Render, then redeploy.

## 3. Try it as a customer

1. Open your `PUBLIC_URL`, choose **Start free trial**, and create a firm.
2. Check a memo; the account bar should show 19 AI checks left.
3. Go to **Account → Subscribe to Solo**. On Stripe's test checkout, pay with the test card
   `4242 4242 4242 4242`, any future expiry date and any CVC.
4. You return to Account with the Solo plan active and 150 checks. **Manage billing** opens the
   Stripe portal, where cancelling turns the plan off at the end of the period.

## 4. Go live

1. Complete Stripe's account activation (business details, bank account).
2. Run `tools/stripe_setup.py` again with the **live** key, with `--webhook`, and put the live
   `sk_live_...` and its `whsec_...` into Render.
3. Add Terms of Service and a Privacy Policy before charging customers. Para processes your
   customers' memo text through HarnessRouter (and Ezio when connected), so say so. Have a lawyer
   review them: these are legal documents for a legal product.
4. Point your own domain at Render if you have one, and update `PUBLIC_URL`.

## What runs where

- **Render** runs Para. Firms, users, sessions, usage and audit logs are in SQLite on the Render
  disk (`/var/data/para.db`). Passwords are salted PBKDF2 hashes; session tokens are stored hashed.
- **Stripe** holds card details and subscriptions. Para stores only the Stripe customer and
  subscription IDs, the plan and its status.
- **HarnessRouter** runs the four AI agents under your key. Firms never see the key or any
  HarnessRouter IDs.
- **Ezio** is the bundled simulation in this release; set `EZIO_URL` and `EZIO_KEY` when Ezio
  provides a server API key.

Back up `/var/data/para.db` (Render disk snapshots, or download it periodically): it is every
firm's account and history.
