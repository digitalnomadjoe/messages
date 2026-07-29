#!/usr/bin/env python3
"""telephone -- bounded autonomous report/ticket cycles on the BRITTLE bus.

A Telephone run is a *bounded* wrapper around the existing loop. It changes no
safety property: the reviewer still decides, the spend guard still bills, lane
isolation and leases still hold. Telephone only decides **when to stop**.

One cycle is:

    source report reviewed -> successor ticket issued -> ticket claimed
      -> work completed -> completion report published -> completion report reviewed

The counter increments only when the completion review lands. Everything else
-- a review-only outcome, an escalation, a blocked ticket, a lost claim, an
owner gate, a spend refusal -- stops the run rather than manufacturing another
cycle.

Every stop decision that bounds the run (max_cycles, manual stop, run already
finished) is computed **here, outside the model**. The model can only supply
evidence about the criterion; it cannot extend a run.

Run state lives on the append-only bus as a `telephone_run` message plus
`telephone_cycle` / `telephone_stop` receipts, so it survives a daemon crash, a
workstation reboot and an agent restart with no local state at all.
"""

from __future__ import annotations

import re
from typing import Any

import messagelib as ml

# Stop reasons. Only SUCCESS_REASONS count as "the run achieved its goal".
STOP_CRITERION_MET = "criterion_met"
STOP_MAX_CYCLES = "max_cycles_reached"
STOP_MANUAL = "manual_stop"
STOP_REVIEW_ONLY = "review_only_no_successor"
STOP_ESCALATED = "owner_decision_required"
STOP_CRITERION_UNKNOWN = "criterion_unknown_or_low_confidence"
STOP_BLOCKED = "ticket_blocked"
STOP_CLAIM_LOST = "claim_expired_unrecoverable"
STOP_SPEND = "spend_guard_refused"
STOP_GUARD = "guard_gate"

SUCCESS_REASONS = (STOP_CRITERION_MET,)

# Reaching the cycle limit is NOT success -- it is exhaustion. Kept explicit so
# nobody later reads "stopped at max_cycles" as "the goal was achieved".
EXHAUSTION_REASONS = (STOP_MAX_CYCLES,)


class TelephoneError(ml.MessageError):
    """Subclasses MessageError so every entry point reports it uniformly."""


# --------------------------------------------------------------------------
# Natural-language invocation parsing
# --------------------------------------------------------------------------

_STOP_RE = re.compile(r"^\s*(stop|halt|cancel)\s+telephone\b", re.I)
_STATUS_RE = re.compile(r"^\s*telephone\s+status\b|^\s*status\s+of\s+telephone\b", re.I)
_LOOPS_RE = re.compile(
    r"(?:for\s+)?(?:~|about\s+|approximately\s+|around\s+)?(\d+)\s*(?:loops?|cycles?|iterations?)",
    re.I)
_MAXIMUM_RE = re.compile(
    r"max(?:imum)?\s*(?:of\s*)?(\d+)\s*(?:loops?|cycles?|iterations?)?", re.I)
_UNTIL_RE = re.compile(r"\buntil\s+(.+?)(?:,\s*max(?:imum)?\b.*)?$", re.I | re.S)
_LANE_RE = re.compile(r"\b(locomotion|control)\b", re.I)


def parse_invocation(text: str) -> dict:
    """Parse a natural-language Telephone command.

    `~10 loops` normalises to a hard maximum of 10 -- "about ten" is never
    licence to run an eleventh.
    """
    raw = (text or "").strip()
    if not raw:
        raise TelephoneError("empty Telephone invocation")

    if _STOP_RE.search(raw):
        out = {"action": "stop"}
        lane = _LANE_RE.search(raw)
        if lane:
            out["lane"] = lane.group(1).lower()
        return out

    if _STATUS_RE.search(raw):
        out = {"action": "status"}
        lane = _LANE_RE.search(raw)
        if lane:
            out["lane"] = lane.group(1).lower()
        return out

    if not re.search(r"\btelephone\b", raw, re.I):
        raise TelephoneError(f"not a Telephone invocation: {raw!r}")

    criterion = None
    m = _UNTIL_RE.search(raw)
    if m:
        criterion = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(".,;")
        if not criterion:
            criterion = None

    # An explicit "maximum N" always wins over a bare "N loops".
    maximum = _MAXIMUM_RE.search(raw)
    loops = _LOOPS_RE.search(raw)
    if maximum:
        max_cycles = int(maximum.group(1))
    elif loops:
        max_cycles = int(loops.group(1))
    else:
        raise TelephoneError(
            "Telephone needs a cycle bound, e.g. 'Run Telephone for 10 loops' "
            "or 'Run Telephone until <criterion>, maximum 12 loops'")

    if max_cycles < 1:
        raise TelephoneError(f"max_cycles must be >= 1, got {max_cycles}")

    out: dict[str, Any] = {"action": "start", "max_cycles": max_cycles,
                           "criterion": criterion}
    lane = _LANE_RE.search(raw)
    if lane:
        out["lane"] = lane.group(1).lower()
    return out


