"""Owner decisions, escalation resolution, and the Guard evidence/state boundary."""

from __future__ import annotations

import contextlib
import io
import json
import unittest

# harness must be imported first: it puts scripts/ on sys.path
from harness import BusTestCase, ml
import messagesctl


class OwnerTestCase(BusTestCase):
    def setUp(self):
        super().setUp()
        self.cfg_path = self.tmp / "config.toml"
        self.cfg_path.write_text(
            f'[repo]\npath = "{self.repo_path}"\nbrittle_path = "{self.brittle}"\n'
            f'[notification]\ncommand = ""\n', encoding="utf-8")

    def run_ctl(self, *argv, expect=0):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = messagesctl.main(["--config", str(self.cfg_path), "--json", *argv])
        self.assertEqual(rc, expect, buf.getvalue())
        return buf.getvalue()

    def escalate(self, summary="Which repair do you want?"):
        return json.loads(self.run_ctl(
            "escalate", "--lane", "locomotion", "--unit", "12U-SYNTH",
            "--summary", summary))["escalation_id"]

    def decision_file(self, text="Lower ankle-roll kd to 0.2. Do not touch the plant.\n"):
        path = self.tmp / "decision.md"
        path.write_text(text, encoding="utf-8")
        return path


class TestResolution(OwnerTestCase):
    def test_resolution_records_the_full_authorization_record(self):
        esc = self.escalate()
        decision = self.decision_file()

        out = json.loads(self.run_ctl(
            "resolve-escalation", "--id", esc, "--decision-file", str(decision),
            "--authorized-action", "Lower ankle-roll kd to 0.2 and re-run C7",
            "--scope", "12U-C11P control-side PD only; no plant mutation",
            "--expires-at", "2026-08-01T00:00:00Z"))

        msg = self.msgs()[out["decision_id"]]
        self.assertEqual(msg.kind, "owner_decision")
        self.assertEqual(msg.get("from"), "joe")
        self.assertEqual(msg.get("escalation_id"), esc)
        self.assertEqual(msg.get("unit"), "12U-SYNTH")
        self.assertEqual(msg.get("authorized_action"),
                         "Lower ankle-roll kd to 0.2 and re-run C7")
        self.assertIn("no plant mutation", str(msg.get("scope")))
        self.assertEqual(msg.get("expires_at"), "2026-08-01T00:00:00Z")
        self.assertEqual(msg.get("checksum"), ml.sha256_text(decision.read_text()))
        self.assertRegex(str(msg.get("created_at")), ml.TS_RE)
        self.assertValid()

    def test_checksum_binds_the_decision_to_its_source_file(self):
        esc = self.escalate()
        decision = self.decision_file()
        out = json.loads(self.run_ctl(
            "resolve-escalation", "--id", esc, "--decision-file", str(decision),
            "--authorized-action", "a", "--scope", "s"))
        recorded = self.msgs()[out["decision_id"]].get("checksum")

        decision.write_text("Actually, promote everything.\n", encoding="utf-8")
        self.assertNotEqual(ml.sha256_text(decision.read_text()), recorded,
                            "a tampered decision file no longer matches its checksum")

    def test_escalation_is_relocated_byte_identically(self):
        esc = self.escalate()
        original = (self.repo_path / ml.DIR_ESC_OPEN / f"{esc}.md").read_bytes()

        self.run_ctl("resolve-escalation", "--id", esc,
                     "--decision-file", str(self.decision_file()),
                     "--authorized-action", "a", "--scope", "s")

        self.assertFalse((self.repo_path / ml.DIR_ESC_OPEN / f"{esc}.md").exists())
        moved = self.repo_path / ml.DIR_ESC_RESOLVED / f"{esc}.md"
        self.assertTrue(moved.exists())
        self.assertEqual(moved.read_bytes(), original,
                         "an escalation may move, but never change")
        self.assertEqual(ml.escalation_state(esc, self.msgs()), "resolved")

    def test_resolution_is_not_repeatable(self):
        esc = self.escalate()
        decision = self.decision_file()
        self.run_ctl("resolve-escalation", "--id", esc, "--decision-file", str(decision),
                     "--authorized-action", "a", "--scope", "s")
        out = self.run_ctl("resolve-escalation", "--id", esc,
                           "--decision-file", str(decision),
                           "--authorized-action", "a", "--scope", "s", expect=2)
        self.assertIn("already resolved", out)

    def test_decision_requires_action_and_scope(self):
        esc = self.escalate()
        out = self.run_ctl("resolve-escalation", "--id", esc,
                           "--decision-file", str(self.decision_file()), expect=2)
        self.assertIn("--authorized-action", out)

    def test_unknown_escalation_rejected(self):
        out = self.run_ctl("resolve-escalation", "--id", ml.new_id(),
                           "--decision-file", str(self.decision_file()),
                           "--authorized-action", "a", "--scope", "s", expect=2)
        self.assertIn("not a known escalation", out)

    def test_open_escalations_empties_after_resolution(self):
        esc = self.escalate()
        self.run_ctl("resolve-escalation", "--id", esc,
                     "--decision-file", str(self.decision_file()),
                     "--authorized-action", "a", "--scope", "s")
        rows = json.loads(self.run_ctl("open-escalations"))
        self.assertEqual(rows, [])


