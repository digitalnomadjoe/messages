"""Telephone bounded runs. Entirely offline -- the reviewer is always mocked."""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest

# harness must be imported first: it puts scripts/ on sys.path
from harness import BusTestCase, ml, mock_response
import messagesctl
import reviewer_daemon as rd
import telephone as tp


class TestParsing(unittest.TestCase):
    def test_plain_loop_count(self):
        out = tp.parse_invocation("Run Telephone for 10 loops")
        self.assertEqual(out["action"], "start")
        self.assertEqual(out["max_cycles"], 10)
        self.assertIsNone(out["criterion"])

    def test_tilde_normalises_to_a_hard_maximum(self):
        for text in ("Run Telephone for ~10 loops",
                     "Run Telephone for about 10 loops",
                     "Run Telephone for approximately 10 cycles",
                     "Run Telephone for around 10 iterations"):
            with self.subTest(text=text):
                self.assertEqual(tp.parse_invocation(text)["max_cycles"], 10,
                                 "'about ten' is never licence to run an eleventh")

    def test_criterion_with_mandatory_maximum(self):
        out = tp.parse_invocation(
            "Run Telephone until both touchdown speeds are below -100, maximum 12 loops")
        self.assertEqual(out["max_cycles"], 12)
        self.assertEqual(out["criterion"], "both touchdown speeds are below -100")

    def test_explicit_maximum_beats_a_bare_count(self):
        out = tp.parse_invocation("Run Telephone for 50 loops, maximum 3 loops")
        self.assertEqual(out["max_cycles"], 3)

    def test_lane_is_extracted(self):
        self.assertEqual(
            tp.parse_invocation("Run Telephone on locomotion for 4 loops")["lane"],
            "locomotion")

    def test_stop_and_status(self):
        self.assertEqual(tp.parse_invocation("Stop Telephone")["action"], "stop")
        self.assertEqual(tp.parse_invocation("Telephone status")["action"], "status")

    def test_unbounded_request_is_refused(self):
        with self.assertRaisesRegex(tp.TelephoneError, "cycle bound"):
            tp.parse_invocation("Run Telephone until it works")

    def test_zero_or_negative_refused(self):
        with self.assertRaises(tp.TelephoneError):
            tp.parse_invocation("Run Telephone for 0 loops")

    def test_non_telephone_text_refused(self):
        with self.assertRaises(tp.TelephoneError):
            tp.parse_invocation("please train the robot")


class TestCriterionSafety(unittest.TestCase):
    def test_clean_criterion_passes(self):
        tp.check_criterion_public_safe("both touchdown speeds are below -100")

    def test_secret_bearing_criterion_refused(self):
        with self.assertRaisesRegex(tp.TelephoneError, "secret scan"):
            tp.check_criterion_public_safe(
                "use key sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4")

    def test_private_pattern_criterion_refused(self):
        with self.assertRaisesRegex(tp.TelephoneError, "secret scan"):
            tp.check_criterion_public_safe("INTERNAL-ONLY-42 threshold",
                                           [r"INTERNAL-ONLY-\d+"])

    def test_overlong_criterion_refused(self):
        with self.assertRaisesRegex(tp.TelephoneError, "under 500"):
            tp.check_criterion_public_safe("x" * 501)


