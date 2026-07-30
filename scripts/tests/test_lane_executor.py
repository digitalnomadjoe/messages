"""Autonomous lane-ticket execution. Offline; handlers are stubbed or read-only."""

from __future__ import annotations

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# harness must be imported first: it puts scripts/ on sys.path
from harness import BusTestCase, ml
import browser_bridge as bb
import lane_executor as le
import telephone as tp

TWO_POLL_BODY = """## Objective
Read `ActiveState`, `MainPID` and `NRestarts` from `brittle-messages-control.service` twice and compare.

## Steps
1. Run `systemctl --user show brittle-messages-control.service --property=ActiveState --property=MainPID --property=NRestarts --no-pager`.
2. Wait 10 seconds and repeat two polls.
3. Compare.

## Prohibitions
Do not restart or modify the service. No code, config or production changes.
"""


class ExecutorTestCase(BusTestCase):
    def setUp(self):
        super().setUp()
        self.cfg["executor"] = {
            "poll_seconds": 1, "task_timeout_seconds": 60,
            "two_poll_gap_seconds": 0.5,
            "reports_dir": str(self.brittle / "rgl" / "reports"),
        }
        self.cfg["browser"] = {"allowed_identities": ["digitalnomadjoe"]}
        # Point the child worker and every messagesctl subprocess at this bus.
        self.cfg_path = self.tmp / "config.toml"
        self.cfg_path.write_text(
            f'[repo]\npath = "{self.repo_path}"\nbrittle_path = "{self.brittle}"\n'
            f'[reviewer]\nmodel = "test-model"\nminimum_confidence = 0.85\n'
            f'[notification]\ncommand = ""\n'
            f"[executor]\npoll_seconds = 1\ntask_timeout_seconds = 60\n"
            f"two_poll_gap_seconds = 0.5\n"
            f'reports_dir = "{self.brittle}/rgl/reports"\n'
            f"[spending]\nmonthly_cap_usd = 100.0\ndaily_cap_usd = 100.0\n"
            f"max_calls_per_day = 1000\nmax_completion_tokens = 1000\n"
            f'ledger_path = "{self.tmp}/led.jsonl"\n'
            f'[spending.pricing_input_usd_per_1m]\n"test-model" = 2.50\n'
            f'[spending.pricing_output_usd_per_1m]\n"test-model" = 10.00\n'
            f'[browser]\nallowed_identities = ["digitalnomadjoe"]\n',
            encoding="utf-8")
        os.environ["BRITTLE_MESSAGES_CONFIG"] = str(self.cfg_path)
        self.addCleanup(os.environ.pop, "BRITTLE_MESSAGES_CONFIG", None)
        self.cfg = ml.load_config(self.cfg_path)
        self.cfg["executor"]["reports_dir"] = str(self.brittle / "rgl" / "reports")

        # The real registry, deliberately. The worker runs in a child process
        # that re-imports lane_executor, so a parent-side stub would not apply
        # anyway -- and the real handler is fast here because the config sets
        # two_poll_gap_seconds = 0.5.
        self._registry = le.REGISTRY
        self.addCleanup(setattr, le, "REGISTRY", self._registry)

    def executor(self, lane="control"):
        return le.LaneExecutor(lane, self.cfg)

    def arm(self, lane="control"):
        """One idle pass, as a real service start does.

        This establishes the high-water mark BEFORE work arrives, which is the
        realistic order: the executor is running, then Telephone publishes a
        ticket. Tickets that predate the mark are deliberately ignored -- see
        test_historic_tickets_are_not_swept_up_on_first_start.
        """
        self.executor(lane).high_water_mark()

    def make_ticket(self, lane="control", body=TWO_POLL_BODY, run_id=None,
                    requires_owner=False, title="Two-poll control check"):
        fm = ml.base_frontmatter("ticket", sender="reviewer", to=lane, lane=lane,
                                 unit="CERT-EXEC", status="open",
                                 requires_owner=requires_owner)
        fm["title"] = title
        if run_id:
            fm["run_id"] = run_id
            fm["cycle_index"] = 1
        rel = f"{ml.DIR_TICKETS}/{lane}/{fm['id']}.md"
        ml.publish(self.repo, {rel: ml.render_message(fm, body)},
                   f"ticket({lane})", cfg=self.cfg)
        return fm["id"]

    def make_run(self, lane="control", max_cycles=1, mode="browser",
                 criterion=None):
        report_id = self.publish_report(lane=lane)
        fm = ml.base_frontmatter("telephone_run", sender="joe", to=lane, lane=lane,
                                 unit="CERT-EXEC", status="open",
                                 in_reply_to=report_id)
        fm.update({"max_cycles": max_cycles, "report_id": report_id,
                   "reviewer_mode": mode, "criterion": criterion})
        ml.publish(self.repo, {f"{ml.DIR_TELEPHONE}/{fm['id']}.md":
                               ml.render_message(fm, "# run\n")},
                   "run", cfg=self.cfg)
        return report_id, fm["id"]

    def state(self, tid):
        return ml.ticket_state(tid, self.msgs())


