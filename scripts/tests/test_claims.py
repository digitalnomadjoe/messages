"""Ticket leases, lane separation, duplicate-claim prevention, reclamation."""

from __future__ import annotations

import contextlib
import datetime as _dt
import io
import json
import unittest

# harness must be imported first: it puts scripts/ on sys.path
from harness import BusTestCase, ml
import messagesctl


@contextlib.contextmanager
def frozen_at(moment: _dt.datetime):
    original = ml.utc_now
    ml.utc_now = lambda: moment
    try:
        yield
    finally:
        ml.utc_now = original


class ClaimTestCase(BusTestCase):
    def setUp(self):
        super().setUp()
        self.cfg_path = self.tmp / "config.toml"
        self.cfg_path.write_text(
            f'[repo]\npath = "{self.repo_path}"\nbrittle_path = "{self.brittle}"\n'
            f"[claims]\nlease_seconds = 2700\n", encoding="utf-8")

    def run_ctl(self, *argv, expect=0):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = messagesctl.main(["--config", str(self.cfg_path), "--json", *argv])
        self.assertEqual(rc, expect, buf.getvalue())
        return buf.getvalue()


class TestLaneSeparation(ClaimTestCase):
    def test_control_agent_cannot_claim_a_locomotion_ticket(self):
        tid = self.publish_ticket(lane="locomotion")
        out = self.run_ctl("claim", tid, "--agent", "control", expect=2)
        self.assertIn("lane violation", out)
        state = ml.ticket_state(tid, self.msgs())
        self.assertEqual(state["status"], "open", "a rejected claim must change nothing")

    def test_locomotion_agent_cannot_claim_a_control_ticket(self):
        tid = self.publish_ticket(lane="control")
        out = self.run_ctl("claim", tid, "--agent", "locomotion", expect=2)
        self.assertIn("lane violation", out)

    def test_next_ticket_never_offers_the_other_lane(self):
        self.publish_ticket(lane="control", title="control work")
        out = json.loads(self.run_ctl("next-ticket", "--lane", "locomotion"))
        self.assertIsNone(out["ticket"])

    def test_validator_rejects_a_handwritten_cross_lane_claim(self):
        tid = self.publish_ticket(lane="locomotion")
        now = ml.utc_now()
        fm = ml.base_frontmatter("receipt", sender="control", to="reviewer",
                                 lane="control", unit="12U-SYNTH", status="claimed",
                                 in_reply_to=tid)
        fm.update({"receipt_type": "claim", "agent": "control", "ticket_id": tid,
                   "claimed_at": ml.iso(now),
                   "lease_expires_at": ml.iso(now + _dt.timedelta(hours=1)),
                   "brittle_commit": "abcdef1"})
        path = self.repo_path / ml.DIR_RECEIPTS / f"{fm['id']}.md"
        path.write_text(ml.render_message(fm, "# sneaky\n"), encoding="utf-8")
        problems = ml.validate_repo(self.repo_path)
        self.assertTrue(any("may not claim" in p for p in problems), problems)