class TestEvaluateGate(unittest.TestCase):
    """The bounding decision, isolated from any bus or model."""

    def state(self, **over):
        s = {"status": "active", "cycles_completed": 0, "max_cycles": 3,
             "criterion": None, "stop_reason": None}
        s.update(over)
        return s

    def ev(self, state, **kw):
        base = dict(verdict_mode="ticket", criterion_status=None,
                    criterion_confidence=None, threshold=0.85, cycle_closed=False)
        base.update(kw)
        return tp.evaluate(state, **base)

    def test_continues_when_cycles_remain(self):
        out = self.ev(self.state(cycles_completed=1))
        self.assertTrue(out["issue_ticket"])
        self.assertFalse(out["stop"])

    def test_hard_maximum_stops_regardless_of_model(self):
        out = self.ev(self.state(cycles_completed=2), cycle_closed=True,
                      criterion_status="not_met", criterion_confidence=0.99)
        self.assertFalse(out["issue_ticket"])
        self.assertEqual(out["stop_reason"], tp.STOP_MAX_CYCLES)
        self.assertEqual(out["cycles_after"], 3)

    def test_max_cycles_is_not_success(self):
        self.assertNotIn(tp.STOP_MAX_CYCLES, tp.SUCCESS_REASONS)
        self.assertIn("NOT a success", tp.describe_stop(tp.STOP_MAX_CYCLES))

    def test_criterion_met_at_threshold_stops_successfully(self):
        out = self.ev(self.state(criterion="x"), cycle_closed=True,
                      criterion_status="met", criterion_confidence=0.85)
        self.assertEqual(out["stop_reason"], tp.STOP_CRITERION_MET)
        self.assertFalse(out["escalate"])
        self.assertIn(out["stop_reason"], tp.SUCCESS_REASONS)

    def test_criterion_met_below_threshold_escalates(self):
        out = self.ev(self.state(criterion="x"), cycle_closed=True,
                      criterion_status="met", criterion_confidence=0.5)
        self.assertEqual(out["stop_reason"], tp.STOP_CRITERION_UNKNOWN)
        self.assertTrue(out["escalate"])

    def test_criterion_unknown_escalates(self):
        out = self.ev(self.state(criterion="x"), criterion_status="unknown",
                      criterion_confidence=0.99)
        self.assertEqual(out["stop_reason"], tp.STOP_CRITERION_UNKNOWN)
        self.assertTrue(out["escalate"])

    def test_criterion_missing_escalates(self):
        out = self.ev(self.state(criterion="x"), criterion_status=None)
        self.assertEqual(out["stop_reason"], tp.STOP_CRITERION_UNKNOWN)
        self.assertTrue(out["escalate"])

    def test_criterion_not_met_continues_when_budget_remains(self):
        out = self.ev(self.state(criterion="x", cycles_completed=0),
                      cycle_closed=True, criterion_status="not_met",
                      criterion_confidence=0.9)
        self.assertTrue(out["issue_ticket"])

    def test_criterion_not_met_still_stops_at_the_limit(self):
        out = self.ev(self.state(criterion="x", cycles_completed=2, max_cycles=3),
                      cycle_closed=True, criterion_status="not_met",
                      criterion_confidence=0.9)
        self.assertEqual(out["stop_reason"], tp.STOP_MAX_CYCLES)

    def test_review_only_stops(self):
        out = self.ev(self.state(), verdict_mode="review_only")
        self.assertEqual(out["stop_reason"], tp.STOP_REVIEW_ONLY)

    def test_escalation_stops(self):
        out = self.ev(self.state(), verdict_mode="escalation")
        self.assertEqual(out["stop_reason"], tp.STOP_ESCALATED)
        self.assertTrue(out["escalate"])

    def test_already_stopped_run_issues_nothing(self):
        out = self.ev(self.state(status="stopped", stop_reason=tp.STOP_MANUAL))
        self.assertFalse(out["issue_ticket"])
        self.assertEqual(out["stop_reason"], tp.STOP_MANUAL)


class TelephoneBusTestCase(BusTestCase):
    def setUp(self):
        super().setUp()
        self.cfg_path = self.tmp / "config.toml"
        self.cfg_path.write_text(
            f'[repo]\npath = "{self.repo_path}"\nbrittle_path = "{self.brittle}"\n'
            f'[reviewer]\nmodel = "test-model"\n'
            f'prompt_path = "{self.repo_path}/prompts/brittle-reviewer.md"\n'
            f"minimum_confidence = 0.85\n"
            f'[notification]\ncommand = ""\n'
            f"[spending]\nmonthly_cap_usd = 100.0\ndaily_cap_usd = 100.0\n"
            f"max_calls_per_day = 1000\nmax_completion_tokens = 1000\n"
            f'ledger_path = "{self.tmp}/led.jsonl"\n'
            f'[spending.pricing_input_usd_per_1m]\n"test-model" = 2.50\n'
            f'[spending.pricing_output_usd_per_1m]\n"test-model" = 10.00\n',
            encoding="utf-8")

    def ctl(self, *argv, expect=0):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = messagesctl.main(["--config", str(self.cfg_path), "--json", *argv])
        self.assertEqual(rc, expect, buf.getvalue())
        return buf.getvalue()

    def daemon(self):
        return rd.ReviewerDaemon(ml.load_config(self.cfg_path))

    def mock(self, **over):
        os.environ["BRITTLE_REVIEWER_MOCK"] = mock_response(self.tmp, **over)

    def start_run(self, lane="control", max_cycles=3, criterion=None):
        rid = self.publish_report(lane=lane)
        argv = ["telephone", "start", "--lane", lane, "--report", rid,
                "--max-cycles", str(max_cycles)]
        if criterion:
            argv += ["--criterion", criterion]
        out = json.loads(self.ctl(*argv))
        return rid, out["run_id"]

    def state(self, run_id):
        msgs = self.msgs()
        return tp.run_state(msgs[run_id], msgs)

    def complete_cycle(self, lane="control"):
        """Claim the open ticket, publish a completion report, complete it."""
        msgs = self.msgs()
        tickets = [m for m in sorted(msgs.values(), key=ml.Message.sort_key)
                   if m.kind == "ticket"
                   and ml.ticket_state(m.id, msgs)["status"] == "open"]
        self.assertTrue(tickets, "expected an open ticket to work")
        t = tickets[-1]
        self.ctl("claim", t.id, "--agent", lane)
        local = self.write_local_report(f"done-{t.id[-8:]}.md")
        out = json.loads(self.ctl("publish-report", "--lane", lane, "--unit",
                                  "12U-SYNTH", "--report", str(local),
                                  "--in-reply-to", t.id))
        self.ctl("complete", t.id, "--report-id", out["report_id"])
        return t.id, out["report_id"]