class TestAutonomousCompletion(ExecutorTestCase):
    def test_claims_and_completes_one_read_only_ticket(self):
        self.arm()
        tid = self.make_ticket()
        stats = self.executor().run_once()

        self.assertEqual(stats["claimed"], 1, stats)
        self.assertEqual(stats["completed"], 1, stats)
        st = self.state(tid)
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["claim_agent"], "control")

        report = self.msgs()[stats["report_id"]]
        self.assertEqual(report.kind, "report")
        self.assertEqual(report.get("lane"), "control")
        self.assertEqual(report.get("in_reply_to"), tid)
        self.assertIn("service_two_poll_check", report.body)
        self.assertIn("Overall: PASS", report.body)
        self.assertIn("No state-changing action was taken", report.body)
        self.assertIn("Executed automatically", report.body)
        self.assertValid()

    def test_local_report_is_written_and_authoritative(self):
        self.arm()
        self.make_ticket()
        stats = self.executor().run_once()
        report = self.msgs()[stats["report_id"]]
        local = Path(str(report.get("local_source_path")))
        self.assertTrue(local.exists(), local)
        self.assertEqual(ml.sha256_file(local), report.get("local_source_sha256"))

    def test_no_human_command_between_ticket_and_report(self):
        """One executor pass takes an open ticket to a published completion."""
        self.arm()
        tid = self.make_ticket()
        self.assertEqual(self.state(tid)["status"], "open")
        self.executor().run_once()
        self.assertEqual(self.state(tid)["status"], "completed")


class TestClaimOrdering(ExecutorTestCase):
    def test_claim_is_published_only_after_a_worker_has_started(self):
        """If no worker can start, nothing is claimed."""
        self.arm()
        tid = self.make_ticket()
        original = le.HANDSHAKE_STARTED
        le.HANDSHAKE_STARTED = "NEVER-SENT-BY-WORKER"
        self.addCleanup(setattr, le, "HANDSHAKE_STARTED", original)
        original_timeout = le.HANDSHAKE_TIMEOUT
        le.HANDSHAKE_TIMEOUT = 3.0
        self.addCleanup(setattr, le, "HANDSHAKE_TIMEOUT", original_timeout)

        stats = self.executor().run_once()
        self.assertEqual(stats["claimed"], 0)
        claims = [m for m in self.msgs().values()
                  if m.get("receipt_type") in ("claim", "renew", "reclaim")]
        self.assertEqual(claims, [], "nothing may be claimed without a live worker")

    def test_worker_refuses_to_work_without_claim_confirmation(self):
        payload = self.tmp / "p.json"
        payload.write_text(json.dumps(
            {"ticket_id": "x", "handler": "service_two_poll_check",
             "lane": "control"}), encoding="utf-8")
        out = self.tmp / "o.md"
        import io
        import contextlib

        real_stdin = os.dup(0)
        try:
            r, w = os.pipe()
            os.write(w, b"NOT-THE-GO-TOKEN\n")
            os.close(w)
            os.dup2(r, 0)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = le.worker_main(str(payload), str(out))
        finally:
            os.dup2(real_stdin, 0)
            os.close(real_stdin)
        self.assertEqual(rc, 3, "worker must refuse without the go-ahead")
        self.assertFalse(out.exists(), "no work product without a claim")


