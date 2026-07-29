"""Frontmatter parsing, schema validation, IDs and uniqueness."""

from __future__ import annotations

import unittest

from harness import BusTestCase, ml


def _fm(**over):
    fm = ml.base_frontmatter("ticket", sender="reviewer", to="locomotion",
                             lane="locomotion", unit="12U-SYNTH", status="open")
    fm.update(over)
    return fm


class TestFrontmatter(unittest.TestCase):
    def test_roundtrip_preserves_every_field(self):
        fm = _fm(title="A: colon, and \"quotes\"", confidence=0.87)
        text = ml.render_message(fm, "# body\n\ncontent\n")
        back, body = ml.parse_message(text)
        self.assertEqual(back, fm)
        self.assertIn("content", body)

    def test_nulls_and_booleans_survive(self):
        fm = _fm(unit=None, confidence=None, requires_owner=False)
        back, _ = ml.parse_message(ml.render_message(fm, "x"))
        self.assertIsNone(back["unit"])
        self.assertIsNone(back["confidence"])
        self.assertIs(back["requires_owner"], False)

    def test_numeric_looking_strings_stay_strings(self):
        fm = _fm(unit="12", title="0.85")
        back, _ = ml.parse_message(ml.render_message(fm, "x"))
        self.assertEqual(back["unit"], "12")
        self.assertEqual(back["title"], "0.85")

    def test_indented_frontmatter_rejected(self):
        text = "---\nid: x\n  nested: y\n---\nbody\n"
        with self.assertRaisesRegex(ml.MessageError, "nested/indented"):
            ml.parse_message(text)

    def test_duplicate_key_rejected(self):
        text = "---\nid: a\nid: b\n---\nbody\n"
        with self.assertRaisesRegex(ml.MessageError, "duplicate frontmatter key"):
            ml.parse_message(text)

    def test_unterminated_block_rejected(self):
        with self.assertRaisesRegex(ml.MessageError, "unterminated"):
            ml.parse_message("---\nid: a\nkind: report\n")

    def test_missing_leading_marker_rejected(self):
        with self.assertRaisesRegex(ml.MessageError, "must begin with"):
            ml.parse_message("# not a message\n")


class TestSchema(unittest.TestCase):
    def test_valid_message_passes(self):
        ml.validate_frontmatter(_fm())

    def test_missing_required_field_rejected(self):
        fm = _fm()
        del fm["confidence"]
        with self.assertRaisesRegex(ml.MessageError, "missing required field"):
            ml.validate_frontmatter(fm)

    def test_unknown_field_rejected(self):
        fm = _fm()
        fm["exfiltrate"] = "yes"
        with self.assertRaisesRegex(ml.MessageError, "unknown field"):
            ml.validate_frontmatter(fm)

    def test_malformed_id_rejected(self):
        with self.assertRaisesRegex(ml.MessageError, "malformed id"):
            ml.validate_frontmatter(_fm(id="TICKET-1"))

    def test_bad_status_rejected(self):
        with self.assertRaisesRegex(ml.MessageError, "bad status"):
            ml.validate_frontmatter(_fm(status="in-progress"))

    def test_every_allowed_status_accepted(self):
        for status in ml.STATUSES:
            fm = _fm(kind="receipt", status=status, receipt_type="block",
                     in_reply_to=ml.new_id())
            ml.validate_frontmatter(fm)

    def test_bad_lane_rejected(self):
        with self.assertRaisesRegex(ml.MessageError, "bad lane"):
            ml.validate_frontmatter(_fm(lane="perception"))

    def test_requires_owner_must_be_boolean(self):
        with self.assertRaisesRegex(ml.MessageError, "requires_owner must be"):
            ml.validate_frontmatter(_fm(requires_owner="yes"))

    def test_confidence_range_enforced(self):
        with self.assertRaisesRegex(ml.MessageError, "confidence out of range"):
            ml.validate_frontmatter(_fm(confidence=1.4))

    def test_escalation_must_require_owner(self):
        fm = _fm(kind="escalation", lane="locomotion", requires_owner=False)
        with self.assertRaisesRegex(ml.MessageError, "requires_owner: true"):
            ml.validate_frontmatter(fm)

    def test_published_ticket_status_must_be_open(self):
        with self.assertRaisesRegex(ml.MessageError, "must have status 'open'"):
            ml.validate_frontmatter(_fm(status="claimed"))

    def test_ticket_lane_must_be_an_agent_lane(self):
        with self.assertRaisesRegex(ml.MessageError, "ticket lane must be"):
            ml.validate_frontmatter(_fm(lane="reviewer"))

    def test_report_requires_local_provenance(self):
        fm = _fm(kind="report", local_source_path=None, local_source_sha256=None)
        with self.assertRaisesRegex(ml.MessageError, "local_source_path"):
            ml.validate_frontmatter(fm)

    def test_claim_receipt_requires_lease_fields(self):
        fm = _fm(kind="receipt", status="claimed", in_reply_to=ml.new_id())
        fm["receipt_type"] = "claim"
        with self.assertRaisesRegex(ml.MessageError, "claim receipt requires"):
            ml.validate_frontmatter(fm)

    def test_owner_decision_must_come_from_joe(self):
        fm = _fm(kind="owner_decision", status="resolved")
        fm.update({"authorized_action": "a", "scope": "s", "checksum": "c"})
        with self.assertRaisesRegex(ml.MessageError, "must originate from 'joe'"):
            ml.validate_frontmatter(fm)

    def test_bad_reference_rejected(self):
        with self.assertRaisesRegex(ml.MessageError, "bad reference"):
            ml.validate_frontmatter(_fm(in_reply_to="not-an-id"))