class TestRunLifecycle(TelephoneBusTestCase):
    def test_start_persists_run_on_the_bus(self):
        rid, run_id = self.start_run(max_cycles=3, criterion="service is stable")
        run = self.msgs()[run_id]
        self.assertEqual(run.kind, "telephone_run")
        self.assertEqual(run.get("lane"), "control")
        self.assertEqual(run.get("max_cycles"), 3)
        self.assertEqual(run.get("criterion"), "service is stable")
        self.assertEqual(run.get("report_id"), rid)
        self.assertValid()

    def test_state_survives_a_full_reload(self):
        _, run_id = self.start_run(max_cycles=2)
        # nothing cached: a brand-new load from git is the only source of truth
        msgs = ml.load_messages(self.repo_path)
        st = tp.run_state(msgs[run_id], msgs)
        self.assertEqual(st["status"], "active")
        self.assertEqual(st["cycles_completed"], 0)
        self.assertEqual(st["max_cycles"], 2)

    def test_only_one_active_run_per_lane(self):
        _, _ = self.start_run(lane="control", max_cycles=2)
        rid2 = self.publish_report(lane="control")
        out = self.ctl("telephone", "start", "--lane", "control",
                       "--report", rid2, "--max-cycles", "2", expect=2)
        self.assertIn("already has an active Telephone run", out)

    def test_refuses_to_start_over_an_unrelated_open_ticket(self):
        self.publish_ticket(lane="control", title="unrelated work")
        rid = self.publish_report(lane="control")
        out = self.ctl("telephone", "start", "--lane", "control",
                       "--report", rid, "--max-cycles", "2", expect=2)
        self.assertIn("unrelated open ticket", out)

    def test_refuses_to_start_over_an_unrelated_claimed_ticket(self):
        tid = self.publish_ticket(lane="control", title="unrelated work")
        self.ctl("claim", tid, "--agent", "control")
        rid = self.publish_report(lane="control")
        out = self.ctl("telephone", "start", "--lane", "control",
                       "--report", rid, "--max-cycles", "2", expect=2)
        self.assertIn("unrelated claimed ticket", out)

    def test_other_lane_does_not_block_a_start(self):
        self.publish_ticket(lane="locomotion", title="loco work")
        rid = self.publish_report(lane="control")
        self.ctl("telephone", "start", "--lane", "control",
                 "--report", rid, "--max-cycles", "2")

    def test_unsafe_criterion_refused_before_publication(self):
        rid = self.publish_report(lane="control")
        out = self.ctl("telephone", "start", "--lane", "control", "--report", rid,
                       "--max-cycles", "2", "--criterion",
                       "token sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4", expect=2)
        self.assertIn("secret scan", out)
        self.assertEqual([m for m in self.msgs().values()
                          if m.kind == "telephone_run"], [])

    def test_invocation_string_drives_start(self):
        rid = self.publish_report(lane="control")
        out = json.loads(self.ctl(
            "telephone", "start", "--report", rid,
            "--invocation", "Run Telephone on control for ~5 loops"))
        self.assertEqual(out["max_cycles"], 5)
        self.assertEqual(out["lane"], "control")


