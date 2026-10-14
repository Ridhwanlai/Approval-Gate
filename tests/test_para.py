"""Para tests. No network and no HarnessRouter key: a fake HarnessRouter and the real mock Ezio.

  python3 -m unittest discover -s tests -v
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="para-test-")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


MOCK_PORT = _free_port()
os.environ.update({"EZIO_URL": f"http://127.0.0.1:{MOCK_PORT}", "PARA_DATA_DIR": TMP, "HR_API_KEY": "sk-hr-test-not-real"})
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_hr import FakeHR  # noqa: E402
from para import agents, config, gate, pipeline, server  # noqa: E402
from para.hr_client import Client, HRError  # noqa: E402

AUDIT, STYLE, DRAFT, WRITE = "chrn_audit", "chrn_style", "chrn_draft", "chrn_write"
CLEAN_STYLE = json.dumps({"reads_as": "analysis", "issues": [], "notes": "Neutral."})
PRACTICE = {i["id"]: i for i in json.loads((ROOT / "firm" / "practice_set.json").read_text())["items"]}


def audit_reply(*authorities):
    return json.dumps({"authorities": list(authorities), "notes": "test"})


def auth(quote, status, entry=None, problems=()):
    return {"quote": quote, "status": status, "firm_entry": entry, "problems": list(problems), "stated_holding": None}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {**os.environ, "HOST": "127.0.0.1", "PORT": str(MOCK_PORT), "LATENCY": "0"}
        cls.mock = subprocess.Popen([sys.executable, str(ROOT / "mock_ezio.py")], env=env, stdout=subprocess.DEVNULL)
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{MOCK_PORT}/v1/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        cls.hr = FakeHR()
        agents.set_client(Client(base_url=cls.hr.url, api_key="sk-hr-test-not-real", uhp_version="2026-09-28"))
        reg = Path(TMP) / "registry.json"
        reg.write_text(json.dumps({"features": {agents.CITE_AUDIT: AUDIT, agents.STYLE_REVIEW: STYLE, agents.REVISION: DRAFT, agents.DRAFT: WRITE}}))
        config.REGISTRY = reg

    @classmethod
    def tearDownClass(cls):
        cls.hr.close()
        cls.mock.terminate()

    def setUp(self):
        self.hr.replies.clear()
        self.hr.modes.clear()
        self.hr.calls.clear()
        self.hr.replies[STYLE] = CLEAN_STYLE
        self.hr.replies[AUDIT] = audit_reply()

    def check(self, text):
        job = pipeline.start_check(text, {}, background=False)
        return job, job.result


# ------------------------------------------------------------------ the client

class ClientTests(Base):
    def test_streams_and_reads_ids(self):
        self.hr.replies[AUDIT] = "hello"
        events = []
        run = agents.client().run(AUDIT, "x", on_event=lambda k, d: events.append(k))
        self.assertEqual(run.text, "hello")
        self.assertTrue(run.session_id.startswith("hsess") and run.response_id.startswith("resp_"))
        self.assertIn("started", events)
        headers = self.hr.calls[0]["headers"]
        self.assertEqual(headers["UHP-Version"], "2026-09-28")
        self.assertTrue(headers["Idempotency-Key"])

    def test_recovers_dropped_stream_without_resubmitting(self):
        self.hr.replies[AUDIT] = "recovered"
        self.hr.modes[AUDIT] = "drop"
        run = agents.client().run(AUDIT, "x")
        self.assertEqual(run.text, "recovered")
        self.assertEqual(len(self.hr.calls), 1)

    def test_retries_server_errors_with_same_idempotency_key(self):
        self.hr.modes[AUDIT] = "http500"
        with self.assertRaises(HRError) as e:
            agents.client().run(AUDIT, "x", retries=1)
        self.assertEqual(e.exception.code, "unavailable")
        self.assertEqual(len(self.hr.calls), 2)
        self.assertEqual(self.hr.calls[0]["headers"]["Idempotency-Key"], self.hr.calls[1]["headers"]["Idempotency-Key"])

    def test_unauthorized_is_not_retried(self):
        self.hr.modes[AUDIT] = "http401"
        with self.assertRaises(HRError) as e:
            agents.client().run(AUDIT, "x")
        self.assertEqual((e.exception.code, len(self.hr.calls)), ("unauthorized", 1))

    def test_billing_error_is_classified_and_not_retried(self):
        self.hr.modes[AUDIT] = "http402"
        with self.assertRaises(HRError) as e:
            agents.client().run(AUDIT, "x")
        self.assertEqual((e.exception.code, len(self.hr.calls)), ("billing", 1))

    def test_chosen_model_is_sent_per_run(self):
        from para import settings
        settings.set_model(agents.CITE_AUDIT, "gpt-5.4-mini")
        try:
            agents.run_feature(agents.CITE_AUDIT, "x")
            self.assertEqual(self.hr.calls[-1]["model"], "gpt-5.4-mini")
            settings.set_model(agents.CITE_AUDIT, None)
            agents.run_feature(agents.CITE_AUDIT, "x")
            self.assertIsNone(self.hr.calls[-1]["model"])
        finally:
            settings.set_model(agents.CITE_AUDIT, None)

    def test_failed_run_is_classified(self):
        self.hr.modes[AUDIT] = "fail"
        with self.assertRaises(HRError) as e:
            agents.client().run(AUDIT, "x")
        self.assertEqual(e.exception.code, "failed")


# ------------------------------------------------------------------ validating agent output

class ValidationTests(unittest.TestCase):
    sot = gate.load_source_of_truth()
    memo = "In Varghese v. China Southern Airlines, the court held X. Under Gideon, 372 U.S. 335, counsel is required."

    def test_accepts_verbatim_linked_findings(self):
        ok, bad = agents.parse_audit(audit_reply(auth("Varghese v. China Southern Airlines", "fabricated", "Varghese v. China Southern Airlines")), self.memo, self.sot)
        self.assertEqual((len(ok), len(bad)), (1, 0))

    def test_rejects_invented_quote_and_unknown_entry(self):
        ok, bad = agents.parse_audit(audit_reply(auth("Smith v. Jones", "unverified"),
                                                 auth("Gideon", "verified", "Gideon v. Florida")), self.memo, self.sot)
        self.assertEqual((len(ok), len(bad)), (0, 2))

    def test_rejects_status_that_contradicts_the_list(self):
        ok, bad = agents.parse_audit(audit_reply(auth("Gideon", "fabricated", "Gideon v. Wainwright")), self.memo, self.sot)
        self.assertEqual((len(ok), len(bad)), (0, 1))

    def test_unlinked_match_becomes_unverified(self):
        ok, _ = agents.parse_audit(audit_reply(auth("the court held X", "fabricated", "Varghese v. China Southern Airlines")), self.memo, self.sot)
        self.assertEqual(ok[0]["status"], "unverified")

    def test_role_mismatch_and_garbage_raise(self):
        for reply in ('{"error": "role_mismatch"}', "I could not do that."):
            with self.assertRaises(ValueError):
                agents.parse_audit(reply, self.memo, self.sot)

    def test_code_fence_is_tolerated(self):
        ok, _ = agents.parse_audit("```json\n" + audit_reply(auth("Gideon", "verified", "Gideon v. Wainwright")) + "\n```", self.memo, self.sot)
        self.assertEqual(len(ok), 1)

    def test_style_quotes_must_be_verbatim(self):
        res, bad = agents.parse_review(json.dumps({"reads_as": "advocacy", "issues": [
            {"quote": "counsel is required", "kind": "overconfidence", "why": "w"},
            {"quote": "slam dunk", "kind": "overconfidence", "why": "w"}]}), self.memo)
        self.assertEqual((len(res["issues"]), len(bad)), (1, 1))


# ------------------------------------------------------------------ the product path, end to end

class GateTests(Base):
    def test_good_memo_approves_and_sends(self):
        text = PRACTICE["pl-02"]["text"]
        self.hr.replies[AUDIT] = audit_reply(auth("Gideon v. Wainwright, 372 U.S. 335 (1963)", "verified", "Gideon v. Wainwright"))
        job, r = self.check(text)
        self.assertEqual(r["decision"], "APPROVE", r["reason"])
        self.assertTrue(r["action_executed"])
        actions = (Path(TMP) / "actions.log").read_text()
        self.assertIn(job.id, actions)
        self.assertEqual({c["harness_id"] for c in self.hr.calls}, {AUDIT, STYLE})

    def test_fabricated_case_blocks_even_if_agent_says_verified(self):
        text = PRACTICE["pl-07"]["text"]
        self.hr.replies[AUDIT] = audit_reply(auth("Varghese v. China Southern Airlines", "unverified"))
        _, r = self.check(text)
        self.assertEqual(r["decision"], "BLOCK")
        self.assertFalse(r["action_executed"])

    def test_agent_cannot_lower_a_list_hold(self):
        text = PRACTICE["pl-11"]["text"]  # a case on neither list
        self.hr.replies[AUDIT] = audit_reply(auth("Harlan v. Pinecrest Mutual", "unverified"))
        _, r = self.check(text)
        self.assertEqual(r["decision"], "HOLD_FOR_REVIEW")

    def test_agent_misstatement_holds_a_memo_the_lists_passed(self):
        text = PRACTICE["pl-02"]["text"]
        self.hr.replies[AUDIT] = audit_reply(auth("Gideon v. Wainwright", "misstated", "Gideon v. Wainwright", ["holding overstated"]))
        _, r = self.check(text)
        self.assertEqual(r["decision"], "HOLD_FOR_REVIEW")
        self.assertIn("misstating Gideon v. Wainwright", r["reason"])

    def test_agent_finds_fabricated_case_the_regex_missed(self):
        text = "As the China Southern Airlines decision explained, tolling applies during bankruptcy."
        lists_alone = gate.check_lists(text, gate.load_source_of_truth())
        self.assertFalse([lvl for lvl, _, _ in lists_alone if lvl == "BLOCK"], "the regex should miss this one")
        self.hr.replies[AUDIT] = audit_reply(auth("China Southern Airlines decision", "fabricated", "Varghese v. China Southern Airlines"))
        _, r = self.check(text)
        self.assertEqual(r["decision"], "BLOCK")

    def test_style_reviewer_holds_advocacy(self):
        text = PRACTICE["pl-02"]["text"] + " We will win this one easily."
        self.hr.replies[STYLE] = json.dumps({"reads_as": "advocacy", "issues": [{"quote": "We will win this one easily.", "kind": "overconfidence", "why": "predicts outcome"}]})
        _, r = self.check(text)
        self.assertEqual(r["decision"], "HOLD_FOR_REVIEW")
        self.assertIn("advocacy", r["reason"])

    def test_agent_outage_fails_closed_to_review(self):
        self.hr.modes[AUDIT] = "http500"
        _, r = self.check(PRACTICE["pl-02"]["text"])
        self.assertEqual(r["decision"], "HOLD_FOR_REVIEW")
        self.assertFalse(r["action_executed"])

    def test_off_task_reply_fails_closed(self):
        self.hr.replies[STYLE] = '{"error": "role_mismatch"}'
        _, r = self.check(PRACTICE["pl-02"]["text"])
        self.assertEqual(r["decision"], "HOLD_FOR_REVIEW")

    def test_revision_placeholders_are_held_not_sent(self):
        text = "[AUTHORITY NEEDED: The Montreal Convention tolls the statute of limitations during bankruptcy.]"
        _, r = self.check(text)
        self.assertEqual(r["decision"], "HOLD_FOR_REVIEW")
        self.assertIn("placeholder", r["reason"])
        self.assertFalse(r["action_executed"])

    def test_review_only_for_holds_and_only_once(self):
        _, blocked = self.check(PRACTICE["pl-07"]["text"])
        self.assertEqual(pipeline.review(blocked["check_id"], "proceed", "looks fine", "A")[0], 409)
        self.hr.modes[AUDIT] = "http500"
        _, held = self.check(PRACTICE["pl-02"]["text"])
        code, body = pipeline.review(held["check_id"], "proceed", "checked by hand", "A. Attorney")
        self.assertEqual((code, body["action_executed"]), (200, True))
        self.assertEqual(pipeline.review(held["check_id"], "proceed", "again", "A")[0], 409)

    def test_revision_reads_files_and_never_sends(self):
        _, r = self.check(PRACTICE["pl-07"]["text"])
        self.hr.replies[DRAFT] = json.dumps({"revised_file": "revised_memo.md", "changes_file": "changes.md", "revised_memo": "fallback", "changes": []})
        self.hr.replies[f"{DRAFT}:files"] = {"revised_memo.md": "[AUTHORITY NEEDED: tolling]", "changes.md": "- Removed Varghese (fabricated)"}
        before = (Path(TMP) / "actions.log").read_text() if (Path(TMP) / "actions.log").exists() else ""
        job, err = pipeline.start_revision(r["check_id"])
        self.assertIsNone(err)
        for _ in range(100):
            if job.state == "done":
                break
            time.sleep(0.05)
        self.assertEqual(job.result["revised_memo"], "[AUTHORITY NEEDED: tolling]")
        self.assertEqual(job.result["changes"], ["Removed Varghese (fabricated)"])
        after = (Path(TMP) / "actions.log").read_text() if (Path(TMP) / "actions.log").exists() else ""
        self.assertEqual(before, after)

    def test_revision_refused_for_approved_memo(self):
        self.hr.replies[AUDIT] = audit_reply(auth("Gideon v. Wainwright", "verified", "Gideon v. Wainwright"))
        _, r = self.check(PRACTICE["pl-02"]["text"])
        self.assertIsNotNone(pipeline.start_revision(r["check_id"])[1])


class HttpTests(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from http.server import ThreadingHTTPServer
        import threading
        cls.web = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.web.server_address[1]}"
        threading.Thread(target=cls.web.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.web.shutdown()
        super().tearDownClass()

    def req(self, path, body=None):
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=30) as f:
                return f.status, json.loads(f.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_async_check_and_safe_payloads(self):
        code, body = self.req("/api/checks", {"text": PRACTICE["pl-07"]["text"], "context": {}, "harness_id": "chrn_evil"})
        self.assertEqual(code, 202)
        for _ in range(100):
            code, job = self.req(f"/api/checks/{body['id']}")
            if job["state"] == "done":
                break
            time.sleep(0.05)
        self.assertEqual(job["result"]["decision"], "BLOCK")
        self.assertNotIn("chrn_evil", {c["harness_id"] for c in self.hr.calls})
        dump = json.dumps(job)
        for secret in ("hsess", "resp_", "chrn_", "sk-hr-"):
            self.assertNotIn(secret, dump)
        _, audit = self.req("/audit")
        self.assertNotIn("hsess", json.dumps(audit))
        code, trace = self.req(f"/api/trace/{body['id']}/{agents.CITE_AUDIT}")
        self.assertEqual(code, 200)
        self.assertEqual(trace["turns"][0]["status"], "completed")

    def wait(self, path):
        for _ in range(200):
            _, job = self.req(path)
            if job.get("state") == "done":
                return job
            time.sleep(0.05)
        self.fail(f"{path} never finished")

    def test_review_and_revision_routes_over_http(self):
        self.hr.modes[AUDIT] = "http500"  # agent outage -> HOLD, so the memo is reviewable
        _, held = self.req("/api/checks", {"text": PRACTICE["pl-02"]["text"]})
        self.assertTrue(held["id"].startswith("chk_"))
        self.assertEqual(self.wait(f"/api/checks/{held['id']}")["result"]["decision"], "HOLD_FOR_REVIEW")
        code, body = self.req(f"/api/checks/{held['id']}/review", {"outcome": "reject", "note": "needs a cite-check", "reviewer": "A"})
        self.assertEqual((code, body["action_executed"]), (200, False))

        self.hr.modes.clear()
        self.hr.replies[DRAFT] = json.dumps({"revised_memo": "[AUTHORITY NEEDED: tolling]", "changes": ["Removed Varghese"]})
        _, blocked = self.req("/api/checks", {"text": PRACTICE["pl-07"]["text"]})
        self.wait(f"/api/checks/{blocked['id']}")
        code, rev = self.req(f"/api/checks/{blocked['id']}/revision", {})
        self.assertEqual(code, 202)
        self.assertTrue(rev["id"].startswith("rev_"))
        result = self.wait(f"/api/revisions/{rev['id']}")["result"]
        self.assertEqual((result["ok"], result["revised_memo"]), (True, "[AUTHORITY NEEDED: tolling]"))

    def test_models_route_lists_offers_and_saves_choice(self):
        code, body = self.req("/api/models")
        self.assertEqual(code, 200)
        audit = next(a for a in body["agents"] if a["feature"] == agents.CITE_AUDIT)
        self.assertEqual(audit["default"], "claude-sonnet-5.5")
        self.assertEqual([o["id"] for o in audit["options"]], ["claude-sonnet-5.5", "gpt-5.4-mini"])

        def put(body):
            r = urllib.request.Request(self.base + "/api/models", data=json.dumps(body).encode(), method="PUT",
                                       headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(r, timeout=10) as f:
                    return f.status, json.loads(f.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        try:
            self.assertEqual(put({"feature": agents.CITE_AUDIT, "model": "retired-model"})[0], 400)
            self.assertEqual(put({"feature": "build_my_app", "model": "gpt-5.4-mini"})[0], 400)
            code, saved = put({"feature": agents.CITE_AUDIT, "model": "gpt-5.4-mini"})
            self.assertEqual((code, saved["choice"]), (200, "gpt-5.4-mini"))
        finally:
            self.assertEqual(put({"feature": agents.CITE_AUDIT, "model": None})[1]["choice"], None)

    def test_draft_route_writes_a_draft_and_sends_nothing(self):
        self.hr.replies[WRITE] = json.dumps({"memo": "Gideon v. Wainwright, 372 U.S. 335 (1963), requires counsel.",
                                             "authorities_used": ["Gideon v. Wainwright"], "open_points": []})
        actions = Path(TMP) / "actions.log"
        before = actions.read_text() if actions.exists() else ""
        code, body = self.req("/api/drafts", {"question": "Must the state appoint counsel for an indigent felony defendant?"})
        self.assertEqual(code, 202)
        self.assertTrue(body["id"].startswith("drf_"))
        result = self.wait(f"/api/drafts/{body['id']}")["result"]
        self.assertEqual((result["ok"], result["authorities_used"]), (True, ["Gideon v. Wainwright"]))
        self.assertIn("<question>", self.hr.calls[-1]["input"])
        self.assertEqual(before, actions.read_text() if actions.exists() else "")
        self.assertEqual(self.req("/api/drafts", {"question": ""})[0], 400)

    def test_health_reports_agent_status(self):
        self.assertEqual(self.req("/health")[1]["agents"], "configured")

    def test_rejects_bad_input(self):
        self.assertEqual(self.req("/api/checks", {"text": ""})[0], 400)
        self.assertEqual(self.req("/api/checks/chk_000000000000/review", {"outcome": "proceed", "note": "x"})[0], 404)


class CliTests(unittest.TestCase):
    def test_save_key_is_owner_only_and_readable_back(self):
        from para import cli
        home = Path(tempfile.mkdtemp(prefix="para-home-"))
        old = config.KEY_FILE
        config.KEY_FILE = home / "harnessrouter.key"
        try:
            cli.save_key("sk-hr-abcdefgh12345")
            self.assertEqual(config.KEY_FILE.stat().st_mode & 0o777, 0o600)
            os.environ.pop("HR_API_KEY", None)
            self.assertEqual(config.hr_api_key(), "sk-hr-abcdefgh12345")
        finally:
            config.KEY_FILE = old
            os.environ["HR_API_KEY"] = "sk-hr-test-not-real"

    def test_key_format(self):
        from para import cli
        self.assertTrue(cli.KEY_RE.fullmatch("sk-hr-abcdefgh12345"))
        self.assertFalse(cli.KEY_RE.fullmatch("sk-ant-abcdefgh12345"))


if __name__ == "__main__":
    unittest.main()
