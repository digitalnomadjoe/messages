"""The CI immutability gate: `messagesctl validate --diff-base <REF>`.

This is the gate that stops a published message from being edited, deleted or
renamed after it lands on main. It is exercised here because a gate with no
test is a gate that silently stops working.
"""

from __future__ import annotations

import contextlib
import io
import unittest

# harness must be imported first: it puts scripts/ on sys.path
from harness import BusTestCase, ml
import messagesctl


class TestDiffImmutability(BusTestCase):
    def setUp(self):
        super().setUp()
        self.cfg_path = self.tmp / "config.toml"
        self.cfg_path.write_text(
            f'[repo]\npath = "{self.repo_path}"\nbrittle_path = "{self.brittle}"\n'
            f'[notification]\ncommand = ""\n', encoding="utf-8")

    def validate(self, base: str):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = messagesctl.main(["--config", str(self.cfg_path),
                                   "validate", "--diff-base", base])
        return rc, buf.getvalue()

    def commit_all(self, message: str):
        self.repo.git("add", "-A")
        self.repo.git("commit", "-m", message, identity=True)

    def test_unchanged_history_passes(self):
        self.publish_ticket()
        rc, _ = self.validate(self.repo.head())
        self.assertEqual(rc, 0)

    def test_new_messages_pass(self):
        base = self.repo.head()
        self.publish_ticket()
        self.publish_report()
        rc, out = self.validate(base)
        self.assertEqual(rc, 0, out)

    def test_modifying_a_published_message_fails(self):
        tid = self.publish_ticket()
        base = self.repo.head()
        victim = self.repo_path / ml.DIR_TICKETS / "locomotion" / f"{tid}.md"
        victim.write_text(victim.read_text(encoding="utf-8") + "\ntampered\n",
                          encoding="utf-8")
        self.commit_all("tamper")

        rc, out = self.validate(base)
        self.assertEqual(rc, 1)
        self.assertIn("immutable", out)
        self.assertIn(tid, out)

    def test_deleting_a_published_message_fails(self):
        tid = self.publish_ticket()
        base = self.repo.head()
        (self.repo_path / ml.DIR_TICKETS / "locomotion" / f"{tid}.md").unlink()
        self.commit_all("delete")

        rc, out = self.validate(base)
        self.assertEqual(rc, 1)
        self.assertIn("immutable", out)

    def test_renaming_a_published_message_fails(self):
        tid = self.publish_ticket()
        base = self.repo.head()
        src = self.repo_path / ml.DIR_TICKETS / "locomotion" / f"{tid}.md"
        dst = self.repo_path / ml.DIR_TICKETS / "control" / f"{tid}.md"
        self.repo.git("mv", str(src.relative_to(self.repo_path)),
                      str(dst.relative_to(self.repo_path)))
        self.commit_all("rename across lanes")

        rc, out = self.validate(base)
        self.assertEqual(rc, 1)
        self.assertTrue("may not be renamed" in out or "immutable" in out, out)

    def test_sanctioned_escalation_relocation_passes(self):
        fm = ml.base_frontmatter("escalation", sender="locomotion", to="joe",
                                 lane="locomotion", unit="U", status="open",
                                 requires_owner=True)
        fm["title"] = "probe"
        ml.publish(self.repo, {f"{ml.DIR_ESC_OPEN}/{fm['id']}.md":
                               ml.render_message(fm, "# probe\n")},
                   "escalation", cfg=self.cfg)
        base = self.repo.head()

        decision = self.tmp / "d.md"
        decision.write_text("approved\n", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = messagesctl.main(["--config", str(self.cfg_path),
                                   "resolve-escalation", "--id", fm["id"],
                                   "--decision-file", str(decision),
                                   "--authorized-action", "a", "--scope", "s"])
        self.assertEqual(rc, 0, buf.getvalue())

        rc, out = self.validate(base)
        self.assertEqual(rc, 0, out)
        self.assertFalse((self.repo_path / ml.DIR_ESC_OPEN / f"{fm['id']}.md").exists())
        self.assertTrue((self.repo_path / ml.DIR_ESC_RESOLVED / f"{fm['id']}.md").exists())

    def test_state_index_may_change_freely(self):
        self.publish_ticket()
        base = self.repo.head()
        idx = self.repo_path / ml.INDEX_PATH
        idx.write_text('{"schema": 1, "scratch": true}\n', encoding="utf-8")
        self.commit_all("index churn")

        rc, out = self.validate(base)
        # The generated index is disposable, so the immutability gate ignores it.
        # (Full `validate` still flags it as stale -- that is a different gate.)
        self.assertIn("stale", out)
        self.assertNotIn("immutable", out)

    def test_docs_and_code_may_change_freely(self):
        base = self.repo.head()
        (self.repo_path / "prompts" / "brittle-reviewer.md").write_text(
            "# reviewer\nRevised instructions.\n", encoding="utf-8")
        self.commit_all("edit the reviewer prompt")

        rc, out = self.validate(base)
        self.assertEqual(rc, 0, out)


if __name__ == "__main__":
    unittest.main()