class TestCycleCounting(TelephoneBusTestCase):
    def test_cycle_increments_only_after_the_completion_review(self):
        _, run_id = self.start_run(max_cycles=3)
        self.mock(target_lane="control")

        self.daemon().run_once()                       # review start report -> ticket
        self.assertEqual(self.state(run_id)["cycles_completed"], 0,
                         "issuing a ticket is not a completed cycle")

        self.complete_cycle()                          # claim, work, complete
        self.assertEqual(self.state(run_id)["cycles_completed"], 0,
                         "publishing the completion report is not enough")

        self.daemon().run_once()                       # review the completion report
        self.assertEqual(self.state(run_id)["cycles_completed"], 1,
                         "the cycle lands only when its completion review does")

    def test_count_limited_run_terminates_exactly_at_the_limit(self):
        _, run_id = self.start_run(max_cycles=2)
        self.mock(target_lane="control")
        for _ in range(6):
            self.daemon().run_once()
            st = self.state(run_id)
            if st["status"] != "active":
                break
            self.complete_cycle()
        st = self.state(run_id)
        self.assertEqual(st["cycles_completed"], 2)
        self.assertEqual(st["stop_reason"], tp.STOP_MAX_CYCLES)
        self.assertEqual(st["status"], "stopped")

    def test_no_successor_ticket_after_the_limit(self):
        _, run_id = self.start_run(max_cycles=1)
        self.mock(target_lane="control")
        self.daemon().run_once()
        self.complete_cycle()
        self.daemon().run_once()
        before = sum(1 for m in self.msgs().values() if m.kind == "ticket")
        self.daemon().run_once()
        after = sum(1 for m in self.msgs().values() if m.kind == "ticket")
        self.assertEqual(before, after, "a stopped run must issue no further ticket")

    def test_no_duplicate_successor_ticket_per_review(self):
        _, run_id = self.start_run(max_cycles=3)
        self.mock(target_lane="control")
        self.daemon().run_once()
        n1 = sum(1 for m in self.msgs().values() if m.kind == "ticket")
        for _ in range(3):
            self.daemon().run_once()                  # nothing new to review
        n2 = sum(1 for m in self.msgs().values() if m.kind == "ticket")
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 1)

    def test_run_totals_api_calls_and_spend(self):
        _, run_id = self.start_run(max_cycles=2)
        self.mock(target_lane="control")
        self.daemon().run_once()
        self.complete_cycle()
        self.daemon().run_once()
        st = self.state(run_id)
        self.assertEqual(st["cycles_completed"], 1)
        # mocked calls cost nothing, but the accounting fields must exist
        self.assertIsInstance(st["api_calls"], int)
        self.assertIsInstance(st["spend_usd"], float)


class TestCriterionTermination(TelephoneBusTestCase):
    def test_criterion_met_stops_successfully_with_no_successor(self):
        _, run_id = self.start_run(max_cycles=3, criterion="service is stable")
        self.mock(target_lane="control", criterion_status="not_met",
                  criterion_confidence=0.9)
        self.daemon().run_once()
        self.complete_cycle()

        self.mock(target_lane="control", criterion_status="met",
                  criterion_confidence=0.95,
                  criterion_evidence="PID stable across both polls")
        self.daemon().run_once()

        st = self.state(run_id)
        self.assertEqual(st["stop_reason"], tp.STOP_CRITERION_MET)
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["cycles_completed"], 1)
        self.assertEqual(sum(1 for m in self.msgs().values() if m.kind == "ticket"), 1,
                         "criterion met must issue no successor ticket")

    def test_criterion_unknown_escalates_and_stops(self):
        _, run_id = self.start_run(max_cycles=3, criterion="service is stable")
        self.mock(target_lane="control", criterion_status="unknown",
                  criterion_confidence=0.9)
        self.daemon().run_once()
        st = self.state(run_id)
        self.assertEqual(st["stop_reason"], tp.STOP_CRITERION_UNKNOWN)
        self.assertEqual(sum(1 for m in self.msgs().values()
                             if m.kind == "escalation"), 1)
        self.assertEqual(sum(1 for m in self.msgs().values() if m.kind == "ticket"), 0)

    def test_low_criterion_confidence_escalates(self):
        _, run_id = self.start_run(max_cycles=3, criterion="service is stable")
        self.mock(target_lane="control", criterion_status="met",
                  criterion_confidence=0.4)
        self.daemon().run_once()
        st = self.state(run_id)
        self.assertEqual(st["stop_reason"], tp.STOP_CRITERION_UNKNOWN)
        self.assertNotEqual(st["status"], "completed")

    def test_hard_maximum_overrides_a_not_met_criterion(self):
        _, run_id = self.start_run(max_cycles=1, criterion="service is stable")
        self.mock(target_lane="control", criterion_status="not_met",
                  criterion_confidence=0.99)
        self.daemon().run_once()
        self.complete_cycle()
        self.daemon().run_once()
        st = self.state(run_id)
        self.assertEqual(st["stop_reason"], tp.STOP_MAX_CYCLES)
        self.assertEqual(st["status"], "stopped",
                         "exhaustion is never reported as success")