# --------------------------------------------------------------------------
# Criterion safety
# --------------------------------------------------------------------------


def check_criterion_public_safe(criterion: str | None,
                                private_patterns=()) -> None:
    """A criterion is published to a public repository. Fail closed."""
    if criterion is None:
        return
    if len(criterion) > 500:
        raise TelephoneError(
            f"criterion is {len(criterion)} chars; keep it under 500 for a "
            f"public bus")
    findings = ml.scan_secrets(criterion, private_patterns)
    if findings:
        raise TelephoneError(
            f"criterion text failed the secret scan ({findings[0]}); it would "
            f"be published to a public repository")


# --------------------------------------------------------------------------
# Run state, folded from the append-only bus
# --------------------------------------------------------------------------


def runs(msgs: dict) -> list:
    return sorted([m for m in msgs.values() if m.kind == "telephone_run"],
                  key=ml.Message.sort_key)


def run_state(run, msgs: dict) -> dict:
    """Fold a run message plus its receipts into current state."""
    receipts = sorted(
        [m for m in msgs.values()
         if m.kind == "receipt" and m.get("run_id") == run.id],
        key=ml.Message.sort_key)
    cycles = [r for r in receipts if r.get("receipt_type") == "telephone_cycle"]
    stops = [r for r in receipts if r.get("receipt_type") == "telephone_stop"]

    stopped = stops[0] if stops else None
    latest_criterion = None
    latest_conf = None
    for r in cycles + ([stopped] if stopped else []):
        if r is None:
            continue
        if r.get("criterion_status"):
            latest_criterion = r.get("criterion_status")
        # Only overwrite the confidence when this receipt actually carries one;
        # a later receipt without it must not erase an earlier measurement.
        if r.get("criterion_confidence") is not None:
            latest_conf = r.get("criterion_confidence")

    completed = len(cycles)
    max_cycles = int(run.get("max_cycles") or 0)
    reason = stopped.get("stop_reason") if stopped else None

    if stopped is not None:
        status = "completed" if reason in SUCCESS_REASONS else "stopped"
    else:
        status = "active"

    return {
        "run_id": run.id,
        "lane": run.get("lane"),
        "unit": run.get("unit"),
        "criterion": run.get("criterion"),
        "max_cycles": max_cycles,
        "cycles_completed": completed,
        "cycles_remaining": max(0, max_cycles - completed),
        "status": status,
        "stop_reason": reason,
        "stopped_receipt": stopped.id if stopped else None,
        "criterion_status": latest_criterion,
        "criterion_confidence": latest_conf,
        "start_report": run.get("report_id"),
        "cycle_receipts": [r.id for r in cycles],
        "api_calls": sum(int(r.get("api_calls") or 0) for r in receipts),
        "spend_usd": round(sum(float(r.get("spend_usd") or 0.0)
                               for r in receipts), 6),
    }


def active_run_for_lane(msgs: dict, lane: str):
    """The one active run for a lane, or None. Fails loudly on duplicates."""
    found = [r for r in runs(msgs)
             if r.get("lane") == lane and run_state(r, msgs)["status"] == "active"]
    if len(found) > 1:
        raise TelephoneError(
            f"{len(found)} active Telephone runs on lane {lane!r}: "
            f"{[r.id for r in found]}; only one is permitted")
    return found[0] if found else None


def ticket_run_id(ticket) -> str | None:
    return ticket.get("run_id")


def run_for_report(report, msgs: dict, lane: str):
    """The run governing this report -- even if that run has already stopped.

    Resolving only the *active* run would let a manual stop leak: the reviewer
    would see no run, fall back to plain autonomous behaviour, and issue a
    successor ticket for work the owner had just halted.
    """
    parent = report.get("in_reply_to")
    if parent:
        ticket = msgs.get(parent)
        if ticket is not None and ticket.kind == "ticket" and ticket.get("run_id"):
            run = msgs.get(str(ticket.get("run_id")))
            if run is not None and run.kind == "telephone_run":
                return run
    return active_run_for_lane(msgs, lane)


def closes_cycle(report, msgs: dict, run) -> bool:
    """True when this report is the completion report of a ticket in this run."""
    parent = report.get("in_reply_to")
    if not parent:
        return False
    ticket = msgs.get(parent)
    if ticket is None or ticket.kind != "ticket":
        return False
    return ticket_run_id(ticket) == run.id