class TestIdentity(unittest.TestCase):
    def test_id_grammar(self):
        self.assertRegex(ml.new_id(), ml.ID_RE)

    def test_ids_are_unique_under_load(self):
        ids = {ml.new_id() for _ in range(2000)}
        self.assertEqual(len(ids), 2000)


class TestOrdering(unittest.TestCase):
    """(created_at, id) must reflect authorship order, not a random suffix."""

    def test_timestamps_carry_sub_second_resolution(self):
        stamp = ml.iso(ml.utc_now())
        self.assertRegex(stamp, ml.TS_RE)
        self.assertRegex(stamp, r"\.\d{6}Z$",
                         "second-resolution timestamps cannot order a burst")

    def test_created_at_dominates_the_random_id_suffix(self):
        # An earlier message with a high random suffix must still sort first.
        early = _fm(id="BRITTLE-20260728T101500Z-ffffffff",
                    created_at="2026-07-28T10:15:00.000001Z")
        late = _fm(id="BRITTLE-20260728T101500Z-00000000",
                   created_at="2026-07-28T10:15:00.000002Z")
        msgs = [ml.Message(late, "b", "x/late.md"), ml.Message(early, "b", "x/early.md")]
        self.assertEqual([m.id for m in sorted(msgs, key=ml.Message.sort_key)],
                         [early["id"], late["id"]])

    def test_identical_timestamps_fall_back_to_a_stable_id_order(self):
        same = "2026-07-28T10:15:00.000000Z"
        a = _fm(id="BRITTLE-20260728T101500Z-00000001", created_at=same)
        b = _fm(id="BRITTLE-20260728T101500Z-00000002", created_at=same)
        msgs = [ml.Message(b, "x", "x/b.md"), ml.Message(a, "x", "x/a.md")]
        self.assertEqual([m.id for m in sorted(msgs, key=ml.Message.sort_key)],
                         [a["id"], b["id"]], "ties must still be a total order")

    def test_second_precision_timestamps_still_parse(self):
        self.assertEqual(ml.parse_iso("2026-07-28T10:15:00Z").minute, 15)
        self.assertEqual(ml.parse_iso("2026-07-28T10:15:00.250Z").microsecond, 250000)

    def test_iso_roundtrips(self):
        now = ml.utc_now()
        self.assertEqual(ml.iso(ml.parse_iso(ml.iso(now))), ml.iso(now))


class TestUniqueness(BusTestCase):
    def test_duplicate_ids_across_directories_rejected(self):
        mid = ml.new_id()
        for lane in ("locomotion", "control"):
            fm = _fm(id=mid, kind="report", lane=lane, status="open",
                     local_source_path="/tmp/x.md", local_source_sha256="0" * 64)
            path = self.repo_path / ml.DIR_REPORTS / lane / f"{mid}.md"
            path.write_text(ml.render_message(fm, "body"), encoding="utf-8")
        with self.assertRaisesRegex(ml.MessageError, "duplicate message id"):
            ml.load_messages(self.repo_path)

    def test_filename_must_match_id(self):
        fm = _fm()
        path = self.repo_path / ml.DIR_TICKETS / "locomotion" / "wrong-name.md"
        path.write_text(ml.render_message(fm, "body"), encoding="utf-8")
        with self.assertRaisesRegex(ml.MessageError, "filename must be"):
            ml.load_messages(self.repo_path)

    def test_wrong_directory_detected_by_validator(self):
        fm = _fm()
        path = self.repo_path / ml.DIR_TICKETS / "control" / f"{fm['id']}.md"
        path.write_text(ml.render_message(fm, "body"), encoding="utf-8")
        problems = ml.validate_repo(self.repo_path)
        self.assertTrue(any("belongs in" in p for p in problems), problems)

    def test_dangling_reference_detected(self):
        fm = _fm(in_reply_to=ml.new_id())
        path = self.repo_path / ml.DIR_TICKETS / "locomotion" / f"{fm['id']}.md"
        path.write_text(ml.render_message(fm, "body"), encoding="utf-8")
        problems = ml.validate_repo(self.repo_path)
        self.assertTrue(any("unknown message" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
