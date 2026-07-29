"""Reviewer daemon: structured output, thresholds, hard gates, idempotency."""

from __future__ import annotations

import json
import os
import unittest

# harness must be imported first: it puts scripts/ on sys.path
from harness import BusTestCase, ml, mock_response, raw_mock
import reviewer_daemon as rd
import messagelib


class ReviewerTestCase(BusTestCase):
    def daemon(self, **cfg_over):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["reviewer"].update(cfg_over)
        return rd.ReviewerDaemon(cfg)

    def use_mock(self, **overrides):
        os.environ["BRITTLE_REVIEWER_MOCK"] = mock_response(self.tmp, **overrides)

    def use_raw_mock(self, raw: str):
        os.environ["BRITTLE_REVIEWER_MOCK"] = raw_mock(self.tmp, raw)

    def kinds(self):
        out = {}
        for m in self.msgs().values():
            out.setdefault(m.kind, []).append(m)
        return out


class TestStructuredOutput(unittest.TestCase):
    def test_valid_response_accepted(self):
        payload = {
            "summary": "s", "target_lane": "locomotion", "next_action": "n",
            "ticket_title": "t", "ticket_markdown": "m", "requires_owner": False,
            "owner_question": None, "confidence": 0.9, "reasoning_summary": "r",
            "criterion_status": None, "criterion_evidence": None,
            "criterion_confidence": None,
        }
        self.assertEqual(rd.validate_review(json.dumps(payload))["confidence"], 0.9)

    def test_non_json_rejected(self):
        with self.assertRaisesRegex(rd.ReviewerError, "not valid JSON"):
            rd.validate_review("I think the policy looks fine, honestly.")

    def test_missing_field_rejected(self):
        with self.assertRaisesRegex(rd.ReviewerError, "missing field"):
            rd.validate_review(json.dumps({"summary": "s"}))

    def test_unknown_field_rejected(self):
        payload = {
            "summary": "s", "target_lane": None, "next_action": "n",
            "ticket_title": None, "ticket_markdown": None, "requires_owner": True,
            "owner_question": "q", "confidence": 0.5, "reasoning_summary": "r",
            "criterion_status": None, "criterion_evidence": None,
            "criterion_confidence": None,
            "tool_calls": [{"exec": "rm -rf /"}],
        }
        with self.assertRaisesRegex(rd.ReviewerError, "unknown field"):
            rd.validate_review(json.dumps(payload))

    def test_bad_lane_rejected(self):
        payload = {
            "summary": "s", "target_lane": "perception", "next_action": "n",
            "ticket_title": None, "ticket_markdown": None, "requires_owner": False,
            "owner_question": None, "confidence": 0.9, "reasoning_summary": "r",
            "criterion_status": None, "criterion_evidence": None,
            "criterion_confidence": None,
        }
        with self.assertRaisesRegex(rd.ReviewerError, "bad target_lane"):
            rd.validate_review(json.dumps(payload))

    def test_out_of_range_confidence_rejected(self):
        payload = {
            "summary": "s", "target_lane": None, "next_action": "n",
            "ticket_title": None, "ticket_markdown": None, "requires_owner": False,
            "owner_question": None, "confidence": 4.2, "reasoning_summary": "r",
            "criterion_status": None, "criterion_evidence": None,
            "criterion_confidence": None,
        }
        with self.assertRaisesRegex(rd.ReviewerError, "out of range"):
            rd.validate_review(json.dumps(payload))

    def test_schema_is_strict_and_total(self):
        self.assertFalse(rd.REVIEW_SCHEMA["additionalProperties"])
        self.assertEqual(sorted(rd.REVIEW_SCHEMA["required"]),
                         sorted(rd.REVIEW_SCHEMA["properties"]))


class TestCriterionFields(unittest.TestCase):
    """The Telephone criterion trio is part of the strict contract."""

    def payload(self, **over):
        base = {
            "summary": "s", "target_lane": "control", "next_action": "n",
            "ticket_title": "t", "ticket_markdown": "m", "requires_owner": False,
            "owner_question": None, "confidence": 0.9, "reasoning_summary": "r",
            "criterion_status": None, "criterion_evidence": None,
            "criterion_confidence": None,
        }
        base.update(over)
        return json.dumps(base)

    def test_criterion_fields_are_required(self):
        import json as _j
        d = _j.loads(self.payload())
        del d["criterion_status"]
        with self.assertRaisesRegex(rd.ReviewerError, "missing field"):
            rd.validate_review(_j.dumps(d))

    def test_valid_criterion_values_accepted(self):
        for status in ("met", "not_met", "unknown", None):
            out = rd.validate_review(self.payload(criterion_status=status,
                                                  criterion_confidence=0.9))
            self.assertEqual(out["criterion_status"], status)

    def test_bad_criterion_status_rejected(self):
        with self.assertRaisesRegex(rd.ReviewerError, "bad criterion_status"):
            rd.validate_review(self.payload(criterion_status="probably"))

    def test_criterion_confidence_range_enforced(self):
        with self.assertRaisesRegex(rd.ReviewerError, "criterion_confidence out of range"):
            rd.validate_review(self.payload(criterion_confidence=1.7))

    def test_criterion_evidence_must_be_text(self):
        with self.assertRaisesRegex(rd.ReviewerError, "criterion_evidence"):
            rd.validate_review(self.payload(criterion_evidence=42))