# --------------------------------------------------------------------------
# Start-time preconditions
# --------------------------------------------------------------------------


def assert_can_start(msgs: dict, lane: str, start_report_id: str,
                     private_patterns=(), criterion: str | None = None) -> None:
    check_criterion_public_safe(criterion, private_patterns)

    existing = active_run_for_lane(msgs, lane)
    if existing is not None:
        st = run_state(existing, msgs)
        raise TelephoneError(
            f"lane {lane!r} already has an active Telephone run {existing.id} "
            f"({st['cycles_completed']}/{st['max_cycles']} cycles). Stop it "
            f"first: messagesctl telephone stop --lane {lane}")

    report = msgs.get(start_report_id)
    if report is None or report.kind != "report":
        raise TelephoneError(f"{start_report_id} is not a known report")
    if report.get("lane") != lane:
        raise TelephoneError(
            f"start report {start_report_id} is on lane {report.get('lane')!r}, "
            f"not {lane!r}")

    # Refuse to start on top of unrelated in-flight work.
    for m in msgs.values():
        if m.kind != "ticket" or m.get("lane") != lane:
            continue
        state = ml.ticket_state(m.id, msgs)
        if state["status"] in ("open", "claimed"):
            raise TelephoneError(
                f"lane {lane!r} already has an unrelated {state['status']} ticket "
                f"{m.id}; resolve it before starting a Telephone run")


# --------------------------------------------------------------------------
# The bounding decision -- enforced outside the model
# --------------------------------------------------------------------------


def evaluate(state: dict, *, verdict_mode: str, criterion_status: str | None,
             criterion_confidence: float | None, threshold: float,
             cycle_closed: bool) -> dict:
    """Decide whether the run continues, and with what successor.

    Returns {'issue_ticket', 'stop', 'stop_reason', 'escalate', 'cycles_after'}.

    Order matters. The hard bounds are checked before anything the model said,
    so no model output can extend a run past its limit.
    """
    cycles_after = state["cycles_completed"] + (1 if cycle_closed else 0)
    max_cycles = state["max_cycles"]

    def stop(reason: str, escalate: bool = False) -> dict:
        return {"issue_ticket": False, "stop": True, "stop_reason": reason,
                "escalate": escalate, "cycles_after": cycles_after}

    # 1. Hard bounds, model-independent.
    if state["status"] != "active":
        return stop(state["stop_reason"] or STOP_MANUAL)

    # 2. The reviewer produced no executable successor. Never fabricate one.
    if verdict_mode == "escalation":
        return stop(STOP_ESCALATED, escalate=True)
    if verdict_mode == "review_only":
        return stop(STOP_REVIEW_ONLY)

    # 3. Criterion evaluation, only when a criterion was set.
    if state["criterion"]:
        if criterion_status == "met":
            if criterion_confidence is None or criterion_confidence < threshold:
                return stop(STOP_CRITERION_UNKNOWN, escalate=True)
            return stop(STOP_CRITERION_MET)
        if criterion_status != "not_met":
            # 'unknown', missing, or anything unexpected -> ask the owner.
            return stop(STOP_CRITERION_UNKNOWN, escalate=True)
        if criterion_confidence is not None and criterion_confidence < threshold:
            return stop(STOP_CRITERION_UNKNOWN, escalate=True)

    # 4. The cycle limit stops the run regardless of what the model wants.
    if cycles_after >= max_cycles:
        return stop(STOP_MAX_CYCLES)

    return {"issue_ticket": True, "stop": False, "stop_reason": None,
            "escalate": False, "cycles_after": cycles_after}


def describe_stop(reason: str | None) -> str:
    return {
        STOP_CRITERION_MET: "criterion met -- run succeeded",
        STOP_MAX_CYCLES: ("cycle limit reached -- run exhausted its budget; "
                          "this is NOT a success signal"),
        STOP_MANUAL: "stopped manually by the owner",
        STOP_REVIEW_ONLY: "reviewer issued no successor ticket",
        STOP_ESCALATED: "owner decision required",
        STOP_CRITERION_UNKNOWN: ("criterion could not be judged with sufficient "
                                 "confidence -- escalated"),
        STOP_BLOCKED: "ticket was blocked",
        STOP_CLAIM_LOST: "claim expired and could not be recovered",
        STOP_SPEND: "local spending guard refused further calls",
        STOP_GUARD: "Guard gate reached",
        None: "still running",
    }.get(reason, reason or "unknown")


__all__ = [n for n in dir() if not n.startswith("_")]
