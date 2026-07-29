#!/usr/bin/env python3
"""browser_bridge -- mechanically materialize browser GPT proposals as bus messages.

A browser GPT (ChatGPT with a GitHub connector, or any agent that can only read
and write this repository) is the Telephone operator, the report reviewer, the
stopping-criterion judge and the successor-ticket author. It writes structured
*requests* into `projects/brittle/browser_requests/`.

This bridge is the workstation half. It is deliberately, aggressively dumb:

    It validates. It publishes exactly what was submitted. It authors nothing.

There is no language model in this file, no summarisation, no rewriting, no
"improving" of ticket prose, and no choosing between alternatives. Ticket
markdown reaches the bus byte-for-byte as the browser GPT wrote it, or it is
refused. A test asserts the byte-for-byte property, and another asserts this
module imports no model client.

Every request is answered with an immutable `browser_result` receipt -- accepted
or refused, with the reason. A refusal is a published fact, not a silent drop.
"""

from __future__ import annotations

import argparse
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

LOG = logging.getLogger("brittle-browser-bridge")

REQUEST_ID_RE = re.compile(r"^BREQ-\d{8}T\d{6}Z-[0-9a-f]{8}$")

REQUEST_KINDS = (
    "status_request",
    "telephone_start",
    "telephone_stop",
    "review_and_ticket",
    "review_only",
    "criterion_judgement",
    "resolve_escalation",
)

REQUIRED_REQUEST_FIELDS = (
    "request_id", "kind", "project", "submitted_by", "created_at",
    "lane", "unit", "idempotency_key", "rationale",
)

OPTIONAL_REQUEST_FIELDS = (
    "run_id", "report_id", "ticket_id", "escalation_id", "payload",
    "criterion_status", "criterion_confidence", "criterion_evidence",
    "max_cycles", "criterion", "reason",
    "authorized_action", "scope",
)

BROWSER_STATUS_PATH = f"{ml.DIR_STATE}/browser_status.json"


def scrub(text: str) -> str:
    """Never echo a submitted secret back into a published refusal."""
    for _name, pattern in ml.SECRET_PATTERNS:
        text = pattern.sub("<redacted>", text)
    return text


class BridgeRefusal(MessageError):
    """A request that must be refused, with a receipt explaining why."""


# --------------------------------------------------------------------------
# Request validation -- schema, identity, safety
# --------------------------------------------------------------------------


def validate_request(req: dict, *, rel: str = "<mem>") -> None:
    where = f"{rel}: "
    if not isinstance(req, dict):
        raise BridgeRefusal(where + "request must be a JSON object")

    missing = [f for f in REQUIRED_REQUEST_FIELDS if f not in req]
    if missing:
        raise BridgeRefusal(where + f"missing required field(s): {missing}")
    allowed = set(REQUIRED_REQUEST_FIELDS) | set(OPTIONAL_REQUEST_FIELDS)
    unknown = [f for f in req if f not in allowed]
    if unknown:
        raise BridgeRefusal(where + f"unknown field(s): {sorted(unknown)}")

    if not REQUEST_ID_RE.match(str(req["request_id"])):
        raise BridgeRefusal(where + f"malformed request_id: {req['request_id']!r}")
    if req["kind"] not in REQUEST_KINDS:
        raise BridgeRefusal(where + f"bad kind: {req['kind']!r}")
    if req["project"] != ml.PROJECT:
        raise BridgeRefusal(where + f"bad project: {req['project']!r}")
    if not ml.TS_RE.match(str(req["created_at"])):
        raise BridgeRefusal(where + f"bad created_at: {req['created_at']!r}")
    if not str(req["idempotency_key"]).strip():
        raise BridgeRefusal(where + "idempotency_key may not be empty")
    if req["kind"] != "status_request" and req["lane"] not in ml.AGENT_LANES:
        raise BridgeRefusal(where + f"lane must be one of {ml.AGENT_LANES}")

    for key in ("run_id", "report_id", "ticket_id", "escalation_id"):
        val = req.get(key)
        if val is not None and not ml.ID_RE.match(str(val)):
            raise BridgeRefusal(where + f"bad reference in {key}: {val!r}")

    cs = req.get("criterion_status")
    if cs is not None and cs not in ("met", "not_met", "unknown"):
        raise BridgeRefusal(where + f"bad criterion_status: {cs!r}")
    cc = req.get("criterion_confidence")
    if cc is not None:
        if isinstance(cc, bool) or not isinstance(cc, (int, float)):
            raise BridgeRefusal(where + "criterion_confidence must be a number")
        if not (0.0 <= float(cc) <= 1.0):
            raise BridgeRefusal(where + f"criterion_confidence out of range: {cc}")

    kind = req["kind"]
    payload = req.get("payload") or {}
    if kind in ("review_and_ticket", "review_only"):
        if not req.get("report_id"):
            raise BridgeRefusal(where + f"{kind} requires report_id")
        for f in ("summary", "next_action", "confidence"):
            if f not in payload:
                raise BridgeRefusal(where + f"{kind} payload requires {f!r}")
        conf = payload["confidence"]
        if isinstance(conf, bool) or not isinstance(conf, (int, float)):
            raise BridgeRefusal(where + "payload.confidence must be a number")
        if not (0.0 <= float(conf) <= 1.0):
            raise BridgeRefusal(where + f"payload.confidence out of range: {conf}")
    if kind == "review_and_ticket":
        for f in ("ticket_title", "ticket_markdown", "target_lane"):
            if not payload.get(f):
                raise BridgeRefusal(where + f"review_and_ticket payload requires {f!r}")
        if payload["target_lane"] not in ml.AGENT_LANES:
            raise BridgeRefusal(
                where + f"bad target_lane: {payload['target_lane']!r}")
    if kind == "telephone_start":
        mc = req.get("max_cycles")
        if not isinstance(mc, int) or isinstance(mc, bool) or mc < 1:
            raise BridgeRefusal(where + f"telephone_start needs max_cycles >= 1, got {mc!r}")
        if not req.get("report_id"):
            raise BridgeRefusal(where + "telephone_start requires report_id")
    if kind == "telephone_stop" and not req.get("run_id"):
        raise BridgeRefusal(where + "telephone_stop requires run_id")
    if kind == "criterion_judgement":
        if not req.get("run_id"):
            raise BridgeRefusal(where + "criterion_judgement requires run_id")
        if req.get("criterion_status") is None:
            raise BridgeRefusal(where + "criterion_judgement requires criterion_status")
    if kind == "resolve_escalation":
        if not req.get("escalation_id"):
            raise BridgeRefusal(where + "resolve_escalation requires escalation_id")
        for f in ("authorized_action", "scope"):
            if not req.get(f):
                raise BridgeRefusal(where + f"resolve_escalation requires {f!r}")