class TestNoDuplicateExecution(ExecutorTestCase):
    def test_second_pass_does_not_reclaim_a_completed_ticket(self):
        self.arm()
        tid = self.make_ticket()
        self.executor().run_once()
        head = self.repo.head()
        for _ in range(3):
            self.executor().run_once()
        self.assertEqual(self.repo.head(), head, "idle passes must add no commits")
        claims = [m for m in self.msgs().values()
                  if m.get("receipt_type") in ("claim", "renew", "reclaim")]
        self.assertEqual(len(claims), 1)

    def test_concurrent_passes_produce_one_claim(self):
        self.arm()
        self.make_ticket()
        results = []

        def go(_):
            try:
                results.append(self.executor().run_once())
            except Exception as exc:  # noqa: BLE001
                results.append({"claimed": 0, "errors": [str(exc)]})

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(go, range(4)))

        claims = [m for m in self.msgs().values()
                  if m.get("receipt_type") in ("claim", "renew", "reclaim")]
        self.assertEqual(len(claims), 1,
                         f"exactly one claim expected, got {len(claims)}")

    def test_restart_does_not_re_execute_a_completed_ticket(self):
        self.arm()
        tid = self.make_ticket()
        self.executor().run_once()
        reports_before = sum(1 for m in self.msgs().values() if m.kind == "report")
        # a brand-new executor object == a service restart
        for _ in range(2):
            le.LaneExecutor("control", self.cfg).run_once()
        self.assertEqual(sum(1 for m in self.msgs().values() if m.kind == "report"),
                         reports_before)

    def test_interrupted_non_idempotent_attempt_is_blocked_not_retried(self):
        self.arm()
        tid = self.make_ticket()

        class OneShot(le.Handler):
            name = "one_shot"
            idempotent = False

            def matches(self, ticket):
                return True

            def run(self, ticket, cfg):
                return "# should never run\n"

        le.REGISTRY = (OneShot(),)
        # simulate: claimed, mid-flight, then the process died
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            import messagesctl
            messagesctl.main(["--config", str(self.cfg_path), "claim", tid,
                              "--agent", "control"])
        le.save_executor_state("control", {
            "high_water_mark": "2000-01-01T00:00:00.000000Z",
            "in_flight_ticket": tid, "in_flight_handler": "one_shot"})

        stats = self.executor().run_once()
        self.assertEqual(stats["blocked"], 1, stats)
        self.assertEqual(self.state(tid)["status"], "blocked")
        self.assertIn("interrupted", self.state(tid)["block"].body.lower())


