"""The JSON schema and the Python validator must never drift apart.

A gate re-implemented at a second entry point will drift. messagelib is the
single executable source of truth; protocol/message.schema.json is
documentation and editor support. This test binds them together so that adding
a field to one without the other fails CI.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from harness import SCRIPTS, ml

SCHEMA_PATH = SCRIPTS.parent / "protocol" / "message.schema.json"


class TestSchemaParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SCHEMA_PATH.exists():
            raise unittest.SkipTest(f"{SCHEMA_PATH} not present")
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_required_fields_match(self):
        self.assertEqual(sorted(self.schema["required"]), sorted(ml.REQUIRED_FIELDS))

    def test_property_set_matches(self):
        self.assertEqual(sorted(self.schema["properties"]), sorted(ml.ALL_FIELDS))

    def test_no_additional_properties(self):
        self.assertFalse(self.schema["additionalProperties"],
                         "a public bus must reject unknown frontmatter fields")

    def test_kind_enum_matches(self):
        self.assertEqual(sorted(self.schema["properties"]["kind"]["enum"]),
                         sorted(ml.KINDS))

    def test_lane_enum_matches(self):
        self.assertEqual(sorted(self.schema["properties"]["lane"]["enum"]),
                         sorted(ml.LANES))

    def test_status_enum_matches(self):
        self.assertEqual(sorted(self.schema["properties"]["status"]["enum"]),
                         sorted(ml.STATUSES))

    def test_receipt_type_enum_matches(self):
        enum = [v for v in self.schema["properties"]["receipt_type"]["enum"]
                if v is not None]
        self.assertEqual(sorted(enum), sorted(ml.RECEIPT_TYPES))

    def test_id_pattern_matches(self):
        self.assertEqual(self.schema["properties"]["id"]["pattern"],
                         ml.ID_RE.pattern)

    def test_created_at_pattern_matches(self):
        self.assertEqual(self.schema["properties"]["created_at"]["pattern"],
                         ml.TS_RE.pattern)

    def test_examples_validate_against_the_python_validator(self):
        examples = sorted((SCRIPTS.parent / "protocol" / "examples").glob("*.md"))
        self.assertTrue(examples, "protocol/examples/ must contain worked examples")
        for path in examples:
            with self.subTest(example=path.name):
                fm, _ = ml.parse_message(path.read_text(encoding="utf-8"))
                ml.validate_frontmatter(fm, rel_path=str(path))


class TestPolicyConstants(unittest.TestCase):
    def test_message_payloads_are_text_only(self):
        self.assertEqual(ml.ALLOWED_EXT_MESSAGES, {".md", ".json"})

    def test_dangerous_extensions_are_forbidden(self):
        for ext in (".npz", ".npy", ".ckpt", ".pt", ".mp4", ".png", ".log",
                    ".env", ".pem", ".key", ".zip"):
            self.assertIn(ext, ml.FORBIDDEN_EXT, ext)

    def test_size_limits_are_bounded(self):
        self.assertLessEqual(ml.MAX_MESSAGE_BYTES, 512 * 1024)
        self.assertLessEqual(ml.MAX_REPO_FILE_BYTES, 1024 * 1024)

    def test_default_threshold_is_the_specified_one(self):
        self.assertEqual(ml.DEFAULT_MIN_CONFIDENCE, 0.85)


if __name__ == "__main__":
    unittest.main()
