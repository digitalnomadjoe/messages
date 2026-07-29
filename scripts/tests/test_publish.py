"""Publication guarantees: immutability, new-files-only, secrets, spooling, retry."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from harness import BusTestCase, ml

# Synthetic, non-functional credential shapes used to prove the scanner fires.
FAKE_OPENAI = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
FAKE_GITHUB = "ghp_" + "0123456789abcdefghijABCDEFGHIJ0123456789"
FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"


def _ticket_content(title="T"):
    fm = ml.base_frontmatter("ticket", sender="reviewer", to="locomotion",
                             lane="locomotion", unit="12U-SYNTH", status="open")
    fm["title"] = title
    return fm, ml.render_message(fm, f"# {title}\n\nbody\n")


class TestImmutability(BusTestCase):
    def test_published_message_cannot_be_overwritten(self):
        fm, content = _ticket_content()
        rel = f"{ml.DIR_TICKETS}/locomotion/{fm['id']}.md"
        ml.publish(self.repo, {rel: content}, "ticket", cfg=self.cfg)
        with self.assertRaisesRegex(ml.MessageError, "refusing to overwrite"):
            ml.publish(self.repo, {rel: content}, "again", cfg=self.cfg)

    def test_edited_message_blocks_the_next_publication(self):
        tid = self.publish_ticket()
        victim = self.repo_path / ml.DIR_TICKETS / "locomotion" / f"{tid}.md"
        victim.write_text(victim.read_text(encoding="utf-8") + "\ntampered\n",
                          encoding="utf-8")
        fm, content = _ticket_content("second")
        rel = f"{ml.DIR_TICKETS}/locomotion/{fm['id']}.md"
        with self.assertRaisesRegex(ml.MessageError, "immutable"):
            ml.publish(self.repo, {rel: content}, "second", cfg=self.cfg)

    def test_deleted_message_blocks_the_next_publication(self):
        tid = self.publish_ticket()
        (self.repo_path / ml.DIR_TICKETS / "locomotion" / f"{tid}.md").unlink()
        fm, content = _ticket_content("second")
        rel = f"{ml.DIR_TICKETS}/locomotion/{fm['id']}.md"
        with self.assertRaisesRegex(ml.MessageError, "immutable"):
            ml.publish(self.repo, {rel: content}, "second", cfg=self.cfg)

    def test_history_is_never_rewritten(self):
        first = self.repo.head()
        self.publish_ticket()
        log = self.repo.git("rev-list", "--all").split()
        self.assertIn(first, log, "the pre-existing commit must still be reachable")


class TestNewFilesOnly(BusTestCase):
    def test_unrelated_untracked_files_are_not_committed(self):
        stray = self.repo_path / "STRAY_NOTES.md"
        stray.write_text("# not mine\n", encoding="utf-8")
        self.publish_ticket()
        tracked = self.repo.git("ls-files").split("\n")
        self.assertNotIn("STRAY_NOTES.md", tracked,
                         "publication must stage explicit paths, never `git add -A`")

    def test_index_is_refreshed_in_the_same_commit(self):
        tid = self.publish_ticket()
        idx = json.loads((self.repo_path / ml.INDEX_PATH).read_text(encoding="utf-8"))
        self.assertEqual(idx["open_ticket_by_lane"]["locomotion"], tid)
        self.assertIn(ml.INDEX_PATH, self.repo.git("ls-files"))


class TestPublicRepoSafety(BusTestCase):
    def test_secret_in_payload_aborts_publication(self):
        fm, _ = _ticket_content()
        body = f"# T\n\nRun with key {FAKE_OPENAI}\n"
        rel = f"{ml.DIR_TICKETS}/locomotion/{fm['id']}.md"
        before = self.repo.head()
        with self.assertRaisesRegex(ml.MessageError, "secret scan failed"):
            ml.publish(self.repo, {rel: ml.render_message(fm, body)}, "t", cfg=self.cfg)
        self.assertEqual(self.repo.head(), before, "nothing may be committed")
        self.assertFalse((self.repo_path / rel).exists())

    def test_scanner_catches_multiple_credential_shapes(self):
        for token in (FAKE_OPENAI, FAKE_GITHUB, FAKE_AWS,
                      "-----BEGIN RSA PRIVATE KEY-----"):
            self.assertTrue(ml.scan_secrets(f"value: {token}"), token)

    def test_scanner_allows_documented_env_references(self):
        self.assertEqual(ml.scan_secrets('api_key_env = "OPENAI_API_KEY"'), [])
        self.assertEqual(ml.scan_secrets('api_key: "<REDACTED>"'), [])

    def test_configured_private_pattern_is_detected(self):
        cfg = dict(self.cfg)
        cfg["safety"] = {"private_patterns": [r"INTERNAL-ONLY-\d+"]}
        fm, _ = _ticket_content()
        rel = f"{ml.DIR_TICKETS}/locomotion/{fm['id']}.md"
        content = ml.render_message(fm, "# T\n\nINTERNAL-ONLY-42\n")
        with self.assertRaisesRegex(ml.MessageError, "secret scan failed"):
            ml.publish(self.repo, {rel: content}, "t", cfg=cfg)

    def test_forbidden_extension_rejected(self):
        with self.assertRaisesRegex(ml.MessageError, "message payloads allow only"):
            ml.publish(self.repo,
                       {f"{ml.DIR_REPORTS}/locomotion/policy.npz": "binary-ish"},
                       "bad", cfg=self.cfg)

    def test_forbidden_extension_outside_message_dirs_rejected(self):
        with self.assertRaisesRegex(ml.MessageError, "forbidden file type"):
            ml.publish(self.repo, {"artifacts/run.mp4": "x"}, "bad", cfg=self.cfg)

    def test_oversized_message_rejected(self):
        fm, _ = _ticket_content()
        rel = f"{ml.DIR_TICKETS}/locomotion/{fm['id']}.md"
        content = ml.render_message(fm, "# T\n\n" + ("x" * (ml.MAX_MESSAGE_BYTES + 10)))
        with self.assertRaisesRegex(ml.MessageError, "exceeds the"):
            ml.publish(self.repo, {rel: content}, "big", cfg=self.cfg)

    def test_path_traversal_rejected(self):
        with self.assertRaisesRegex(ml.MessageError, "escapes the repository"):
            ml.check_path_policy("../../etc/passwd", 10)


class TestConcurrencyAndSpooling(BusTestCase):
    def test_concurrent_append_is_retried_and_lands(self):
        other, ocfg = self.second_clone()

        # A competing agent publishes first, advancing the remote.
        cfm = ml.base_frontmatter("ticket", sender="reviewer", to="control",
                                  lane="control", unit="12U-SYNTH", status="open")
        cfm["title"] = "competitor"
        ml.publish(other, {f"{ml.DIR_TICKETS}/control/{cfm['id']}.md":
                           ml.render_message(cfm, "# competitor\n")},
                   "competitor ticket", cfg=ocfg)

        # Our repo is stale. Skip exactly one pull so we commit on a stale base.
        original = ml.Repo.pull_ff_only
        calls = {"n": 0}

        def flaky(self_inner):
            calls["n"] += 1
            if calls["n"] == 1:
                return
            return original(self_inner)

        ml.Repo.pull_ff_only = flaky
        try:
            fm, content = _ticket_content("ours")
            rel = f"{ml.DIR_TICKETS}/locomotion/{fm['id']}.md"
            result = ml.publish(self.repo, {rel: content}, "ours", cfg=self.cfg)
        finally:
            ml.Repo.pull_ff_only = original

        self.assertTrue(result.pushed, result.detail)
        self.assertGreaterEqual(calls["n"], 2, "the rejection must trigger a retry")
        msgs = self.msgs()
        self.assertIn(cfm["id"], msgs, "the competitor's message must survive")
        self.assertIn(fm["id"], msgs, "our message must land too")
        self.assertValid()

    def test_failed_push_retains_the_commit_and_marks_it_for_retry(self):
        original = ml.Repo.git_ok

        def no_push(self_inner, *args):
            if args and args[0] == "push":
                return False
            return original(self_inner, *args)

        ml.Repo.git_ok = no_push
        try:
            fm, content = _ticket_content("unpushable")
            rel = f"{ml.DIR_TICKETS}/locomotion/{fm['id']}.md"
            result = ml.publish(self.repo, {rel: content}, "unpushable", cfg=self.cfg)
        finally:
            ml.Repo.git_ok = original

        self.assertFalse(result.pushed)
        self.assertTrue((self.repo_path / rel).exists(), "commit retained locally")
        marker = Path(ml.spool_dir()) / "pending_push.json"
        self.assertTrue(marker.exists(), "a durable retry marker must exist")
        self.assertIn(result.commit, json.loads(marker.read_text())["commits"])

    def test_unreachable_remote_spools_the_message(self):
        self.repo.git("remote", "set-url", "origin", str(self.tmp / "gone.git"))
        fm, content = _ticket_content("spooled")
        rel = f"{ml.DIR_TICKETS}/locomotion/{fm['id']}.md"
        with self.assertRaisesRegex(ml.MessageError, "spooled"):
            ml.publish(self.repo, {rel: content}, "spooled", cfg=self.cfg)

        spooled = list((Path(ml.spool_dir()) / "outbox").glob("*.json"))
        self.assertEqual(len(spooled), 1)
        payload = json.loads(spooled[0].read_text(encoding="utf-8"))
        self.assertIn(rel, payload["files"])
        self.assertIn("spooled", payload["files"][rel])

    def test_sync_replays_the_spool_once_the_remote_returns(self):
        import messagesctl

        self.repo.git("remote", "set-url", "origin", str(self.tmp / "gone.git"))
        fm, content = _ticket_content("deferred")
        rel = f"{ml.DIR_TICKETS}/locomotion/{fm['id']}.md"
        with self.assertRaises(ml.MessageError):
            ml.publish(self.repo, {rel: content}, "deferred", cfg=self.cfg)
        self.repo.git("remote", "set-url", "origin", str(self.remote))

        cfg_path = self.tmp / "config.toml"
        cfg_path.write_text(
            f'[repo]\npath = "{self.repo_path}"\nbrittle_path = "{self.brittle}"\n',
            encoding="utf-8")
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = messagesctl.main(["--config", str(cfg_path), "sync"])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("spool_replayed=1", buf.getvalue())
        self.assertIn(fm["id"], self.msgs())
        self.assertEqual(list((Path(ml.spool_dir()) / "outbox").glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
