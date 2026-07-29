"""Browser-native Telephone. Entirely offline; no model is ever called."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

# harness must be imported first: it puts scripts/ on sys.path
from harness import BusTestCase, ml, mock_response
import browser_bridge as bb
import messagesctl
import reviewer_daemon as rd
import telephone as tp

IDENTITY = "digitalnomadjoe"


class BrowserTestCase(BusTestCase):
    def setUp(self):
        super().setUp()
        self.cfg["browser"] = {"allowed_identities": [IDENTITY], "poll_seconds": 1}
        self.cfg["reviewer"]["minimum_confidence"] = 0.85
        # the request author must also be the commit author
        self.repo.git("config", "user.name", IDENTITY)
        self.repo.git("config", "user.email", f"{IDENTITY}@example.com")

    def start_browser_run(self, max_cycles=3, criterion=None):
        report_id = self.publish_report(lane="control")
        rid, _ = self.submit("telephone_start", report_id=report_id,
                             max_cycles=max_cycles, criterion=criterion)
        self.bridge().run_once()
        runs = [m for m in self.msgs().values() if m.kind == "telephone_run"]
        self.assertEqual(len(runs), 1, self.result_for(rid).body)
        return report_id, runs[0].id, rid

    def bridge(self, cfg=None):
        return bb.BrowserBridge(cfg or self.cfg)

    def request(self, kind, **over):
        rid = ("BREQ-" + ml.utc_now().strftime("%Y%m%dT%H%M%SZ") + "-"
               + ml.secrets.token_hex(4))
        req = {
            "request_id": rid, "kind": kind, "project": "brittle",
            "submitted_by": IDENTITY, "created_at": ml.iso(ml.utc_now()),
            "lane": "control", "unit": "CERT-BROWSER",
            "idempotency_key": ml.secrets.token_hex(8),
            "rationale": "synthetic browser-mode test",
        }
        req.update(over)
        return rid, req

    def submit(self, kind, *, author=IDENTITY, **over):
        """Write a request file and commit it as `author` (like a GitHub push)."""
        rid, req = self.request(kind, **over)
        rel = f"{ml.DIR_BROWSER_REQUESTS}/{rid}.json"
        path = self.repo_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(req, indent=2) + "\n", encoding="utf-8")
        self.repo.git("add", "--", rel)
        self.repo.git("-c", f"user.name={author}",
                      "-c", f"user.email={author}@example.com",
                      "commit", "-m", f"browser request {rid}")
        return rid, req

    def result_for(self, rid):
        for m in self.msgs().values():
            if m.get("receipt_type") == "browser_result" and m.get("request_id") == rid:
                return m
        return None

    def kinds(self):
        out = {}
        for m in self.msgs().values():
            out.setdefault(m.kind, []).append(m)
        return out


class TestRequestValidation(unittest.TestCase):
    def base(self, **over):
        req = {
            "request_id": "BREQ-20260729T120000Z-aabbccdd", "kind": "review_only",
            "project": "brittle", "submitted_by": IDENTITY,
            "created_at": "2026-07-29T12:00:00.000000Z", "lane": "control",
            "unit": "U", "idempotency_key": "k1", "rationale": "why",
            "report_id": "BRITTLE-20260729T120000Z-11223344",
            "payload": {"summary": "s", "next_action": "n", "confidence": 0.9},
        }
        req.update(over)
        return req

    def test_valid_request_passes(self):
        bb.validate_request(self.base())

    def test_missing_field_refused(self):
        r = self.base()
        del r["idempotency_key"]
        with self.assertRaisesRegex(bb.BridgeRefusal, "missing required field"):
            bb.validate_request(r)

    def test_unknown_field_refused(self):
        with self.assertRaisesRegex(bb.BridgeRefusal, "unknown field"):
            bb.validate_request(self.base(exec_command="rm -rf /"))

    def test_bad_kind_refused(self):
        with self.assertRaisesRegex(bb.BridgeRefusal, "bad kind"):
            bb.validate_request(self.base(kind="run_shell"))

    def test_malformed_request_id_refused(self):
        with self.assertRaisesRegex(bb.BridgeRefusal, "malformed request_id"):
            bb.validate_request(self.base(request_id="req-1"))

    def test_bad_lane_refused(self):
        with self.assertRaisesRegex(bb.BridgeRefusal, "lane must be one of"):
            bb.validate_request(self.base(lane="reviewer"))

    def test_review_and_ticket_requires_ticket_fields(self):
        r = self.base(kind="review_and_ticket")
        with self.assertRaisesRegex(bb.BridgeRefusal, "ticket_title"):
            bb.validate_request(r)

    def test_confidence_range_enforced(self):
        r = self.base()
        r["payload"]["confidence"] = 3.0
        with self.assertRaisesRegex(bb.BridgeRefusal, "confidence out of range"):
            bb.validate_request(r)

    def test_bad_criterion_status_refused(self):
        with self.assertRaisesRegex(bb.BridgeRefusal, "bad criterion_status"):
            bb.validate_request(self.base(criterion_status="probably"))

    def test_telephone_start_needs_a_positive_bound(self):
        r = self.base(kind="telephone_start", max_cycles=0)
        with self.assertRaisesRegex(bb.BridgeRefusal, "max_cycles"):
            bb.validate_request(r)

    def test_all_seven_kinds_are_supported(self):
        self.assertEqual(set(bb.REQUEST_KINDS), {
            "status_request", "telephone_start", "telephone_stop",
            "review_and_ticket", "review_only", "criterion_judgement",
            "resolve_escalation"})


class TestNoModelInTheBridge(unittest.TestCase):
    def test_bridge_imports_no_model_client(self):
        src = Path(bb.__file__).read_text(encoding="utf-8")
        for banned in ("import openai", "from openai", "import anthropic",
                       "chat/completions", "api.openai.com"):
            self.assertNotIn(banned, src,
                             f"the bridge must never call a model ({banned})")

    def test_bridge_does_not_import_the_reviewer_transport(self):
        src = Path(bb.__file__).read_text(encoding="utf-8")
        self.assertNotIn("call_model", src)


class TestIdentity(BrowserTestCase):
    def test_allowlisted_identity_accepted(self):
        rid, _ = self.submit("status_request", lane="control")
        self.bridge().run_once()
        r = self.result_for(rid)
        self.assertIsNotNone(r)
        self.assertEqual(r.get("status"), "completed")

    def test_unknown_declared_identity_refused(self):
        rid, _ = self.submit("status_request", submitted_by="mallory")
        self.bridge().run_once()
        r = self.result_for(rid)
        self.assertEqual(r.get("status"), "blocked")
        self.assertIn("not allowlisted", str(r.get("reason")))

    def test_spoofed_identity_refused_by_commit_author(self):
        """Declaring an allowed name while committing as someone else fails."""
        rid, _ = self.submit("status_request", author="mallory")
        self.bridge().run_once()
        r = self.result_for(rid)
        self.assertEqual(r.get("status"), "blocked")
        self.assertIn("commit author", str(r.get("reason")))

    def test_empty_allowlist_refuses_everything(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["browser"]["allowed_identities"] = []
        rid, _ = self.submit("status_request")
        self.bridge(cfg).run_once()
        r = self.result_for(rid)
        self.assertEqual(r.get("status"), "blocked")
        self.assertIn("fail closed", str(r.get("reason")))


class TestSafetyRefusals(BrowserTestCase):
    def test_malformed_json_refused_with_a_receipt(self):
        rid = "BREQ-20260729T120000Z-deadbeef"
        rel = f"{ml.DIR_BROWSER_REQUESTS}/{rid}.json"
        (self.repo_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.repo_path / rel).write_text("{not json\n", encoding="utf-8")
        self.repo.git("add", "--", rel)
        self.repo.git("commit", "-m", "bad request")
        self.bridge().run_once()
        r = self.result_for(rid)
        self.assertEqual(r.get("status"), "blocked")
        self.assertIn("not valid JSON", str(r.get("reason")))

    def test_secret_bearing_request_refused(self):
        rid, _ = self.submit(
            "status_request",
            rationale="use key sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4")
        self.bridge().run_once()
        r = self.result_for(rid)
        self.assertEqual(r.get("status"), "blocked")
        self.assertIn("secret scan", str(r.get("reason")))

    def test_filename_must_match_request_id(self):
        rid, req = self.request("status_request")
        rel = f"{ml.DIR_BROWSER_REQUESTS}/BREQ-20260729T120000Z-00000000.json"
        (self.repo_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.repo_path / rel).write_text(json.dumps(req), encoding="utf-8")
        self.repo.git("add", "--", rel)
        self.repo.git("commit", "-m", "mismatched")
        self.bridge().run_once()
        r = self.result_for("BREQ-20260729T120000Z-00000000")
        self.assertEqual(r.get("status"), "blocked")
        self.assertIn("does not match", str(r.get("reason")))

    def test_duplicate_idempotency_key_refused(self):
        rid1, req1 = self.submit("status_request")
        self.bridge().run_once()
        rid2, _ = self.submit("status_request",
                              idempotency_key=req1["idempotency_key"])
        self.bridge().run_once()
        self.assertEqual(self.result_for(rid1).get("status"), "completed")
        r2 = self.result_for(rid2)
        self.assertEqual(r2.get("status"), "blocked")
        self.assertIn("already used", str(r2.get("reason")))

    def test_resolve_escalation_is_refused_from_the_browser(self):
        rid, _ = self.submit("resolve_escalation",
                             escalation_id="BRITTLE-20260729T120000Z-11223344",
                             authorized_action="do it", scope="everything")
        self.bridge().run_once()
        r = self.result_for(rid)
        self.assertEqual(r.get("status"), "blocked")
        self.assertIn("owner authority", str(r.get("reason")))

    def test_browser_requests_are_not_canonical_messages(self):
        self.submit("status_request")
        msgs = self.msgs()
        self.assertFalse(
            [m for m in msgs.values() if m.rel.startswith(ml.DIR_BROWSER_REQUESTS)],
            "request files must never load as canonical bus messages")

    def test_request_dir_rejects_non_json(self):
        with self.assertRaisesRegex(ml.MessageError, "must be .json"):
            ml.check_path_policy(f"{ml.DIR_BROWSER_REQUESTS}/x.md", 10)


class TestBrowserTelephone(BrowserTestCase):
    def test_start_creates_a_browser_mode_run(self):
        report_id, run_id, rid = self.start_browser_run(max_cycles=3,
                                                        criterion="it is stable")
        run = self.msgs()[run_id]
        self.assertEqual(run.get("reviewer_mode"), "browser")
        self.assertEqual(run.get("max_cycles"), 3)
        self.assertEqual(run.get("criterion"), "it is stable")
        self.assertEqual(run.get("request_id"), rid)
        self.assertValid()

    def test_api_reviewer_ignores_a_browser_run(self):
        report_id, run_id, _ = self.start_browser_run()
        daemon = rd.ReviewerDaemon(self.cfg)
        pending = daemon.pending_reports(self.msgs())
        self.assertNotIn(report_id, [m.id for m in pending],
                         "the API reviewer must not see a browser-mode report")

        os.environ["BRITTLE_REVIEWER_MOCK"] = mock_response(self.tmp)
        head = self.repo.head()
        stats = daemon.run_once()
        self.assertEqual(stats["reviewed"], 0)
        self.assertEqual(self.repo.head(), head, "API reviewer must publish nothing")

    def test_ticket_markdown_is_published_verbatim(self):
        report_id, run_id, _ = self.start_browser_run()
        exact = ("## Objective\nDo exactly this and nothing else.\n\n"
                 "## Steps\n1. First.\n2. Second.\n\n"
                 "## Acceptance\n- A specific number: 42\n")
        rid, _ = self.submit(
            "review_and_ticket", report_id=report_id,
            payload={"summary": "s", "next_action": "n", "confidence": 0.95,
                     "ticket_title": "Exact title", "ticket_markdown": exact,
                     "target_lane": "control"})
        self.bridge().run_once()

        tickets = [m for m in self.msgs().values() if m.kind == "ticket"]
        self.assertEqual(len(tickets), 1, self.result_for(rid).body)
        t = tickets[0]
        self.assertEqual(t.body.rstrip("\n"), exact.rstrip("\n"),
                         "the bridge must not alter submitted ticket prose")
        self.assertEqual(t.get("title"), "Exact title")
        self.assertEqual(t.get("reviewer_mode"), "browser")
        self.assertEqual(t.get("run_id"), run_id)

    def test_review_and_ticket_publishes_review_ticket_and_ack_atomically(self):
        report_id, run_id, _ = self.start_browser_run()
        rid, _ = self.submit(
            "review_and_ticket", report_id=report_id,
            payload={"summary": "s", "next_action": "n", "confidence": 0.95,
                     "ticket_title": "T", "ticket_markdown": "## Steps\n1. Go.\n",
                     "target_lane": "control"})
        self.bridge().run_once()
        k = self.kinds()
        # review, ticket and acknowledgement must share ONE commit, so a crash
        # can never leave a ticket without its review or acknowledgement
        review_rel = k["review"][0].rel
        commit = self.repo.git("log", "-1", "--format=%H", "--", review_rel).strip()
        touched = self.repo.git("show", "--name-only", "--format=", commit).split()
        self.assertIn(k["ticket"][0].rel, touched)
        ack = [m for m in self.msgs().values()
               if m.get("receipt_type") == "reviewer_ack"][0]
        self.assertIn(ack.rel, touched)
        self.assertEqual(len(k["review"]), 1)
        self.assertEqual(len(k["ticket"]), 1)
        self.assertTrue(ml.reviewer_acked(report_id, self.msgs()))

    def test_double_review_of_one_report_refused(self):
        report_id, run_id, _ = self.start_browser_run()
        payload = {"summary": "s", "next_action": "n", "confidence": 0.95,
                   "ticket_title": "T", "ticket_markdown": "x",
                   "target_lane": "control"}
        self.submit("review_and_ticket", report_id=report_id, payload=payload)
        self.bridge().run_once()
        rid2, _ = self.submit("review_and_ticket", report_id=report_id,
                              payload=payload)
        self.bridge().run_once()
        r = self.result_for(rid2)
        self.assertEqual(r.get("status"), "blocked")
        self.assertIn("already has a reviewer acknowledgement", str(r.get("reason")))

    def test_browser_review_of_an_api_mode_run_refused(self):
        report_id = self.publish_report(lane="control")
        argvs = ["--repo", str(self.repo_path)]
        # start an API-mode run directly
        fm = ml.base_frontmatter("telephone_run", sender="joe", to="control",
                                 lane="control", unit="U", status="open",
                                 in_reply_to=report_id)
        fm.update({"max_cycles": 3, "report_id": report_id, "reviewer_mode": "api"})
        ml.publish(self.repo, {f"{ml.DIR_TELEPHONE}/{fm['id']}.md":
                               ml.render_message(fm, "# api run\n")},
                   "api run", cfg=self.cfg)
        rid, _ = self.submit("review_only", report_id=report_id,
                             payload={"summary": "s", "next_action": "n",
                                      "confidence": 0.9})
        self.bridge().run_once()
        r = self.result_for(rid)
        self.assertEqual(r.get("status"), "blocked")
        self.assertIn("double-review", str(r.get("reason")))

    def test_cycle_limit_is_enforced_against_the_browser(self):
        report_id, run_id, _ = self.start_browser_run(max_cycles=1)
        payload = {"summary": "s", "next_action": "n", "confidence": 0.95,
                   "ticket_title": "T", "ticket_markdown": "## Steps\n1. Go.\n",
                   "target_lane": "control"}
        self.submit("review_and_ticket", report_id=report_id, payload=payload)
        self.bridge().run_once()

        # complete cycle 1
        t = [m for m in self.msgs().values() if m.kind == "ticket"][0]
        import contextlib
        import io
        cfgp = self.tmp / "c.toml"
        cfgp.write_text(f'[repo]\npath = "{self.repo_path}"\n'
                        f'brittle_path = "{self.brittle}"\n', encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            messagesctl.main(["--config", str(cfgp), "claim", t.id,
                              "--agent", "control"])
        local = self.write_local_report("done.md")
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            messagesctl.main(["--config", str(cfgp), "--json", "publish-report",
                              "--lane", "control", "--unit", "U",
                              "--report", str(local), "--in-reply-to", t.id])
        comp = [m for m in self.msgs().values()
                if m.kind == "report" and m.get("in_reply_to") == t.id][0]

        # review the completion -> closes cycle 1 of 1
        self.submit("review_only", report_id=comp.id,
                    payload={"summary": "s", "next_action": "n", "confidence": 0.9})
        self.bridge().run_once()
        st = tp.run_state(self.msgs()[run_id], self.msgs())
        self.assertEqual(st["cycles_completed"], 1)
        self.assertEqual(st["stop_reason"], tp.STOP_MAX_CYCLES)

        # the run is finished; it may issue no further successor
        self.assertEqual(tp.run_state(self.msgs()[run_id], self.msgs())["status"],
                         "stopped")

    def test_final_cycle_may_not_issue_another_ticket(self):
        """At the limit, a review_and_ticket for the run's own report is refused."""
        import contextlib
        import io

        report_id, run_id, _ = self.start_browser_run(max_cycles=1)
        payload = {"summary": "s", "next_action": "n", "confidence": 0.95,
                   "ticket_title": "T", "ticket_markdown": "## Steps\n1. Go.\n",
                   "target_lane": "control"}
        self.submit("review_and_ticket", report_id=report_id, payload=payload)
        self.bridge().run_once()

        t = [m for m in self.msgs().values() if m.kind == "ticket"][0]
        cfgp = self.tmp / "c2.toml"
        cfgp.write_text(f'[repo]\npath = "{self.repo_path}"\n'
                        f'brittle_path = "{self.brittle}"\n', encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            messagesctl.main(["--config", str(cfgp), "claim", t.id,
                              "--agent", "control"])
            messagesctl.main(["--config", str(cfgp), "--json", "publish-report",
                              "--lane", "control", "--unit", "U",
                              "--report", str(self.write_local_report("d2.md")),
                              "--in-reply-to", t.id])
        comp = [m for m in self.msgs().values()
                if m.kind == "report" and m.get("in_reply_to") == t.id][0]

        # reviewing the completion closes cycle 1 of 1 -> no successor allowed
        rid, _ = self.submit("review_and_ticket", report_id=comp.id,
                             payload=payload)
        self.bridge().run_once()
        r = self.result_for(rid)
        self.assertEqual(r.get("status"), "blocked")
        self.assertIn("may not issue another ticket", str(r.get("reason")))
        self.assertEqual(sum(1 for m in self.msgs().values() if m.kind == "ticket"), 1,
                         "the cycle bound must hold against the browser")

    def test_criterion_met_stops_the_browser_run(self):
        report_id, run_id, _ = self.start_browser_run(max_cycles=3,
                                                       criterion="it is stable")
        self.submit("review_only", report_id=report_id,
                    payload={"summary": "s", "next_action": "n", "confidence": 0.95},
                    criterion_status="met", criterion_confidence=0.95,
                    criterion_evidence="PID identical across both polls")
        self.bridge().run_once()
        st = tp.run_state(self.msgs()[run_id], self.msgs())
        self.assertEqual(st["stop_reason"], tp.STOP_CRITERION_MET)
        self.assertEqual(st["status"], "completed")

    def test_low_criterion_confidence_stops_and_escalates(self):
        report_id, run_id, _ = self.start_browser_run(max_cycles=3,
                                                       criterion="it is stable")
        self.submit("review_only", report_id=report_id,
                    payload={"summary": "s", "next_action": "n", "confidence": 0.9},
                    criterion_status="met", criterion_confidence=0.3)
        self.bridge().run_once()
        st = tp.run_state(self.msgs()[run_id], self.msgs())
        self.assertEqual(st["stop_reason"], tp.STOP_CRITERION_UNKNOWN)

    def test_stop_request_halts_the_run(self):
        report_id, run_id, _ = self.start_browser_run()
        rid, _ = self.submit("telephone_stop", run_id=run_id, reason="enough")
        self.bridge().run_once()
        st = tp.run_state(self.msgs()[run_id], self.msgs())
        self.assertEqual(st["status"], "stopped")
        self.assertEqual(st["stop_reason"], tp.STOP_MANUAL)


class TestBrowserStatus(BrowserTestCase):
    def test_status_is_sanitized_and_complete(self):
        report_id, run_id, _ = self.start_browser_run(max_cycles=2,
                                                       criterion="it is stable")
        payload = bb.build_browser_status(self.cfg, self.msgs(), self.repo,
                                          self.bridge())
        for key in ("telephone_runs", "open_tickets_by_lane", "open_escalations",
                    "reports_awaiting_review", "services", "gateway_heartbeat",
                    "last_successful_sync", "api_reviewer_mode_enabled",
                    "spending_guard", "browser_requests"):
            self.assertIn(key, payload)
        run = payload["telephone_runs"][0]
        for key in ("run_id", "lane", "unit", "reviewer_mode", "state",
                    "cycles_completed", "max_cycles", "criterion",
                    "criterion_status", "current_ticket", "current_claim",
                    "blocker", "stop_reason"):
            self.assertIn(key, run)
        self.assertEqual(run["reviewer_mode"], "browser")

        text = json.dumps(payload)
        self.assertEqual(ml.scan_secrets(text), [])
        for banned in ("sk-", "Authorization", "Bearer"):
            self.assertNotIn(banned, text, f"status leaked {banned!r}")

        # A workstation path may appear ONLY inside a remediation command --
        # that is the feature, since Joe has to paste it. Everywhere else in the
        # status it would be a leak.
        without_actions = json.dumps(
            {k: v for k, v in payload.items() if k != "required_local_actions"})
        self.assertNotIn("/home/robojoe", without_actions,
                         "workstation paths belong only in published commands")
        for action in payload["required_local_actions"]:
            for field, val in action.items():
                if "/home/robojoe" in val:
                    self.assertIn(field, ("command", "verify_command"),
                                  f"path leaked into {field}")
                    self.assertTrue(val.startswith(bb._ALLOWED_COMMAND_PREFIXES),
                                    f"unvetted command form: {val!r}")

    def test_status_lists_pending_and_refused_requests(self):
        rid_bad, _ = self.submit("status_request", submitted_by="mallory")
        self.bridge().run_once()
        rid_pending, _ = self.submit("status_request")
        payload = bb.build_browser_status(self.cfg, self.msgs(), self.repo,
                                          self.bridge())
        self.assertIn(rid_pending, payload["browser_requests"]["pending"])
        self.assertIn(rid_bad,
                      [r["request_id"] for r in payload["browser_requests"]["refused"]])

    def test_status_reports_whether_api_mode_is_enabled(self):
        payload = bb.build_browser_status(self.cfg, self.msgs(), self.repo,
                                          self.bridge())
        self.assertIsInstance(payload["api_reviewer_mode_enabled"], bool)


class TestBridgeIdempotency(BrowserTestCase):
    def test_reprocessing_produces_no_duplicates(self):
        report_id, run_id, _ = self.start_browser_run()
        self.submit("review_and_ticket", report_id=report_id,
                    payload={"summary": "s", "next_action": "n", "confidence": 0.95,
                             "ticket_title": "T", "ticket_markdown": "x",
                             "target_lane": "control"})
        self.bridge().run_once()
        head = self.repo.head()
        counts = ml.build_index(self.msgs())["counts"]
        for _ in range(3):
            self.bridge().run_once()
        self.assertEqual(self.repo.head(), head, "restarts must add no commits")
        self.assertEqual(ml.build_index(self.msgs())["counts"], counts)

    def test_every_request_gets_exactly_one_result_receipt(self):
        rid, _ = self.submit("status_request")
        for _ in range(3):
            self.bridge().run_once()
        results = [m for m in self.msgs().values()
                   if m.get("receipt_type") == "browser_result"
                   and m.get("request_id") == rid]
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
