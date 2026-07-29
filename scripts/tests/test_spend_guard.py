"""Fail-closed local spending guard.

Every test here is offline. Nothing in this module opens a socket, and
`TestNoNetwork` proves the guard refuses before a socket could be opened.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# harness must be imported first: it puts scripts/ on sys.path
from harness import BusTestCase, ml
import messagesctl
import reviewer_daemon as rd
from spend_guard import (MICRO, Reservation, SpendGuard, SpendGuardError,
                         SpendLimitExceeded, usd)

MODEL = "test-model"


def base_cfg(state: Path, **spend) -> dict:
    s = {
        "monthly_cap_usd": 5.00,
        "daily_cap_usd": 0.50,
        "max_calls_per_day": 10,
        "max_completion_tokens": 1000,
        "ledger_path": str(state / "spend_ledger.jsonl"),
        "pricing_input_usd_per_1m": {MODEL: 2.50},
        "pricing_output_usd_per_1m": {MODEL: 10.00},
    }
    s.update(spend)
    return {"reviewer": {"model": MODEL}, "spending": s}


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="brittle-spend-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = _dt.datetime(2026, 7, 29, 12, 0, 0, tzinfo=_dt.timezone.utc)

    def now(self):
        return self.clock

    def guard(self, **spend) -> SpendGuard:
        return SpendGuard(base_cfg(self.tmp, **spend), now_fn=self.now)

    def ledger_lines(self) -> list[dict]:
        p = self.tmp / "spend_ledger.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


class TestPricing(GuardTestCase):
    def test_unknown_model_fails_closed(self):
        g = self.guard()
        with self.assertRaisesRegex(SpendGuardError, "no configured price"):
            g.reserve("gpt-9-unreleased", 1000)
        self.assertEqual(self.ledger_lines(), [],
                         "a refused request must not touch the ledger")

    def test_model_priced_on_only_one_side_fails_closed(self):
        g = self.guard(pricing_output_usd_per_1m={})
        with self.assertRaisesRegex(SpendGuardError, "no configured price"):
            g.reserve(MODEL, 1000)

    def test_empty_pricing_tables_refuse_everything(self):
        g = self.guard(pricing_input_usd_per_1m={}, pricing_output_usd_per_1m={})
        with self.assertRaises(SpendGuardError):
            g.reserve(MODEL, 10)

    def test_malformed_price_fails_closed(self):
        g = self.guard(pricing_input_usd_per_1m={MODEL: "free"})
        with self.assertRaisesRegex(SpendGuardError, "malformed price"):
            g.reserve(MODEL, 10)

    def test_reservation_is_worst_case(self):
        g = self.guard()
        # 4000 request bytes counted as 4000 input tokens + 1000 output tokens
        # at $2.50/$10.00 per 1M  ->  4000*2.5 + 1000*10 = 20000 micro = $0.02
        res = g.reserve(MODEL, 4000)
        self.assertEqual(res.reserved_micro, 20_000)
        self.assertEqual(usd(res.reserved_micro), 0.02)
        self.assertEqual(res.est_input_tokens, 4000)
        self.assertEqual(res.max_output_tokens, 1000)


class TestCaps(GuardTestCase):
    def test_daily_cap_enforced(self):
        g = self.guard(daily_cap_usd=0.05, max_calls_per_day=100)
        for _ in range(2):
            g.reserve(MODEL, 4000)           # $0.02 each -> $0.04
        with self.assertRaisesRegex(SpendLimitExceeded, "daily cap"):
            g.reserve(MODEL, 4000)           # would reach $0.06 > $0.05
        self.assertEqual(g.usage()["calls_today"], 2)

    def test_monthly_cap_enforced(self):
        g = self.guard(daily_cap_usd=100.0, monthly_cap_usd=0.05,
                       max_calls_per_day=100)
        g.reserve(MODEL, 4000)
        g.reserve(MODEL, 4000)
        with self.assertRaisesRegex(SpendLimitExceeded, "monthly cap"):
            g.reserve(MODEL, 4000)

    def test_max_calls_per_day_enforced(self):
        g = self.guard(max_calls_per_day=3, daily_cap_usd=1000.0,
                       monthly_cap_usd=1000.0)
        for _ in range(3):
            g.reserve(MODEL, 10)
        with self.assertRaisesRegex(SpendLimitExceeded, "daily call limit"):
            g.reserve(MODEL, 10)

    def test_a_single_oversized_request_is_refused_outright(self):
        g = self.guard(daily_cap_usd=0.001)
        with self.assertRaises(SpendLimitExceeded):
            g.reserve(MODEL, 100_000)
        self.assertEqual(self.ledger_lines(), [])

    def test_refusal_leaves_allowance_untouched(self):
        g = self.guard(daily_cap_usd=0.05, max_calls_per_day=100)
        g.reserve(MODEL, 4000)
        before = g.usage()
        with self.assertRaises(SpendLimitExceeded):
            g.reserve(MODEL, 400_000)
        self.assertEqual(g.usage(), before)

    def test_defaults_match_the_agreed_limits(self):
        g = SpendGuard({"spending": {"ledger_path": str(self.tmp / "l.jsonl")}},
                       now_fn=self.now)
        self.assertEqual(g.monthly_cap_micro, 5_000_000)     # $5.00
        self.assertEqual(g.daily_cap_micro, 500_000)         # $0.50
        self.assertEqual(g.max_calls_per_day, 10)
        self.assertEqual(g.max_completion_tokens, 1000)


class TestFinalization(GuardTestCase):
    def test_actual_usage_replaces_the_reservation(self):
        g = self.guard()
        res = g.reserve(MODEL, 4000)                 # reserved $0.02
        actual = g.finalize(res, input_tokens=900, output_tokens=200)
        # 900*2.5 + 200*10 = 4250 micro
        self.assertEqual(actual, 4250)
        self.assertEqual(g.usage()["day_micro"], 4250,
                         "the reservation must be released down to actual cost")

    def test_timeout_charges_the_full_reservation(self):
        g = self.guard()
        res = g.reserve(MODEL, 4000)
        g.finalize_uncertain(res, "timeout")
        self.assertEqual(g.usage()["day_micro"], res.reserved_micro)

    def test_definite_non_billing_releases_the_reservation(self):
        g = self.guard()
        res = g.reserve(MODEL, 4000)
        g.release(res, "http 429")
        self.assertEqual(g.usage()["day_micro"], 0)
        self.assertEqual(g.usage()["calls_today"], 1,
                         "an attempt still counts against the daily call limit")

    def test_outstanding_reservation_counts_at_full_price(self):
        g = self.guard()
        res = g.reserve(MODEL, 4000)
        self.assertEqual(g.usage()["day_micro"], res.reserved_micro)
        self.assertEqual(g.usage()["outstanding_today"], 1)

    def test_double_finalize_refused(self):
        g = self.guard()
        res = g.reserve(MODEL, 4000)
        g.finalize(res, 100, 100)
        with self.assertRaisesRegex(SpendGuardError, "already finalize"):
            g.finalize(res, 100, 100)
        with self.assertRaisesRegex(SpendGuardError, "already finalize"):
            g.finalize_uncertain(res, "retry")

    def test_usage_exceeding_the_reservation_is_charged_in_full(self):
        g = self.guard()
        res = g.reserve(MODEL, 10)          # tiny reservation
        actual = g.finalize(res, input_tokens=100_000, output_tokens=100_000)
        self.assertGreater(actual, res.reserved_micro)
        self.assertEqual(g.usage()["day_micro"], actual,
                         "never under-count money that was actually spent")


class TestRestartAndLedger(GuardTestCase):
    def test_restart_still_sees_an_outstanding_reservation(self):
        g1 = self.guard()
        res = g1.reserve(MODEL, 4000)
        # process dies here, no terminal entry written
        g2 = self.guard()                       # a fresh process
        self.assertEqual(g2.usage()["day_micro"], res.reserved_micro)
        self.assertEqual(g2.usage()["outstanding_today"], 1)

    def test_restart_cannot_overspend_via_forgotten_reservations(self):
        capped = dict(daily_cap_usd=0.05, max_calls_per_day=100)
        for _ in range(2):
            # each reservation is made by a fresh guard == a restarted process,
            # and none of them is ever finalized
            self.guard(**capped).reserve(MODEL, 4000)
        with self.assertRaises(SpendLimitExceeded):
            self.guard(**capped).reserve(MODEL, 4000)

    def test_missing_ledger_is_zero_spend(self):
        g = self.guard()
        self.assertEqual(g.usage()["day_micro"], 0)
        self.assertEqual(g.usage()["calls_today"], 0)

    def test_corrupt_ledger_fails_closed(self):
        g = self.guard()
        g.reserve(MODEL, 100)
        path = self.tmp / "spend_ledger.jsonl"
        path.write_text(path.read_text() + "{not json at all\n", encoding="utf-8")
        with self.assertRaisesRegex(SpendGuardError, "corrupt"):
            g.reserve(MODEL, 100)
        with self.assertRaisesRegex(SpendGuardError, "corrupt"):
            g.usage()

    def test_non_entry_json_fails_closed(self):
        g = self.guard()
        path = self.tmp / "spend_ledger.jsonl"
        path.write_text('["not", "an", "entry"]\n', encoding="utf-8")
        with self.assertRaisesRegex(SpendGuardError, "corrupt"):
            g.reserve(MODEL, 100)

    def test_ledger_is_append_only_and_0600(self):
        g = self.guard()
        r1 = g.reserve(MODEL, 100)
        first = (self.tmp / "spend_ledger.jsonl").read_text()
        g.finalize(r1, 10, 10)
        second = (self.tmp / "spend_ledger.jsonl").read_text()
        self.assertTrue(second.startswith(first), "entries may only be appended")
        mode = (self.tmp / "spend_ledger.jsonl").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, f"ledger mode is {oct(mode)}, want 0600")

    def test_ledger_records_money_and_tokens_only(self):
        g = self.guard()
        res = g.reserve(MODEL, 4000)
        g.finalize(res, 900, 200)
        text = (self.tmp / "spend_ledger.jsonl").read_text().lower()
        for forbidden in ("bearer", "authorization", "sk-", "prompt", "content",
                          "message", "report", "survival"):
            self.assertNotIn(forbidden, text, f"ledger leaked {forbidden!r}")
        entry = self.ledger_lines()[0]
        self.assertEqual(set(entry) - {"type", "id", "ts", "day", "month", "model",
                                       "est_input_tokens", "max_output_tokens",
                                       "reserved_micro", "reserved_usd"}, set())


class TestConcurrency(GuardTestCase):
    def test_concurrent_reservations_cannot_overspend(self):
        # cap allows exactly 3 reservations of $0.02
        g_cfg = dict(daily_cap_usd=0.06, monthly_cap_usd=100.0, max_calls_per_day=100)
        ok, refused = [], []

        def attempt(_):
            try:
                self.guard(**g_cfg).reserve(MODEL, 4000)
                ok.append(1)
            except SpendLimitExceeded:
                refused.append(1)

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(attempt, range(12)))

        self.assertEqual(len(ok), 3, f"expected exactly 3 grants, got {len(ok)}")
        self.assertEqual(len(refused), 9)
        self.assertLessEqual(self.guard(**g_cfg).usage()["day_micro"], 60_000)

    def test_concurrent_call_limit_is_exact(self):
        cfg = dict(max_calls_per_day=4, daily_cap_usd=1000.0, monthly_cap_usd=1000.0)
        granted = []

        def attempt(_):
            try:
                self.guard(**cfg).reserve(MODEL, 10)
                granted.append(1)
            except SpendLimitExceeded:
                pass

        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(attempt, range(10)))
        self.assertEqual(len(granted), 4)

    def test_concurrent_finalize_cannot_double_charge(self):
        g = self.guard()
        res = g.reserve(MODEL, 4000)
        errors, wins = [], []

        def attempt(_):
            try:
                self.guard().finalize(res, 100, 100)
                wins.append(1)
            except SpendGuardError:
                errors.append(1)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(attempt, range(8)))
        self.assertEqual(len(wins), 1, "exactly one finalize may win")
        self.assertEqual(len(errors), 7)


class TestRollover(GuardTestCase):
    def test_utc_day_rollover_resets_the_daily_budget(self):
        g = self.guard(daily_cap_usd=0.05, max_calls_per_day=2)
        g.reserve(MODEL, 4000)
        g.reserve(MODEL, 4000)
        with self.assertRaises(SpendLimitExceeded):
            g.reserve(MODEL, 4000)

        self.clock = self.clock + _dt.timedelta(days=1)
        g2 = self.guard(daily_cap_usd=0.05, max_calls_per_day=2)
        g2.reserve(MODEL, 4000)                      # new UTC day, fresh budget
        self.assertEqual(g2.usage()["calls_today"], 1)

    def test_month_budget_accumulates_across_days(self):
        g = self.guard(daily_cap_usd=100.0, monthly_cap_usd=0.05,
                       max_calls_per_day=100)
        g.reserve(MODEL, 4000)
        self.clock = self.clock + _dt.timedelta(days=1)
        g2 = self.guard(daily_cap_usd=100.0, monthly_cap_usd=0.05,
                        max_calls_per_day=100)
        g2.reserve(MODEL, 4000)
        with self.assertRaisesRegex(SpendLimitExceeded, "monthly"):
            g2.reserve(MODEL, 4000)

    def test_month_rollover_resets_the_monthly_budget(self):
        g = self.guard(daily_cap_usd=100.0, monthly_cap_usd=0.05,
                       max_calls_per_day=100)
        g.reserve(MODEL, 4000)
        g.reserve(MODEL, 4000)
        with self.assertRaises(SpendLimitExceeded):
            g.reserve(MODEL, 4000)

        self.clock = _dt.datetime(2026, 8, 1, 0, 0, 1, tzinfo=_dt.timezone.utc)
        g2 = self.guard(daily_cap_usd=100.0, monthly_cap_usd=0.05,
                        max_calls_per_day=100)
        g2.reserve(MODEL, 4000)
        self.assertEqual(g2.usage()["month"], "2026-08")

    def test_day_boundary_is_utc_not_local(self):
        self.clock = _dt.datetime(2026, 7, 29, 23, 59, 59, tzinfo=_dt.timezone.utc)
        g = self.guard()
        g.reserve(MODEL, 100)
        self.assertEqual(g.usage()["day"], "2026-07-29")
        self.clock = _dt.datetime(2026, 7, 30, 0, 0, 1, tzinfo=_dt.timezone.utc)
        self.assertEqual(self.guard().usage()["day"], "2026-07-30")
        self.assertEqual(self.guard().usage()["calls_today"], 0)


class TestStatus(GuardTestCase):
    def test_status_reports_the_full_picture(self):
        g = self.guard()
        res = g.reserve(MODEL, 4000)
        g.finalize(res, 900, 200)
        s = g.status()
        self.assertTrue(s["healthy"])
        self.assertFalse(s["blocked"])
        self.assertEqual(s["utc_day"], "2026-07-29")
        self.assertEqual(s["utc_month"], "2026-07")
        self.assertEqual(s["day_committed_usd"], 0.00425)
        self.assertEqual(s["day_cap_usd"], 0.50)
        self.assertEqual(s["day_remaining_usd"], round(0.5 - 0.00425, 6))
        self.assertEqual(s["month_cap_usd"], 5.00)
        self.assertEqual(s["calls_today"], 1)
        self.assertEqual(s["max_calls_per_day"], 10)
        self.assertEqual(s["max_completion_tokens"], 1000)
        self.assertIn(MODEL, s["priced_models"])

    def test_status_blocks_on_exhausted_daily_cap(self):
        g = self.guard(daily_cap_usd=0.02, max_calls_per_day=100)
        g.reserve(MODEL, 4000)                     # exactly $0.02
        s = g.status()
        self.assertTrue(s["blocked"])
        self.assertIn("daily cap exhausted", s["blocked_reasons"])

    def test_status_blocks_on_call_limit(self):
        g = self.guard(max_calls_per_day=1)
        g.reserve(MODEL, 10)
        self.assertIn("daily call limit reached (1/1)", g.status()["blocked_reasons"])

    def test_status_blocks_on_unpriced_configured_model(self):
        cfg = base_cfg(self.tmp)
        cfg["reviewer"]["model"] = "gpt-9-unreleased"
        s = SpendGuard(cfg, now_fn=self.now).status()
        self.assertTrue(s["blocked"])
        self.assertTrue(any("no configured price" in r for r in s["blocked_reasons"]))

    def test_status_reports_unhealthy_ledger_without_crashing(self):
        (self.tmp / "spend_ledger.jsonl").write_text("garbage\n", encoding="utf-8")
        s = self.guard().status()
        self.assertFalse(s["healthy"])
        self.assertTrue(s["blocked"])
        self.assertIn("ledger unreadable", s["blocked_reasons"])


class TestMessagesctlStatus(BusTestCase):
    def test_status_includes_spending_block(self):
        import contextlib
        import io

        cfg_path = self.tmp / "config.toml"
        cfg_path.write_text(
            f'[repo]\npath = "{self.repo_path}"\nbrittle_path = "{self.brittle}"\n'
            f'[reviewer]\nmodel = "{MODEL}"\n'
            f"[spending]\nmonthly_cap_usd = 5.00\ndaily_cap_usd = 0.50\n"
            f"max_calls_per_day = 10\nmax_completion_tokens = 1000\n"
            f'ledger_path = "{self.tmp}/led.jsonl"\n'
            f'[spending.pricing_input_usd_per_1m]\n"{MODEL}" = 2.50\n'
            f'[spending.pricing_output_usd_per_1m]\n"{MODEL}" = 10.00\n',
            encoding="utf-8")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            messagesctl.main(["--config", str(cfg_path), "--json", "status"])
        s = json.loads(buf.getvalue())["spending"]
        self.assertEqual(s["day_cap_usd"], 0.50)
        self.assertEqual(s["month_cap_usd"], 5.00)
        self.assertEqual(s["max_calls_per_day"], 10)
        self.assertFalse(s["blocked"])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            messagesctl.main(["--config", str(cfg_path), "status"])
        text = buf.getvalue()
        self.assertIn("spend guard", text)
        self.assertIn("committed", text)
        self.assertIn("remaining", text)
        self.assertIn("calls today", text)


class TestNoNetwork(GuardTestCase):
    """The guard must refuse BEFORE a socket could be opened."""

    def setUp(self):
        super().setUp()
        self.opened = []
        import urllib.request

        self._real = urllib.request.urlopen

        def tripwire(*a, **k):
            self.opened.append(a[:1])
            raise AssertionError("NETWORK CALL ATTEMPTED — guard failed to refuse")

        urllib.request.urlopen = tripwire
        self.addCleanup(setattr, urllib.request, "urlopen", self._real)
        os.environ.pop("BRITTLE_REVIEWER_MOCK", None)
        os.environ["OPENAI_API_KEY"] = "test-key-not-real-and-never-sent"
        self.addCleanup(os.environ.pop, "OPENAI_API_KEY", None)

    def test_exhausted_cap_refuses_before_any_socket_opens(self):
        cfg = base_cfg(self.tmp, daily_cap_usd=0.02, max_calls_per_day=100)
        cfg["reviewer"].update({"model": MODEL, "request_timeout_seconds": 5})
        g = SpendGuard(cfg, now_fn=self.now)
        g.reserve(MODEL, 4000)                       # consumes the whole cap

        with self.assertRaises(SpendLimitExceeded):
            rd.call_model(cfg, "system prompt", "user content" * 200,
                          guard=SpendGuard(cfg, now_fn=self.now))
        self.assertEqual(self.opened, [], "no network call may be attempted")

    def test_unpriced_model_refuses_before_any_socket_opens(self):
        cfg = base_cfg(self.tmp)
        cfg["reviewer"]["model"] = "gpt-9-unreleased"
        with self.assertRaisesRegex(SpendGuardError, "no configured price"):
            rd.call_model(cfg, "system", "user",
                          guard=SpendGuard(cfg, now_fn=self.now))
        self.assertEqual(self.opened, [])

    def test_corrupt_ledger_refuses_before_any_socket_opens(self):
        (self.tmp / "spend_ledger.jsonl").write_text("garbage\n", encoding="utf-8")
        cfg = base_cfg(self.tmp)
        with self.assertRaisesRegex(SpendGuardError, "corrupt"):
            rd.call_model(cfg, "system", "user",
                          guard=SpendGuard(cfg, now_fn=self.now))
        self.assertEqual(self.opened, [])

    def test_mocked_response_never_touches_guard_or_network(self):
        mock = self.tmp / "mock.json"
        mock.write_text(json.dumps({
            "summary": "s", "target_lane": None, "next_action": "n",
            "ticket_title": None, "ticket_markdown": None, "requires_owner": False,
            "owner_question": None, "confidence": 0.9, "reasoning_summary": "r",
        }), encoding="utf-8")
        os.environ["BRITTLE_REVIEWER_MOCK"] = str(mock)
        self.addCleanup(os.environ.pop, "BRITTLE_REVIEWER_MOCK", None)

        cfg = base_cfg(self.tmp)
        out = rd.call_model(cfg, "system", "user")
        self.assertIn("reasoning_summary", out)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.ledger_lines(), [],
                         "a mocked call costs nothing and must not be ledgered")


class TestDaemonIntegration(GuardTestCase):
    """Failure paths charge correctly, using a fake transport (still no network)."""

    def setUp(self):
        super().setUp()
        import urllib.request

        self._real = urllib.request.urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", self._real)
        os.environ.pop("BRITTLE_REVIEWER_MOCK", None)
        os.environ["OPENAI_API_KEY"] = "test-key-not-real-and-never-sent"
        self.addCleanup(os.environ.pop, "OPENAI_API_KEY", None)
        self.cfg = base_cfg(self.tmp)
        self.cfg["reviewer"].update({"model": MODEL, "request_timeout_seconds": 5})

    def _patch(self, fn):
        import urllib.request

        urllib.request.urlopen = fn

    def test_timeout_charges_full_reservation(self):
        def boom(*a, **k):
            raise TimeoutError("timed out")

        self._patch(boom)
        g = SpendGuard(self.cfg, now_fn=self.now)
        with self.assertRaises(rd.ReviewerError):
            rd.call_model(self.cfg, "sys", "usr", guard=g)
        u = g.usage()
        self.assertGreater(u["day_micro"], 0, "a timeout must still be charged")
        kinds = [e["type"] for e in self.ledger_lines()]
        self.assertEqual(kinds, ["reserve", "uncertain"])

    def test_4xx_releases_the_reservation(self):
        import urllib.error

        def reject(*a, **k):
            raise urllib.error.HTTPError("u", 429, "quota", {},
                                         __import__("io").BytesIO(b'{"error":{}}'))

        self._patch(reject)
        g = SpendGuard(self.cfg, now_fn=self.now)
        with self.assertRaises(rd.ReviewerError):
            rd.call_model(self.cfg, "sys", "usr", guard=g)
        self.assertEqual(g.usage()["day_micro"], 0, "a 429 was never billed")
        self.assertEqual([e["type"] for e in self.ledger_lines()],
                         ["reserve", "release"])

    def test_5xx_charges_full_reservation(self):
        import urllib.error

        def boom(*a, **k):
            raise urllib.error.HTTPError("u", 500, "oops", {},
                                         __import__("io").BytesIO(b'{"error":{}}'))

        self._patch(boom)
        g = SpendGuard(self.cfg, now_fn=self.now)
        with self.assertRaises(rd.ReviewerError):
            rd.call_model(self.cfg, "sys", "usr", guard=g)
        self.assertGreater(g.usage()["day_micro"], 0,
                           "a 5xx may have been billed; charge it")

    def test_response_without_usage_charges_full_reservation(self):
        import io as _io

        class Resp:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        self._patch(lambda *a, **k: Resp())
        g = SpendGuard(self.cfg, now_fn=self.now)
        rd.call_model(self.cfg, "sys", "usr", guard=g)
        self.assertEqual([e["type"] for e in self.ledger_lines()],
                         ["reserve", "uncertain"])

    def test_successful_call_finalizes_actual_usage(self):
        class Resp:
            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 900, "completion_tokens": 200},
                }).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        self._patch(lambda *a, **k: Resp())
        g = SpendGuard(self.cfg, now_fn=self.now)
        rd.call_model(self.cfg, "sys", "usr", guard=g)
        self.assertEqual(g.usage()["day_micro"], 4250)
        self.assertEqual([e["type"] for e in self.ledger_lines()],
                         ["reserve", "finalize"])

    def test_no_secret_or_message_content_reaches_ledger_or_logs(self):
        import logging

        SECRET = "sk-proj-" + "Zz9YyXxWwVvUuTtSsRrQqPpOoNn0123456789"
        BODY = ("CONFIDENTIAL-REPORT-BODY survival 812 steps, crown checkpoint "
                "sil027v2_rollpolish, pelvis roll 2.76 degrees")
        os.environ["OPENAI_API_KEY"] = SECRET

        class Resp:
            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 900, "completion_tokens": 200},
                }).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        self._patch(lambda *a, **k: Resp())

        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        logging.disable(logging.NOTSET)
        rd.LOG.addHandler(handler)
        rd.LOG.setLevel(logging.DEBUG)
        self.addCleanup(rd.LOG.removeHandler, handler)

        g = SpendGuard(self.cfg, now_fn=self.now)
        rd.call_model(self.cfg, "SYSTEM PROMPT " + BODY, "USER CONTENT " + BODY,
                      guard=g)

        ledger_text = (self.tmp / "spend_ledger.jsonl").read_text()
        log_text = "\n".join(records)
        status_text = json.dumps(g.status())

        self.assertTrue(records, "the guard should log something")
        for surface, name in ((ledger_text, "ledger"), (log_text, "logs"),
                              (status_text, "status")):
            self.assertNotIn(SECRET, surface, f"credential leaked into {name}")
            self.assertNotIn("sk-proj-", surface, f"credential shape in {name}")
            self.assertNotIn("CONFIDENTIAL-REPORT-BODY", surface,
                             f"report body leaked into {name}")
            self.assertNotIn("sil027v2", surface, f"checkpoint name in {name}")
            self.assertNotIn("SYSTEM PROMPT", surface, f"prompt leaked into {name}")

    def test_request_carries_the_completion_token_ceiling(self):
        captured = {}

        class Resp:
            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def capture(req, *a, **k):
            captured["body"] = json.loads(req.data.decode())
            return Resp()

        self._patch(capture)
        rd.call_model(self.cfg, "sys", "usr",
                      guard=SpendGuard(self.cfg, now_fn=self.now))
        self.assertEqual(captured["body"]["max_tokens"], 1000)


if __name__ == "__main__":
    unittest.main()
