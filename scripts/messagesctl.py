#!/usr/bin/env python3
"""messagesctl -- operate the BRITTLE GitHub message bus.

Every mutating command: locks the repo, pulls --ff-only, validates, creates
only new files, runs secret + schema checks, commits, pushes, retries bounded
on concurrent append, and spools durably if the push cannot land.

Never force-pushes, amends published commits, rewrites history, deletes
messages, or overwrites another agent's files.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import messagelib as ml  # noqa: E402
from messagelib import MessageError  # noqa: E402
from spend_guard import SpendGuard  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _cfg(args) -> dict:
    cfg = ml.load_config(getattr(args, "config", None))
    if getattr(args, "repo", None):
        cfg.setdefault("repo", {})["path"] = args.repo
    return cfg


def _repo(cfg: dict) -> ml.Repo:
    return ml.Repo(cfg["repo"]["path"])


def _private(cfg: dict) -> list[str]:
    return list(cfg.get("safety", {}).get("private_patterns", []) or [])


def _emit(args, payload: dict, human: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(human)


def _lease_seconds(cfg: dict) -> int:
    return int(cfg.get("claims", {}).get("lease_seconds") or ml.DEFAULT_LEASE_SECONDS)


def _require_not_paused(msgs, action: str) -> None:
    st = ml.autonomy_state(msgs)
    if st["paused"]:
        raise MessageError(
            f"autonomy is PAUSED -- refusing to {action}. "
            f"Queued messages are untouched; run `messagesctl resume` to continue."
        )


# --------------------------------------------------------------------------
# validate / sync / rebuild-index
# --------------------------------------------------------------------------

_MOVE_OK = (ml.DIR_ESC_OPEN, ml.DIR_ESC_RESOLVED)


def _diff_immutability(repo: ml.Repo, base: str) -> list[str]:
    """CI gate: nothing under the message dirs may be modified or deleted."""
    problems: list[str] = []
    raw = repo.git("diff", "--name-status", "-M100%", f"{base}...HEAD", check=False)
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        code = parts[0]
        paths = parts[1:]
        rel = paths[0]
        if not any(rel.startswith(d + "/") for d in ml.MESSAGE_DIRS):
            continue
        if code.startswith("A"):
            continue
        if code.startswith("R") and len(paths) == 2:
            src, dst = paths
            if (src.startswith(_MOVE_OK[0] + "/") and dst.startswith(_MOVE_OK[1] + "/")
                    and os.path.basename(src) == os.path.basename(dst)):
                continue
            problems.append(f"{src} -> {dst}: published messages may not be renamed")
            continue
        problems.append(
            f"{rel}: {code} -- published messages are immutable "
            f"(append a receipt instead of editing)"
        )
    return problems


def cmd_validate(args) -> int:
    cfg = _cfg(args)
    repo = _repo(cfg)
    problems = ml.validate_repo(repo.path, private_patterns=_private(cfg),
                                check_index=not args.no_index_check)
    if args.diff_base:
        problems += _diff_immutability(repo, args.diff_base)
    if problems:
        print(f"INVALID -- {len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    msgs = ml.load_messages(repo.path)
    print(f"OK -- {len(msgs)} message(s) validated in {repo.path}")
    return 0


def cmd_rebuild_index(args) -> int:
    cfg = _cfg(args)
    repo = _repo(cfg)
    with ml.repo_lock(repo.path):
        msgs = ml.load_messages(repo.path)
        text = ml.index_text(ml.build_index(msgs))
        path = repo.path / ml.INDEX_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        changed = (not path.exists()) or path.read_text(encoding="utf-8") != text
        path.write_text(text, encoding="utf-8")
        if not changed:
            print(f"index already current: {ml.INDEX_PATH}")
            return 0
        if args.commit:
            repo.git("add", "--", ml.INDEX_PATH)
            repo.git("commit", "-m", "chore(state): rebuild message index", identity=True)
            if repo.has_remote() and not repo.git_ok("push", "origin", f"HEAD:{repo.branch()}"):
                ml._mark_unpushed(repo, repo.head())
                print("index rebuilt and committed; push deferred to the sync timer")
                return 0
        print(f"index rebuilt: {ml.INDEX_PATH}")
    return 0


def cmd_sync(args) -> int:
    """Pull, retry deferred pushes, replay the outbox spool."""
    cfg = _cfg(args)
    repo = _repo(cfg)
    out = {"pulled": False, "pushed": 0, "spool_replayed": 0, "errors": []}
    with ml.repo_lock(repo.path):
        try:
            repo.pull_ff_only()
            out["pulled"] = True
        except MessageError as exc:
            out["errors"].append(f"pull: {exc}")

        # deferred pushes
        marker = ml.spool_dir() / "pending_push.json"
        if marker.exists() and repo.has_remote():
            if repo.git_ok("push", "origin", f"HEAD:{repo.branch()}"):
                out["pushed"] = 1
                ml._clear_unpushed()
            else:
                out["errors"].append("deferred push still failing")

        # outbox spool (messages that never made it into a commit)
        outbox = ml.spool_dir() / "outbox"
        for path in sorted(outbox.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                files = payload.get("files") or {}
                files = {k: v for k, v in files.items() if not (repo.path / k).exists()}
                if not files:
                    path.unlink()
                    continue
                ml.publish(repo, files, "chore(spool): replay deferred publication",
                           cfg=cfg)
                path.unlink()
                out["spool_replayed"] += 1
            except MessageError as exc:
                out["errors"].append(f"spool {path.name}: {exc}")

    _emit(args, out,
          f"sync: pulled={out['pulled']} pushed={out['pushed']} "
          f"spool_replayed={out['spool_replayed']} errors={len(out['errors'])}")
    for e in out["errors"]:
        print(f"  ! {e}", file=sys.stderr)
    return 0 if not out["errors"] else 1


# --------------------------------------------------------------------------
# publish-report
# --------------------------------------------------------------------------


def cmd_publish_report(args) -> int:
    cfg = _cfg(args)
    repo = _repo(cfg)
    src = Path(args.report).expanduser().resolve()
    if not src.is_file():
        raise MessageError(f"report not found: {src}")
    if src.suffix.lower() != ".md":
        raise MessageError("only Markdown reports may be mirrored (.md)")

    before_sha = ml.sha256_file(src)
    before_mtime = src.stat().st_mtime_ns
    original = src.read_text(encoding="utf-8")

    private = _private(cfg)
    findings = ml.scan_secrets(original, private)
    redacted = bool(findings)
    truncated = False

    if redacted:
        mirror = (
            "> **Redacted mirror.** The full report matched a configured private "
            "pattern and remains local-only.\n>\n"
            f"> Local path: `{src}`\n"
            f"> SHA-256: `{before_sha}`\n"
            f"> Findings: {len(findings)} pattern hit(s).\n\n"
            "## Communication summary\n\n"
            f"{args.summary or 'Report withheld pending redaction review.'}\n"
        )
    else:
        mirror = original
        budget = ml.MAX_MESSAGE_BYTES - 4096
        if len(mirror.encode("utf-8")) > budget:
            truncated = True
            data = mirror.encode("utf-8")[:budget]
            mirror = data.decode("utf-8", errors="ignore") + (
                "\n\n---\n\n*[mirror truncated at the public-bus size limit; the "
                f"authoritative full report is local at `{src}`, SHA-256 `{before_sha}`]*\n"
            )

    now = ml.utc_now()
    fm = ml.base_frontmatter(
        "report",
        sender=args.lane,
        to="reviewer",
        lane=args.lane,
        unit=args.unit,
        status="open",
        requires_owner=False,
        confidence=None,
        in_reply_to=args.in_reply_to,
        source_commit=ml.brittle_commit(cfg["repo"]["brittle_path"]),
        local_source_path=str(src),
        local_source_sha256=before_sha,
        now=now,
    )
    fm["title"] = args.title or src.stem
    fm["mirror_bytes"] = len(mirror.encode("utf-8"))
    if redacted:
        fm["redacted"] = True
    if truncated:
        fm["truncated"] = True

    body = (
        f"# {fm['title']}\n\n"
        f"| field | value |\n| --- | --- |\n"
        f"| local path | `{src}` |\n"
        f"| sha256 | `{before_sha}` |\n"
        f"| brittle commit | `{fm['source_commit']}` |\n"
        f"| lane | {args.lane} |\n"
        f"| unit | {args.unit or '-'} |\n"
        f"| mirrored at | {fm['created_at']} |\n\n"
        f"{ml.MIRROR_MARKER}\n\n"
        f"{mirror}"
    )
    rel = f"{ml.DIR_REPORTS}/{args.lane}/{fm['id']}.md"
    content = ml.render_message(fm, body)

    with ml.repo_lock(repo.path):
        result = ml.publish(repo, {rel: content},
                            f"report({args.lane}): {fm['title'][:60]} [{fm['id']}]",
                            cfg=cfg)

    # local source must be byte-identical and untouched
    after_sha = ml.sha256_file(src)
    if after_sha != before_sha or src.stat().st_mtime_ns != before_mtime:
        raise MessageError(
            f"FATAL: local report changed during mirroring ({before_sha} -> {after_sha})"
        )

    payload = {
        "report_id": fm["id"], "path": rel, "local_source_path": str(src),
        "local_source_sha256": before_sha, "redacted": redacted,
        "truncated": truncated, **result.as_dict(),
    }
    _emit(args, payload,
          f"published report {fm['id']} -> {rel} "
          f"(commit {result.commit[:8]}, pushed={result.pushed}"
          f"{', REDACTED' if redacted else ''})")
    return 0


# --------------------------------------------------------------------------
# publish-ticket
# --------------------------------------------------------------------------

_TICKET_HINTS = ("title", "unit", "requires_owner", "confidence", "supersedes",
                 "in_reply_to", "next_action")


def cmd_publish_ticket(args) -> int:
    cfg = _cfg(args)
    repo = _repo(cfg)
    src = Path(args.ticket).expanduser().resolve()
    if not src.is_file():
        raise MessageError(f"ticket file not found: {src}")
    raw = src.read_text(encoding="utf-8")

    hints: dict = {}
    body = raw
    if raw.startswith("---\n"):
        parsed, body = ml.parse_message(raw)
        bad = [k for k in parsed if k not in _TICKET_HINTS]
        if bad:
            raise MessageError(
                f"ticket file may only pre-set {list(_TICKET_HINTS)}; found {sorted(bad)}"
            )
        hints = parsed

    title = args.title or hints.get("title")
    if not title:
        for line in body.split("\n"):
            if line.startswith("# "):
                title = line[2:].strip()
                break
    if not title:
        raise MessageError("ticket needs a title (--title, frontmatter, or a '# ' heading)")

    with ml.repo_lock(repo.path):
        repo.pull_ff_only()
        msgs = ml.load_messages(repo.path)
        if not args.force:
            _require_not_paused(msgs, "issue a new ticket")

        fm = ml.base_frontmatter(
            "ticket",
            sender=args.author,
            to=args.lane,
            lane=args.lane,
            unit=args.unit or hints.get("unit"),
            status="open",
            requires_owner=bool(hints.get("requires_owner", False)),
            confidence=hints.get("confidence"),
            in_reply_to=args.in_reply_to or hints.get("in_reply_to"),
            supersedes=args.supersedes or hints.get("supersedes"),
            source_commit=ml.brittle_commit(cfg["repo"]["brittle_path"]),
        )
        fm["title"] = title
        if fm["requires_owner"]:
            raise MessageError(
                "a ticket that requires owner judgment must be published as an "
                "escalation instead (messagesctl escalate)"
            )
        rel = f"{ml.DIR_TICKETS}/{args.lane}/{fm['id']}.md"
        content = ml.render_message(fm, body)
        result = ml.publish(repo, {rel: content},
                            f"ticket({args.lane}): {title[:60]} [{fm['id']}]", cfg=cfg)

    _emit(args, {"ticket_id": fm["id"], "path": rel, **result.as_dict()},
          f"published ticket {fm['id']} -> {rel} (commit {result.commit[:8]}, "
          f"pushed={result.pushed})")
    return 0


# --------------------------------------------------------------------------
# next-ticket / claim / complete / block
# --------------------------------------------------------------------------


def cmd_next_ticket(args) -> int:
    cfg = _cfg(args)
    repo = _repo(cfg)
    with ml.repo_lock(repo.path):
        if not args.no_pull:
            try:
                repo.pull_ff_only()
            except MessageError as exc:
                print(f"warning: {exc}", file=sys.stderr)
        msgs = ml.load_messages(repo.path)

    now = ml.utc_now()
    candidates = []
    for m in sorted(msgs.values(), key=ml.Message.sort_key):
        if m.kind != "ticket" or m.get("lane") != args.lane:
            continue
        st = ml.ticket_state(m.id, msgs, now=now)
        if st["status"] == "open":
            candidates.append((m, st))

    if not candidates:
        _emit(args, {"ticket": None, "lane": args.lane},
              f"no open ticket for lane {args.lane}")
        return 0

    m, st = candidates[0]
    payload = {
        "ticket": m.id,
        "lane": m.get("lane"),
        "unit": m.get("unit"),
        "title": m.get("title"),
        "path": m.rel,
        "created_at": m.get("created_at"),
        "in_reply_to": m.get("in_reply_to"),
        "reclaimable_after_expiry": st["lease_expired"] and st["claim"] is not None,
        "body": m.body,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"{m.id}  [{m.get('lane')}/{m.get('unit') or '-'}]  {m.get('title')}")
        print(f"  path: {m.rel}")
        print(f"  created: {m.get('created_at')}")
        print("\n" + m.body.rstrip() + "\n")
    return 0


def cmd_claim(args) -> int:
    cfg = _cfg(args)
    repo = _repo(cfg)
    now = ml.utc_now()
    lease = _dt.timedelta(seconds=_lease_seconds(cfg))

    with ml.repo_lock(repo.path):
        repo.pull_ff_only()
        msgs = ml.load_messages(repo.path)
        ticket = msgs.get(args.message_id)
        if ticket is None or ticket.kind != "ticket":
            raise MessageError(f"{args.message_id} is not a known ticket")
        if args.agent not in ml.AGENT_LANES:
            raise MessageError(f"agent must be one of {ml.AGENT_LANES}")
        if ticket.get("lane") != args.agent:
            raise MessageError(
                f"lane violation: a {args.agent} agent may not claim the "
                f"{ticket.get('lane')} ticket {ticket.id}"
            )

        st = ml.ticket_state(ticket.id, msgs, now=now)
        receipt_type = "claim"
        if st["status"] == "completed":
            raise MessageError(f"{ticket.id} is already completed")
        if st["status"] == "blocked":
            raise MessageError(f"{ticket.id} is blocked; publish a new ticket instead")
        if st["status"] == "superseded":
            raise MessageError(f"{ticket.id} was superseded by {st['superseded_by']}")
        if st["status"] == "claimed":
            if st["claim_agent"] == args.agent and args.renew:
                receipt_type = "renew"
            else:
                raise MessageError(
                    f"{ticket.id} is already claimed by {st['claim_agent']} until "
                    f"{st['lease_expires_at']}"
                )
        elif st["claim"] is not None:
            receipt_type = "reclaim"  # prior lease expired

        fm = ml.base_frontmatter(
            "receipt",
            sender=args.agent,
            to="reviewer",
            lane=args.agent,
            unit=ticket.get("unit"),
            status="claimed",
            in_reply_to=ticket.id,
            source_commit=ml.brittle_commit(cfg["repo"]["brittle_path"]),
            now=now,
        )
        fm.update({
            "receipt_type": receipt_type,
            "agent": args.agent,
            "ticket_id": ticket.id,
            "claimed_at": ml.iso(now),
            "lease_expires_at": ml.iso(now + lease),
            "brittle_commit": ml.brittle_commit(cfg["repo"]["brittle_path"]) or "0000000",
        })
        body = (
            f"# {receipt_type.title()} receipt\n\n"
            f"Agent **{args.agent}** {receipt_type}s ticket `{ticket.id}`.\n\n"
            f"- claimed_at: {fm['claimed_at']}\n"
            f"- lease_expires_at: {fm['lease_expires_at']}\n"
            f"- brittle commit: `{fm['brittle_commit']}`\n"
        )
        rel = f"{ml.DIR_RECEIPTS}/{fm['id']}.md"
        result = ml.publish(repo, {rel: ml.render_message(fm, body)},
                            f"receipt({receipt_type}): {ticket.id} by {args.agent} "
                            f"[{fm['id']}]", cfg=cfg)

    _emit(args, {"receipt_id": fm["id"], "receipt_type": receipt_type,
                 "ticket": ticket.id, "lease_expires_at": fm["lease_expires_at"],
                 **result.as_dict()},
          f"{receipt_type} {ticket.id} as {args.agent}; lease until "
          f"{fm['lease_expires_at']} (receipt {fm['id']}, pushed={result.pushed})")
    return 0


def _terminal_receipt(args, cfg, *, receipt_type: str, status: str,
                      extra: dict, body: str) -> tuple[dict, ml.PublishResult]:
    repo = _repo(cfg)
    with ml.repo_lock(repo.path):
        repo.pull_ff_only()
        msgs = ml.load_messages(repo.path)
        ticket = msgs.get(args.message_id)
        if ticket is None or ticket.kind != "ticket":
            raise MessageError(f"{args.message_id} is not a known ticket")
        st = ml.ticket_state(ticket.id, msgs)
        if st["status"] in ("completed", "superseded"):
            raise MessageError(f"{ticket.id} is already {st['status']}")
        agent = st["claim_agent"] or ticket.get("lane")
        fm = ml.base_frontmatter(
            "receipt",
            sender=str(agent),
            to="reviewer",
            lane=str(ticket.get("lane")),
            unit=ticket.get("unit"),
            status=status,
            in_reply_to=ticket.id,
            source_commit=ml.brittle_commit(cfg["repo"]["brittle_path"]),
        )
        fm.update({"receipt_type": receipt_type, "agent": str(agent),
                   "ticket_id": ticket.id,
                   "brittle_commit": ml.brittle_commit(cfg["repo"]["brittle_path"])})
        fm.update(extra)
        rel = f"{ml.DIR_RECEIPTS}/{fm['id']}.md"
        result = ml.publish(repo, {rel: ml.render_message(fm, body)},
                            f"receipt({receipt_type}): {ticket.id} [{fm['id']}]",
                            cfg=cfg)
    return fm, result


def cmd_complete(args) -> int:
    cfg = _cfg(args)
    repo = _repo(cfg)
    msgs = ml.load_messages(repo.path)
    report = msgs.get(args.report_id)
    if report is None or report.kind != "report":
        raise MessageError(f"--report-id {args.report_id} is not a known report")

    body = (
        "# Completion receipt\n\n"
        f"Ticket `{args.message_id}` completed.\n\n"
        f"- report message: `{args.report_id}`\n"
        f"- local report path: `{report.get('local_source_path')}`\n"
        f"- local report sha256: `{report.get('local_source_sha256')}`\n"
        f"- brittle commit: `{report.get('source_commit')}`\n"
    )
    fm, result = _terminal_receipt(
        args, cfg, receipt_type="complete", status="completed",
        extra={
            "report_id": args.report_id,
            "local_source_path": report.get("local_source_path"),
            "local_source_sha256": report.get("local_source_sha256"),
        },
        body=body,
    )
    _emit(args, {"receipt_id": fm["id"], **result.as_dict()},
          f"completed {args.message_id} -> report {args.report_id} "
          f"(receipt {fm['id']}, pushed={result.pushed})")
    return 0


def cmd_block(args) -> int:
    cfg = _cfg(args)
    body = (
        "# Blocked receipt\n\n"
        f"Ticket `{args.message_id}` cannot proceed.\n\n"
        f"**Reason:** {args.reason}\n"
    )
    fm, result = _terminal_receipt(
        args, cfg, receipt_type="block", status="blocked",
        extra={"reason": args.reason}, body=body,
    )
    _emit(args, {"receipt_id": fm["id"], **result.as_dict()},
          f"blocked {args.message_id} (receipt {fm['id']}, pushed={result.pushed})")
    return 0


# --------------------------------------------------------------------------
# escalate / resolve-escalation
# --------------------------------------------------------------------------


def cmd_escalate(args) -> int:
    cfg = _cfg(args)
    repo = _repo(cfg)
    now = ml.utc_now()
    detail = ""
    if args.detail_file:
        detail = Path(args.detail_file).expanduser().read_text(encoding="utf-8")

    with ml.repo_lock(repo.path):
        repo.pull_ff_only()
        fm = ml.base_frontmatter(
            "escalation",
            sender=args.lane,
            to="joe",
            lane=args.lane,
            unit=args.unit,
            status="open",
            requires_owner=True,
            confidence=args.confidence,
            in_reply_to=args.in_reply_to,
            source_commit=ml.brittle_commit(cfg["repo"]["brittle_path"]),
            now=now,
        )
        fm["title"] = args.summary[:120]
        body = (
            "# Owner decision required\n\n"
            f"**Question for Joe:** {args.summary}\n\n"
            f"- lane: `{args.lane}`\n"
            f"- unit: `{args.unit or '-'}`\n"
            f"- brittle commit: `{fm['source_commit']}`\n"
            f"- raised at: {fm['created_at']}\n"
            + (f"- confidence: {args.confidence}\n" if args.confidence is not None else "")
            + (f"\n## Detail\n\n{detail}\n" if detail else "")
            + "\n## How to answer\n\n"
            "```bash\n"
            "messagesctl resolve-escalation \\\n"
            f"  --id {fm['id']} \\\n"
            "  --decision-file /path/to/decision.md\n"
            "```\n"
        )
        rel = f"{ml.DIR_ESC_OPEN}/{fm['id']}.md"
        result = ml.publish(repo, {rel: ml.render_message(fm, body)},
                            f"escalation({args.lane}): {fm['title'][:60]} [{fm['id']}]",
                            cfg=cfg)

        # notify only after the escalation is durable
        status, ndetail = ml.notify(cfg, escalation_id=fm["id"], summary=args.summary,
                                    lane=args.lane, unit=args.unit, rel_path=rel)

        nfm = ml.base_frontmatter(
            "receipt", sender=args.lane, to="joe", lane=args.lane, unit=args.unit,
            status="open", requires_owner=True, in_reply_to=fm["id"],
        )
        nfm.update({"receipt_type": "escalation_notice", "agent": args.lane,
                    "escalation_id": fm["id"], "notification_status": status,
                    "notification_detail": ndetail[:200]})
        nbody = (
            "# Escalation notification receipt\n\n"
            f"- escalation: `{fm['id']}`\n"
            f"- notification_status: **{status}**\n"
            f"- detail: {ndetail}\n"
        )
        nrel = f"{ml.DIR_RECEIPTS}/{nfm['id']}.md"
        nresult = ml.publish(repo, {nrel: ml.render_message(nfm, nbody)},
                             f"receipt(escalation_notice): {fm['id']} "
                             f"notification={status} [{nfm['id']}]", cfg=cfg)

    payload = {"escalation_id": fm["id"], "path": rel,
               "notification_status": status, "notification_detail": ndetail,
               "notice_receipt": nfm["id"], "commit": nresult.commit,
               "pushed": result.pushed and nresult.pushed}
    _emit(args, payload,
          f"escalation {fm['id']} published -> {rel}\n"
          f"  notification_status: {status} ({ndetail})\n"
          f"  pushed: {payload['pushed']}")
    return 0 if status != "failed" else 3


def cmd_resolve_escalation(args) -> int:
    cfg = _cfg(args)
    repo = _repo(cfg)
    decision_path = Path(args.decision_file).expanduser().resolve()
    if not decision_path.is_file():
        raise MessageError(f"decision file not found: {decision_path}")
    raw = decision_path.read_text(encoding="utf-8")
    checksum = ml.sha256_text(raw)

    hints: dict = {}
    body = raw
    if raw.startswith("---\n"):
        hints, body = ml.parse_message(raw)
    authorized = args.authorized_action or hints.get("authorized_action")
    scope = args.scope or hints.get("scope")
    if not authorized or not scope:
        raise MessageError(
            "owner decision must state --authorized-action and --scope "
            "(or set them in the decision file's frontmatter)"
        )

    with ml.repo_lock(repo.path):
        repo.pull_ff_only()
        msgs = ml.load_messages(repo.path)
        esc = msgs.get(args.id)
        if esc is None or esc.kind != "escalation":
            raise MessageError(f"{args.id} is not a known escalation")
        if ml.escalation_state(esc.id, msgs) == "resolved":
            raise MessageError(f"{esc.id} is already resolved")

        fm = ml.base_frontmatter(
            "owner_decision", sender="joe", to=str(esc.get("lane")),
            lane=str(esc.get("lane")), unit=esc.get("unit"), status="resolved",
            requires_owner=False, in_reply_to=esc.id,
            local_source_path=str(decision_path),
            local_source_sha256=checksum,
        )
        fm.update({
            "escalation_id": esc.id,
            "authorized_action": authorized,
            "scope": scope,
            "checksum": checksum,
            "expires_at": args.expires_at,
        })
        if args.autonomy:
            fm["autonomy"] = args.autonomy
        dbody = (
            "# Owner decision\n\n"
            f"- decision id: `{fm['id']}`\n"
            f"- resolves escalation: `{esc.id}`\n"
            f"- unit: `{esc.get('unit') or '-'}`\n"
            f"- authorized action: **{authorized}**\n"
            f"- scope: {scope}\n"
            f"- expires_at: {args.expires_at or 'none'}\n"
            f"- checksum (sha256 of source decision file): `{checksum}`\n"
            f"- source: `{decision_path}`\n\n"
            "> Guard-critical authorizations must ALSO be recorded in Guard live "
            "state by the control agent. This message is communication evidence, "
            "not a Guard state mutation.\n\n"
            "---\n\n" + body
        )
        drel = f"{ml.DIR_DECISIONS}/{fm['id']}.md"

        rfm = ml.base_frontmatter(
            "receipt", sender="joe", to=str(esc.get("lane")), lane=str(esc.get("lane")),
            unit=esc.get("unit"), status="resolved", in_reply_to=esc.id,
        )
        rfm.update({"receipt_type": "escalation_resolved", "agent": "joe",
                    "escalation_id": esc.id, "decision_id": fm["id"]})
        rbody = (f"# Escalation resolved\n\nEscalation `{esc.id}` resolved by owner "
                 f"decision `{fm['id']}`.\n")
        rrel = f"{ml.DIR_RECEIPTS}/{rfm['id']}.md"

        moves = {esc.rel: f"{ml.DIR_ESC_RESOLVED}/{os.path.basename(esc.rel)}"} \
            if esc.rel.startswith(ml.DIR_ESC_OPEN + "/") else {}

        result = ml.publish(
            repo,
            {drel: ml.render_message(fm, dbody), rrel: ml.render_message(rfm, rbody)},
            f"owner_decision: resolve {esc.id} [{fm['id']}]",
            cfg=cfg, moves=moves,
        )

    _emit(args, {"decision_id": fm["id"], "escalation_id": esc.id,
                 "receipt_id": rfm["id"], "checksum": checksum, **result.as_dict()},
          f"resolved {esc.id} with owner decision {fm['id']} "
          f"(commit {result.commit[:8]}, pushed={result.pushed})\n"
          f"  reminder: record Guard-critical authorization in Guard live state "
          f"via the control agent.")
    return 0


# --------------------------------------------------------------------------
# pause / resume / status / open-escalations / tail / notify-test
# --------------------------------------------------------------------------


def _autonomy_decision(args, cfg: dict, mode: str) -> int:
    repo = _repo(cfg)
    marker = ml.state_dir() / "PAUSED"
    if mode == "paused":
        marker.write_text(ml.iso(ml.utc_now()) + "\n", encoding="utf-8")
    elif marker.exists():
        marker.unlink()

    committed = {"pushed": False, "commit": "", "error": ""}
    try:
        with ml.repo_lock(repo.path):
            repo.pull_ff_only()
            fm = ml.base_frontmatter(
                "owner_decision", sender="joe", to="reviewer", lane="joe",
                unit="autonomy", status="resolved", requires_owner=False,
            )
            action = ("PAUSE autonomous ticket issuance"
                      if mode == "paused" else "RESUME autonomous ticket issuance")
            fm.update({
                "autonomy": mode,
                "authorized_action": action,
                "scope": "reviewer daemon ticket issuance only; queued messages untouched",
                "checksum": ml.sha256_text(f"{mode}|{fm['created_at']}"),
            })
            body = (
                f"# Autonomy {mode}\n\n"
                f"{action}.\n\n"
                "Queued tickets, reports, receipts and escalations are left exactly "
                "as they are. Only the issuance of *new* autonomous tickets is "
                f"{'suspended' if mode == 'paused' else 'permitted'}.\n"
            )
            rel = f"{ml.DIR_DECISIONS}/{fm['id']}.md"
            result = ml.publish(repo, {rel: ml.render_message(fm, body)},
                                f"owner_decision(autonomy): {mode} [{fm['id']}]", cfg=cfg)
            committed = {"pushed": result.pushed, "commit": result.commit, "error": ""}
    except MessageError as exc:
        committed["error"] = str(exc)

    local = "engaged" if mode == "paused" else "cleared"
    _emit(args, {"autonomy": mode, "local_marker": local, **committed},
          f"autonomy {mode.upper()} (local marker {local}; "
          f"committed={bool(committed['commit'])}, pushed={committed['pushed']})"
          + (f"\n  warning: {committed['error']}" if committed["error"] else ""))
    return 0


def cmd_pause(args) -> int:
    return _autonomy_decision(args, _cfg(args), "paused")


def cmd_resume(args) -> int:
    return _autonomy_decision(args, _cfg(args), "active")


def cmd_status(args) -> int:
    cfg = _cfg(args)
    repo = _repo(cfg)
    msgs = ml.load_messages(repo.path)
    now = ml.utc_now()
    idx = ml.build_index(msgs, now=now)
    auto = ml.autonomy_state(msgs)
    marker = ml.spool_dir() / "pending_push.json"
    outbox = list((ml.spool_dir() / "outbox").glob("*.json"))
    unreviewed = [
        m.id for m in sorted(msgs.values(), key=ml.Message.sort_key)
        if m.kind == "report" and not ml.reviewer_acked(m.id, msgs)
    ]
    spend = SpendGuard(cfg).status()
    payload = {
        "repo": str(repo.path),
        "head": repo.head()[:12],
        "autonomy": "PAUSED" if auto["paused"] else "ACTIVE",
        "autonomy_source": auto,
        "messages": idx["counts"],
        "open_ticket_by_lane": idx["open_ticket_by_lane"],
        "active_claims": idx["active_claims"],
        "open_escalations": idx["open_escalations"],
        "latest_report_by_lane": idx["latest_report_by_lane"],
        "reports_awaiting_review": unreviewed,
        "deferred_push": marker.exists(),
        "spooled_outbox": len(outbox),
        "spending": spend,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"repo            : {payload['repo']} @ {payload['head']}")
    print(f"autonomy        : {payload['autonomy']}")
    print(f"messages        : " + ", ".join(f"{k}={v}" for k, v in idx["counts"].items()))
    print(f"open tickets    : {idx['open_ticket_by_lane'] or '-'}")
    print(f"active claims   : {idx['active_claims'] or '-'}")
    print(f"open escalations: {idx['open_escalations'] or '-'}")
    print(f"awaiting review : {unreviewed or '-'}")
    print(f"deferred push   : {payload['deferred_push']}   spooled outbox: {len(outbox)}")
    print()
    print(f"spend guard     : {'BLOCKED' if spend['blocked'] else 'OK'}"
          f"{'  (' + '; '.join(spend['blocked_reasons']) + ')' if spend['blocked'] else ''}")
    print(f"  day  {spend['utc_day']}  : ${spend['day_committed_usd']:.4f} committed "
          f"/ ${spend['day_cap_usd']:.2f} cap   remaining ${spend['day_remaining_usd']:.4f}")
    print(f"  month {spend['utc_month']}    : ${spend['month_committed_usd']:.4f} committed "
          f"/ ${spend['month_cap_usd']:.2f} cap   remaining ${spend['month_remaining_usd']:.4f}")
    print(f"  calls today   : {spend['calls_today']}/{spend['max_calls_per_day']}"
          f"   outstanding: {spend['outstanding_reservations_today']}"
          f"   max completion tokens: {spend['max_completion_tokens']}")
    print(f"  priced models : {spend['priced_models'] or '(none — all requests refused)'}")
    if not spend["healthy"]:
        print(f"  ledger        : UNHEALTHY — {spend['detail']}")
    return 0


def cmd_open_escalations(args) -> int:
    cfg = _cfg(args)
    repo = _repo(cfg)
    msgs = ml.load_messages(repo.path)
    rows = []
    for m in sorted(msgs.values(), key=ml.Message.sort_key):
        if m.kind != "escalation" or ml.escalation_state(m.id, msgs) != "open":
            continue
        notice = next(
            (r for r in msgs.values()
             if r.kind == "receipt" and r.get("receipt_type") == "escalation_notice"
             and r.get("in_reply_to") == m.id), None)
        rows.append({
            "id": m.id, "lane": m.get("lane"), "unit": m.get("unit"),
            "created_at": m.get("created_at"), "question": m.get("title"),
            "notification_status": notice.get("notification_status") if notice else "unknown",
            "path": m.rel,
        })
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no open escalations")
        return 0
    for r in rows:
        print(f"{r['id']}  [{r['lane']}/{r['unit'] or '-'}]  notify={r['notification_status']}")
        print(f"    {r['question']}")
    return 0


def cmd_tail(args) -> int:
    cfg = _cfg(args)
    repo = _repo(cfg)
    msgs = ml.load_messages(repo.path)
    ordered = sorted(msgs.values(), key=ml.Message.sort_key)[-args.n :]
    if args.json:
        print(json.dumps([
            {"id": m.id, "kind": m.kind, "lane": m.get("lane"), "from": m.get("from"),
             "to": m.get("to"), "created_at": m.get("created_at"),
             "title": m.get("title") or m.get("receipt_type"), "path": m.rel}
            for m in ordered], indent=2))
        return 0
    for m in ordered:
        label = m.get("title") or m.get("receipt_type") or ""
        print(f"{m.get('created_at')}  {m.kind:<14} {str(m.get('lane')):<11} "
              f"{m.id}  {str(label)[:60]}")
    return 0


def cmd_notify_test(args) -> int:
    cfg = _cfg(args)
    status, detail = ml.notify(
        cfg, escalation_id="BRITTLE-00000000T000000Z-00000000",
        summary=args.summary, lane="locomotion", unit="notify-test",
        rel_path="(dry-run)")
    _emit(args, {"notification_status": status, "detail": detail},
          f"notification_status: {status}\n  detail: {detail}")
    return 0 if status == "sent" else 1


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="messagesctl", description=__doc__.split("\n")[0])
    p.add_argument("--config", help="path to config.toml")
    p.add_argument("--repo", help="override [repo].path")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("validate", help="validate the whole repository")
    s.add_argument("--diff-base", help="also enforce immutability against this git ref")
    s.add_argument("--no-index-check", action="store_true")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("sync", help="pull, retry deferred pushes, replay the spool")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("rebuild-index", help="regenerate state/index.json from history")
    s.add_argument("--commit", action="store_true")
    s.set_defaults(func=cmd_rebuild_index)

    s = sub.add_parser("publish-report", help="mirror a local BRITTLE report")
    s.add_argument("--lane", required=True, choices=ml.AGENT_LANES)
    s.add_argument("--unit", required=True)
    s.add_argument("--report", required=True)
    s.add_argument("--title")
    s.add_argument("--in-reply-to")
    s.add_argument("--summary", help="communication summary used if the report is redacted")
    s.set_defaults(func=cmd_publish_report)

    s = sub.add_parser("publish-ticket", help="publish a ticket for a lane")
    s.add_argument("--lane", required=True, choices=ml.AGENT_LANES)
    s.add_argument("--ticket", required=True)
    s.add_argument("--unit")
    s.add_argument("--title")
    s.add_argument("--author", default="reviewer")
    s.add_argument("--in-reply-to")
    s.add_argument("--supersedes")
    s.add_argument("--force", action="store_true", help="ignore the autonomy pause")
    s.set_defaults(func=cmd_publish_ticket)

    s = sub.add_parser("next-ticket", help="show the next open ticket for a lane")
    s.add_argument("--lane", required=True, choices=ml.AGENT_LANES)
    s.add_argument("--no-pull", action="store_true")
    s.set_defaults(func=cmd_next_ticket)

    s = sub.add_parser("claim", help="claim a ticket (publishes a claim receipt)")
    s.add_argument("message_id")
    s.add_argument("--agent", required=True, choices=ml.AGENT_LANES)
    s.add_argument("--renew", action="store_true", help="renew an existing own lease")
    s.set_defaults(func=cmd_claim)

    s = sub.add_parser("complete", help="publish a completion receipt")
    s.add_argument("message_id")
    s.add_argument("--report-id", required=True)
    s.set_defaults(func=cmd_complete)

    s = sub.add_parser("block", help="publish a blocked receipt")
    s.add_argument("message_id")
    s.add_argument("--reason", required=True)
    s.set_defaults(func=cmd_block)

    s = sub.add_parser("escalate", help="raise an owner decision and ping Joe")
    s.add_argument("--lane", required=True, choices=ml.LANES)
    s.add_argument("--unit")
    s.add_argument("--summary", required=True)
    s.add_argument("--detail-file")
    s.add_argument("--in-reply-to")
    s.add_argument("--confidence", type=float)
    s.set_defaults(func=cmd_escalate)

    s = sub.add_parser("resolve-escalation", help="record an owner decision")
    s.add_argument("--id", required=True)
    s.add_argument("--decision-file", required=True)
    s.add_argument("--authorized-action")
    s.add_argument("--scope")
    s.add_argument("--expires-at")
    s.add_argument("--autonomy", choices=("paused", "active"))
    s.set_defaults(func=cmd_resolve_escalation)

    for name, fn, helptext in (
        ("pause", cmd_pause, "stop issuing new autonomous tickets"),
        ("resume", cmd_resume, "resume autonomous ticket issuance"),
        ("status", cmd_status, "operational summary"),
        ("open-escalations", cmd_open_escalations, "list unresolved escalations"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.set_defaults(func=fn)

    s = sub.add_parser("tail", help="show the most recent messages")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(func=cmd_tail)

    s = sub.add_parser("notify-test", help="exercise the configured notification command")
    s.add_argument("--summary", default="BRITTLE messages: notification self-test")
    s.set_defaults(func=cmd_notify_test)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except MessageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