class TestGuardBoundary(OwnerTestCase):
    def test_decision_states_it_is_evidence_not_guard_state(self):
        esc = self.escalate("Authorize the Guard unlock for 12U-C11P?")
        out = json.loads(self.run_ctl(
            "resolve-escalation", "--id", esc,
            "--decision-file", str(self.decision_file()),
            "--authorized-action", "Unlock Guard for 12U-C11P",
            "--scope", "12U-C11P only"))

        body = self.msgs()[out["decision_id"]].body
        self.assertIn("communication evidence", body)
        self.assertIn("not a Guard state mutation", body)
        self.assertIn("control agent", body)

    def test_cli_reminds_the_operator_to_record_guard_state(self):
        esc = self.escalate()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            messagesctl.main(["--config", str(self.cfg_path), "resolve-escalation",
                              "--id", esc, "--decision-file", str(self.decision_file()),
                              "--authorized-action", "a", "--scope", "s"])
        self.assertIn("Guard live state", buf.getvalue())

    def test_bus_exposes_no_guard_mutation_command(self):
        parser = messagesctl.build_parser()
        commands = set(parser._subparsers._group_actions[0].choices)
        self.assertFalse([c for c in commands if "guard" in c.lower()],
                         "the bus must not offer a Guard mutation path")

    def test_owner_decision_from_a_non_owner_is_rejected(self):
        fm = ml.base_frontmatter("owner_decision", sender="reviewer", to="locomotion",
                                 lane="locomotion", unit="U", status="resolved")
        fm.update({"authorized_action": "promote", "scope": "everything",
                   "checksum": "0" * 64})
        with self.assertRaisesRegex(ml.MessageError, "must originate from 'joe'"):
            ml.validate_frontmatter(fm)


class TestAutonomyDecisions(OwnerTestCase):
    def test_pause_records_an_owner_decision_and_a_local_marker(self):
        out = json.loads(self.run_ctl("pause"))
        self.assertEqual(out["autonomy"], "paused")
        self.assertTrue((ml.state_dir() / "PAUSED").exists())

        decisions = [m for m in self.msgs().values()
                     if m.kind == "owner_decision" and m.get("autonomy") == "paused"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].get("from"), "joe")
        self.assertIn("PAUSE", str(decisions[0].get("authorized_action")))
        self.assertTrue(ml.autonomy_state(self.msgs())["paused"])

    def test_pause_deletes_and_modifies_nothing(self):
        tid = self.publish_ticket()
        before = {m.id: (self.repo_path / m.rel).read_bytes() for m in self.msgs().values()}

        self.run_ctl("pause")

        after = self.msgs()
        for mid, payload in before.items():
            self.assertIn(mid, after, "pause must not delete queued messages")
            self.assertEqual((self.repo_path / after[mid].rel).read_bytes(), payload)
        self.assertEqual(ml.ticket_state(tid, after)["status"], "open")

    def test_paused_bus_refuses_to_issue_a_new_ticket(self):
        self.run_ctl("pause")
        ticket_file = self.tmp / "t.md"
        ticket_file.write_text("# New work\n\nDo it.\n", encoding="utf-8")
        out = self.run_ctl("publish-ticket", "--lane", "locomotion",
                           "--ticket", str(ticket_file), "--unit", "U", expect=2)
        self.assertIn("PAUSED", out)

    def test_resume_clears_the_pause(self):
        self.run_ctl("pause")
        self.run_ctl("resume")
        self.assertFalse((ml.state_dir() / "PAUSED").exists())
        self.assertFalse(ml.autonomy_state(self.msgs())["paused"])

        ticket_file = self.tmp / "t.md"
        ticket_file.write_text("# New work\n\nDo it.\n", encoding="utf-8")
        self.run_ctl("publish-ticket", "--lane", "locomotion",
                     "--ticket", str(ticket_file), "--unit", "U")

    def test_ticket_requiring_owner_cannot_be_published_as_a_ticket(self):
        ticket_file = self.tmp / "t.md"
        ticket_file.write_text(
            "---\ntitle: Needs Joe\nrequires_owner: true\n---\n# Needs Joe\n",
            encoding="utf-8")
        out = self.run_ctl("publish-ticket", "--lane", "locomotion",
                           "--ticket", str(ticket_file), "--unit", "U", expect=2)
        self.assertIn("escalation", out)


if __name__ == "__main__":
    unittest.main()
