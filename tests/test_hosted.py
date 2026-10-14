"""Hosted-version tests: accounts, plans, usage and (later) billing. No network.

  python3 -m unittest discover -s tests -v
"""
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

    def test_audit_is_kept_per_firm(self):
        a, _ = self.new_firm()
        b, _ = self.new_firm(email="other@firm.test", name="Other LLP")
        store.add_audit(a, {"decision": "BLOCK"})
        self.assertEqual(store.read_audit(a), [{"decision": "BLOCK"}])
        self.assertEqual(store.read_audit(b), [])


if __name__ == "__main__":
    unittest.main()