def check_request_safety(raw_text: str, req: dict, private_patterns=()) -> None:
    findings = ml.scan_secrets(raw_text, private_patterns)
    if findings:
        raise BridgeRefusal(
            f"request failed the secret scan ({findings[0]}); refused before "
            f"anything was published")
    tp.check_criterion_public_safe(req.get("criterion"), private_patterns)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def commit_author_of(repo: ml.Repo, rel: str) -> tuple[str, str]:
    """(name, email) of the commit that ADDED this file. '' if unknown."""
    out = repo.git("log", "--diff-filter=A", "--format=%an%x1f%ae", "--", rel,
                   check=False).strip()
    if not out:
        return ("", "")
    first = out.split("\n")[-1]
    parts = first.split("\x1f")
    return (parts[0], parts[1] if len(parts) > 1 else "")


def check_identity(req: dict, repo: ml.Repo, rel: str, allowed: list[str]) -> str:
    """Both the declared identity and the actual committer must be allowlisted.

    The declared field alone is self-asserted and worth little. The commit
    author is bound by whoever authenticated the push to GitHub, so both are
    checked and both must pass.
    """
    if not allowed:
        raise BridgeRefusal(
            "no [browser].allowed_identities configured; refusing every browser "
            "request (fail closed)")
    norm = {a.strip().lower() for a in allowed if a.strip()}
    declared = str(req.get("submitted_by") or "").strip().lower()
    if declared not in norm:
        raise BridgeRefusal(
            f"declared identity {req.get('submitted_by')!r} is not allowlisted")

    name, email = commit_author_of(repo, rel)
    actual = {name.strip().lower(), email.strip().lower(),
              email.split("@")[0].strip().lower() if email else ""}
    if not (actual & norm):
        raise BridgeRefusal(
            f"commit author {name or '(unknown)'} <{email or '(unknown)'}> is not "
            f"allowlisted; the declared identity {req.get('submitted_by')!r} does "
            f"not match who actually committed the request")
    return declared


# --------------------------------------------------------------------------
# The bridge
# --------------------------------------------------------------------------


class BrowserBridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.repo = ml.Repo(cfg["repo"]["path"])
        self.allowed = list(cfg.get("browser", {}).get("allowed_identities", []) or [])
        self.private = list(cfg.get("safety", {}).get("private_patterns", []) or [])

    # --- discovery ----------------------------------------------------

    def request_paths(self) -> list[str]:
        base = self.repo.path / ml.DIR_BROWSER_REQUESTS
        if not base.exists():
            return []
        return sorted(
            str(p.relative_to(self.repo.path)).replace(os.sep, "/")
            for p in base.rglob("*.json"))

    def answered(self, msgs: dict) -> dict[str, ml.Message]:
        out = {}
        for m in msgs.values():
            if m.kind == "receipt" and m.get("receipt_type") == "browser_result":
                if m.get("request_id"):
                    out[str(m.get("request_id"))] = m
        return out

    def idempotency_keys(self, msgs: dict) -> set[str]:
        return {str(m.get("idempotency_key")) for m in msgs.values()
                if m.get("idempotency_key")}

    # --- one pass -----------------------------------------------------

    def run_once(self) -> dict:
        stats = {"seen": 0, "accepted": 0, "refused": 0, "errors": []}
        with ml.repo_lock(self.repo.path):
            try:
                self.repo.pull_ff_only()
            except MessageError as exc:
                stats["errors"].append(f"pull: {exc}")

            msgs = ml.load_messages(self.repo.path)
            answered = self.answered(msgs)

            for rel in self.request_paths():
                rid = Path(rel).stem
                if rid in answered:
                    continue
                stats["seen"] += 1
                try:
                    self.process(rel, msgs)
                    stats["accepted"] += 1
                except BridgeRefusal as exc:
                    self.refuse(rel, str(exc), msgs)
                    stats["refused"] += 1
                except MessageError as exc:
                    self.refuse(rel, f"publication failed: {exc}", msgs)
                    stats["refused"] += 1
                msgs = ml.load_messages(self.repo.path)

            try:
                self.write_status(msgs)
            except MessageError as exc:
                stats["errors"].append(f"status: {exc}")
        return stats

    # --- refusal ------------------------------------------------------

    def refuse(self, rel: str, reason: str, msgs: dict) -> None:
        reason = scrub(reason)
        rid = Path(rel).stem
        LOG.warning("refusing %s: %s", rid, reason)
        lane, unit, submitted = "control", None, None
        try:
            req = json.loads((self.repo.path / rel).read_text(encoding="utf-8"))
            lane = req.get("lane") if req.get("lane") in ml.AGENT_LANES else "control"
            unit, submitted = req.get("unit"), req.get("submitted_by")
        except Exception:
            pass
        fm = ml.base_frontmatter(
            "receipt", sender="bridge", to="joe", lane=str(lane), unit=unit,
            status="blocked", requires_owner=False)
        fm.update({"receipt_type": "browser_result", "agent": "bridge",
                   "request_id": rid if REQUEST_ID_RE.match(rid) else None,
                   "submitted_by": str(submitted)[:80] if submitted else None,
                   "reason": reason[:200]})
        body = (f"# Browser request REFUSED\n\n- request: `{rid}`\n"
                f"- file: `{rel}`\n- reason: {reason}\n\n"
                "No canonical message was published for this request.\n")
        ml.publish(self.repo, {f"{ml.DIR_RECEIPTS}/{fm['id']}.md":
                               ml.render_message(fm, body)},
                   f"browser(refused): {rid} [{fm['id']}]", cfg=self.cfg)

    def accept(self, rid: str, req: dict, extra_files: dict, summary: str,
               produced: list[str]) -> None:
        fm = ml.base_frontmatter(
            "receipt", sender="bridge", to=str(req.get("lane") or "control"),
            lane=str(req.get("lane") or "control"), unit=req.get("unit"),
            status="completed", requires_owner=False)
        fm.update({"receipt_type": "browser_result", "agent": "bridge",
                   "request_id": rid,
                   "submitted_by": str(req.get("submitted_by"))[:80],
                   "idempotency_key": str(req.get("idempotency_key"))[:120],
                   "reviewer_mode": "browser"})
        body = (f"# Browser request accepted\n\n- request: `{rid}`\n"
                f"- submitted by: `{req.get('submitted_by')}`\n"
                f"- kind: `{req.get('kind')}`\n"
                f"- produced: {', '.join(f'`{p}`' for p in produced) or '(nothing)'}\n\n"
                f"{summary}\n\n"
                "> The bridge validated and published this request. It authored "
                "no content: every field originates from the submitted request.\n")
        files = dict(extra_files)
        files[f"{ml.DIR_RECEIPTS}/{fm['id']}.md"] = ml.render_message(fm, body)
        ml.publish(self.repo, files,
                   f"browser({req.get('kind')}): {rid} -> "
                   f"{', '.join(produced) or 'ack'} [{fm['id']}]", cfg=self.cfg)

    # --- processing ---------------------------------------------------

    def process(self, rel: str, msgs: dict) -> None:
        rid = Path(rel).stem
        path = self.repo.path / rel
        raw = path.read_text(encoding="utf-8")
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BridgeRefusal(f"not valid JSON: {exc}") from exc

        if str(req.get("request_id")) != rid:
            raise BridgeRefusal(
                f"filename {rid} does not match request_id {req.get('request_id')!r}")

        validate_request(req, rel=rel)
        check_request_safety(raw, req, self.private)
        check_identity(req, self.repo, rel, self.allowed)

        if str(req["idempotency_key"]) in self.idempotency_keys(msgs):
            raise BridgeRefusal(
                f"idempotency_key {req['idempotency_key']!r} was already used; "
                f"refusing to act twice on the same intent")

        handler = {
            "status_request": self._status_request,
            "telephone_start": self._telephone_start,
            "telephone_stop": self._telephone_stop,
            "review_and_ticket": self._review,
            "review_only": self._review,
            "criterion_judgement": self._criterion_judgement,
            "resolve_escalation": self._resolve_escalation,
        }[req["kind"]]
        handler(rid, req, msgs)

    # --- handlers -----------------------------------------------------

    def _status_request(self, rid: str, req: dict, msgs: dict) -> None:
        self.accept(rid, req, {}, "Status cache refreshed; see "
                    f"`{BROWSER_STATUS_PATH}`.", [])

    def _telephone_start(self, rid: str, req: dict, msgs: dict) -> None:
        lane = str(req["lane"])
        tp.assert_can_start(msgs, lane, str(req["report_id"]), self.private,
                            req.get("criterion"))
        report = msgs[str(req["report_id"])]
        fm = ml.base_frontmatter(
            "telephone_run", sender=str(req["submitted_by"]), to=lane, lane=lane,
            unit=req.get("unit") or report.get("unit"), status="open",
            requires_owner=False, in_reply_to=str(req["report_id"]),
            source_commit=ml.brittle_commit(self.cfg["repo"]["brittle_path"]))
        fm.update({
            "title": f"Browser Telephone run on {lane} (max {req['max_cycles']})",
            "max_cycles": int(req["max_cycles"]),
            "criterion": req.get("criterion"),
            "report_id": str(req["report_id"]),
            "reviewer_mode": "browser",
            "request_id": rid,
            "submitted_by": str(req["submitted_by"])[:80],
        })
        body = (
            f"# Telephone run (browser mode)\n\n"
            f"- lane: `{lane}`\n- unit: `{fm['unit'] or '-'}`\n"
            f"- start report: `{req['report_id']}`\n"
            f"- maximum cycles: **{req['max_cycles']}** (enforced outside every model)\n"
            f"- criterion: {req.get('criterion') or '(none -- count-bounded)'}\n"
            f"- reviewer mode: **browser** -- the API reviewer daemon ignores this run\n"
            f"- opened by browser request `{rid}`\n\n"
            f"**Rationale (submitted).** {req['rationale']}\n")
        rel = f"{ml.DIR_TELEPHONE}/{fm['id']}.md"
        self.accept(rid, req, {rel: ml.render_message(fm, body)},
                    f"Started browser-mode run `{fm['id']}`.", [fm["id"]])

    def _telephone_stop(self, rid: str, req: dict, msgs: dict) -> None:
        run = msgs.get(str(req["run_id"]))
        if run is None or run.kind != "telephone_run":
            raise BridgeRefusal(f"{req['run_id']} is not a known Telephone run")
        st = tp.run_state(run, msgs)
        if st["status"] != "active":
            raise BridgeRefusal(f"run is already {st['status']} ({st['stop_reason']})")
        fm = ml.base_frontmatter(
            "receipt", sender=str(req["submitted_by"]), to=str(run.get("lane")),
            lane=str(run.get("lane")), unit=run.get("unit"), status="blocked",
            requires_owner=False, in_reply_to=run.id)
        fm.update({"receipt_type": "telephone_stop", "agent": "browser",
                   "run_id": run.id, "stop_reason": tp.STOP_MANUAL,
                   "cycles_completed": st["cycles_completed"],
                   "reason": str(req.get("reason") or req["rationale"])[:200],
                   "request_id": rid, "api_calls": 0, "spend_usd": 0.0})
        body = (f"# Telephone run stopped (browser request)\n\n"
                f"- run: `{run.id}`\n"
                f"- cycles completed: {st['cycles_completed']}/{st['max_cycles']}\n"
                f"- request: `{rid}`\n\n"
                "No further successor ticket will be issued. Work already claimed\n"
                "may still be completed or explicitly blocked.\n")
        rel = f"{ml.DIR_RECEIPTS}/{fm['id']}.md"
        self.accept(rid, req, {rel: ml.render_message(fm, body)},
                    f"Stopped run `{run.id}`.", [fm["id"]])

    def _review(self, rid: str, req: dict, msgs: dict) -> None:
        """Materialize the browser GPT's review, and its ticket, verbatim."""
        report = msgs.get(str(req["report_id"]))
        if report is None or report.kind != "report":
            raise BridgeRefusal(f"{req['report_id']} is not a known report")
        if ml.reviewer_acked(report.id, msgs):
            raise BridgeRefusal(f"{report.id} already has a reviewer acknowledgement")

        payload = req["payload"]
        lane = str(req["lane"])
        run = tp.run_for_report(report, msgs, lane)
        if run is not None and str(run.get("reviewer_mode") or "api") != "browser":
            raise BridgeRefusal(
                f"run {run.id} is in API reviewer mode; a browser review would "
                f"double-review this report")

        wants_ticket = req["kind"] == "review_and_ticket"
        tel = tel_state = None
        cycle_closed = False
        if run is not None:
            tel_state = tp.run_state(run, msgs)
            cycle_closed = tp.closes_cycle(report, msgs, run)
            threshold = float(self.cfg.get("reviewer", {}).get("minimum_confidence")
                              or ml.DEFAULT_MIN_CONFIDENCE)
            tel = tp.evaluate(
                tel_state,
                verdict_mode="ticket" if wants_ticket else "review_only",
                criterion_status=req.get("criterion_status"),
                criterion_confidence=req.get("criterion_confidence"),
                threshold=threshold, cycle_closed=cycle_closed)
            if wants_ticket and not tel["issue_ticket"]:
                raise BridgeRefusal(
                    f"run {run.id} may not issue another ticket: "
                    f"{tp.describe_stop(tel['stop_reason'])}. Submit a "
                    f"review_only request instead.")

        now = ml.utc_now()
        files: dict[str, str] = {}
        produced: list[str] = []

        rfm = ml.base_frontmatter(
            "review", sender=str(req["submitted_by"]),
            to=str(payload.get("target_lane") or lane), lane="reviewer",
            unit=req.get("unit") or report.get("unit"), status="acknowledged",
            requires_owner=False,
            confidence=round(float(payload["confidence"]), 3),
            in_reply_to=report.id, source_commit=report.get("source_commit"),
            now=now)
        rfm.update({"title": f"Review of {report.get('title') or report.id}",
                    "review_of": report.id, "reviewer_mode": "browser",
                    "request_id": rid,
                    "submitted_by": str(req["submitted_by"])[:80],
                    "next_action": str(payload["next_action"])[:200],
                    "target_lane": payload.get("target_lane")})
        if run is not None:
            rfm["run_id"] = run.id
            rfm["criterion_status"] = req.get("criterion_status")
            cc = req.get("criterion_confidence")
            rfm["criterion_confidence"] = round(float(cc), 3) if cc is not None else None
        rbody = (
            f"# Review of `{report.id}`\n\n"
            f"**Summary.** {payload['summary']}\n\n"
            f"**Next action.** {payload['next_action']}\n\n"
            f"**Rationale.** {req['rationale']}\n\n"
            f"| field | value |\n| --- | --- |\n"
            f"| confidence | {float(payload['confidence']):.2f} |\n"
            f"| reviewer mode | browser |\n"
            f"| authored by | {req['submitted_by']} |\n"
            f"| browser request | `{rid}` |\n"
            + (f"| criterion | {req.get('criterion_status')} |\n"
               if req.get("criterion_status") else "")
            + (f"\n**Criterion evidence.** {req['criterion_evidence']}\n"
               if req.get("criterion_evidence") else ""))
        files[f"{ml.DIR_REVIEWS}/{rfm['id']}.md"] = ml.render_message(rfm, rbody)
        produced.append(rfm["id"])

        ticket_id = None
        if wants_ticket:
            tfm = ml.base_frontmatter(
                "ticket", sender=str(req["submitted_by"]),
                to=str(payload["target_lane"]), lane=str(payload["target_lane"]),
                unit=req.get("unit") or report.get("unit"), status="open",
                requires_owner=False,
                confidence=round(float(payload["confidence"]), 3),
                in_reply_to=report.id, source_commit=report.get("source_commit"),
                now=now)
            tfm.update({"title": str(payload["ticket_title"])[:120],
                        "reviewer_mode": "browser", "request_id": rid,
                        "submitted_by": str(req["submitted_by"])[:80],
                        "next_action": str(payload["next_action"])[:200]})
            if run is not None:
                tfm["run_id"] = run.id
                tfm["cycle_index"] = tel["cycles_after"] + 1
            # VERBATIM. The bridge does not touch submitted ticket prose.
            tbody = str(payload["ticket_markdown"])
            files[f"{ml.DIR_TICKETS}/{tfm['lane']}/{tfm['id']}.md"] = \
                ml.render_message(tfm, tbody)
            ticket_id = tfm["id"]
            produced.append(ticket_id)

        afm = ml.base_frontmatter(
            "receipt", sender=str(req["submitted_by"]), to=str(report.get("lane")),
            lane="reviewer", unit=report.get("unit"), status="acknowledged",
            requires_owner=False, in_reply_to=report.id,
            source_commit=report.get("source_commit"), now=now)
        afm.update({"receipt_type": "reviewer_ack", "agent": "browser",
                    "report_id": report.id, "review_of": report.id,
                    "reviewer_mode": "browser", "request_id": rid})
        if ticket_id:
            afm["ticket_id"] = ticket_id
        files[f"{ml.DIR_RECEIPTS}/{afm['id']}.md"] = ml.render_message(
            afm, f"# Reviewer acknowledgement (browser)\n\n"
                 f"- report: `{report.id}`\n- review: `{rfm['id']}`\n"
                 + (f"- ticket: `{ticket_id}`\n" if ticket_id else "")
                 + f"- browser request: `{rid}`\n")
        produced.append(afm["id"])

        if run is not None:
            files.update(self._telephone_receipts(run, tel, tel_state, report,
                                                  rfm, req, cycle_closed, now,
                                                  produced))
        self.accept(rid, req, files,
                    f"Published the submitted review"
                    + (f" and ticket `{ticket_id}`" if ticket_id else "")
                    + ".", produced)

    def _telephone_receipts(self, run, tel, tel_state, report, rfm, req,
                            cycle_closed, now, produced) -> dict:
        files = {}
        if cycle_closed:
            cfm = ml.base_frontmatter(
                "receipt", sender="bridge", to=str(run.get("lane")),
                lane=str(run.get("lane")), unit=run.get("unit"),
                status="completed", in_reply_to=run.id, now=now)
            cc = req.get("criterion_confidence")
            cfm.update({"receipt_type": "telephone_cycle", "agent": "browser",
                        "run_id": run.id, "cycle_index": tel["cycles_after"],
                        "report_id": report.id, "review_of": report.id,
                        "criterion_status": req.get("criterion_status"),
                        "criterion_confidence": round(float(cc), 3) if cc is not None else None,
                        "reviewer_mode": "browser",
                        "api_calls": 0, "spend_usd": 0.0})
            files[f"{ml.DIR_RECEIPTS}/{cfm['id']}.md"] = ml.render_message(
                cfm, f"# Telephone cycle {tel['cycles_after']}/{tel_state['max_cycles']}"
                     f" (browser)\n\n- run: `{run.id}`\n"
                     f"- completion report: `{report.id}`\n"
                     f"- review: `{rfm['id']}`\n"
                     f"- criterion: {req.get('criterion_status') or '-'}\n")
            produced.append(cfm["id"])

        if tel["stop"] and tel_state["status"] == "active":
            cc = req.get("criterion_confidence")
            sfm = ml.base_frontmatter(
                "receipt", sender="bridge", to=str(run.get("lane")),
                lane=str(run.get("lane")), unit=run.get("unit"),
                status=("completed" if tel["stop_reason"] in tp.SUCCESS_REASONS
                        else "blocked"),
                requires_owner=bool(tel["escalate"]), in_reply_to=run.id, now=now)
            sfm.update({"receipt_type": "telephone_stop", "agent": "browser",
                        "run_id": run.id, "stop_reason": tel["stop_reason"],
                        "cycles_completed": tel["cycles_after"],
                        "criterion_status": req.get("criterion_status"),
                        "criterion_confidence": round(float(cc), 3) if cc is not None else None,
                        "reviewer_mode": "browser", "api_calls": 0, "spend_usd": 0.0})
            files[f"{ml.DIR_RECEIPTS}/{sfm['id']}.md"] = ml.render_message(
                sfm, f"# Telephone run stopped (browser)\n\n- run: `{run.id}`\n"
                     f"- reason: **{tel['stop_reason']}** -- "
                     f"{tp.describe_stop(tel['stop_reason'])}\n"
                     f"- cycles: {tel['cycles_after']}/{tel_state['max_cycles']}\n"
                     + ("\n> Reaching the cycle limit is exhaustion, not success.\n"
                        if tel["stop_reason"] in tp.EXHAUSTION_REASONS else ""))
            produced.append(sfm["id"])
        return files

    def _criterion_judgement(self, rid: str, req: dict, msgs: dict) -> None:
        run = msgs.get(str(req["run_id"]))
        if run is None or run.kind != "telephone_run":
            raise BridgeRefusal(f"{req['run_id']} is not a known Telephone run")
        st = tp.run_state(run, msgs)
        if st["status"] != "active":
            raise BridgeRefusal(f"run is already {st['status']}")
        threshold = float(self.cfg.get("reviewer", {}).get("minimum_confidence")
                          or ml.DEFAULT_MIN_CONFIDENCE)
        tel = tp.evaluate(st, verdict_mode="review_only",
                          criterion_status=req.get("criterion_status"),
                          criterion_confidence=req.get("criterion_confidence"),
                          threshold=threshold, cycle_closed=False)
        cc = req.get("criterion_confidence")
        fm = ml.base_frontmatter(
            "receipt", sender=str(req["submitted_by"]), to=str(run.get("lane")),
            lane=str(run.get("lane")), unit=run.get("unit"),
            status=("completed" if tel["stop_reason"] in tp.SUCCESS_REASONS
                    else "blocked"),
            requires_owner=bool(tel["escalate"]), in_reply_to=run.id)
        fm.update({"receipt_type": "telephone_stop", "agent": "browser",
                   "run_id": run.id, "stop_reason": tel["stop_reason"],
                   "cycles_completed": st["cycles_completed"],
                   "criterion_status": req.get("criterion_status"),
                   "criterion_confidence": round(float(cc), 3) if cc is not None else None,
                   "reviewer_mode": "browser", "request_id": rid,
                   "api_calls": 0, "spend_usd": 0.0})
        body = (f"# Criterion judgement (browser)\n\n- run: `{run.id}`\n"
                f"- criterion: {run.get('criterion')}\n"
                f"- judgement: **{req.get('criterion_status')}**"
                f" (confidence {cc if cc is not None else '-'})\n"
                f"- outcome: {tp.describe_stop(tel['stop_reason'])}\n"
                + (f"\n**Evidence.** {req['criterion_evidence']}\n"
                   if req.get("criterion_evidence") else ""))
        self.accept(rid, req, {f"{ml.DIR_RECEIPTS}/{fm['id']}.md":
                               ml.render_message(fm, body)},
                    f"Recorded criterion judgement for `{run.id}`.", [fm["id"]])

    def _resolve_escalation(self, rid: str, req: dict, msgs: dict) -> None:
        raise BridgeRefusal(
            "browser access grants no owner authority: an owner decision must "
            "be recorded by the owner on the workstation via "
            "`messagesctl resolve-escalation`")

    # --- status cache -------------------------------------------------

    def _heartbeat_stale(self) -> bool:
        interval = float(self.cfg.get("browser", {})
                         .get("heartbeat_seconds") or 900)
        out = self.repo.git("log", "-1", "--format=%ct", "--",
                            BROWSER_STATUS_PATH, check=False).strip()
        if not out:
            return True
        try:
            import time as _t
            return (_t.time() - float(out)) > interval
        except ValueError:
            return True

    def write_status(self, msgs: dict) -> None:
        payload = build_browser_status(self.cfg, msgs, self.repo, self)
        path = self.repo.path / BROWSER_STATUS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, indent=2, sort_keys=False) + "\n"

        # Heartbeat fields change every pass. Committing on every poll would
        # bury the bus in noise commits, so the file is only rewritten when the
        # SUBSTANTIVE content changed, or when the heartbeat has gone stale
        # enough to be worth refreshing.
        # repo_head is self-referential: committing the status file changes it,
        # which would look like a content change and commit again, forever.
        volatile = ("generated_at", "gateway_heartbeat", "last_successful_sync",
                    "repo_head")
        if path.exists():
            try:
                prev = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                prev = None
            if prev is not None:
                same = ({k: v for k, v in prev.items() if k not in volatile}
                        == {k: v for k, v in payload.items() if k not in volatile})
                if same and not self._heartbeat_stale():
                    return
        findings = ml.scan_secrets(text, self.private)
        if findings:
            raise MessageError(f"browser status failed the secret scan: {findings[0]}")
        path.write_text(text, encoding="utf-8")
        self.repo.git("add", "--", BROWSER_STATUS_PATH)
        if self.repo.git("diff", "--cached", "--name-only").strip():
            self.repo.git("commit", "-m", "chore(state): refresh browser status",
                          identity=True)
            if self.repo.has_remote():
                self.repo.git_ok("push", "origin", f"HEAD:{self.repo.branch()}")


