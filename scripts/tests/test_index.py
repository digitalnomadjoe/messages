"""State indexes: disposable, deterministic, rebuildable from history."""

from __future__ import annotations

import contextlib
import io
import json
import unittest

# harness must be imported first: it puts scripts/ on sys.path
from harness import BusTestCase, ml
import messagesctl


class TestIndex(BusTestCase):
    def setUp(self):
        super().setUp()
        self.cfg_path = self.tmp / "config.toml"
        self.cfg_path.write_text(
            f'[repo]\npath = "{self.repo_path}"\nbrittle_path = "{self.brittle}"\n',
            encoding="utf-8")

    def run_ctl(self, *argv, expect=0):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = messagesctl.main(["--config", str(self.cfg_path), "--json", *argv])
        self.assertEqual(rc, expect, buf.getvalue())
        return buf.getvalue()

    def index(self) -> dict:
        return json.loads((self.repo_path / ml.INDEX_PATH).read_text(encoding="utf-8"))

    def test_index_is_deterministic(self):
        self.publish_ticket()
        self.publish_report()
        first = ml.index_text(ml.build_index(self.msgs()))
        second = ml.index_text(ml.build_index(self.msgs()))
        self.assertEqual(first, second,
                         "the index must be a pure function of history (no timestamps)")

    def test_index_tracks_open_tickets_and_claims(self):
        tid = self.publish_ticket()
        self.assertEqual(self.index()["open_ticket_by_lane"]["locomotion"], tid)

        self.run_ctl("claim", tid, "--agent", "locomotion")
        idx = self.index()
        self.assertNotIn("locomotion", idx["open_ticket_by_lane"])
        self.assertEqual(idx["active_claims"][tid]["agent"], "locomotion")

    def test_index_tracks_latest_report_and_reviewer_cursor(self):
        rid = self.publish_report()
        self.assertEqual(self.index()["latest_report_by_lane"]["locomotion"], rid)
        self.assertIsNone(self.index()["reviewer_cursor"])

        fm = ml.base_frontmatter("receipt", sender="reviewer", to="locomotion",
                                 lane="reviewer", unit="12U-SYNTH",
                                 status="acknowledged", in_reply_to=rid)
        fm.update({"receipt_type": "reviewer_ack", "agent": "reviewer", "report_id": rid})
        ml.publish(self.repo, {f"{ml.DIR_RECEIPTS}/{fm['id']}.md":
                               ml.render_message(fm, "# ack\n")}, "ack", cfg=self.cfg)
        self.assertEqual(self.index()["reviewer_cursor"], rid)

    def test_index_tracks_open_escalations(self):
        fm = ml.base_frontmatter("escalation", sender="locomotion", to="joe",
                                 lane="locomotion", unit="U", status="open",
                                 requires_owner=True)
        fm["title"] = "q"
        ml.publish(self.repo, {f"{ml.DIR_ESC_OPEN}/{fm['id']}.md":
                               ml.render_message(fm, "# q\n")}, "esc", cfg=self.cfg)
        self.assertEqual(self.index()["open_escalations"], [fm["id"]])

    def test_index_counts_every_kind(self):
        self.publish_ticket()
        self.publish_report()
        counts = self.index()["counts"]
        self.assertEqual(counts["ticket"], 1)
        self.assertEqual(counts["report"], 1)
        self.assertEqual(set(counts), set(ml.KINDS))

    def test_deleted_index_is_fully_rebuilt_from_history(self):
        tid = self.publish_ticket()
        rid = self.publish_report()
        self.run_ctl("claim", tid, "--agent", "locomotion")
        expected = self.index()

        (self.repo_path / ml.INDEX_PATH).unlink()
        self.run_ctl("rebuild-index")

        self.assertEqual(self.index(), expected,
                         "the index is a disposable cache; history is authoritative")
        self.assertEqual(self.index()["latest_report_by_lane"]["locomotion"], rid)

    def test_corrupted_index_is_repaired(self):
        self.publish_ticket()
        (self.repo_path / ml.INDEX_PATH).write_text('{"schema": 1}\n', encoding="utf-8")
        problems = ml.validate_repo(self.repo_path)
        self.assertTrue(any("stale" in p for p in problems), problems)

        self.run_ctl("rebuild-index")
        self.assertEqual(ml.validate_repo(self.repo_path), [])

    def test_stale_index_is_a_validation_failure(self):
        self.publish_ticket()
        idx = self.index()
        idx["open_ticket_by_lane"] = {}
        (self.repo_path / ml.INDEX_PATH).write_text(
            json.dumps(idx, indent=2) + "\n", encoding="utf-8")
        problems = ml.validate_repo(self.repo_path)
        self.assertTrue(any(ml.INDEX_PATH in p for p in problems), problems)


class TestStatusAndTail(BusTestCase):
    def setUp(self):
        super().setUp()
        self.cfg_path = self.tmp / "config.toml"
        self.cfg_path.write_text(
            f'[repo]\npath = "{self.repo_path}"\nbrittle_path = "{self.brittle}"\n',
            encoding="utf-8")

    def _run(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            messagesctl.main(["--config", str(self.cfg_path), "--json", *argv])
        return json.loads(buf.getvalue())

    def test_status_reports_the_operational_picture(self):
        tid = self.publish_ticket()
        rid = self.publish_report()
        out = self._run("status")
        self.assertEqual(out["autonomy"], "ACTIVE")
        self.assertEqual(out["open_ticket_by_lane"]["locomotion"], tid)
        self.assertIn(rid, out["reports_awaiting_review"])
        self.assertFalse(out["deferred_push"])

    def test_status_shows_paused(self):
        (ml.state_dir() / "PAUSED").write_text("x", encoding="utf-8")
        self.assertEqual(self._run("status")["autonomy"], "PAUSED")

    def test_tail_returns_the_most_recent_messages(self):
        tid = self.publish_ticket()
        rid = self.publish_report()
        rows = self._run("tail", "-n", "5")
        self.assertEqual([r["id"] for r in rows][-2:], [tid, rid])
        self.assertEqual([r["kind"] for r in rows][-2:], ["ticket", "report"])


if __name__ == "__main__":
    unittest.main()