class TestHardGates(unittest.TestCase):
    def test_gate_language_is_detected(self):
        cases = {
            "Promote the checkpoint to main": "promotion",
            "Overwrite the canonical reference trajectory": "canonical-mutation",
            "Update latest to point at the new policy": "latest-pointer",
            "Refresh the policy card": "policy-card",
            "Copy the crown checkpoint": "crown",
            "Bypass the survival gate": "gate-override",
            "Needs Joe's approval first": "owner-authorization",
            "Extend the observation space with contact dims": "interface-change",
            "Disable the Guard check": "guard-mutation",
        }
        for text, expected in cases.items():
            self.assertIn(expected, rd.hard_gate_hits(text), text)

    def test_ordinary_work_trips_nothing(self):
        self.assertEqual(
            rd.hard_gate_hits("Re-run the smoke at three seeds and report the spread."),
            [])


class TestReviewPasses(ReviewerTestCase):
    def test_high_confidence_review_issues_one_ticket_in_one_commit(self):
        rid = self.publish_report()
        self.use_mock()
        before = len(self.repo.git("rev-list", "HEAD").split())

        stats = self.daemon().run_once()

        self.assertEqual(stats["reviewed"], 1, stats)
        after = len(self.repo.git("rev-list", "HEAD").split())
        self.assertEqual(after - before, 1,
                         "review + ticket + acknowledgement must be one commit")

        kinds = self.kinds()
        self.assertEqual(len(kinds["review"]), 1)
        self.assertEqual(len(kinds["ticket"]), 1)
        self.assertTrue(ml.reviewer_acked(rid, self.msgs()))
        self.assertEqual(kinds["ticket"][0].get("lane"), "locomotion")
        self.assertEqual(kinds["review"][0].get("review_of"), rid)
        self.assertValid()

    def test_review_records_model_and_prompt_hash(self):
        self.publish_report()
        self.use_mock()
        self.daemon().run_once()
        review = self.kinds()["review"][0]
        self.assertEqual(review.get("reviewer_model"), "test-model")
        self.assertRegex(str(review.get("prompt_sha256")), r"^[0-9a-f]{64}$")

    def test_restart_creates_no_duplicate_review_or_ticket(self):
        self.publish_report()
        self.use_mock()
        daemon = self.daemon()
        daemon.run_once()
        head_after_first = self.repo.head()

        # a fresh daemon object == a service restart
        stats = self.daemon().run_once()

        self.assertEqual(stats["reviewed"], 0, stats)
        self.assertEqual(self.repo.head(), head_after_first, "no new commits")
        self.assertEqual(len(self.kinds()["review"]), 1)
        self.assertEqual(len(self.kinds()["ticket"]), 1)

    def test_already_reviewed_report_is_skipped(self):
        self.publish_report()
        self.use_mock()
        self.daemon().run_once()
        self.assertEqual(self.daemon().pending_reports(self.msgs()), [])

    def test_malformed_response_publishes_nothing(self):
        rid = self.publish_report()
        self.use_raw_mock("sure thing boss, ship it")
        before = self.repo.head()

        stats = self.daemon().run_once()

        self.assertEqual(self.repo.head(), before, "nothing may be published")
        self.assertEqual(stats["reviewed"], 0)
        self.assertTrue(stats["errors"])
        self.assertFalse(ml.reviewer_acked(rid, self.msgs()),
                         "the report must stay queued for retry")

    def test_missing_credentials_fail_closed(self):
        self.publish_report()
        os.environ.pop("BRITTLE_REVIEWER_MOCK", None)
        os.environ.pop("OPENAI_API_KEY", None)
        before = self.repo.head()

        stats = self.daemon().run_once()

        self.assertEqual(self.repo.head(), before)
        self.assertTrue(any("no OpenAI credential" in e for e in stats["errors"]),
                        stats["errors"])

    def test_reports_are_reviewed_oldest_first(self):
        first = self.publish_report()
        second = self.publish_report(
            local=self.write_local_report("SECOND.md"))
        pending = self.daemon().pending_reports(self.msgs())
        self.assertEqual([m.id for m in pending], [first, second])


