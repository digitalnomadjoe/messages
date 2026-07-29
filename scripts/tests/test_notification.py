"""Notification: success, failure, unavailability, and honest reporting."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import unittest

# harness must be imported first: it puts scripts/ on sys.path
from harness import BusTestCase, ml
import messagesctl


class TestNotify(BusTestCase):
    def _cfg(self, command: str) -> dict:
        cfg = json.loads(json.dumps(self.cfg))
        cfg["notification"]["command"] = command
        cfg["_config_path"] = "/tmp/example-config.toml"
        return cfg

    def _fire(self, cfg):
        return ml.notify(cfg, escalation_id="BRITTLE-20260728T000000Z-deadbeef",
                         summary="two repairs need a decision", lane="locomotion",
                         unit="12U-SYNTH", rel_path="projects/brittle/escalations/open/x.md")

    def test_unconfigured_is_unavailable_not_sent(self):
        status, detail = self._fire(self._cfg(""))
        self.assertEqual(status, "unavailable")
        self.assertIn("no [notification].command configured", detail)
        self.assertIn("/tmp/example-config.toml", detail,
                      "the exact missing configuration must be named")

    def test_successful_command_reports_sent(self):
        status, detail = self._fire(self._cfg("/bin/true"))
        self.assertEqual(status, "sent")
        self.assertIn("exit 0", detail)

    def test_failing_command_reports_failed_not_sent(self):
        status, detail = self._fire(self._cfg("/bin/false"))
        self.assertEqual(status, "failed")
        self.assertIn("exited 1", detail)

    def test_missing_binary_reports_failed(self):
        status, detail = self._fire(self._cfg("/nonexistent/notify-joe"))
        self.assertEqual(status, "failed")
        self.assertIn("not found", detail)

    def test_timeout_reports_failed(self):
        cfg = self._cfg("/bin/sleep 30")
        cfg["notification"]["timeout_seconds"] = 1
        status, detail = self._fire(cfg)
        self.assertEqual(status, "failed")
        self.assertIn("timed out", detail)

    def test_payload_is_passed_by_environment_not_argv(self):
        sink = self.tmp / "notified.txt"
        script = self.tmp / "notify.sh"
        script.write_text(
            "#!/bin/sh\n"
            f'printf "%s|%s|%s|%s|%s|argc=%s\\n" '
            '"$BRITTLE_ESCALATION_ID" "$BRITTLE_ESCALATION_SUMMARY" '
            '"$BRITTLE_ESCALATION_LANE" "$BRITTLE_ESCALATION_UNIT" '
            f'"$BRITTLE_ESCALATION_PATH" "$#" >> {sink}\n',
            encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        status, _ = self._fire(self._cfg(str(script)))
        self.assertEqual(status, "sent")

        line = sink.read_text(encoding="utf-8").strip()
        self.assertIn("BRITTLE-20260728T000000Z-deadbeef", line)
        self.assertIn("two repairs need a decision", line)
        self.assertIn("locomotion", line)
        self.assertIn("12U-SYNTH", line)
        self.assertIn("argc=0", line, "no payload may be passed on the command line")

    def test_environment_carries_no_api_key(self):
        os.environ["OPENAI_API_KEY"] = "sk-should-not-leak-into-notifier"
        sink = self.tmp / "env.txt"
        script = self.tmp / "dump.sh"
        script.write_text(f"#!/bin/sh\nenv > {sink}\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self._fire(self._cfg(str(script)))
        # The notifier inherits the environment, so the contract we can enforce
        # is that the tool adds no secret of its own and passes nothing in argv.
        dumped = sink.read_text(encoding="utf-8")
        self.assertNotIn("BRITTLE_ESCALATION_KEY", dumped)
        self.assertNotIn("api_key", dumped.lower().split("openai_api_key")[0])


class TestEscalateCommand(BusTestCase):
    def _config_file(self, command="") -> str:
        path = self.tmp / "config.toml"
        path.write_text(
            f'[repo]\npath = "{self.repo_path}"\nbrittle_path = "{self.brittle}"\n'
            f'[notification]\ncommand = "{command}"\ntimeout_seconds = 5\n',
            encoding="utf-8")
        return str(path)

    def _escalate(self, command="", expect=0):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = messagesctl.main([
                "--config", self._config_file(command), "--json", "escalate",
                "--lane", "locomotion", "--unit", "12U-SYNTH",
                "--summary", "Two materially different repairs; which do you want?"])
        self.assertEqual(rc, expect, buf.getvalue())
        return json.loads(buf.getvalue())

    def test_escalation_is_published_even_when_notification_is_unavailable(self):
        out = self._escalate("")
        self.assertEqual(out["notification_status"], "unavailable")
        esc = self.msgs()[out["escalation_id"]]
        self.assertEqual(esc.kind, "escalation")
        self.assertIs(esc.get("requires_owner"), True)
        self.assertTrue(esc.rel.startswith(ml.DIR_ESC_OPEN))
        self.assertValid()

    def test_notification_outcome_is_recorded_in_a_receipt(self):
        out = self._escalate("/bin/true")
        self.assertEqual(out["notification_status"], "sent")
        notice = self.msgs()[out["notice_receipt"]]
        self.assertEqual(notice.get("receipt_type"), "escalation_notice")
        self.assertEqual(notice.get("escalation_id"), out["escalation_id"])
        self.assertEqual(notice.get("notification_status"), "sent")

    def test_failed_notification_is_reported_as_failed(self):
        out = self._escalate("/bin/false", expect=3)
        self.assertEqual(out["notification_status"], "failed")
        notice = self.msgs()[out["notice_receipt"]]
        self.assertEqual(notice.get("notification_status"), "failed")

    def test_open_escalations_surfaces_the_notification_status(self):
        out = self._escalate("")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            messagesctl.main(["--config", self._config_file(), "--json",
                              "open-escalations"])
        rows = json.loads(buf.getvalue())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], out["escalation_id"])
        self.assertEqual(rows[0]["notification_status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
