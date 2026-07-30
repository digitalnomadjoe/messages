#!/usr/bin/env python3
"""lane_executor -- persistent per-lane agent that claims and executes tickets.

Closes the last manual gap: once Telephone publishes a ticket, no human command
is needed before its completion report appears.

THE CENTRAL SAFETY DECISION
    A daemon cannot safely "execute a ticket" in general -- ticket prose is
    arbitrary text, and obeying arbitrary text is arbitrary code execution. So
    this executor does NOT interpret tickets. It matches each ticket against a
    CLOSED REGISTRY of narrowly-defined handlers, each of which performs one
    specific, bounded, read-only task. Anything that does not match a handler
    exactly is BLOCKED with a published receipt explaining why, so a human or a
    full agent session can pick it up.

    That means autonomy here is deliberately narrow. It is not "the daemon can
    do any ticket"; it is "the daemon can do these specific safe things, and
    refuses the rest out loud". Widening it means adding a reviewed handler,
    not loosening the gate.

CLAIM ORDERING
    A claim is published only after an execution process has actually started
    and handshaked. The worker then waits for confirmation that the claim
    landed before doing any work. So there is never a claim without a live
    executor, and never work without a claim.

RESTART SAFETY
    An interrupted attempt is never blindly re-run. Idempotent (read-only)
    handlers may retry; anything else is blocked. On first start the executor
    records a high-water mark so it never sweeps up unrelated historic tickets.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import messagelib as ml  # noqa: E402
from messagelib import MessageError  # noqa: E402
import telephone as tp  # noqa: E402
from reviewer_daemon import hard_gate_hits  # noqa: E402
from spend_guard import SpendGuard  # noqa: E402

LOG = logging.getLogger("brittle-lane-executor")

HANDSHAKE_STARTED = "EXECUTOR-WORKER-STARTED"
HANDSHAKE_GO = "CLAIM-PUBLISHED-PROCEED"
HANDSHAKE_TIMEOUT = 30.0

# Verbs that mean a ticket wants to change something. Their presence disqualifies
# every read-only handler, whatever else the text says.
MUTATION_RE = re.compile(
    r"(?i)\b(restart|reboot|start|stop|enable|disable|reload|kill|edit|modify|"
    r"write|delete|remove|install|uninstall|deploy|train|launch|promote|"
    r"overwrite|patch|commit|push|chmod|chown|rm)\b")

# A mutation verb inside an explicit prohibition is not a request to mutate.
PROHIBITION_CONTEXT_RE = re.compile(
    r"(?i)(?:do not|don't|never|no|without|zero|prohibit|forbid|must not|"
    r"refrain)\b[^.\n]{0,80}$")


class ExecutorError(MessageError):
    pass


class Blocked(ExecutorError):
    """This ticket must be blocked, not executed."""


# --------------------------------------------------------------------------
# Handler registry -- closed by construction
# --------------------------------------------------------------------------


def _strip_prohibitions(text: str) -> str:
    """Drop lines that only forbid things, so their verbs do not read as asks."""
    keep = []
    for line in text.split("\n"):
        low = line.lower()
        if re.search(r"(?i)^\s*[-*\d.]*\s*(do not|don't|never|no\b|zero\b)", low):
            continue
        if "prohibition" in low:
            continue
        keep.append(line)
    return "\n".join(keep)


def _mutation_requested(text: str) -> list[str]:
    body = _strip_prohibitions(text)
    hits = []
    for m in MUTATION_RE.finditer(body):
        before = body[max(0, m.start() - 90):m.start()]
        if PROHIBITION_CONTEXT_RE.search(before):
            continue
        hits.append(m.group(0).lower())
    return sorted(set(hits))


class Handler:
    name = "abstract"
    idempotent = False
    description = ""

    def matches(self, ticket: ml.Message) -> bool:
        raise NotImplementedError

    def run(self, ticket: ml.Message, cfg: dict) -> str:
        raise NotImplementedError


class ServiceTwoPollCheck(Handler):
    """Read ActiveState/MainPID/NRestarts from a bus service twice and compare."""

    name = "service_two_poll_check"
    idempotent = True
    description = ("Two read-only systemctl polls of a brittle-messages lane "
                   "service, ~10s apart, comparing ActiveState, MainPID and "
                   "NRestarts.")

    UNIT_RE = re.compile(r"brittle-messages-(control|locomotion)\.service")
    TWO_POLL_RE = re.compile(
        r"(?i)\btwo\b[^.\n]{0,40}\bpolls?\b|\btwice\b|\btwo consecutive\b|"
        r"\bpoll\b[^.\n]{0,20}\btwice\b")

    def matches(self, ticket: ml.Message) -> bool:
        body = ticket.body
        if not self.UNIT_RE.search(body):
            return False
        if "systemctl" not in body.lower():
            return False
        if not self.TWO_POLL_RE.search(body):
            return False
        needed = ("activestate", "nrestarts")
        if not all(n in body.lower() for n in needed):
            return False
        if not re.search(r"(?i)\b(mainpid|pid)\b", body):
            return False
        return True

    def run(self, ticket: ml.Message, cfg: dict) -> str:
        unit = f"brittle-messages-{self.UNIT_RE.search(ticket.body).group(1)}.service"
        gap_target = float(cfg.get("executor", {}).get("two_poll_gap_seconds") or 10.0)
        band_lo, band_hi = gap_target * 0.8, gap_target * 1.5
        cmd = ["systemctl", "--user", "show", unit,
               "--property=ActiveState", "--property=MainPID",
               "--property=NRestarts", "--no-pager"]
        polls = []
        for i in range(2):
            if i:
                time.sleep(gap_target)
            stamp = ml.iso(ml.utc_now())
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=30)
            if out.returncode != 0:
                raise Blocked(f"`systemctl show {unit}` exited "
                              f"{out.returncode}; cannot gather evidence")
            fields = dict(
                line.split("=", 1) for line in out.stdout.strip().split("\n")
                if "=" in line)
            polls.append({"utc": stamp, "raw": out.stdout.strip(),
                          "fields": fields})

        a, b = polls[0]["fields"], polls[1]["fields"]
        keys = ("ActiveState", "MainPID", "NRestarts")
        identical = all(a.get(k) == b.get(k) for k in keys)
        active = a.get("ActiveState") == "active" == b.get("ActiveState")
        try:
            pid_ok = int(a.get("MainPID", 0)) > 0
            restarts_ok = int(a.get("NRestarts", -1)) >= 0
        except ValueError:
            pid_ok = restarts_ok = False
        gap = (ml.parse_iso(polls[1]["utc"]) - ml.parse_iso(polls[0]["utc"])).total_seconds()
        in_band = band_lo <= gap <= band_hi
        overall = identical and active and pid_ok and restarts_ok and in_band

        rows = "\n".join(
            f"| `{k}` | {a.get(k)} | {b.get(k)} | {'yes' if a.get(k) == b.get(k) else 'NO'} |"
            for k in keys)
        return f"""# Autonomous execution — {unit} two-poll check

