#!/usr/bin/env python3
"""spend_guard -- a fail-closed local hard cap on OpenAI spending.

This is a *local* guard, not a billing source of truth. It exists so that a
runaway loop, a retry storm or a mistaken config cannot spend more than Joe
agreed to, independent of anything OpenAI does or does not enforce.

Design rules, all of which fail closed:

* Unknown or missing model pricing  -> refuse the request.
* Corrupt or unreadable ledger      -> refuse the request.
* Reservation would breach any cap  -> refuse the request.
* Timed-out or uncertain outcome    -> charge the FULL reservation.
* An outstanding reservation with no terminal entry (e.g. the process was
  killed mid-flight) keeps counting at full price, so a restart can never
  "forget" money it may have spent.

Money is handled in integer micro-dollars (1e-6 USD) throughout, so no float
rounding can ever let a cap drift upward.

The guard never sees or records a prompt, a report body, a credential or an
authorization header. It records only: model name, token counts, byte count,
and money.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import math
import os
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import messagelib as ml

MICRO = 1_000_000  # micro-dollars per USD

DEFAULT_SPENDING: dict[str, Any] = {
    "monthly_cap_usd": 5.00,
    "daily_cap_usd": 0.50,
    "max_calls_per_day": 10,
    "max_completion_tokens": 1000,
    "ledger_path": "",
    # Pricing is deliberately EMPTY here. Prices belong in local configuration;
    # a model with no configured price is refused rather than guessed.
    "pricing_input_usd_per_1m": {},
    "pricing_output_usd_per_1m": {},
}

TERMINAL_TYPES = ("finalize", "release", "uncertain")


class SpendGuardError(RuntimeError):
    """Any condition that must stop a billable request."""


class SpendLimitExceeded(SpendGuardError):
    """The reservation would breach a configured cap."""


class Reservation:
    __slots__ = ("id", "model", "day", "month", "reserved_micro",
                 "est_input_tokens", "max_output_tokens")

    def __init__(self, rid: str, model: str, day: str, month: str,
                 reserved_micro: int, est_input_tokens: int,
                 max_output_tokens: int):
        self.id = rid
        self.model = model
        self.day = day
        self.month = month
        self.reserved_micro = reserved_micro
        self.est_input_tokens = est_input_tokens
        self.max_output_tokens = max_output_tokens

    def as_dict(self) -> dict:
        return {
            "id": self.id, "model": self.model, "day": self.day,
            "month": self.month, "reserved_micro": self.reserved_micro,
            "reserved_usd": round(self.reserved_micro / MICRO, 6),
            "est_input_tokens": self.est_input_tokens,
            "max_output_tokens": self.max_output_tokens,
        }


def usd(micro: int) -> float:
    return round(micro / MICRO, 6)


class SpendGuard:
    def __init__(self, cfg: dict, now_fn: Callable[[], _dt.datetime] | None = None):
        self.cfg = cfg
        spending = dict(DEFAULT_SPENDING)
        spending.update(cfg.get("spending", {}) or {})
        self.s = spending
        self._now = now_fn or ml.utc_now

    # --- configuration ----------------------------------------------------

    @property
    def daily_cap_micro(self) -> int:
        return int(round(float(self.s["daily_cap_usd"]) * MICRO))

    @property
    def monthly_cap_micro(self) -> int:
        return int(round(float(self.s["monthly_cap_usd"]) * MICRO))

    @property
    def max_calls_per_day(self) -> int:
        return int(self.s["max_calls_per_day"])

    @property
    def max_completion_tokens(self) -> int:
        return int(self.s["max_completion_tokens"])

    def ledger_path(self) -> Path:
        configured = str(self.s.get("ledger_path") or "").strip()
        if configured:
            return Path(os.path.expanduser(configured))
        return ml.state_dir() / "spend_ledger.jsonl"

    def price_micro_per_token(self, model: str) -> tuple[float, float]:
        """(input, output) micro-dollars per token.  Fails closed."""
        ins = self.s.get("pricing_input_usd_per_1m") or {}
        outs = self.s.get("pricing_output_usd_per_1m") or {}
        if model not in ins or model not in outs:
            raise SpendGuardError(
                f"no configured price for model {model!r}: set "
                f"[spending.pricing_input_usd_per_1m] and "
                f"[spending.pricing_output_usd_per_1m] entries for it. "
                f"Refusing the request rather than guessing a price."
            )
        try:
            # USD per 1M tokens == micro-dollars per token.
            in_rate = float(ins[model])
            out_rate = float(outs[model])
        except (TypeError, ValueError) as exc:
            raise SpendGuardError(f"malformed price for {model!r}: {exc}") from exc
        if in_rate < 0 or out_rate < 0:
            raise SpendGuardError(f"negative price configured for {model!r}")
        return in_rate, out_rate

    # --- ledger -----------------------------------------------------------

    @contextmanager
    def _locked(self):
        path = self.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_suffix(path.suffix + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _read(self) -> list[dict]:
        path = self.ledger_path()
        if not path.exists():
            return []           # a missing ledger is simply "nothing spent yet"
        entries: list[dict] = []
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SpendGuardError(
                    f"spend ledger corrupt at {path}:{lineno} ({exc}); refusing "
                    f"all billable requests until it is repaired or moved aside"
                ) from exc
            if not isinstance(entry, dict) or "type" not in entry:
                raise SpendGuardError(
                    f"spend ledger corrupt at {path}:{lineno} (not a ledger "
                    f"entry); refusing all billable requests"
                )
            entries.append(entry)
        return entries

    def _append(self, entry: dict) -> None:
        path = self.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    # --- accounting -------------------------------------------------------

    @staticmethod
    def _keys(now: _dt.datetime) -> tuple[str, str]:
        now = now.astimezone(_dt.timezone.utc)
        return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")

    def _effective(self, entries: list[dict]) -> dict:
        """Fold the append-only ledger into per-reservation effective cost.

        A reservation with no terminal entry still counts at full price. That
        is what makes a mid-flight kill safe: the money stays reserved until
        something explicitly says otherwise.
        """
        terminal: dict[str, dict] = {}
        for e in entries:
            if e["type"] in TERMINAL_TYPES:
                ref = e.get("ref")
                if ref and ref not in terminal:
                    terminal[ref] = e

        rows = []
        for e in entries:
            if e["type"] != "reserve":
                continue
            t = terminal.get(e["id"])
            if t is None:
                cost, state = int(e["reserved_micro"]), "outstanding"
            elif t["type"] == "finalize":
                cost, state = int(t["actual_micro"]), "finalized"
            elif t["type"] == "release":
                cost, state = 0, "released"
            else:
                cost, state = int(e["reserved_micro"]), "uncertain"
            rows.append({"id": e["id"], "day": e["day"], "month": e["month"],
                         "micro": cost, "state": state})
        return {"rows": rows, "terminal": terminal}

    def usage(self, now: _dt.datetime | None = None) -> dict:
        with self._locked():
            entries = self._read()
        return self._usage_from(entries, now or self._now())

    def _usage_from(self, entries: list[dict], now: _dt.datetime) -> dict:
        day, month = self._keys(now)
        folded = self._effective(entries)
        day_micro = sum(r["micro"] for r in folded["rows"] if r["day"] == day)
        month_micro = sum(r["micro"] for r in folded["rows"] if r["month"] == month)
        calls_today = sum(1 for r in folded["rows"] if r["day"] == day)
        outstanding = [r for r in folded["rows"]
                       if r["state"] == "outstanding" and r["day"] == day]
        return {
            "day": day, "month": month,
            "day_micro": day_micro, "month_micro": month_micro,
            "day_usd": usd(day_micro), "month_usd": usd(month_micro),
            "calls_today": calls_today,
            "outstanding_today": len(outstanding),
            "day_remaining_micro": max(0, self.daily_cap_micro - day_micro),
            "month_remaining_micro": max(0, self.monthly_cap_micro - month_micro),
        }

    # --- the guard --------------------------------------------------------

    def reserve(self, model: str, request_body_bytes: int,
                max_output_tokens: int | None = None) -> Reservation:
        """Reserve a conservative worst-case cost, or refuse.

        The input estimate treats every *byte* of the request body as a token.
        Real tokenisation is roughly 4 bytes per token, so this over-reserves
        by about 4x on purpose -- the guard should err toward refusing.
        """
        now = self._now()
        day, month = self._keys(now)
        in_rate, out_rate = self.price_micro_per_token(model)
        max_out = int(max_output_tokens if max_output_tokens is not None
                      else self.max_completion_tokens)
        est_in = int(request_body_bytes)
        reserved_micro = int(math.ceil(est_in * in_rate + max_out * out_rate))

        with self._locked():
            entries = self._read()          # fails closed if corrupt
            u = self._usage_from(entries, now)

            if u["calls_today"] + 1 > self.max_calls_per_day:
                raise SpendLimitExceeded(
                    f"daily call limit reached: {u['calls_today']}/"
                    f"{self.max_calls_per_day} calls already made on {day} (UTC)"
                )
            if u["day_micro"] + reserved_micro > self.daily_cap_micro:
                raise SpendLimitExceeded(
                    f"daily cap would be exceeded: ${usd(u['day_micro']):.6f} "
                    f"already committed + ${usd(reserved_micro):.6f} reserved > "
                    f"${usd(self.daily_cap_micro):.2f} cap for {day} (UTC)"
                )
            if u["month_micro"] + reserved_micro > self.monthly_cap_micro:
                raise SpendLimitExceeded(
                    f"monthly cap would be exceeded: ${usd(u['month_micro']):.6f} "
                    f"already committed + ${usd(reserved_micro):.6f} reserved > "
                    f"${usd(self.monthly_cap_micro):.2f} cap for {month} (UTC)"
                )

            rid = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{secrets.token_hex(4)}"
            self._append({
                "type": "reserve", "id": rid, "ts": ml.iso(now),
                "day": day, "month": month, "model": model,
                "est_input_tokens": est_in, "max_output_tokens": max_out,
                "reserved_micro": reserved_micro,
                "reserved_usd": usd(reserved_micro),
            })
        return Reservation(rid, model, day, month, reserved_micro, est_in, max_out)

    def _terminate(self, res: Reservation, kind: str, payload: dict) -> None:
        if kind not in TERMINAL_TYPES:
            raise SpendGuardError(f"bad terminal type {kind!r}")
        with self._locked():
            entries = self._read()
            for e in entries:
                if e["type"] in TERMINAL_TYPES and e.get("ref") == res.id:
                    raise SpendGuardError(
                        f"reservation {res.id} is already {e['type']}d; "
                        f"refusing to double-finalize"
                    )
            entry = {"type": kind, "ref": res.id, "ts": ml.iso(self._now())}
            entry.update(payload)
            self._append(entry)

    def finalize(self, res: Reservation, input_tokens: int,
                 output_tokens: int) -> int:
        """Record the real cost of a call that definitely completed."""
        in_rate, out_rate = self.price_micro_per_token(res.model)
        actual = int(math.ceil(int(input_tokens) * in_rate
                               + int(output_tokens) * out_rate))
        # If the provider reports MORE usage than we reserved, charge the higher
        # real figure. Deliberately not clamped to the reservation: the guard's
        # job is to never under-count money that was actually spent.
        self._terminate(res, "finalize", {
            "actual_micro": actual, "actual_usd": usd(actual),
            "input_tokens": int(input_tokens), "output_tokens": int(output_tokens),
            "outcome": "ok",
        })
        return actual

    def release(self, res: Reservation, reason: str) -> None:
        """The request was definitely NOT billed (e.g. a 4xx rejection)."""
        self._terminate(res, "release", {"actual_micro": 0, "actual_usd": 0.0,
                                         "outcome": "not_billed",
                                         "reason": reason[:120]})

    def finalize_uncertain(self, res: Reservation, reason: str) -> None:
        """Timeout or unknown billing outcome -- charge the full reservation."""
        self._terminate(res, "uncertain", {
            "actual_micro": res.reserved_micro,
            "actual_usd": usd(res.reserved_micro),
            "outcome": "uncertain", "reason": reason[:120],
        })

    # --- reporting --------------------------------------------------------

    def status(self, now: _dt.datetime | None = None) -> dict:
        now = now or self._now()
        try:
            u = self.usage(now)
            healthy, detail = True, "ok"
        except SpendGuardError as exc:
            day, month = self._keys(now)
            u = {"day": day, "month": month, "day_micro": 0, "month_micro": 0,
                 "day_usd": 0.0, "month_usd": 0.0, "calls_today": 0,
                 "outstanding_today": 0, "day_remaining_micro": 0,
                 "month_remaining_micro": 0}
            healthy, detail = False, str(exc)

        reasons = []
        if not healthy:
            reasons.append("ledger unreadable")
        else:
            if u["calls_today"] >= self.max_calls_per_day:
                reasons.append(
                    f"daily call limit reached ({u['calls_today']}/{self.max_calls_per_day})")
            if u["day_remaining_micro"] <= 0:
                reasons.append("daily cap exhausted")
            if u["month_remaining_micro"] <= 0:
                reasons.append("monthly cap exhausted")
        pricing = sorted(set(self.s.get("pricing_input_usd_per_1m") or {})
                         & set(self.s.get("pricing_output_usd_per_1m") or {}))
        model = str(self.cfg.get("reviewer", {}).get("model") or "")
        if model and model not in pricing:
            reasons.append(f"no configured price for model {model!r}")

        return {
            "healthy": healthy,
            "detail": detail,
            "utc_day": u["day"],
            "utc_month": u["month"],
            "day_committed_usd": u["day_usd"],
            "day_cap_usd": usd(self.daily_cap_micro),
            "day_remaining_usd": usd(u["day_remaining_micro"]),
            "month_committed_usd": u["month_usd"],
            "month_cap_usd": usd(self.monthly_cap_micro),
            "month_remaining_usd": usd(u["month_remaining_micro"]),
            "calls_today": u["calls_today"],
            "max_calls_per_day": self.max_calls_per_day,
            "outstanding_reservations_today": u["outstanding_today"],
            "max_completion_tokens": self.max_completion_tokens,
            "priced_models": pricing,
            "ledger": str(self.ledger_path()),
            "blocked": bool(reasons),
            "blocked_reasons": reasons,
        }


__all__ = ["SpendGuard", "SpendGuardError", "SpendLimitExceeded", "Reservation",
           "DEFAULT_SPENDING", "usd", "MICRO"]