class TestGates(ExecutorTestCase):
    def test_owner_gated_ticket_is_never_claimed(self):
        self.arm()
        tid = self.make_ticket(requires_owner=True)
        stats = self.executor().run_once()
        self.assertEqual(stats["claimed"], 0)
        self.assertEqual(stats["blocked"], 1)
        st = self.state(tid)
        self.assertEqual(st["status"], "blocked")
        self.assertIn("requires_owner", st["block"].body)

    def test_paused_autonomy_prevents_execution(self):
        self.arm()
        tid = self.make_ticket()
        (ml.state_dir() / "PAUSED").write_text("x", encoding="utf-8")
        stats = self.executor().run_once()
        self.assertEqual(stats["claimed"], 0)
        self.assertEqual(self.state(tid)["status"], "open",
                         "a paused bus must leave the ticket untouched")

    def test_one_ticket_per_lane(self):
        self.arm()
        first = self.make_ticket(title="first")
        second = self.make_ticket(title="second")
        self.executor().run_once()          # completes first
        self.assertEqual(self.state(first)["status"], "completed")
        # second is picked up only on a later pass, never concurrently
        claims = [m for m in self.msgs().values()
                  if m.get("receipt_type") == "claim"]
        self.assertEqual(len(claims), 1)

    def test_lane_isolation(self):
        self.arm("control")
        loco = self.make_ticket(lane="locomotion")
        stats = self.executor("control").run_once()
        self.assertEqual(stats["claimed"], 0)
        self.assertEqual(self.state(loco)["status"], "open")

    def test_historic_tickets_are_not_swept_up_on_first_start(self):
        tid = self.make_ticket()
        # first start records a high-water mark AFTER this ticket
        import time as _t
        _t.sleep(0.01)
        le.save_executor_state("control", {
            "high_water_mark": ml.iso(ml.utc_now())})
        stats = self.executor().run_once()
        self.assertEqual(stats["claimed"], 0)
        self.assertEqual(self.state(tid)["status"], "open")

    def test_spend_guard_block_prevents_execution(self):
        self.arm()
        tid = self.make_ticket()
        cfg = json.loads(json.dumps(self.cfg))
        cfg["spending"]["max_calls_per_day"] = 0
        ex = le.LaneExecutor("control", cfg)
        stats = ex.run_once()
        self.assertEqual(stats["claimed"], 0)
        self.assertEqual(self.state(tid)["status"], "open")


class TestClassification(unittest.TestCase):
    def ticket(self, body, **over):
        fm = ml.base_frontmatter("ticket", sender="reviewer", to="control",
                                 lane="control", unit="U", status="open")
        fm.update(over)
        return ml.Message(fm, body, "x/y.md")

    def test_matches_the_two_poll_handler(self):
        h = le.classify(self.ticket(TWO_POLL_BODY))
        self.assertEqual(h.name, "service_two_poll_check")

    def test_hard_gate_text_is_blocked(self):
        for body in ("Promote the checkpoint to main.",
                     "Overwrite the canonical reference.",
                     "Update latest to the new policy.",
                     "Refresh the policy card.",
                     "Disable the Guard check."):
            with self.subTest(body=body):
                with self.assertRaises(le.Blocked):
                    le.classify(self.ticket(body))

    def test_mutation_request_is_blocked(self):
        with self.assertRaisesRegex(le.Blocked, "state-changing"):
            le.classify(self.ticket(
                "Restart brittle-messages-control.service and confirm it "
                "returns with systemctl two polls of ActiveState MainPID "
                "NRestarts."))

    def test_prohibition_lines_do_not_read_as_mutation_requests(self):
        """'Do not restart' must not be mistaken for 'restart'."""
        h = le.classify(self.ticket(TWO_POLL_BODY))
        self.assertEqual(h.name, "service_two_poll_check")

    def test_unknown_ticket_is_blocked_not_guessed(self):
        with self.assertRaisesRegex(le.Blocked, "no autonomous handler"):
            le.classify(self.ticket("Tune the reward shaping and report."))

    def test_ambiguous_multi_match_is_blocked(self):
        class A(le.Handler):
            name = "a"

            def matches(self, t):
                return True

        class B(le.Handler):
            name = "b"

            def matches(self, t):
                return True

        original = le.REGISTRY
        le.REGISTRY = (A(), B())
        try:
            with self.assertRaisesRegex(le.Blocked, "multiple handlers"):
                le.classify(self.ticket("anything"))
        finally:
            le.REGISTRY = original

    def test_registry_is_read_only_by_declaration(self):
        for h in le.REGISTRY:
            self.assertTrue(h.idempotent,
                            f"{h.name} must be read-only/idempotent to auto-run")


