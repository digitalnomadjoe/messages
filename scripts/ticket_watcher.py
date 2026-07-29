#!/usr/bin/env python3
"""ticket_watcher -- read-only lane watcher for the BRITTLE message bus.

Polls one lane for newly available work and announces it. It never claims,
never publishes and never mutates the repository: claiming is a deliberate act
performed by the agent that will actually do the work, so that a claim always
corresponds to a real working session.

On a lane transition (a different ticket becomes the next open one) it logs a
concise line, invokes the configured notification command, and writes the
current lane state to the local state directory for an agent session to read.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import messagelib as ml  # noqa: E402
from messagelib import MessageError  # noqa: E402

LOG = logging.getLogger("brittle-watcher")
_STOP = False


def _handle_signal(signum, _frame):
    global _STOP
    _STOP = True
    LOG.info("received signal %s -- stopping", signum)


def lane_snapshot(repo: ml.Repo, lane: str) -> dict:
    msgs = ml.load_messages(repo.path)
    now = ml.utc_now()
    nxt = None
    for m in sorted(msgs.values(), key=ml.Message.sort_key):
        if m.kind == "ticket" and m.get("lane") == lane:
            if ml.ticket_state(m.id, msgs, now=now)["status"] == "open":
                nxt = m
                break
    claims = {
        tid: info for tid, info in ml.build_index(msgs, now=now)["active_claims"].items()
        if str(msgs[tid].get("lane")) == lane
    }
    return {
        "lane": lane,
        "next_open_ticket": nxt.id if nxt else None,
        "title": nxt.get("title") if nxt else None,
        "unit": nxt.get("unit") if nxt else None,
        "path": nxt.rel if nxt else None,
        "active_claims": claims,
        "paused": ml.autonomy_state(msgs)["paused"],
    }


def run_once(cfg: dict, lane: str, state_file: Path) -> dict:
    repo = ml.Repo(cfg["repo"]["path"])
    with ml.repo_lock(repo.path):
        try:
            repo.pull_ff_only()
        except MessageError as exc:
            LOG.warning("pull failed: %s", exc)
        snap = lane_snapshot(repo, lane)

    previous = None
    if state_file.exists():
        try:
            previous = json.loads(state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = None
    changed = (previous or {}).get("next_open_ticket") != snap["next_open_ticket"]
    state_file.write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")

    if changed and snap["next_open_ticket"]:
        LOG.info("lane %s: ticket %s available -- %s",
                 lane, snap["next_open_ticket"], snap["title"])
        status, detail = ml.notify(
            cfg, escalation_id=snap["next_open_ticket"],
            summary=f"BRITTLE {lane}: ticket ready -- {snap['title']}",
            lane=lane, unit=snap["unit"], rel_path=snap["path"] or "")
        LOG.info("lane %s: notification_status=%s (%s)", lane, status, detail)
        snap["notification_status"] = status
    elif changed:
        LOG.info("lane %s: no open tickets", lane)
    return snap


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ticket_watcher", description=__doc__.split("\n")[0])
    p.add_argument("--lane", required=True, choices=ml.AGENT_LANES)
    p.add_argument("--config")
    p.add_argument("--once", action="store_true")
    p.add_argument("--poll-seconds", type=float, default=30.0)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    cfg = ml.load_config(args.config)
    state_file = ml.state_dir() / f"lane-{args.lane}.json"
    LOG.info("watching lane %s (repo=%s, poll=%ss)",
             args.lane, cfg["repo"]["path"], args.poll_seconds)

    while True:
        try:
            run_once(cfg, args.lane, state_file)
        except MessageError as exc:
            LOG.error("pass failed: %s", exc)
        except Exception as exc:  # a watcher must not die on one bad pass
            LOG.exception("unexpected error: %s", exc)
        if args.once or _STOP:
            break
        for _ in range(int(max(1, args.poll_seconds))):
            if _STOP:
                break
            time.sleep(1)
    LOG.info("watcher stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
