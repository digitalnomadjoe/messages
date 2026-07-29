"""Synthetic end-to-end run of the whole loop.

Uses only synthetic messages and a mocked OpenAI reviewer. No live production
BRITTLE ticket is involved at any point.

Set BRITTLE_E2E_TRANSCRIPT=/path/to/file.txt to capture the numbered transcript.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import unittest
from pathlib import Path

# harness must be imported first: it puts scripts/ on sys.path
from harness import SYNTHETIC_REPORT, BusTestCase, ml, mock_response
import messagesctl
import reviewer_daemon as rd
import ticket_watcher


class TestSyntheticEndToEnd(BusTestCase):
    def setUp(self):
        super().setUp()
        self.transcript: list[str] = []
        self.notify_sink = self.tmp / "joe-was-pinged.txt"
        self.notify_script = self.tmp / "notify-joe.sh"
        self.notify_script.write_text(
            "#!/bin/sh\n"
            f'printf "%s :: %s\\n" "$BRITTLE_ESCALATION_ID" '
            f'"$BRITTLE_ESCALATION_SUMMARY" >> {self.notify_sink}\n',
            encoding="utf-8")
        self.notify_script.chmod(self.notify_script.stat().st_mode | stat.S_IEXEC)

        self.cfg_path = self.tmp / "config.toml"
        self.cfg_path.write_text(
            f'[repo]\npath = "{self.repo_path}"\nbrittle_path = "{self.brittle}"\n'
            f'[reviewer]\nmodel = "mock-model"\n'
            f'prompt_path = "{self.repo_path}/prompts/brittle-reviewer.md"\n'
            f"minimum_confidence = 0.85\n"
            f'[notification]\ncommand = "{self.notify_script}"\ntimeout_seconds = 5\n',
            encoding="utf-8")
        self.cfg = ml.load_config(self.cfg_path)

    def tearDown(self):
        dest = os.environ.get("BRITTLE_E2E_TRANSCRIPT")
        if dest:
            Path(dest).write_text("\n".join(self.transcript) + "\n", encoding="utf-8")
        super().tearDown() if hasattr(super(), "tearDown") else None

    def say(self, step: str, detail: str) -> None:
        self.transcript.append(f"{step:<6} {detail}")

    def ctl(self, *argv, expect=0):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = messagesctl.main(["--config", str(self.cfg_path), "--json", *argv])
        self.assertEqual(rc, expect, buf.getvalue())
        return buf.getvalue()

    def remote_has(self, rel: str) -> bool:
        proc = subprocess.run(
            ["git", "-C", str(self.remote), "cat-file", "-e", f"main:{rel}"],
            capture_output=True)
        return proc.returncode == 0

    def daemon(self):
        return rd.ReviewerDaemon(ml.load_config(self.cfg_path))

    # ------------------------------------------------------------------

    def test_full_loop(self):
        # -- 1. locomotion agent publishes a synthetic report ------------
        local = self.write_local_report("SYNTHETIC_E2E_REPORT.md")
        local_sha = ml.sha256_file(local)
        local_bytes = local.read_bytes()

        out = json.loads(self.ctl("publish-report", "--lane", "locomotion",
                                  "--unit", "12U-SYNTH", "--report", str(local)))
        report_id = out["report_id"]
        self.say("1", f"locomotion published report {report_id}")

        # -- 2. the local report remains authoritative -------------------
        self.assertEqual(local.read_bytes(), local_bytes)
        self.assertEqual(ml.sha256_file(local), local_sha)
        self.say("2", f"local source byte-identical, sha256={local_sha[:16]}... "
                      f"path={local}")

        # -- 3. the duplicate is on the remote ---------------------------
        self.assertTrue(out["pushed"])
        self.assertTrue(self.remote_has(out["path"]))
        mirrored = self.msgs()[report_id]
        self.assertEqual(mirrored.get("local_source_sha256"), local_sha)
        self.assertIn("survival 812 steps", mirrored.body)
        self.say("3", f"mirror pushed to remote at {out['path']} "
                      f"(commit {out['commit'][:8]})")

        # -- 4. the reviewer daemon detects it ---------------------------
        daemon = self.daemon()
        pending = daemon.pending_reports(self.msgs())
        self.assertEqual([m.id for m in pending], [report_id])
        self.say("4", f"reviewer daemon detected 1 unreviewed report: {report_id}")

        # -- 5. mocked reviewer publishes a review and a locomotion ticket
        os.environ["BRITTLE_REVIEWER_MOCK"] = mock_response(self.tmp)
        stats = daemon.run_once()
        self.assertEqual(stats["reviewed"], 1, stats)

        msgs = self.msgs()
        review = next(m for m in msgs.values() if m.kind == "review")
        ticket = next(m for m in msgs.values() if m.kind == "ticket")
        self.assertEqual(review.get("review_of"), report_id)
        self.assertEqual(ticket.get("lane"), "locomotion")
        self.assertTrue(ml.reviewer_acked(report_id, msgs))
        self.say("5", f"reviewer published review {review.id} + locomotion ticket "
                      f"{ticket.id} (conf {review.get('confidence')})")

        # -- 6. the locomotion watcher announces it; the agent claims it --
        snap = ticket_watcher.run_once(self.cfg, "locomotion",
                                       self.tmp / "lane-locomotion.json")
        self.assertEqual(snap["next_open_ticket"], ticket.id)
        self.assertEqual(snap.get("notification_status"), "sent")

        claim = json.loads(self.ctl("claim", ticket.id, "--agent", "locomotion"))
        state = ml.ticket_state(ticket.id, self.msgs())
        self.assertEqual(state["status"], "claimed")
        self.assertEqual(state["claim_agent"], "locomotion")
        self.say("6", f"watcher announced {ticket.id}; locomotion claimed it "
                      f"(receipt {claim['receipt_id']}, lease "
                      f"{claim['lease_expires_at']})")

        # -- 11. the control lane cannot claim a locomotion ticket -------
        denied = self.ctl("claim", ticket.id, "--agent", "control", expect=2)
        self.assertIn("lane violation", denied)
        self.assertEqual(ml.ticket_state(ticket.id, self.msgs())["claim_agent"],
                         "locomotion")
        self.say("11", "control lane claim of the locomotion ticket REJECTED "
                       "(lane violation); the locomotion claim is untouched")

        # -- 7. the agent publishes a completion report and receipt ------
        follow_up = self.write_local_report(
            "SYNTHETIC_E2E_FOLLOWUP.md",
            SYNTHETIC_REPORT.replace("812", "834") + "\nThree-seed repeat done.\n")
        out2 = json.loads(self.ctl("publish-report", "--lane", "locomotion",
                                   "--unit", "12U-SYNTH", "--report", str(follow_up),
                                   "--in-reply-to", ticket.id))
        done = json.loads(self.ctl("complete", ticket.id,
                                   "--report-id", out2["report_id"]))
        final = ml.ticket_state(ticket.id, self.msgs())
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["completion"].get("report_id"), out2["report_id"])
        self.assertEqual(final["completion"].get("local_source_sha256"),
                         ml.sha256_file(follow_up))
        self.say("7", f"locomotion published report {out2['report_id']} and "
                      f"completion receipt {done['receipt_id']}; ticket completed")

        # -- 8. the reviewer acknowledges it without duplicating work ----
        reviews_before = sum(1 for m in self.msgs().values() if m.kind == "review")
        tickets_before = sum(1 for m in self.msgs().values() if m.kind == "ticket")
        os.environ["BRITTLE_REVIEWER_MOCK"] = mock_response(
            self.tmp, confidence=0.95, target_lane=None,
            ticket_title=None, ticket_markdown=None,
            summary="Three-seed spread is tight; nothing further needed.",
            next_action="No further action; the lane is clean.")
        stats = self.daemon().run_once()
        self.assertEqual(stats["reviewed"], 1)
        msgs = self.msgs()
        self.assertTrue(ml.reviewer_acked(out2["report_id"], msgs))
        self.assertEqual(sum(1 for m in msgs.values() if m.kind == "review"),
                         reviews_before + 1)
        self.assertEqual(sum(1 for m in msgs.values() if m.kind == "ticket"),
                         tickets_before, "no duplicate ticket")
        self.say("8", f"reviewer acknowledged {out2['report_id']} with a review "
                      f"and no new ticket (tickets still {tickets_before})")

        # -- 9. a low-confidence report escalates instead of ticketing ---
        shaky = self.write_local_report(
            "SYNTHETIC_E2E_SHAKY.md",
            SYNTHETIC_REPORT + "\nResult is ambiguous across two candidate repairs.\n")
        out3 = json.loads(self.ctl("publish-report", "--lane", "locomotion",
                                   "--unit", "12U-SYNTH", "--report", str(shaky)))
        os.environ["BRITTLE_REVIEWER_MOCK"] = mock_response(
            self.tmp, confidence=0.41,
            summary="Two candidate repairs are equally supported.",
            next_action="Choose between the control-side fix and the plant rebaseline.",
            owner_question="Control-side kd fix, or plant rebaseline first?",
            ticket_title=None, ticket_markdown=None)
        tickets_before = sum(1 for m in self.msgs().values() if m.kind == "ticket")

        self.daemon().run_once()

        msgs = self.msgs()
        escalations = [m for m in msgs.values() if m.kind == "escalation"]
        self.assertEqual(len(escalations), 1)
        esc = escalations[0]
        self.assertIs(esc.get("requires_owner"), True)
        self.assertIn("confidence 0.41 < threshold 0.85", esc.body)
        self.assertEqual(sum(1 for m in msgs.values() if m.kind == "ticket"),
                         tickets_before, "a low-confidence review issues no ticket")
        self.say("9", f"low-confidence report {out3['report_id']} produced "
                      f"escalation {esc.id} and NO ticket")

        # -- 10. the notification hook was actually invoked --------------
        notice = next(m for m in msgs.values()
                      if m.get("receipt_type") == "escalation_notice"
                      and m.get("escalation_id") == esc.id)
        self.assertEqual(notice.get("notification_status"), "sent")
        self.assertTrue(self.notify_sink.exists())
        pinged = self.notify_sink.read_text(encoding="utf-8")
        self.assertIn(esc.id, pinged)
        self.say("10", f"notification hook invoked; receipt {notice.id} records "
                       f"notification_status=sent")

        # -- 12. a daemon restart creates no duplicates ------------------
        head_before = self.repo.head()
        counts_before = ml.build_index(self.msgs())["counts"]
        for _ in range(3):
            self.daemon().run_once()
        self.assertEqual(self.repo.head(), head_before, "restart must add no commits")
        self.assertEqual(ml.build_index(self.msgs())["counts"], counts_before)
        self.say("12", f"3 daemon restarts produced 0 new commits "
                       f"(HEAD still {head_before[:8]}, counts unchanged)")

        # -- closing integrity ------------------------------------------
        self.assertValid()
        final_counts = ml.build_index(self.msgs())["counts"]
        self.say("--", f"repository valid; final counts {json.dumps(final_counts)}")
        self.assertEqual(local.read_bytes(), local_bytes,
                         "the original local report is still byte-identical")


if __name__ == "__main__":
    unittest.main()