> Executed automatically by the **{ticket.get('lane')} lane executor** under
> handler `{self.name}`. Read-only: two `systemctl show` reads and this report.

**status:** {'PASS' if overall else 'FAIL'} — acceptance criteria {'all met' if overall else 'NOT all met'}
**decision:** none
**key result:** {unit} {'identical across both polls' if identical else 'CHANGED between polls'} — ActiveState={a.get('ActiveState')}, MainPID={a.get('MainPID')}, NRestarts={a.get('NRestarts')}
**next action:** none; the two-poll evidence is recorded
**report path:** recorded by messagesctl on publication

## Identifiers

| field | value |
| --- | --- |
| ticket | `{ticket.id}` |
| lane / unit | `{ticket.get('lane')}` / `{ticket.get('unit')}` |
| telephone run | `{ticket.get('run_id') or '-'}` |
| handler | `{self.name}` (idempotent) |
| executor | autonomous {ticket.get('lane')} lane executor |

## Exact command

Run identically for both polls:

```
{' '.join(cmd)}
```

## Raw outputs

**Poll 1 — {polls[0]['utc']}**

```
{polls[0]['raw']}
```

**Poll 2 — {polls[1]['utc']}**

```
{polls[1]['raw']}
```

## Comparison

| field | poll 1 | poll 2 | identical |
| --- | --- | --- | --- |
{rows}

Separation: **{gap:.1f} seconds** (target {gap_target:.1f} s).

## Acceptance criteria

| # | criterion | result |
| --- | --- | --- |
| 1 | exactly 2 polls, {band_lo:.1f}–{band_hi:.1f} s apart | {'PASS' if in_band else 'FAIL'} — {gap:.1f} s |
| 2 | each poll reports the 3 requested fields | {'PASS' if all(k in a and k in b for k in keys) else 'FAIL'} |
| 3 | values match, ActiveState=active, MainPID>0, NRestarts>=0 | {'PASS' if identical and active and pid_ok and restarts_ok else 'FAIL'} |
| 4 | zero state-changing actions | PASS — see below |