class TestTerminationPaths(TelephoneBusTestCase):
    def test_review_only_stops_the_run(self):
        _, run_id = self.start_run(max_cycles=3)
        self.mock(target_lane=None, ticket_markdown=None, ticket_title=None)
        self.daemon().run_once()
        st = self.state(run_id)
        self.assertEqual(st["stop_reason"], tp.STOP_REVIEW_ONLY)
        self.assertEqual(st["status"], "stopped")

    def test_escalation_stops_the_run(self):
        _, run_id = self.start_run(max_cycles=3)
        self.mock(target_lane="control", confidence=0.2)
        self.daemon().run_once()
        self.assertEqual(self.state(run_id)["stop_reason"], tp.STOP_ESCALATED)

    def test_hard_gate_in_the_review_stops_the_run(self):
        _, run_id = self.start_run(max_cycles=3)
        self.mock(target_lane="control", confidence=0.99,
                  ticket_markdown="## Steps\n1. Promote the checkpoint.\n")
        self.daemon().run_once()
        self.assertEqual(self.state(run_id)["stop_reason"], tp.STOP_ESCALATED)

    def test_blocked_ticket_leaves_the_run_without_a_new_cycle(self):
        _, run_id = self.start_run(max_cycles=3)
        self.mock(target_lane="control")
        self.daemon().run_once()
        msgs = self.msgs()
        t = [m for m in msgs.values() if m.kind == "ticket"][0]
        self.ctl("claim", t.id, "--agent", "control")
        self.ctl("block", t.id, "--reason", "environment unavailable")
        before = sum(1 for m in self.msgs().values() if m.kind == "ticket")
        self.daemon().run_once()
        self.assertEqual(sum(1 for m in self.msgs().values() if m.kind == "ticket"),
                         before, "a blocked ticket must not spawn another cycle")
        self.assertEqual(self.state(run_id)["cycles_completed"], 0)

    def test_manual_stop_prevents_new_tickets_but_allows_finishing_work(self):
        _, run_id = self.start_run(max_cycles=3)
        self.mock(target_lane="control")
        self.daemon().run_once()
        msgs = self.msgs()
        t = [m for m in msgs.values() if m.kind == "ticket"][0]
        self.ctl("claim", t.id, "--agent", "control")

        self.ctl("telephone", "stop", "--lane", "control", "--reason", "enough")
        st = self.state(run_id)
        self.assertEqual(st["status"], "stopped")
        self.assertEqual(st["stop_reason"], tp.STOP_MANUAL)

        # already-claimed work can still be completed
        local = self.write_local_report("after-stop.md")
        out = json.loads(self.ctl("publish-report", "--lane", "control",
                                  "--unit", "12U-SYNTH", "--report", str(local),
                                  "--in-reply-to", t.id))
        self.ctl("complete", t.id, "--report-id", out["report_id"])
        self.assertEqual(ml.ticket_state(t.id, self.msgs())["status"], "completed")

        # ...but no successor is ever issued
        before = sum(1 for m in self.msgs().values() if m.kind == "ticket")
        self.daemon().run_once()
        self.assertEqual(sum(1 for m in self.msgs().values() if m.kind == "ticket"),
                         before)

    def test_spend_guard_block_stops_before_any_review(self):
        _, run_id = self.start_run(max_cycles=3)
        os.environ.pop("BRITTLE_REVIEWER_MOCK", None)   # real path -> guard applies
        cfg = ml.load_config(self.cfg_path)
        cfg["spending"]["max_calls_per_day"] = 0        # nothing may be spent
        daemon = rd.ReviewerDaemon(cfg)
        stats = daemon.run_once()
        self.assertTrue(stats.get("spend_blocked"), stats)
        self.assertEqual(self.state(run_id)["cycles_completed"], 0)
        self.assertEqual(sum(1 for m in self.msgs().values() if m.kind == "ticket"), 0)

    def test_no_watcher_auto_claim_during_a_run(self):
        import ticket_watcher

        _, run_id = self.start_run(max_cycles=3)
        self.mock(target_lane="control")
        self.daemon().run_once()
        cfg = ml.load_config(self.cfg_path)
        for _ in range(3):
            ticket_watcher.run_once(cfg, "control", self.tmp / "lane.json")
        claims = [m for m in self.msgs().values()
                  if m.get("receipt_type") in ("claim", "renew", "reclaim")]
        self.assertEqual(claims, [], "the watcher must never claim automatically")