# --------------------------------------------------------------------------
# Browser-readable status cache
# --------------------------------------------------------------------------


def _service_state(unit: str) -> dict:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "-p", "ActiveState", "-p", "ExecMainPID", "-p", "NRestarts"],
            capture_output=True, text=True, timeout=10).stdout
        d = dict(line.split("=", 1) for line in out.strip().split("\n") if "=" in line)
        return {"active": d.get("ActiveState") == "active",
                "pid": int(d.get("ExecMainPID") or 0),
                "restarts": int(d.get("NRestarts") or 0)}
    except Exception:
        return {"active": None, "pid": 0, "restarts": 0}


def status_age_seconds(repo: ml.Repo) -> float | None:
    """How long since the status cache was last committed."""
    out = repo.git("log", "-1", "--format=%ct", "--", BROWSER_STATUS_PATH,
                   check=False).strip()
    if not out:
        return None
    try:
        import time as _t
        return max(0.0, _t.time() - float(out))
    except ValueError:
        return None


def build_browser_status(cfg: dict, msgs: dict, repo: ml.Repo,
                         bridge: "BrowserBridge | None" = None) -> dict:
    """Sanitized, rebuildable view for an agent with only GitHub access.

    Contains no credential, no prompt, no private report text and no
    proprietary evidence -- only run bookkeeping and service liveness.
    """
    now = ml.utc_now()
    idx = ml.build_index(msgs, now=now)

    runs = []
    for run in tp.runs(msgs):
        st = tp.run_state(run, msgs)
        ticket = claim = blocker = None
        for m in sorted(msgs.values(), key=ml.Message.sort_key):
            if m.kind == "ticket" and m.get("run_id") == run.id:
                ts = ml.ticket_state(m.id, msgs)
                ticket = {"id": m.id, "status": ts["status"],
                          "title": str(m.get("title") or "")[:120]}
                claim = ts["claim"].id if ts["claim"] else None
                blocker = (ts["block"].get("reason") if ts["status"] == "blocked"
                           and ts["block"] else None)
        runs.append({
            "run_id": st["run_id"], "lane": st["lane"], "unit": st["unit"],
            "reviewer_mode": str(run.get("reviewer_mode") or "api"),
            "state": st["status"],
            "cycles_completed": st["cycles_completed"],
            "max_cycles": st["max_cycles"],
            "criterion": st["criterion"],
            "criterion_status": st["criterion_status"],
            "criterion_confidence": st["criterion_confidence"],
            "stop_reason": st["stop_reason"],
            "stop_reason_detail": tp.describe_stop(st["stop_reason"]),
            "current_ticket": ticket, "current_claim": claim, "blocker": blocker,
            "start_report": st["start_report"],
            "api_calls": st["api_calls"], "spend_usd": st["spend_usd"],
        })

    awaiting = [m.id for m in sorted(msgs.values(), key=ml.Message.sort_key)
                if m.kind == "report" and not ml.reviewer_acked(m.id, msgs)]

    answered = bridge.answered(msgs) if bridge else {}
    pending, refused = [], []
    if bridge:
        for rel in bridge.request_paths():
            rid = Path(rel).stem
            r = answered.get(rid)
            if r is None:
                pending.append(rid)
            elif r.get("status") == "blocked":
                refused.append({"request_id": rid,
                                "reason": str(r.get("reason") or "")[:200]})

    api_enabled = _service_state("brittle-message-reviewer.service")
    spend = None
    try:
        from spend_guard import SpendGuard
        s = SpendGuard(cfg).status()
        spend = {k: s[k] for k in ("day_committed_usd", "day_cap_usd",
                                   "day_remaining_usd", "month_committed_usd",
                                   "month_cap_usd", "month_remaining_usd",
                                   "calls_today", "max_calls_per_day", "blocked")}
    except Exception:
        spend = None

    readiness = compute_readiness(cfg, msgs, repo, status_age_seconds(repo))

    return {
        "schema": 1,
        "generated_at": ml.iso(now),
        # HEAD as of generation -- necessarily one commit behind the commit
        # that publishes this very file. Do not use it to correlate.
        "repo_head_at_generation": repo.head()[:12],
        **readiness,
        "telephone_runs": runs,
        "open_tickets_by_lane": idx["open_ticket_by_lane"],
        "active_claims": idx["active_claims"],
        "open_escalations": idx["open_escalations"],
        "reports_awaiting_review": awaiting,
        "message_counts": idx["counts"],
        "api_reviewer_mode_enabled": bool(api_enabled["active"]),
        "services": {
            "bridge": _service_state("brittle-browser-bridge.service"),
            "api_reviewer": api_enabled,
            "lane_locomotion": _service_state("brittle-messages-locomotion.service"),
            "lane_control": _service_state("brittle-messages-control.service"),
        },
        "gateway_heartbeat": ml.iso(now),
        "last_successful_sync": ml.iso(now),
        "spending_guard": spend,
        "browser_requests": {"pending": pending, "refused": refused},
        "notes": (
            "Sanitized, rebuildable cache. Canonical truth is the append-only "
            "message history under projects/brittle/. Contains no credentials, "
            "prompts, private report text or proprietary evidence."),
    }