**Overall: {'PASS' if overall else 'FAIL'}.**

## No-mutation statement

No state-changing action was taken. Two `systemctl show` reads and this
write-up. Nothing was started, stopped, restarted, reloaded, enabled, disabled
or reconfigured. No BRITTLE production state, code, configuration, Guard state,
training, simulation, policy, canonical artefact, interface, promotion or
`latest` pointer was touched.

Corroborated by `NRestarts={a.get('NRestarts')}` and an unchanged
`MainPID={a.get('MainPID')}` across both polls.
"""


class BusValidateCheck(Handler):
    """Run the repository validator read-only and report the result."""

    name = "bus_validate_check"
    idempotent = True
    description = "Read-only `messagesctl validate` of the message bus."

    def matches(self, ticket: ml.Message) -> bool:
        low = ticket.body.lower()
        return ("messagesctl validate" in low or
                ("validate" in low and "message bus" in low)) and \
               "systemctl" not in low

    def run(self, ticket: ml.Message, cfg: dict) -> str:
        script = Path(__file__).resolve().parent / "messagesctl.py"
        out = subprocess.run([sys.executable, str(script), "validate"],
                             capture_output=True, text=True, timeout=180)
        ok = out.returncode == 0
        return f"""# Autonomous execution — message bus validation

> Executed automatically by the **{ticket.get('lane')} lane executor** under
> handler `{self.name}`. Read-only.

**status:** {'PASS' if ok else 'FAIL'}
**decision:** none
**key result:** `messagesctl validate` exited {out.returncode}
**next action:** none
**report path:** recorded by messagesctl on publication

## Identifiers

| field | value |
| --- | --- |
| ticket | `{ticket.id}` |
| handler | `{self.name}` (idempotent) |

## Output

```
{(out.stdout or out.stderr).strip()[:4000]}
```

## No-mutation statement