class TestEscalationPaths(ReviewerTestCase):
    def _assert_escalated(self, reason_fragment: str):
        kinds = self.kinds()
        self.assertNotIn("ticket", kinds, "no executable ticket may be published")
        self.assertEqual(len(kinds["escalation"]), 1)
        esc = kinds["escalation"][0]
        self.assertIs(esc.get("requires_owner"), True)
        self.assertIn(reason_fragment, esc.body)
        self.assertEqual(len(kinds["review"]), 1)
        self.assertIs(kinds["review"][0].get("requires_owner"), True)
        return esc

    def test_low_confidence_escalates_instead_of_ticketing(self):
        self.publish_report()
        self.use_mock(confidence=0.42,
                      owner_question="Which of the two repairs do you want?")
        self.daemon().run_once()
        esc = self._assert_escalated("confidence 0.42 < threshold 0.85")
        self.assertIn("Which of the two repairs", esc.body)

    def test_confidence_exactly_at_threshold_is_allowed(self):
        self.publish_report()
        self.use_mock(confidence=0.85)
        self.daemon().run_once()
        self.assertIn("ticket", self.kinds())

    def test_hard_gate_overrides_high_confidence(self):
        self.publish_report()
        self.use_mock(
            confidence=0.99,
            next_action="Promote the checkpoint and update latest.",
            ticket_markdown="## Steps\n1. Promote to main.\n")
        self.daemon().run_once()
        self._assert_escalated("hard gate")

    def test_reviewer_requires_owner_flag_is_honoured(self):
        self.publish_report()
        self.use_mock(confidence=0.97, requires_owner=True,
                      owner_question="Approve the plant rebaseline?")
        self.daemon().run_once()
        self._assert_escalated("reviewer set requires_owner")

    def test_guard_mutation_may_not_be_routed_to_locomotion(self):
        self.publish_report()
        self.use_mock(confidence=0.95, target_lane="locomotion",
                      ticket_markdown="## Steps\n1. Override the Guard check.\n")
        self.daemon().run_once()
        self._assert_escalated("Guard mutation requested on the locomotion lane")

    def test_no_proposed_action_publishes_review_only(self):
        rid = self.publish_report()
        self.use_mock(confidence=0.95, target_lane=None, ticket_markdown=None,
                      ticket_title=None)
        self.daemon().run_once()
        kinds = self.kinds()
        self.assertNotIn("ticket", kinds)
        self.assertNotIn("escalation", kinds)
        self.assertEqual(len(kinds["review"]), 1)
        self.assertTrue(ml.reviewer_acked(rid, self.msgs()))

    def test_escalation_notice_receipt_records_notification_status(self):
        self.publish_report()
        self.use_mock(confidence=0.10)
        self.daemon().run_once()
        notices = [m for m in self.msgs().values()
                   if m.get("receipt_type") == "escalation_notice"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].get("notification_status"), "unavailable")
        self.assertIn("no [notification].command configured",
                      str(notices[0].get("notification_detail")))

    def test_missing_notice_is_sent_on_restart(self):
        self.publish_report()
        self.use_mock(confidence=0.10)
        daemon = self.daemon()
        daemon.run_once()

        # simulate a crash between publishing the escalation and notifying
        notice = [m for m in self.msgs().values()
                  if m.get("receipt_type") == "escalation_notice"][0]
        self.repo.git("rm", "-q", "--", notice.rel)
        self.repo.git("commit", "-m", "simulate crash before notify", identity=True)

        stats = self.daemon().run_once()
        self.assertEqual(stats["notices"], 1, stats)
        self.assertEqual(len([m for m in self.msgs().values()
                              if m.get("receipt_type") == "escalation_notice"]), 1)


class TestPauseSemantics(ReviewerTestCase):
    def test_paused_daemon_leaves_reports_queued_and_unacknowledged(self):
        rid = self.publish_report()
        self.use_mock()
        (ml.state_dir() / "PAUSED").write_text("paused\n", encoding="utf-8")
        before = self.repo.head()

        stats = self.daemon().run_once()

        self.assertEqual(stats["skipped"], 1, stats)
        self.assertEqual(self.repo.head(), before, "pause must publish nothing")
        self.assertFalse(ml.reviewer_acked(rid, self.msgs()))

    def test_resume_picks_up_the_queued_report_exactly_once(self):
        rid = self.publish_report()
        self.use_mock()
        marker = ml.state_dir() / "PAUSED"
        marker.write_text("paused\n", encoding="utf-8")
        self.daemon().run_once()
        marker.unlink()

        self.daemon().run_once()

        self.assertTrue(ml.reviewer_acked(rid, self.msgs()))
        self.assertEqual(len(self.kinds()["review"]), 1)


class TestDecisionPolicy(unittest.TestCase):
    def _review(self, **over):
        base = {
            "summary": "s", "target_lane": "locomotion", "next_action": "n",
            "ticket_title": "t", "ticket_markdown": "m", "requires_owner": False,
            "owner_question": None, "confidence": 0.9, "reasoning_summary": "r",
        }
        base.update(over)
        return base

    def _cfg(self, threshold=0.85):
        return {"reviewer": {"minimum_confidence": threshold}}

    def test_ticket_when_clean(self):
        self.assertEqual(rd.decide(self._review(), self._cfg())["mode"], "ticket")

    def test_confidence_never_overrides_a_hard_gate(self):
        verdict = rd.decide(
            self._review(confidence=1.0, ticket_markdown="Promote it to main."),
            self._cfg())
        self.assertEqual(verdict["mode"], "escalation")

    def test_threshold_is_configurable(self):
        review = self._review(confidence=0.70)
        self.assertEqual(rd.decide(review, self._cfg(0.85))["mode"], "escalation")
        self.assertEqual(rd.decide(review, self._cfg(0.60))["mode"], "ticket")


if __name__ == "__main__":
    unittest.main()
