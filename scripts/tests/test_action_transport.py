"""The Custom GPT Action transport: scope, safety, and the read-only correction.

The standard ChatGPT GitHub connector is read-only (403 on writes), so the
Action is the only browser write path. These tests pin its blast radius.
"""

from __future__ import annotations

import base64
import json
import re
import unittest
from pathlib import Path

# harness must be imported first: it puts scripts/ on sys.path
from harness import BusTestCase, ml
import browser_bridge as bb

REPO_ROOT = Path(bb.__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "actions" / "telephone-action.openapi.yaml"
SETUP_PATH = REPO_ROOT / "actions" / "TELEPHONE_GPT_SETUP.md"

EXPECTED_OPERATIONS = {
    "getTelephoneInstructions",
    "getTelephoneStatus",
    "getTelephoneReport",
    "getTelephoneRequestResult",
    "submitTelephoneRequest",
}


def load_schema():
    try:
        import yaml  # noqa: F401
    except ImportError:
        return None
    import yaml
    return yaml.safe_load(SCHEMA_PATH.read_text(encoding="utf-8"))


class TestSchemaStructure(unittest.TestCase):
    """Parsed checks when PyYAML is available (it is in CI)."""

    @classmethod
    def setUpClass(cls):
        cls.schema = load_schema()
        if cls.schema is None:
            raise unittest.SkipTest("PyYAML not installed")

    def operations(self):
        ops = {}
        for path, item in self.schema["paths"].items():
            for method, op in item.items():
                if method in ("get", "put", "post", "patch", "delete"):
                    ops[op["operationId"]] = (method, path, op)
        return ops

    def test_valid_openapi_envelope(self):
        self.assertTrue(str(self.schema["openapi"]).startswith("3."))
        self.assertIn("info", self.schema)
        self.assertIn("paths", self.schema)
        self.assertEqual(self.schema["servers"][0]["url"], "https://api.github.com")

    def test_exactly_the_five_operations(self):
        self.assertEqual(set(self.operations()), EXPECTED_OPERATIONS)

    def test_exactly_one_write_operation(self):
        writes = [(oid, m, p) for oid, (m, p, _) in self.operations().items()
                  if m in ("put", "post", "patch", "delete")]
        self.assertEqual(len(writes), 1, writes)
        oid, method, path = writes[0]
        self.assertEqual(oid, "submitTelephoneRequest")
        self.assertEqual(method, "put")
        self.assertIn("/projects/brittle/browser_requests/", path)

    def test_the_write_cannot_target_anything_but_browser_requests(self):
        _m, path, _op = self.operations()["submitTelephoneRequest"]
        prefix = path.split("{")[0]
        self.assertTrue(
            prefix.endswith("/projects/brittle/browser_requests/"),
            f"write path prefix is {prefix!r}")
        self.assertEqual(path.count("{"), 1, "only the request ID may vary")

    def test_no_operation_can_write_a_canonical_message_path(self):
        for oid, (method, path, _op) in self.operations().items():
            if method in ("put", "post", "patch", "delete"):
                for d in ml.MESSAGE_DIRS:
                    self.assertNotIn(d, path,
                                     f"{oid} could write a canonical message path")

    def test_no_generic_path_parameter(self):
        for oid, (_m, path, op) in self.operations().items():
            for param in op.get("parameters", []) or []:
                if param.get("in") != "path":
                    continue
                schema = param.get("schema", {})
                self.assertTrue(
                    "enum" in schema or "pattern" in schema,
                    f"{oid}: path parameter {param['name']} is unconstrained")
                if "pattern" in schema:
                    pat = schema["pattern"]
                    self.assertTrue(pat.startswith("^") and pat.endswith("$"),
                                    f"{oid}: pattern must be anchored: {pat}")

    def test_request_id_patterns_forbid_traversal_and_separators(self):
        for oid in ("submitTelephoneRequest", "getTelephoneRequestResult"):
            _m, _p, op = self.operations()[oid]
            pat = op["parameters"][0]["schema"]["pattern"]
            rx = re.compile(pat)
            self.assertTrue(rx.match("BREQ-20260729T180000Z-1a2b3c4d"))
            for bad in ("../../etc/passwd", "..%2F..%2Fx", "a/b", "a%2Fb",
                        "BREQ-20260729T180000Z-1a2b3c4d/../x",
                        "BREQ-20260729T180000Z-1a2b3c4d%2e%2e",
                        "BREQ-20260729T180000Z-ZZZZZZZZ", "", ".", ".."):
                self.assertIsNone(rx.match(bad), f"{oid} accepted {bad!r}")

    def test_report_lane_is_a_closed_enum(self):
        _m, _p, op = self.operations()["getTelephoneReport"]
        lane = [p for p in op["parameters"] if p["name"] == "lane"][0]
        self.assertEqual(sorted(lane["schema"]["enum"]), ["control", "locomotion"])

    def test_report_id_pattern_forbids_traversal(self):
        _m, _p, op = self.operations()["getTelephoneReport"]
        rid = [p for p in op["parameters"] if p["name"] == "reportId"][0]
        rx = re.compile(rid["schema"]["pattern"])
        self.assertTrue(rx.match("BRITTLE-20260729T180000Z-1a2b3c4d"))
        for bad in ("../../../../etc/passwd", "a/b", "a%2Fb", ".."):
            self.assertIsNone(rx.match(bad))

    def test_fixed_read_paths_take_no_parameters(self):
        for oid in ("getTelephoneInstructions", "getTelephoneStatus"):
            _m, path, op = self.operations()[oid]
            self.assertNotIn("{", path, f"{oid} must be a fixed path")
            self.assertFalse(op.get("parameters"), f"{oid} must take no parameters")

    def test_no_issue_workflow_or_admin_surface(self):
        blob = json.dumps(self.schema).lower()
        for banned in ("/issues", "/pulls", "/actions/workflows", "/dispatches",
                       "/hooks", "/collaborators", "/git/refs", "/git/blobs"):
            self.assertNotIn(banned, blob, f"schema exposes {banned}")

    def test_auth_is_a_bearer_token_and_not_a_parameter(self):
        sec = self.schema["components"]["securitySchemes"]
        scheme = list(sec.values())[0]
        self.assertEqual(scheme["type"], "http")
        self.assertEqual(scheme["scheme"], "bearer")
        for oid, (_m, _p, op) in self.operations().items():
            for param in op.get("parameters", []) or []:
                self.assertNotIn("token", param["name"].lower(), oid)
                self.assertNotIn("key", param["name"].lower(), oid)


class TestSchemaTextInvariants(unittest.TestCase):
    """Hold even without PyYAML, so the guarantees are never unchecked."""

    def setUp(self):
        self.text = SCHEMA_PATH.read_text(encoding="utf-8")

    def test_schema_exists_and_names_the_five_operations(self):
        for op in EXPECTED_OPERATIONS:
            self.assertIn(f"operationId: {op}", self.text)

    def test_only_one_write_verb_appears(self):
        self.assertEqual(len(re.findall(r"^\s{4}put:", self.text, re.M)), 1)
        for verb in ("post:", "patch:", "delete:"):
            self.assertEqual(len(re.findall(rf"^\s{{4}}{verb}", self.text, re.M)), 0,
                             f"unexpected {verb} operation")

    def test_no_canonical_message_directory_is_writable(self):
        write_block = self.text.split("submitTelephoneRequest")[0].split(
            "browser_requests/{requestId}.json")[-1]
        for d in ("tickets", "reviews", "receipts", "escalations", "decisions",
                  "telephone"):
            self.assertNotIn(f"projects/brittle/{d}/", write_block)

    def test_states_the_connector_is_read_only(self):
        low = self.text.lower()
        self.assertIn("read-only", low)
        self.assertIn("403", self.text)


class TestSetupGuide(unittest.TestCase):
    def setUp(self):
        self.text = SETUP_PATH.read_text(encoding="utf-8")

    def test_documents_the_token_scope(self):
        for needed in ("Fine-grained", "digitalnomadjoe/messages",
                       "Contents", "Read and write", "Expiration"):
            self.assertIn(needed, self.text)

    def test_forbids_broader_permissions(self):
        for banned in ("Workflows", "Secrets", "Issues", "Pull requests",
                       "Administration"):
            self.assertIn(banned, self.text,
                          f"guide must explicitly deny {banned}")

    def test_requires_private_sharing(self):
        self.assertIn("Only me", self.text)

    def test_forbids_leaking_the_token(self):
        low = self.text.lower()
        self.assertIn("never", low)
        self.assertIn("action parameter", low)

    def test_contains_the_required_local_commands(self):
        for cmd in (
            "sh /home/robojoe/code/messages/scripts/install_services.sh",
            "systemctl --user enable --now brittle-browser-bridge.service",
            "systemctl --user status brittle-browser-bridge.service --no-pager",
        ):
            self.assertIn(cmd, self.text)

    def test_contains_a_copyable_system_prompt(self):
        low = self.text.lower()
        self.assertIn("getTelephoneStatus", self.text)
        self.assertIn("byte-for-byte", low)
        self.assertIn("never report it as success", low)
        self.assertIn("api reviewer", low)

    def test_states_the_connector_cannot_operate_telephone(self):
        low = self.text.lower()
        self.assertIn("read-only", low)
        self.assertIn("403", self.text)
        self.assertIn("cannot operate telephone", low)

    def test_forbids_the_wrong_remedies(self):
        low = self.text.lower()
        self.assertIn("do not try to fix this by reconnecting github", low)

    def test_no_real_token_in_the_guide(self):
        self.assertEqual(ml.scan_secrets(self.text), [])
        self.assertNotIn("github_pat_", self.text)


class TestDocsCorrection(unittest.TestCase):
    def test_all_four_guides_state_the_connector_is_read_only(self):
        for rel in ("AGENTS.md", "TELEPHONE.md", "README.md",
                    "skills/telephone/SKILL.md"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8").lower()
            with self.subTest(doc=rel):
                self.assertIn("read-only", text)
                self.assertIn("403", text)
                self.assertIn("action", text)


class TestActionArtifacts(BusTestCase):
    """The fixed-path files the Action reads."""

    def setUp(self):
        super().setUp()
        self.cfg["browser"] = {"allowed_identities": ["digitalnomadjoe"]}

    def test_instructions_file_combines_the_canonical_guides(self):
        for rel in bb.INSTRUCTION_SOURCES:
            p = self.repo_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# {rel}\ncontent-of-{rel}\n", encoding="utf-8")
        text = bb.build_instructions(self.repo)
        for rel in bb.INSTRUCTION_SOURCES:
            self.assertIn(f"content-of-{rel}", text)
            self.assertIn(f"===== {rel} =====", text)

    def test_result_record_is_sanitized(self):
        fm = ml.base_frontmatter("receipt", sender="bridge", to="joe",
                                 lane="control", unit="U", status="blocked")
        rid = "BREQ-20260729T180000Z-1a2b3c4d"
        fm.update({"receipt_type": "browser_result", "request_id": rid,
                   "reason": "refused because the ticket said /home/robojoe/secret"})
        ml.publish(self.repo, {f"{ml.DIR_RECEIPTS}/{fm['id']}.md":
                               ml.render_message(fm, "# refused\n")},
                   "refusal", cfg=self.cfg)
        msgs = self.msgs()
        rec = bb.result_record(rid, msgs[fm["id"]], msgs)
        self.assertEqual(set(rec) - {"reason"},
                         {"request_id", "status", "result_receipt",
                          "canonical_message_ids", "decided_at"})
        self.assertEqual(rec["status"], "refused")
        self.assertEqual(ml.scan_secrets(json.dumps(rec)), [])

    def test_recent_results_is_bounded_and_sanitized(self):
        payload = bb.build_browser_status(self.cfg, self.msgs(), self.repo,
                                          bb.BrowserBridge(self.cfg))
        br = payload["browser_requests"]
        for key in ("pending", "refused", "recent_results"):
            self.assertIn(key, br)
        self.assertLessEqual(len(br["recent_results"]), bb.RECENT_RESULTS_WINDOW)
        text = json.dumps(br)
        self.assertEqual(ml.scan_secrets(text), [])
        self.assertNotIn("/home/robojoe", text)

    def test_result_files_are_written_per_request(self):
        fm = ml.base_frontmatter("receipt", sender="bridge", to="joe",
                                 lane="control", unit="U", status="completed")
        rid = "BREQ-20260729T180000Z-1a2b3c4d"
        fm.update({"receipt_type": "browser_result", "request_id": rid})
        ml.publish(self.repo, {f"{ml.DIR_RECEIPTS}/{fm['id']}.md":
                               ml.render_message(fm, "# ok\n")}, "ok", cfg=self.cfg)
        bridge = bb.BrowserBridge(self.cfg)
        bridge.write_action_artifacts(self.msgs())
        path = self.repo_path / f"{bb.BROWSER_RESULTS_DIR}/{rid}.json"
        self.assertTrue(path.exists())
        rec = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(rec["request_id"], rid)
        self.assertEqual(rec["status"], "accepted")


class TestActionSubmissionPath(BusTestCase):
    """A submission shaped exactly as the Action makes it."""

    def setUp(self):
        super().setUp()
        self.cfg["browser"] = {"allowed_identities": ["digitalnomadjoe"]}
        self.repo.git("config", "user.name", "digitalnomadjoe")
        self.repo.git("config", "user.email", "digitalnomadjoe@gmail.com")

    def submit_like_action(self, req: dict):
        """base64 -> file, exactly what the Contents API PUT does."""
        rid = req["request_id"]
        encoded = base64.b64encode(
            (json.dumps(req, indent=2) + "\n").encode("utf-8")).decode("ascii")
        rel = f"{ml.DIR_BROWSER_REQUESTS}/{rid}.json"
        path = self.repo_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(encoded))
        self.repo.git("add", "--", rel)
        self.repo.git("commit", "-m", f"telephone: submit {rid}")
        return rid

    def base_request(self, **over):
        rid = ("BREQ-" + ml.utc_now().strftime("%Y%m%dT%H%M%SZ") + "-"
               + ml.secrets.token_hex(4))
        req = {"request_id": rid, "kind": "status_request", "project": "brittle",
               "submitted_by": "digitalnomadjoe",
               "created_at": ml.iso(ml.utc_now()), "lane": "control",
               "unit": "CERT-ACTION", "idempotency_key": ml.secrets.token_hex(8),
               "rationale": "action transport test"}
        req.update(over)
        return req

    def result(self, rid):
        for m in self.msgs().values():
            if m.get("receipt_type") == "browser_result" and m.get("request_id") == rid:
                return m
        return None

    def test_base64_round_trip_submission_is_accepted(self):
        rid = self.submit_like_action(self.base_request())
        bb.BrowserBridge(self.cfg).run_once()
        self.assertEqual(self.result(rid).get("status"), "completed")

    def test_ticket_text_survives_the_base64_transport_unchanged(self):
        report_id = self.publish_report(lane="control")
        exact = ("## Objective\nExact prose with `backticks`, \"quotes\", "
                 "emoji ✅ and\nmultiple lines.\n\n## Steps\n1. One.\n2. Two.\n")
        rid = self.submit_like_action(self.base_request(
            kind="review_and_ticket", report_id=report_id,
            payload={"summary": "s", "next_action": "n", "confidence": 0.95,
                     "ticket_title": "T", "ticket_markdown": exact,
                     "target_lane": "control"}))
        bb.BrowserBridge(self.cfg).run_once()
        self.assertEqual(self.result(rid).get("status"), "completed")
        ticket = [m for m in self.msgs().values() if m.kind == "ticket"][0]
        self.assertEqual(ticket.body.rstrip("\n"), exact.rstrip("\n"))

    def test_malformed_request_fails_closed(self):
        rid = "BREQ-20260729T180000Z-deadbeef"
        rel = f"{ml.DIR_BROWSER_REQUESTS}/{rid}.json"
        (self.repo_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.repo_path / rel).write_text("not json at all\n", encoding="utf-8")
        self.repo.git("add", "--", rel)
        self.repo.git("commit", "-m", "bad")
        bb.BrowserBridge(self.cfg).run_once()
        self.assertEqual(self.result(rid).get("status"), "blocked")

    def test_duplicate_intent_fails_closed(self):
        req = self.base_request()
        rid1 = self.submit_like_action(req)
        bb.BrowserBridge(self.cfg).run_once()
        req2 = self.base_request(idempotency_key=req["idempotency_key"])
        rid2 = self.submit_like_action(req2)
        bb.BrowserBridge(self.cfg).run_once()
        self.assertEqual(self.result(rid1).get("status"), "completed")
        self.assertEqual(self.result(rid2).get("status"), "blocked")

    def test_action_cannot_resolve_an_owner_escalation(self):
        rid = self.submit_like_action(self.base_request(
            kind="resolve_escalation",
            escalation_id="BRITTLE-20260729T120000Z-11223344",
            authorized_action="approve", scope="everything"))
        bb.BrowserBridge(self.cfg).run_once()
        r = self.result(rid)
        self.assertEqual(r.get("status"), "blocked")
        self.assertIn("owner authority", str(r.get("reason")))


if __name__ == "__main__":
    unittest.main()