class TestRestartRecovery(TelephoneBusTestCase):
    def test_run_resumes_across_a_fresh_daemon(self):
        _, run_id = self.start_run(max_cycles=3)
        self.mock(target_lane="control")
        self.daemon().run_once()
        self.complete_cycle()
        self.daemon().run_once()
        self.assertEqual(self.state(run_id)["cycles_completed"], 1)

        # brand-new daemon objects == service restarts; state comes from the bus
        for _ in range(3):
            rd.ReviewerDaemon(ml.load_config(self.cfg_path)).run_once()
        st = self.state(run_id)
        self.assertEqual(st["cycles_completed"], 1, "restarts must not add cycles")
        self.assertEqual(st["status"], "active")

    def test_restart_adds_no_duplicate_messages(self):
        _, run_id = self.start_run(max_cycles=3)
        self.mock(target_lane="control")
        self.daemon().run_once()
        head = self.repo.head()
        counts = ml.build_index(self.msgs())["counts"]
        for _ in range(3):
            rd.ReviewerDaemon(ml.load_config(self.cfg_path)).run_once()
        self.assertEqual(self.repo.head(), head)
        self.assertEqual(ml.build_index(self.msgs())["counts"], counts)


class TestStatusOutput(TelephoneBusTestCase):
    def test_status_is_read_only_and_complete(self):
        _, run_id = self.start_run(max_cycles=3, criterion="service is stable")
        self.mock(target_lane="control", criterion_status="not_met",
                  criterion_confidence=0.9)
        self.daemon().run_once()
        head = self.repo.head()

        rows = json.loads(self.ctl("telephone", "status", "--lane", "control"))
        st = rows[0]
        for key in ("run_id", "lane", "status", "cycles_completed", "max_cycles",
                    "criterion", "criterion_status", "current_ticket",
                    "current_claim", "blocker", "api_calls", "spend_usd",
                    "stop_reason"):
            self.assertIn(key, st, f"status must report {key}")
        self.assertEqual(st["run_id"], run_id)
        self.assertEqual(st["max_cycles"], 3)
        self.assertEqual(st["status"], "active")
        self.assertIsNotNone(st["current_ticket"])
        self.assertEqual(self.repo.head(), head, "status must not mutate the bus")

    def test_human_status_shows_spend_and_stop_reason(self):
        _, run_id = self.start_run(max_cycles=2)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            messagesctl.main(["--config", str(self.cfg_path),
                              "telephone", "status", "--lane", "control"])
        text = buf.getvalue()
        for token in ("run", "cycles", "criterion", "spend", "stop reason",
                      "spend guard"):
            self.assertIn(token, text.lower())

    def test_criterion_confidence_survives_the_stop_receipt(self):
        _, run_id = self.start_run(max_cycles=3, criterion="service is stable")
        self.mock(target_lane="control", criterion_status="met",
                  criterion_confidence=0.9)
        self.daemon().run_once()
        st = self.state(run_id)
        self.assertEqual(st["criterion_status"], "met")
        self.assertEqual(st["criterion_confidence"], 0.9,
                         "a later receipt without a confidence must not erase it")

    def test_status_after_stop_reports_the_exact_reason(self):
        _, run_id = self.start_run(max_cycles=2)
        self.ctl("telephone", "stop", "--lane", "control", "--reason", "done")
        rows = json.loads(self.ctl("telephone", "status", "--run", run_id))
        self.assertEqual(rows[0]["stop_reason"], tp.STOP_MANUAL)
        self.assertIn("manual", rows[0]["stop_reason_detail"])


if __name__ == "__main__":
    unittest.main()