class TestLeases(ClaimTestCase):
    def test_claim_records_identity_lease_and_commit(self):
        tid = self.publish_ticket()
        out = json.loads(self.run_ctl("claim", tid, "--agent", "locomotion"))
        receipt = self.msgs()[out["receipt_id"]]

        self.assertEqual(receipt.get("receipt_type"), "claim")
        self.assertEqual(receipt.get("agent"), "locomotion")
        self.assertEqual(receipt.get("ticket_id"), tid)
        self.assertIsNotNone(receipt.get("claimed_at"))
        self.assertIsNotNone(receipt.get("lease_expires_at"))
        self.assertEqual(receipt.get("brittle_commit"), ml.brittle_commit(self.brittle))
        self.assertEqual(ml.ticket_state(tid, self.msgs())["status"], "claimed")

    def test_second_agent_cannot_claim_an_active_lease(self):
        tid = self.publish_ticket()
        self.run_ctl("claim", tid, "--agent", "locomotion")
        out = self.run_ctl("claim", tid, "--agent", "locomotion", expect=2)
        self.assertIn("already claimed", out)
        claims = [r for r in ml.ticket_state(tid, self.msgs())["receipts"]
                  if r.get("receipt_type") == "claim"]
        self.assertEqual(len(claims), 1, "only one active claim may be accepted")

    def test_renewal_extends_the_lease(self):
        tid = self.publish_ticket()
        first = json.loads(self.run_ctl("claim", tid, "--agent", "locomotion"))
        later = ml.parse_iso(first["lease_expires_at"]) + _dt.timedelta(minutes=5)
        with frozen_at(later - _dt.timedelta(minutes=10)):
            second = json.loads(
                self.run_ctl("claim", tid, "--agent", "locomotion", "--renew"))
        self.assertEqual(second["receipt_type"], "renew")
        self.assertGreater(ml.parse_iso(second["lease_expires_at"]),
                           ml.parse_iso(first["lease_expires_at"]))
        self.assertEqual(ml.ticket_state(tid, self.msgs())["status"], "claimed")

    def test_expired_lease_becomes_reclaimable(self):
        tid = self.publish_ticket()
        first = json.loads(self.run_ctl("claim", tid, "--agent", "locomotion"))
        after = ml.parse_iso(first["lease_expires_at"]) + _dt.timedelta(minutes=1)

        with frozen_at(after):
            self.assertEqual(ml.ticket_state(tid, self.msgs())["status"], "open")
            out = json.loads(self.run_ctl("claim", tid, "--agent", "locomotion"))
        self.assertEqual(out["receipt_type"], "reclaim")
        self.assertEqual(ml.ticket_state(tid, self.msgs())["status"], "claimed")

    def test_completed_ticket_cannot_be_reclaimed_after_expiry(self):
        tid = self.publish_ticket()
        first = json.loads(self.run_ctl("claim", tid, "--agent", "locomotion"))
        rid = self.publish_report()
        self.run_ctl("complete", tid, "--report-id", rid)

        after = ml.parse_iso(first["lease_expires_at"]) + _dt.timedelta(hours=1)
        with frozen_at(after):
            state = ml.ticket_state(tid, self.msgs())
            self.assertEqual(state["status"], "completed")
            out = self.run_ctl("claim", tid, "--agent", "locomotion", expect=2)
        self.assertIn("already completed", out)

    def test_newer_renewal_defeats_a_stale_expiry(self):
        tid = self.publish_ticket()
        first = json.loads(self.run_ctl("claim", tid, "--agent", "locomotion"))
        mid = ml.parse_iso(first["lease_expires_at"]) - _dt.timedelta(minutes=1)
        with frozen_at(mid):
            self.run_ctl("claim", tid, "--agent", "locomotion", "--renew")
        just_after_first = ml.parse_iso(first["lease_expires_at"]) + _dt.timedelta(seconds=30)
        with frozen_at(just_after_first):
            self.assertEqual(ml.ticket_state(tid, self.msgs())["status"], "claimed")


class TestTerminalStates(ClaimTestCase):
    def test_completion_references_ticket_report_and_provenance(self):
        tid = self.publish_ticket()
        self.run_ctl("claim", tid, "--agent", "locomotion")
        rid = self.publish_report()
        out = json.loads(self.run_ctl("complete", tid, "--report-id", rid))

        receipt = self.msgs()[out["receipt_id"]]
        report = self.msgs()[rid]
        self.assertEqual(receipt.get("in_reply_to"), tid)
        self.assertEqual(receipt.get("report_id"), rid)
        self.assertEqual(receipt.get("local_source_path"),
                         report.get("local_source_path"))
        self.assertEqual(receipt.get("local_source_sha256"),
                         report.get("local_source_sha256"))
        self.assertEqual(receipt.get("brittle_commit"), ml.brittle_commit(self.brittle))
        self.assertEqual(ml.ticket_state(tid, self.msgs())["status"], "completed")

    def test_completion_requires_a_real_report_id(self):
        tid = self.publish_ticket()
        out = self.run_ctl("complete", tid, "--report-id", ml.new_id(), expect=2)
        self.assertIn("not a known report", out)

    def test_block_marks_the_ticket_blocked(self):
        tid = self.publish_ticket()
        self.run_ctl("claim", tid, "--agent", "locomotion")
        self.run_ctl("block", tid, "--reason", "inspector never reported episode start")
        state = ml.ticket_state(tid, self.msgs())
        self.assertEqual(state["status"], "blocked")
        self.assertIn("inspector never reported", state["block"].body)

    def test_supersede_marks_the_ticket_superseded(self):
        tid = self.publish_ticket()
        fm = ml.base_frontmatter("ticket", sender="reviewer", to="locomotion",
                                 lane="locomotion", unit="12U-SYNTH", status="open",
                                 supersedes=tid)
        fm["title"] = "replacement"
        ml.publish(self.repo,
                   {f"{ml.DIR_TICKETS}/locomotion/{fm['id']}.md":
                    ml.render_message(fm, "# replacement\n")},
                   "supersede", cfg=self.cfg)
        self.assertEqual(ml.ticket_state(tid, self.msgs())["status"], "superseded")
        self.assertEqual(ml.ticket_state(tid, self.msgs())["superseded_by"], fm["id"])


if __name__ == "__main__":
    unittest.main()