class TestTelephoneBinding(ExecutorTestCase):
    def test_browser_ticket_stays_bound_to_its_run(self):
        self.arm()
        _seed, run_id = self.make_run(max_cycles=1, mode="browser")
        tid = self.make_ticket(run_id=run_id)
        stats = self.executor().run_once()
        self.assertEqual(stats["completed"], 1, stats)

        report = self.msgs()[stats["report_id"]]
        self.assertEqual(report.get("in_reply_to"), tid)
        ticket = self.msgs()[tid]
        self.assertEqual(ticket.get("run_id"), run_id)
        # the completion report closes the run's cycle when reviewed
        self.assertTrue(tp.closes_cycle(report, self.msgs(),
                                        self.msgs()[run_id]))

    def test_ticket_bound_to_a_stopped_run_is_blocked(self):
        self.arm()
        _seed, run_id = self.make_run(max_cycles=1)
        tid = self.make_ticket(run_id=run_id)
        fm = ml.base_frontmatter("receipt", sender="joe", to="control",
                                 lane="control", unit="CERT-EXEC",
                                 status="blocked", in_reply_to=run_id)
        fm.update({"receipt_type": "telephone_stop", "agent": "joe",
                   "run_id": run_id, "stop_reason": tp.STOP_MANUAL,
                   "cycles_completed": 0})
        ml.publish(self.repo, {f"{ml.DIR_RECEIPTS}/{fm['id']}.md":
                               ml.render_message(fm, "# stopped\n")},
                   "stop", cfg=self.cfg)
        stats = self.executor().run_once()
        self.assertEqual(stats["claimed"], 0)
        self.assertEqual(self.state(tid)["status"], "blocked")

    def test_completion_report_is_available_for_browser_review(self):
        self.arm()
        _seed, run_id = self.make_run(max_cycles=1, mode="browser")
        self.make_ticket(run_id=run_id)
        stats = self.executor().run_once()
        msgs = self.msgs()
        awaiting = [m.id for m in msgs.values()
                    if m.kind == "report" and not ml.reviewer_acked(m.id, msgs)]
        self.assertIn(stats["report_id"], awaiting,
                      "the browser reviewer must see the completion report")
        payload = bb.build_browser_status(self.cfg, msgs, self.repo,
                                          bb.BrowserBridge(self.cfg))
        self.assertIn(stats["report_id"], payload["reports_awaiting_review"])

    def test_api_reviewer_still_ignores_browser_mode_runs(self):
        import reviewer_daemon as rd

        self.arm()
        _seed, run_id = self.make_run(max_cycles=1, mode="browser")
        self.make_ticket(run_id=run_id)
        stats = self.executor().run_once()
        pending = rd.ReviewerDaemon(self.cfg).pending_reports(self.msgs())
        self.assertNotIn(stats["report_id"], [m.id for m in pending],
                         "the API reviewer must not touch a browser-mode report")


class TestBrowserStatusExposure(ExecutorTestCase):
    def test_executor_state_is_exposed_and_sanitized(self):
        self.arm()
        self.make_ticket()
        stats = self.executor().run_once()
        payload = bb.build_browser_status(self.cfg, self.msgs(), self.repo,
                                          bb.BrowserBridge(self.cfg))
        self.assertIn("lane_executors", payload)
        control = payload["lane_executors"]["control"]
        for key in ("service_active", "executing", "current_ticket",
                    "current_handler", "last_ticket", "last_report",
                    "last_outcome", "high_water_mark"):
            self.assertIn(key, control)
        self.assertFalse(control["executing"], "idle after completion")
        self.assertEqual(control["last_outcome"], "completed")
        self.assertEqual(control["last_report"], stats["report_id"])

        text = json.dumps(payload["lane_executors"])
        self.assertEqual(ml.scan_secrets(text), [])
        self.assertNotIn("/home/robojoe", text)


class TestWatcherUnchanged(unittest.TestCase):
    def test_ticket_watcher_remains_notification_only(self):
        src = (Path(le.__file__).parent / "ticket_watcher.py").read_text(
            encoding="utf-8")
        self.assertNotIn("messagesctl", src)
        self.assertNotIn("ml.publish", src)
        self.assertIn("It never claims", src)


if __name__ == "__main__":
    unittest.main()