# --------------------------------------------------------------------------
# Local-action catalog + readiness
# --------------------------------------------------------------------------

MESSAGES_DIR = "/home/robojoe/code/messages"
BRIDGE_UNIT = "brittle-browser-bridge.service"

# A CLOSED catalog. Status generation may only ever publish an entry from this
# table -- it can never assemble a command from observed state. That is the
# whole point: a browser agent relays these to Joe verbatim, so an attacker who
# could influence repository content must not be able to influence what Joe is
# told to run.
LOCAL_ACTION_CATALOG: dict[str, dict] = {
    "install_units": {
        "reason": "Browser bridge unit is not installed on the workstation",
        "command": f"sh {MESSAGES_DIR}/scripts/install_services.sh",
        "verify_command": f"systemctl --user status {BRIDGE_UNIT} --no-pager",
        "expected_result": "Loaded: loaded",
    },
    "daemon_reload": {
        "reason": "systemd has not reloaded since the units changed",
        "command": "systemctl --user daemon-reload",
        "verify_command": f"systemctl --user status {BRIDGE_UNIT} --no-pager",
        "expected_result": "Loaded: loaded",
    },
    "enable_bridge": {
        "reason": "Browser bridge is installed but inactive",
        "command": f"systemctl --user enable --now {BRIDGE_UNIT}",
        "verify_command": f"systemctl --user status {BRIDGE_UNIT} --no-pager",
        "expected_result": "Active: active (running)",
    },
    "verify_bridge": {
        "reason": "Browser bridge heartbeat is stale; confirm the service is healthy",
        "command": f"systemctl --user status {BRIDGE_UNIT} --no-pager",
        "verify_command": f"systemctl --user status {BRIDGE_UNIT} --no-pager",
        "expected_result": "Active: active (running)",
    },
    "restart_bridge": {
        "reason": "Browser bridge is running but its heartbeat has not advanced",
        "command": f"systemctl --user restart {BRIDGE_UNIT}",
        "verify_command": f"systemctl --user status {BRIDGE_UNIT} --no-pager",
        "expected_result": "Active: active (running)",
    },
    "sync_repo": {
        "reason": "Repository sync is unhealthy; a push is still deferred",
        "command": f"git -C {MESSAGES_DIR} pull --ff-only",
        "verify_command": f"python3 {MESSAGES_DIR}/scripts/messagesctl.py status",
        "expected_result": "deferred push   : False",
    },
    "enable_lane_control": {
        "reason": "Control lane watcher is not running",
        "command": "systemctl --user enable --now brittle-messages-control.service",
        "verify_command": "systemctl --user status brittle-messages-control.service --no-pager",
        "expected_result": "Active: active (running)",
    },
    "enable_lane_locomotion": {
        "reason": "Locomotion lane watcher is not running",
        "command": "systemctl --user enable --now brittle-messages-locomotion.service",
        "verify_command": "systemctl --user status brittle-messages-locomotion.service --no-pager",
        "expected_result": "Active: active (running)",
    },
    "restart_lane_control": {
        "reason": "Control lane watcher is running but wedged (its snapshot has stopped advancing)",
        "command": "systemctl --user restart brittle-messages-control.service",
        "verify_command": "systemctl --user status brittle-messages-control.service --no-pager",
        "expected_result": "Active: active (running)",
    },
    "restart_lane_locomotion": {
        "reason": "Locomotion lane watcher is running but wedged (its snapshot has stopped advancing)",
        "command": "systemctl --user restart brittle-messages-locomotion.service",
        "verify_command": "systemctl --user status brittle-messages-locomotion.service --no-pager",
        "expected_result": "Active: active (running)",
    },
    "configure_allowlist": {
        "reason": "No browser identity allowlist is configured, so every browser request is refused",
        "command": ("edit ~/.config/brittle-messages/config.toml and set "
                    "[browser] allowed_identities"),
        "verify_command": f"python3 {MESSAGES_DIR}/scripts/messagesctl.py browser-status",
        "expected_result": "browser status rebuilt",
    },
    "resume_autonomy": {
        "reason": "Autonomy is paused, so no new Telephone ticket will be issued",
        "command": f"python3 {MESSAGES_DIR}/scripts/messagesctl.py resume",
        "verify_command": f"python3 {MESSAGES_DIR}/scripts/messagesctl.py status",
        "expected_result": "autonomy        : ACTIVE",
    },
}

