"""Report mirroring: local authority, SHA-256 provenance, redaction, truncation."""

from __future__ import annotations

import json
import unittest

# harness must be imported first: it puts scripts/ on sys.path
from harness import SYNTHETIC_REPORT, BusTestCase, ml
import messagesctl


class TestMirroring(BusTestCase):
    def _config_file(self, private=None):
        path = self.tmp / "config.toml"
        patterns = json.dumps(private or [])
        path.write_text(
            f'[repo]\npath = "{self.repo_path}"\nbrittle_path = "{self.brittle}"\n'
            f"[safety]\nprivate_patterns = {patterns}\n",
            encoding="utf-8")
        return str(path)

    def _publish(self, local, private=None, extra=()):
        argv = ["--config", self._config_file(private), "--json", "publish-report",
                "--lane", "locomotion", "--unit", "12U-SYNTH",
                "--report", str(local), *extra]
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = messagesctl.main(argv)
        self.assertEqual(rc, 0, buf.getvalue())
        return json.loads(buf.getvalue())

    def test_local_report_is_untouched_and_authoritative(self):
        local = self.write_local_report()
        before_bytes = local.read_bytes()
        before_sha = ml.sha256_file(local)
        before_mtime = local.stat().st_mtime_ns

        out = self._publish(local)

        self.assertEqual(local.read_bytes(), before_bytes, "source must be byte-identical")
        self.assertEqual(ml.sha256_file(local), before_sha)
        self.assertEqual(local.stat().st_mtime_ns, before_mtime, "source must not be rewritten")
        self.assertEqual(out["local_source_sha256"], before_sha)
        self.assertEqual(out["local_source_path"], str(local))

    def test_mirror_records_full_provenance(self):
        local = self.write_local_report()
        out = self._publish(local)
        msg = self.msgs()[out["report_id"]]

        self.assertEqual(msg.get("local_source_path"), str(local))
        self.assertEqual(msg.get("local_source_sha256"), ml.sha256_file(local))
        self.assertEqual(msg.get("source_commit"), ml.brittle_commit(self.brittle))
        self.assertEqual(msg.get("lane"), "locomotion")
        self.assertEqual(msg.get("unit"), "12U-SYNTH")
        self.assertRegex(str(msg.get("created_at")), ml.TS_RE)

    def test_mirror_carries_the_report_text(self):
        local = self.write_local_report()
        out = self._publish(local)
        body = self.msgs()[out["report_id"]].body
        self.assertIn(ml.MIRROR_MARKER, body)
        self.assertIn("survival 812 steps", body)

    def test_private_pattern_publishes_only_a_redacted_summary(self):
        secret_text = SYNTHETIC_REPORT + "\nINTERNAL-ONLY-42 do not publish this line\n"
        local = self.write_local_report("PRIVATE_REPORT.md", secret_text)

        out = self._publish(local, private=[r"INTERNAL-ONLY-\d+"],
                            extra=["--summary", "Withheld pending review."])

        self.assertTrue(out["redacted"])
        body = self.msgs()[out["report_id"]].body
        self.assertNotIn("INTERNAL-ONLY-42", body)
        self.assertIn("Redacted mirror", body)
        self.assertIn("Withheld pending review.", body)
        self.assertIn(ml.sha256_file(local), body)
        # the full report is still local and intact
        self.assertIn("INTERNAL-ONLY-42", local.read_text(encoding="utf-8"))

    def test_oversized_report_is_truncated_not_rejected(self):
        big = SYNTHETIC_REPORT + ("\nfiller line for bulk\n" * 20000)
        local = self.write_local_report("BIG_REPORT.md", big)
        out = self._publish(local)
        self.assertTrue(out["truncated"])
        msg = self.msgs()[out["report_id"]]
        self.assertIn("mirror truncated", msg.body)
        self.assertEqual(msg.get("local_source_sha256"), ml.sha256_file(local))
        self.assertLessEqual(
            len((self.repo_path / msg.rel).read_bytes()), ml.MAX_MESSAGE_BYTES)

    def test_checksum_mismatch_is_detected_after_the_fact(self):
        from reviewer_daemon import ReviewerDaemon

        local = self.write_local_report()
        out = self._publish(local)
        local.write_text(SYNTHETIC_REPORT + "\nedited after mirroring\n",
                         encoding="utf-8")

        daemon = ReviewerDaemon(self.cfg)
        notes = daemon.verify_report(self.msgs()[out["report_id"]])
        self.assertTrue(any(n.startswith("CHECKSUM MISMATCH") for n in notes), notes)

    def test_checksum_verified_when_source_is_intact(self):
        from reviewer_daemon import ReviewerDaemon

        local = self.write_local_report()
        out = self._publish(local)
        daemon = ReviewerDaemon(self.cfg)
        notes = daemon.verify_report(self.msgs()[out["report_id"]])
        self.assertIn("local source sha256 verified", notes)

    def test_non_markdown_report_rejected(self):
        import contextlib
        import io

        bad = self.brittle / "rgl" / "reports" / "notes.txt"
        bad.write_text("x", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = messagesctl.main(["--config", self._config_file(), "publish-report",
                                   "--lane", "locomotion", "--unit", "U",
                                   "--report", str(bad)])
        self.assertEqual(rc, 2)
        self.assertIn("only Markdown reports", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