Read-only validation only. Nothing was written, started, stopped or changed.
"""


REGISTRY: tuple[Handler, ...] = (ServiceTwoPollCheck(), BusValidateCheck())


def classify(ticket: ml.Message) -> Handler:
    """Pick the one handler for this ticket, or refuse.

    Ambiguity is a refusal, not a coin flip: two matching handlers block.
    """
    text = ticket.body

    gates = hard_gate_hits(text)
    if gates:
        raise Blocked("ticket text touches hard gate(s): " + ", ".join(gates))

    mutations = _mutation_requested(text)
    if mutations:
        raise Blocked(
            "ticket appears to request state-changing work "
            f"({', '.join(mutations)}); autonomous execution is read-only")

    matches = [h for h in REGISTRY if h.matches(ticket)]
    if not matches:
        raise Blocked(
            "no autonomous handler matches this ticket. Autonomous execution "
            "covers only a closed set of read-only tasks; this one needs a "
            "human or a full agent session. Handlers: "
            + ", ".join(h.name for h in REGISTRY))
    if len(matches) > 1:
        raise Blocked(
            "ticket matches multiple handlers ("
            + ", ".join(h.name for h in matches)
            + "); refusing to guess which was intended")
    return matches[0]


# --------------------------------------------------------------------------
# Eligibility gates
# --------------------------------------------------------------------------


def executor_state_path(lane: str) -> Path:
    return ml.state_dir() / f"executor-{lane}.json"


def load_executor_state(lane: str) -> dict:
    path = executor_state_path(lane)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_executor_state(lane: str, state: dict) -> None:
    path = executor_state_path(lane)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def eligible_ticket(msgs: dict, lane: str, cfg: dict, since: str | None):
    """The one ticket this lane may execute now, or None. Raises Blocked to refuse.

    Every gate here is checked before any claim is published.
    """
    if ml.autonomy_state(msgs)["paused"]:
        LOG.info("autonomy paused -- executing nothing")
        return None

    # One active ticket per lane: if anything on this lane is claimed, stop.
    for m in msgs.values():
        if m.kind != "ticket" or m.get("lane") != lane:
            continue
        st = ml.ticket_state(m.id, msgs)
        if st["status"] == "claimed":
            LOG.debug("lane %s already has claimed ticket %s", lane, m.id)
            return None

    for m in sorted(msgs.values(), key=ml.Message.sort_key):
        if m.kind != "ticket" or m.get("lane") != lane:
            continue
        if ml.ticket_state(m.id, msgs)["status"] != "open":
            continue
        # Never sweep up historic work the executor was not running for.
        if since and str(m.get("created_at")) < since:
            LOG.debug("skipping historic ticket %s (before high-water mark)", m.id)
            continue
        if m.get("requires_owner") is True:
            raise Blocked_(m, "ticket sets requires_owner: owner decision required")

        run_id = m.get("run_id")
        if run_id:
            run = msgs.get(str(run_id))
            if run is None:
                raise Blocked_(m, f"ticket references unknown run {run_id}")
            rs = tp.run_state(run, msgs)
            if rs["status"] != "active":
                raise Blocked_(m, f"telephone run {run_id} is {rs['status']}")
            if run.get("lane") != lane:
                raise Blocked_(m, "ticket run is bound to a different lane")

        guard = SpendGuard(cfg).status()
        if guard["blocked"]:
            LOG.warning("spend guard blocked (%s) -- not starting work",
                        "; ".join(guard["blocked_reasons"]))
            return None
        return m
    return None


class Blocked_(Blocked):
    """Blocked, carrying the ticket that must receive the receipt."""

    def __init__(self, ticket, reason: str):
        super().__init__(reason)
        self.ticket = ticket
        self.reason = reason


# --------------------------------------------------------------------------
# The executor
# --------------------------------------------------------------------------


class LaneExecutor:
    def __init__(self, lane: str, cfg: dict):
        if lane not in ml.AGENT_LANES:
            raise ExecutorError(f"lane must be one of {ml.AGENT_LANES}")
        self.lane = lane
        self.cfg = cfg
        self.repo = ml.Repo(cfg["repo"]["path"])
        self.reports_dir = Path(
            cfg.get("executor", {}).get("reports_dir")
            or (Path(cfg["repo"]["brittle_path"]) / "rgl" / "reports"))

    # --- high-water mark -------------------------------------------------

    def high_water_mark(self) -> str:
        state = load_executor_state(self.lane)
        mark = state.get("high_water_mark")
        if not mark:
            mark = ml.iso(ml.utc_now())
            state["high_water_mark"] = mark
            state["first_started_at"] = mark
            save_executor_state(self.lane, state)
            LOG.info("first start: high-water mark %s (historic tickets ignored)",
                     mark)
        return mark

    # --- publication helpers --------------------------------------------

    def _block(self, ticket, reason: str) -> None:
        LOG.warning("blocking %s: %s", ticket.id, reason)
        script = Path(__file__).resolve().parent / "messagesctl.py"
        subprocess.run(
            [sys.executable, str(script), "block", ticket.id,
             "--reason", f"autonomous executor: {reason}"[:200]],
            capture_output=True, text=True, timeout=120)

    def _record(self, **kw) -> None:
        state = load_executor_state(self.lane)
        state.update(kw)
        save_executor_state(self.lane, state)

    # --- one pass --------------------------------------------------------

    def run_once(self) -> dict:
        stats = {"lane": self.lane, "claimed": 0, "completed": 0,
                 "blocked": 0, "skipped": 0, "errors": []}
        mark = self.high_water_mark()

        # Decide under the lock; ACT outside it. Every mutating action here runs
        # `messagesctl` in a child process, and that child takes the same flock.
        # Calling it while we hold the lock would deadlock across processes
        # until the 120s lock timeout expired.
        decision: dict = {}
        with ml.repo_lock(self.repo.path):
            try:
                self.repo.pull_ff_only()
            except MessageError as exc:
                stats["errors"].append(f"pull: {exc}")
            msgs = ml.load_messages(self.repo.path)

            interrupted = self._interrupted(msgs)
            if interrupted is not None:
                ticket, handler = interrupted
                if handler is None or not handler.idempotent:
                    decision = {"action": "block", "ticket": ticket,
                                "reason": "a previous execution attempt was "
                                          "interrupted and the task is not safe "
                                          "to retry automatically"}
                else:
                    LOG.info("resuming interrupted idempotent ticket %s", ticket.id)

            if not decision:
                try:
                    ticket = eligible_ticket(msgs, self.lane, self.cfg, mark)
                except Blocked_ as exc:
                    decision = {"action": "block", "ticket": exc.ticket,
                                "reason": exc.reason}
                else:
                    if ticket is None:
                        decision = {"action": "idle"}
                    else:
                        try:
                            decision = {"action": "execute", "ticket": ticket,
                                        "handler": classify(ticket)}
                        except Blocked as exc:
                            decision = {"action": "block", "ticket": ticket,
                                        "reason": str(exc)}

        # --- lock released; now act -------------------------------------
        if decision["action"] == "idle":
            stats["skipped"] += 1
            return stats
        if decision["action"] == "block":
            self._block(decision["ticket"], decision["reason"])
            stats["blocked"] += 1
            return stats

        ticket, handler = decision["ticket"], decision["handler"]
        try:
            report_md = self._execute_with_claim(ticket, handler, stats)
        except Blocked as exc:
            self._block(ticket, str(exc))
            stats["blocked"] += 1
            return stats
        except (ExecutorError, MessageError) as exc:
            stats["errors"].append(f"{ticket.id}: {exc}")
            self._block(ticket, f"execution failed: {exc}")
            stats["blocked"] += 1
            return stats

        self._publish_completion(ticket, handler, report_md, stats)
        return stats

    def _interrupted(self, msgs: dict):
        state = load_executor_state(self.lane)
        tid = state.get("in_flight_ticket")
        if not tid or tid not in msgs:
            return None
        st = ml.ticket_state(tid, msgs)
        if st["status"] != "claimed" or st["claim_agent"] != self.lane:
            return None
        handler = None
        for h in REGISTRY:
            if h.name == state.get("in_flight_handler"):
                handler = h
        return msgs[tid], handler

    # --- claim ordering --------------------------------------------------

    def _execute_with_claim(self, ticket, handler, stats) -> str:
        """Start the worker, THEN claim, THEN let it work."""
        payload = {"ticket_id": ticket.id, "handler": handler.name,
                   "lane": self.lane}
        tmp = ml.state_dir() / f"worker-{self.lane}-{ticket.id}.json"
        out_path = ml.state_dir() / f"worker-{self.lane}-{ticket.id}.md"
        tmp.write_text(json.dumps(payload), encoding="utf-8")

        script = Path(__file__).resolve().parent / "lane_executor.py"
        proc = subprocess.Popen(
            [sys.executable, str(script), "--worker",
             "--ticket-file", str(tmp), "--out", str(out_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            env={**os.environ, "BRITTLE_MESSAGES_STATE": str(ml.state_dir())})

        # 1. worker must prove it is alive before anything is claimed
        deadline = time.time() + HANDSHAKE_TIMEOUT
        line = ""
        while time.time() < deadline:
            line = (proc.stdout.readline() or "").strip()
            if line:
                break
        if line != HANDSHAKE_STARTED:
            proc.kill()
            raise ExecutorError(
                f"worker did not signal start (got {line!r}); nothing claimed")
        LOG.info("worker alive for %s; publishing claim", ticket.id)

        # 2. claim, now that a real execution process exists
        self._record(in_flight_ticket=ticket.id, in_flight_handler=handler.name,
                     in_flight_started_at=ml.iso(ml.utc_now()))
        script_ctl = Path(__file__).resolve().parent / "messagesctl.py"
        claim = subprocess.run(
            [sys.executable, str(script_ctl), "--json", "claim", ticket.id,
             "--agent", self.lane],
            capture_output=True, text=True, timeout=180)
        if claim.returncode != 0:
            proc.kill()
            self._record(in_flight_ticket=None)
            raise ExecutorError(
                f"claim refused, so no work was done: {claim.stderr.strip()[:200]}")
        claim_id = json.loads(claim.stdout)["receipt_id"]
        stats["claimed"] += 1
        stats["claim_receipt"] = claim_id
        LOG.info("claimed %s (%s)", ticket.id, claim_id)

        # 3. release the worker only now
        proc.stdin.write(HANDSHAKE_GO + "\n")
        proc.stdin.flush()
        timeout = float(self.cfg.get("executor", {}).get("task_timeout_seconds") or 900)
        try:
            _out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise ExecutorError(f"handler exceeded {timeout}s")
        if proc.returncode != 0:
            raise Blocked(f"handler failed: {(err or '').strip()[:200]}")
        if not out_path.exists():
            raise Blocked("handler produced no report")
        report = out_path.read_text(encoding="utf-8")
        for p in (tmp, out_path):
            p.unlink(missing_ok=True)
        return report

    # --- completion ------------------------------------------------------

    def _publish_completion(self, ticket, handler, report_md: str, stats) -> None:
        stamp = ml.utc_now().strftime("%Y%m%dT%H%M%SZ")
        unit = str(ticket.get("unit") or "NOUNIT").replace("/", "-")
        local = self.reports_dir / f"AUTOEXEC_{unit}_{stamp}.md"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(report_md, encoding="utf-8")

        script = Path(__file__).resolve().parent / "messagesctl.py"
        pub = subprocess.run(
            [sys.executable, str(script), "--json", "publish-report",
             "--lane", self.lane, "--unit", str(ticket.get("unit") or "NOUNIT"),
             "--title", f"Autonomous execution: {handler.name}",
             "--in-reply-to", ticket.id, "--report", str(local)],
            capture_output=True, text=True, timeout=300)
        if pub.returncode != 0:
            stats["errors"].append(f"publish failed: {pub.stderr.strip()[:200]}")
            self._block(ticket, f"report publication failed: {pub.stderr.strip()[:120]}")
            stats["blocked"] += 1
            return
        report_id = json.loads(pub.stdout)["report_id"]

        comp = subprocess.run(
            [sys.executable, str(script), "--json", "complete", ticket.id,
             "--report-id", report_id],
            capture_output=True, text=True, timeout=180)
        if comp.returncode != 0:
            stats["errors"].append(f"complete failed: {comp.stderr.strip()[:200]}")
            return
        stats["completed"] += 1
        stats["report_id"] = report_id
        stats["completion_receipt"] = json.loads(comp.stdout)["receipt_id"]
        self._record(in_flight_ticket=None, in_flight_handler=None,
                     last_ticket=ticket.id, last_report=report_id,
                     last_handler=handler.name,
                     last_outcome="completed",
                     last_finished_at=ml.iso(ml.utc_now()))
        LOG.info("completed %s -> report %s", ticket.id, report_id)


# --------------------------------------------------------------------------
# Worker mode
# --------------------------------------------------------------------------


def worker_main(ticket_file: str, out: str) -> int:
    """Child process: prove alive, wait for the claim, then do the work."""
    payload = json.loads(Path(ticket_file).read_text(encoding="utf-8"))
    print(HANDSHAKE_STARTED, flush=True)

    go = (sys.stdin.readline() or "").strip()
    if go != HANDSHAKE_GO:
        print(f"no claim confirmation (got {go!r}); doing nothing",
              file=sys.stderr)
        return 3

    cfg = ml.load_config()
    msgs = ml.load_messages(cfg["repo"]["path"])
    ticket = msgs.get(payload["ticket_id"])
    if ticket is None:
        print("ticket vanished", file=sys.stderr)
        return 4
    handler = next((h for h in REGISTRY if h.name == payload["handler"]), None)
    if handler is None:
        print("unknown handler", file=sys.stderr)
        return 5
    try:
        Path(out).write_text(handler.run(ticket, cfg), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - report, never crash silently
        print(f"handler error: {exc}", file=sys.stderr)
        return 6
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

_STOP = False


def _sig(signum, _frame):
    global _STOP
    _STOP = True
    LOG.info("received signal %s -- stopping after this pass", signum)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lane_executor",
                               description=__doc__.split("\n")[0])
    p.add_argument("--lane", choices=ml.AGENT_LANES)
    p.add_argument("--config")
    p.add_argument("--once", action="store_true")
    p.add_argument("--poll-seconds", type=float)
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--ticket-file", help=argparse.SUPPRESS)
    p.add_argument("--out", help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    if args.worker:
        return worker_main(args.ticket_file, args.out)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    if not args.lane:
        p.error("--lane is required")
    cfg = ml.load_config(args.config)
    ex = LaneExecutor(args.lane, cfg)
    poll = float(args.poll_seconds
                 or cfg.get("executor", {}).get("poll_seconds") or 30)
    LOG.info("lane executor online: lane=%s repo=%s poll=%ss handlers=%s",
             args.lane, ex.repo.path, poll,
             ",".join(h.name for h in REGISTRY))

    while True:
        try:
            stats = ex.run_once()
            if stats["claimed"] or stats["blocked"] or stats["errors"]:
                LOG.info("pass: %s", json.dumps(stats))
        except Exception as exc:  # noqa: BLE001 - a daemon must not die
            LOG.exception("pass failed: %s", exc)
        if args.once or _STOP:
            break
        for _ in range(int(max(1, poll))):
            if _STOP:
                break
            time.sleep(1)
    LOG.info("lane executor stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