# Only these may begin a published command. Anything else fails closed.
_ALLOWED_COMMAND_PREFIXES = (
    "systemctl --user ",
    f"git -C {MESSAGES_DIR} ",
    f"sh {MESSAGES_DIR}/scripts/",
    f"python3 {MESSAGES_DIR}/scripts/",
    "edit ~/.config/brittle-messages/config.toml",
)

# Shell metacharacters that would turn a relayed command into something else.
_UNSAFE_SHELL = re.compile(r"[;&|`$><\
]|\$\(|&&|\|\|")

_ACTION_FIELDS = ("reason", "command", "verify_command", "expected_result")


def assert_action_safe(action: dict, *, key: str = "<inline>") -> None:
    """Fail closed on anything that is not a known, safe, published action."""
    missing = [f for f in _ACTION_FIELDS if f not in action]
    if missing:
        raise MessageError(f"local action {key!r} missing field(s): {missing}")
    extra = [f for f in action if f not in _ACTION_FIELDS]
    if extra:
        raise MessageError(f"local action {key!r} has unknown field(s): {sorted(extra)}")
    for field in _ACTION_FIELDS:
        val = action[field]
        if not isinstance(val, str) or not val.strip():
            raise MessageError(f"local action {key!r}: {field} must be a non-empty string")
    for field in ("command", "verify_command"):
        cmd = action[field]
        if _UNSAFE_SHELL.search(cmd):
            raise MessageError(
                f"local action {key!r}: {field} contains shell metacharacters; "
                f"refusing to publish a command Joe might paste: {cmd!r}")
        if not cmd.startswith(_ALLOWED_COMMAND_PREFIXES):
            raise MessageError(
                f"local action {key!r}: {field} is not an allowed command form: {cmd!r}")
    if ml.scan_secrets(json.dumps(action)):
        raise MessageError(f"local action {key!r} appears to contain a secret")


def local_action(key: str) -> dict:
    """Fetch a catalog entry, revalidated on every use."""
    if key not in LOCAL_ACTION_CATALOG:
        raise MessageError(
            f"unknown local action {key!r}; commands may only come from the "
            f"published catalog, never assembled from observed state")
    action = dict(LOCAL_ACTION_CATALOG[key])
    assert_action_safe(action, key=key)
    return action


def _unit_installed(unit: str) -> bool:
    dest = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return (dest / "systemd" / "user" / unit).exists()


def _unit_enabled(unit: str) -> bool:
    try:
        out = subprocess.run(["systemctl", "--user", "is-enabled", unit],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() == "enabled"
    except Exception:
        return False


def _lane_snapshot_age(lane: str) -> float | None:
    """Seconds since the lane watcher last wrote its snapshot, or None."""
    path = ml.state_dir() / f"lane-{lane}.json"
    if not path.exists():
        return None
    try:
        import time as _t
        return max(0.0, _t.time() - path.stat().st_mtime)
    except OSError:
        return None


def compute_readiness(cfg: dict, msgs: dict, repo: ml.Repo,
                      heartbeat_age_s: float | None) -> dict:
    """Mechanically decide whether browser-mode Telephone can operate.

    Every blocker maps to a catalog action. Nothing here builds a command.
    """
    blockers: list[str] = []
    actions: list[dict] = []

    def need(key: str, why: str) -> None:
        blockers.append(why)
        act = local_action(key)
        if act not in actions:
            actions.append(act)

    bridge_installed = _unit_installed(BRIDGE_UNIT)
    bridge = _service_state(BRIDGE_UNIT)

    if not bridge_installed:
        need("install_units", "browser bridge unit is not installed")
        need("daemon_reload", "systemd must reload after installing units")
        need("enable_bridge", "browser bridge must be enabled and started")
    elif not bridge["active"]:
        need("enable_bridge", "browser bridge is installed but not running")
    else:
        stale_after = float(cfg.get("browser", {}).get("heartbeat_seconds") or 900) * 3
        if heartbeat_age_s is not None and heartbeat_age_s > stale_after:
            need("restart_bridge",
                 f"browser bridge heartbeat is stale "
                 f"({int(heartbeat_age_s)}s old, expected under {int(stale_after)}s)")
        elif not _unit_enabled(BRIDGE_UNIT):
            need("enable_bridge",
                 "browser bridge is running but not enabled, so it will not "
                 "survive a reboot")

    for lane, enable_key, restart_key in (
            ("control", "enable_lane_control", "restart_lane_control"),
            ("locomotion", "enable_lane_locomotion", "restart_lane_locomotion")):
        if not _service_state(f"brittle-messages-{lane}.service")["active"]:
            need(enable_key, f"{lane} lane watcher is not running")
        else:
            # "active" only means the process exists. A watcher that is failing
            # every pass -- say because a protocol field was added and it is
            # still running the older code -- looks perfectly healthy to
            # systemd. Its snapshot file is the real liveness signal.
            age = _lane_snapshot_age(lane)
            if age is not None and age > 300:
                need(restart_key,
                     f"{lane} lane watcher is active but its snapshot is "
                     f"{int(age)}s old; it is wedged, not working")

    if (ml.spool_dir() / "pending_push.json").exists():
        need("sync_repo", "a push is deferred; repository sync is unhealthy")

    if not (cfg.get("browser", {}).get("allowed_identities") or []):
        need("configure_allowlist",
             "no browser identity allowlist is configured, so every browser "
             "request would be refused")

    if ml.autonomy_state(msgs)["paused"]:
        need("resume_autonomy", "autonomy is paused")

    for act in actions:
        assert_action_safe(act)

    return {
        "browser_telephone_ready": not blockers,
        "local_action_required": bool(actions),
        "readiness_blockers": blockers,
        "required_local_actions": actions,
    }


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

_STOP = False


def _sig(signum, _frame):
    global _STOP
    _STOP = True
    LOG.info("received signal %s -- stopping", signum)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="browser_bridge", description=__doc__.split("\n")[0])
    p.add_argument("--config")
    p.add_argument("--once", action="store_true")
    p.add_argument("--poll-seconds", type=float)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    cfg = ml.load_config(args.config)
    bridge = BrowserBridge(cfg)
    poll = float(args.poll_seconds or cfg.get("browser", {}).get("poll_seconds") or 20)
    LOG.info("browser bridge online: repo=%s identities=%s poll=%ss",
             bridge.repo.path, bridge.allowed or "(none -- all requests refused)", poll)

    while True:
        try:
            stats = bridge.run_once()
            if stats["seen"] or stats["errors"]:
                LOG.info("pass: %s", json.dumps(stats))
        except Exception as exc:
            LOG.exception("pass failed: %s", exc)
        if args.once or _STOP:
            break
        for _ in range(int(max(1, poll))):
            if _STOP:
                break
            time.sleep(1)
    LOG.info("browser bridge stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
