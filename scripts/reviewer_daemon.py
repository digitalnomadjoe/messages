#!/usr/bin/env python3
"""reviewer_daemon -- autonomous OpenAI reviewer for the BRITTLE message bus.

Watches for reports that carry no reviewer acknowledgement, reviews each one
through the OpenAI API under a tracked prompt, and publishes:

  * exactly one review message,
  * zero or one next ticket (or an escalation when owner input is required),
  * exactly one acknowledgement receipt.

The review, the ticket/escalation and the acknowledgement land in a single
commit, so a crash or restart can never produce a duplicate review or a
duplicate ticket.

Stdlib only.  The OpenAI credential is read from the environment (or a 0600
credential file) and never appears in argv, logs, messages or service files.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import messagelib as ml  # noqa: E402
from messagelib import MessageError  # noqa: E402
from spend_guard import SpendGuard, SpendGuardError, SpendLimitExceeded  # noqa: E402
import telephone as tp  # noqa: E402

LOG = logging.getLogger("brittle-reviewer")

# --------------------------------------------------------------------------
# Structured output contract
# --------------------------------------------------------------------------

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary", "target_lane", "next_action", "ticket_title",
        "ticket_markdown", "requires_owner", "owner_question", "confidence",
        "reasoning_summary", "criterion_status", "criterion_evidence",
        "criterion_confidence",
    ],
    "properties": {
        "summary": {"type": "string"},
        "target_lane": {"type": ["string", "null"], "enum": ["locomotion", "control", None]},
        "next_action": {"type": "string"},
        "ticket_title": {"type": ["string", "null"]},
        "ticket_markdown": {"type": ["string", "null"]},
        "requires_owner": {"type": "boolean"},
        "owner_question": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "reasoning_summary": {"type": "string"},
        "criterion_status": {"type": ["string", "null"],
                             "enum": ["met", "not_met", "unknown", None]},
        "criterion_evidence": {"type": ["string", "null"]},
        "criterion_confidence": {"type": ["number", "null"]},
    },
}

# Anything matching these forces owner escalation regardless of confidence.
# Confidence never overrides a hard gate.
HARD_GATES: tuple[tuple[str, str], ...] = (
    ("promotion", r"(?i)\bpromot(e|ion|ing)\b"),
    ("canonical-mutation", r"(?i)\bcanonical\b|\boverwrite the reference\b|\bnew plant\b|\bplant (change|swap|update)\b"),
    ("latest-pointer", r"(?i)\bupdate (the )?latest\b|\blatest\s+(pointer|symlink|tag)\b|\bmain branch\b"),
    ("policy-card", r"(?i)\bpolicy[ _-]?card\b"),
    ("crown", r"(?i)\bcrown\b"),
    ("guard-mutation", r"(?i)\bguard\b.{0,40}\b(mutat|authoriz|unlock|override|bypass|disable)|\b(mutat|authoriz|unlock|override|bypass|disable)\w*\b.{0,40}\bguard\b"),
    ("interface-change", r"(?i)\b(observation|obs|action)[ _-]?(space|interface|contract)\b.{0,30}\b(change|extend|modify|add)|\b(change|extend|modify)\b.{0,30}\b(observation|action)[ _-]?(space|interface|contract)\b"),
    ("architecture", r"(?i)\b(re)?architect(ure|ing)?\b|\bredesign the\b"),
    ("owner-authorization", r"(?i)\bjoe(?:'s)? (approval|authorization|sign-?off|decision)\b|\bowner (approval|authorization|sign-?off)\b"),
    ("gate-override", r"(?i)\b(override|bypass|skip|disable|relax)\b.{0,30}\bgate\b"),
)

GUARD_MUTATION_RE = HARD_GATES[5][1]


def hard_gate_hits(text: str) -> list[str]:
    import re

    hits = []
    for name, pattern in HARD_GATES:
        if re.search(pattern, text or ""):
            hits.append(name)
    return hits


# --------------------------------------------------------------------------
# OpenAI transport
# --------------------------------------------------------------------------


class ReviewerError(RuntimeError):
    pass


def _scrub(text: str) -> str:
    """Redact credential-shaped strings out of upstream error bodies.

    A provider's own error text can echo the credential back (OpenAI's 401
    quotes the key it rejected). That body reaches the journal, so it is
    scrubbed here rather than trusted to arrive already masked.
    """
    for _name, pattern in ml.SECRET_PATTERNS:
        text = pattern.sub("<redacted>", text)
    return text


def _mock_response() -> str | None:
    path = os.environ.get("BRITTLE_REVIEWER_MOCK")
    if not path:
        return None
    return Path(path).read_text(encoding="utf-8")


def call_model(cfg: dict, system_prompt: str, user_content: str,
               guard: SpendGuard | None = None,
               telemetry: dict | None = None) -> str:
    """Return the raw model content string.  Never logs the credential.

    Every billable request passes the local spending guard first. A mocked
    response short-circuits before any guard interaction, because a call that
    never touches the network cannot cost anything.
    """
    mock = _mock_response()
    if mock is not None:
        LOG.info("using mocked reviewer response (BRITTLE_REVIEWER_MOCK) -- "
                 "no network, no spend")
        return mock

    api_key = ml.resolve_api_key(cfg)  # raises MessageError -> fail closed
    rev = cfg.get("reviewer", {})
    base = str(cfg.get("openai", {}).get("base_url") or "https://api.openai.com/v1")
    model = str(rev.get("model") or "gpt-4o-2024-08-06")
    timeout = float(rev.get("request_timeout_seconds") or 120)

    guard = guard or SpendGuard(cfg)
    max_out = guard.max_completion_tokens

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        str(rev.get("max_tokens_field") or "max_tokens"): max_out,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "brittle_review",
                "strict": True,
                "schema": REVIEW_SCHEMA,
            },
        },
    }
    data = json.dumps(payload).encode("utf-8")

    # Reserve worst-case cost BEFORE the socket is opened. A refusal here
    # raises and no request is ever made.
    reservation = guard.reserve(model, len(data), max_output_tokens=max_out)
    LOG.info("spend guard: reserved $%.6f for one %s call "
             "(<=%d bytes in, <=%d completion tokens)",
             reservation.reserved_micro / 1_000_000, model, len(data), max_out)

    try:
        req = urllib.request.Request(
            f"{base.rstrip('/')}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _scrub(exc.read().decode("utf-8", errors="replace"))[:400]
        if 400 <= exc.code < 500:
            # A 4xx was rejected before inference: definitively not billed.
            guard.release(reservation, f"http {exc.code}")
        else:
            # A 5xx may have been billed. Charge the full reservation.
            guard.finalize_uncertain(reservation, f"http {exc.code}")
        raise ReviewerError(f"OpenAI HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Timed out or connection lost mid-flight: billing outcome unknown.
        guard.finalize_uncertain(reservation, f"transport: {type(exc).__name__}")
        raise ReviewerError(f"OpenAI transport error: {exc}") from exc
    except json.JSONDecodeError as exc:
        guard.finalize_uncertain(reservation, "non-JSON response")
        raise ReviewerError(f"OpenAI returned non-JSON: {exc}") from exc
    except BaseException as exc:
        # Killed, interrupted, anything else: never leave a reservation open
        # on a path we control.
        guard.finalize_uncertain(reservation, f"aborted: {type(exc).__name__}")
        raise

    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict) or "prompt_tokens" not in usage:
        guard.finalize_uncertain(reservation, "response carried no usage block")
    else:
        actual = guard.finalize(reservation,
                                int(usage.get("prompt_tokens", 0)),
                                int(usage.get("completion_tokens", 0)))
        LOG.info("spend guard: finalized $%.6f actual (reserved $%.6f)",
                 actual / 1_000_000, reservation.reserved_micro / 1_000_000)
        if telemetry is not None:
            telemetry["actual_micro"] = actual
            telemetry["api_calls"] = telemetry.get("api_calls", 0) + 1

    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ReviewerError(f"unexpected OpenAI response shape: {exc}") from exc


def validate_review(raw: str) -> dict:
    """Parse + hand-validate the structured response.  Fail closed."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewerError(f"reviewer response is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ReviewerError("reviewer response must be a JSON object")

    missing = [k for k in REVIEW_SCHEMA["required"] if k not in data]
    if missing:
        raise ReviewerError(f"reviewer response missing field(s): {missing}")
    extra = [k for k in data if k not in REVIEW_SCHEMA["properties"]]
    if extra:
        raise ReviewerError(f"reviewer response has unknown field(s): {sorted(extra)}")

    if not isinstance(data["summary"], str) or not data["summary"].strip():
        raise ReviewerError("summary must be a non-empty string")
    if not isinstance(data["next_action"], str) or not data["next_action"].strip():
        raise ReviewerError("next_action must be a non-empty string")
    if not isinstance(data["requires_owner"], bool):
        raise ReviewerError("requires_owner must be a boolean")
    if data["target_lane"] not in (None, "locomotion", "control"):
        raise ReviewerError(f"bad target_lane: {data['target_lane']!r}")
    conf = data["confidence"]
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        raise ReviewerError("confidence must be a number")
    if not (0.0 <= float(conf) <= 1.0):
        raise ReviewerError(f"confidence out of range: {conf}")
    for key in ("ticket_title", "ticket_markdown", "owner_question"):
        if data[key] is not None and not isinstance(data[key], str):
            raise ReviewerError(f"{key} must be a string or null")
    if not isinstance(data["reasoning_summary"], str):
        raise ReviewerError("reasoning_summary must be a string")
    if data.get("criterion_status") not in (None, "met", "not_met", "unknown"):
        raise ReviewerError(f"bad criterion_status: {data.get('criterion_status')!r}")
    cc = data.get("criterion_confidence")
    if cc is not None:
        if isinstance(cc, bool) or not isinstance(cc, (int, float)):
            raise ReviewerError("criterion_confidence must be a number or null")
        if not (0.0 <= float(cc) <= 1.0):
            raise ReviewerError(f"criterion_confidence out of range: {cc}")
    if data.get("criterion_evidence") is not None and \
            not isinstance(data["criterion_evidence"], str):
        raise ReviewerError("criterion_evidence must be a string or null")
    return data


# --------------------------------------------------------------------------
# Decision policy
# --------------------------------------------------------------------------


def decide(review: dict, cfg: dict) -> dict:
    """Return {'mode': 'ticket'|'escalation'|'review_only', 'reasons': [...]}"""
    threshold = float(cfg.get("reviewer", {}).get("minimum_confidence")
                      or ml.DEFAULT_MIN_CONFIDENCE)
    reasons: list[str] = []

    surface = "\n".join(filter(None, [
        review.get("summary"), review.get("next_action"),
        review.get("ticket_title"), review.get("ticket_markdown"),
    ]))
    gates = hard_gate_hits(surface)
    if gates:
        reasons.append("hard gate(s): " + ", ".join(gates))
    if review["requires_owner"]:
        reasons.append("reviewer set requires_owner")
    if float(review["confidence"]) < threshold:
        reasons.append(
            f"confidence {float(review['confidence']):.2f} < threshold {threshold:.2f}"
        )

    # A locomotion ticket that needs Guard mutation must go to control, or escalate.
    import re

    if (review.get("target_lane") == "locomotion"
            and re.search(GUARD_MUTATION_RE, surface)):
        reasons.append("Guard mutation requested on the locomotion lane")

    if reasons:
        return {"mode": "escalation", "reasons": reasons, "threshold": threshold}

    if not review.get("ticket_markdown") or not review.get("target_lane"):
        return {"mode": "review_only", "reasons": ["no executable next ticket proposed"],
                "threshold": threshold}

    return {"mode": "ticket", "reasons": [], "threshold": threshold}


# --------------------------------------------------------------------------
# Daemon
# --------------------------------------------------------------------------


class ReviewerDaemon:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.repo = ml.Repo(cfg["repo"]["path"])
        self.prompt_path = Path(
            cfg["reviewer"].get("prompt_path")
            or (self.repo.path / "prompts" / "brittle-reviewer.md")
        )
        self.skill_path = self.repo.path / "skills" / "brittle-reviewer" / "SKILL.md"

    # --- inputs -----------------------------------------------------------

    def load_prompt(self) -> tuple[str, str]:
        if not self.prompt_path.exists():
            raise MessageError(f"reviewer prompt not found: {self.prompt_path}")
        prompt = self.prompt_path.read_text(encoding="utf-8")
        skill = self.skill_path.read_text(encoding="utf-8") if self.skill_path.exists() else ""
        combined = prompt + ("\n\n---\n\n# Operating skill\n\n" + skill if skill else "")
        return combined, ml.sha256_text(combined)

    def verify_report(self, report: ml.Message) -> list[str]:
        """Schema is already enforced on load; check checksum + source relation."""
        notes: list[str] = []
        local = report.get("local_source_path")
        sha = report.get("local_source_sha256")
        if not local or not sha:
            notes.append("report does not record a local source path/sha256")
            return notes
        p = Path(str(local))
        if not p.exists():
            notes.append(f"local source not present on this host: {p}")
        else:
            actual = ml.sha256_file(p)
            if actual != sha:
                notes.append(
                    f"CHECKSUM MISMATCH: local file now {actual[:12]}..., "
                    f"message recorded {str(sha)[:12]}..."
                )
            else:
                notes.append("local source sha256 verified")
        if not report.get("source_commit"):
            notes.append("report does not record a BRITTLE commit")
        return notes

    # --- one pass ---------------------------------------------------------

    def pending_reports(self, msgs: dict[str, ml.Message]) -> list[ml.Message]:
        """Unreviewed reports this daemon owns.

        Reports governed by a browser-mode Telephone run are excluded outright.
        Browser mode and API mode must never review the same report, and the
        cheapest way to guarantee that is for the API side to refuse to see
        them at all.
        """
        out = []
        for m in sorted(msgs.values(), key=ml.Message.sort_key):
            if m.kind != "report" or ml.reviewer_acked(m.id, msgs):
                continue
            try:
                run = tp.run_for_report(m, msgs, str(m.get("lane")))
            except tp.TelephoneError:
                run = None
            if run is not None and tp.is_browser_run(run):
                LOG.debug("skipping %s: browser-mode run %s", m.id, run.id)
                continue
            out.append(m)
        return out

    def pending_notices(self, msgs: dict[str, ml.Message]) -> list[ml.Message]:
        """Escalations published without a notification receipt (crash recovery)."""
        out = []
        for m in sorted(msgs.values(), key=ml.Message.sort_key):
            if m.kind != "escalation" or m.get("from") != "reviewer":
                continue
            has_notice = any(
                r.kind == "receipt" and r.get("receipt_type") == "escalation_notice"
                and r.get("in_reply_to") == m.id for r in msgs.values())
            if not has_notice:
                out.append(m)
        return out

    def run_once(self) -> dict:
        stats = {"reviewed": 0, "tickets": 0, "escalations": 0, "notices": 0,
                 "skipped": 0, "errors": []}
        with ml.repo_lock(self.repo.path):
            try:
                self.repo.pull_ff_only()
            except MessageError as exc:
                stats["errors"].append(f"pull: {exc}")

            problems = ml.validate_repo(
                self.repo.path,
                private_patterns=self.cfg.get("safety", {}).get("private_patterns", []),
                check_index=False)
            if problems:
                stats["errors"].append(f"repository invalid: {problems[0]}")
                return stats

            msgs = ml.load_messages(self.repo.path)

            # crash recovery: send notifications for escalations that lack one
            for esc in self.pending_notices(msgs):
                try:
                    self._publish_notice(esc)
                    stats["notices"] += 1
                except MessageError as exc:
                    stats["errors"].append(f"notice {esc.id}: {exc}")
                msgs = ml.load_messages(self.repo.path)

            auto = ml.autonomy_state(msgs)
            pending = self.pending_reports(msgs)
            if not pending:
                return stats
            if auto["paused"]:
                LOG.info("autonomy PAUSED -- leaving %d report(s) queued, unacknowledged",
                         len(pending))
                stats["skipped"] = len(pending)
                return stats

            # A mocked run makes no network call, so it can cost nothing and is
            # not gated. Real operation is always gated.
            guard_status = ({"blocked": False} if os.environ.get("BRITTLE_REVIEWER_MOCK")
                            else SpendGuard(self.cfg).status())
            if guard_status["blocked"]:
                LOG.warning("spend guard BLOCKED (%s) -- leaving %d report(s) "
                            "queued, unacknowledged",
                            "; ".join(guard_status["blocked_reasons"]), len(pending))
                stats["skipped"] = len(pending)
                stats["spend_blocked"] = guard_status["blocked_reasons"]
                return stats

            for report in pending:
                try:
                    outcome = self.review_report(report, msgs)
                    stats["reviewed"] += 1
                    stats[outcome["mode"] + "s"] = stats.get(outcome["mode"] + "s", 0) + 1
                except SpendLimitExceeded as exc:
                    LOG.warning("spend guard refused the request: %s", exc)
                    stats["errors"].append(f"{report.id}: spend guard: {exc}")
                    break  # leave unacknowledged; retry when the cap resets
                except SpendGuardError as exc:
                    LOG.error("spend guard fail-closed: %s", exc)
                    stats["errors"].append(f"{report.id}: spend guard: {exc}")
                    break
                except (MessageError, ReviewerError) as exc:
                    LOG.error("review of %s failed: %s", report.id, exc)
                    stats["errors"].append(f"{report.id}: {exc}")
                    break  # leave it unacknowledged; retry next pass
                msgs = ml.load_messages(self.repo.path)
        return stats

    # --- publication ------------------------------------------------------

    def review_report(self, report: ml.Message, msgs: dict[str, ml.Message]) -> dict:
        prompt, prompt_sha = self.load_prompt()
        notes = self.verify_report(report)
        if any(n.startswith("CHECKSUM MISMATCH") for n in notes):
            raise MessageError(
                f"{report.id}: {notes[0]} -- refusing to review a report whose "
                f"local source no longer matches"
            )

        max_chars = int(self.cfg["reviewer"].get("max_report_chars") or 24000)
        body = report.body
        if len(body) > max_chars:
            body = body[:max_chars] + "\n\n[...report truncated for review...]"

        user_content = (
            f"# Report {report.id}\n"
            f"- lane: {report.get('lane')}\n"
            f"- unit: {report.get('unit')}\n"
            f"- brittle commit: {report.get('source_commit')}\n"
            f"- local source: {report.get('local_source_path')}\n"
            f"- integrity: {'; '.join(notes)}\n"
            f"- redacted mirror: {bool(report.get('redacted'))}\n\n"
            f"---\n\n{body}\n"
        )

        telemetry: dict = {}
        raw = call_model(self.cfg, prompt, user_content, telemetry=telemetry)
        review = validate_review(raw)
        verdict = decide(review, self.cfg)

        # --- Telephone bounded-run arbitration -------------------------
        # The run's hard bounds are applied here, outside the model. A model
        # that wants to keep going cannot extend a run past max_cycles, past a
        # manual stop, or past a criterion it cannot judge confidently.
        run = tp.run_for_report(report, msgs, str(report.get("lane")))
        tel = None
        tel_state = None
        cycle_closed = False
        if run is not None:
            tel_state = tp.run_state(run, msgs)
            cycle_closed = tp.closes_cycle(report, msgs, run)
            tel = tp.evaluate(
                tel_state,
                verdict_mode=verdict["mode"],
                criterion_status=review.get("criterion_status"),
                criterion_confidence=review.get("criterion_confidence"),
                threshold=verdict["threshold"],
                cycle_closed=cycle_closed,
            )
            if verdict["mode"] == "ticket" and not tel["issue_ticket"]:
                verdict = dict(verdict)
                verdict["mode"] = "escalation" if tel["escalate"] else "review_only"
                verdict["reasons"] = list(verdict["reasons"]) + [
                    f"telephone {run.id}: {tp.describe_stop(tel['stop_reason'])}"]

        spend_usd = round(telemetry.get("actual_micro", 0) / 1_000_000, 6)
        api_calls = int(telemetry.get("api_calls", 0))

        now = ml.utc_now()
        files: dict[str, str] = {}

        # 1. the review message
        rfm = ml.base_frontmatter(
            "review", sender="reviewer",
            to=str(review.get("target_lane") or "joe"),
            lane="reviewer", unit=report.get("unit"),
            status="acknowledged",
            requires_owner=(verdict["mode"] == "escalation"),
            confidence=round(float(review["confidence"]), 3),
            in_reply_to=report.id,
            source_commit=report.get("source_commit"), now=now,
        )
        rfm.update({
            "title": f"Review of {report.get('title') or report.id}",
            "review_of": report.id,
            "target_lane": review.get("target_lane"),
            "next_action": review["next_action"][:200],
            "reviewer_model": str(self.cfg["reviewer"].get("model")),
            "prompt_sha256": prompt_sha,
            "spend_usd": spend_usd,
        })
        if run is not None:
            rfm["run_id"] = run.id
            rfm["criterion_status"] = review.get("criterion_status")
            cc = review.get("criterion_confidence")
            rfm["criterion_confidence"] = (round(float(cc), 3)
                                           if cc is not None else None)
        rbody = (
            f"# Review of `{report.id}`\n\n"
            f"**Summary.** {review['summary']}\n\n"
            f"**Next action.** {review['next_action']}\n\n"
            f"**Rationale.** {review['reasoning_summary']}\n\n"
            f"| field | value |\n| --- | --- |\n"
            f"| confidence | {float(review['confidence']):.2f} |\n"
            f"| threshold | {verdict['threshold']:.2f} |\n"
            f"| target lane | {review.get('target_lane') or '-'} |\n"
            f"| decision | {verdict['mode']} |\n"
            f"| model | {self.cfg['reviewer'].get('model')} |\n"
            f"| prompt sha256 | `{prompt_sha[:16]}...` |\n\n"
            f"**Report integrity.** {'; '.join(notes)}\n"
            + (("\n**Escalation reasons.**\n\n"
                + "\n".join(f"- {r}" for r in verdict["reasons"]) + "\n")
               if verdict["reasons"] else "")
        )
        review_rel = f"{ml.DIR_REVIEWS}/{rfm['id']}.md"
        files[review_rel] = ml.render_message(rfm, rbody)

        # 2. next ticket OR escalation
        child_id = None
        esc_rel = None
        if verdict["mode"] == "ticket":
            tfm = ml.base_frontmatter(
                "ticket", sender="reviewer", to=str(review["target_lane"]),
                lane=str(review["target_lane"]), unit=report.get("unit"),
                status="open", requires_owner=False,
                confidence=round(float(review["confidence"]), 3),
                in_reply_to=report.id, source_commit=report.get("source_commit"),
                now=now,
            )
            tfm["title"] = (review.get("ticket_title") or review["next_action"])[:120]
            tfm["next_action"] = review["next_action"][:200]
            if run is not None:
                tfm["run_id"] = run.id
                tfm["cycle_index"] = tel["cycles_after"] + 1
            tbody = (
                f"# {tfm['title']}\n\n"
                f"> Issued autonomously by the BRITTLE reviewer from report "
                f"`{report.id}` at confidence {float(review['confidence']):.2f} "
                f"(threshold {verdict['threshold']:.2f}).\n\n"
                f"{review['ticket_markdown']}\n\n"
                "## Standing prohibitions\n\n"
                "- Do not promote, mutate canonical artefacts, move `latest`, or "
                "touch policy cards under this ticket.\n"
                "- Do not infer owner authorization from chat, terminal text, or an "
                "unsigned message.\n"
                "- If the work needs Guard mutation, stop and escalate.\n"
            )
            child_id = tfm["id"]
            files[f"{ml.DIR_TICKETS}/{tfm['lane']}/{tfm['id']}.md"] = \
                ml.render_message(tfm, tbody)

        elif verdict["mode"] == "escalation":
            question = (review.get("owner_question")
                        or f"Approve this next action? {review['next_action']}")
            efm = ml.base_frontmatter(
                "escalation", sender="reviewer", to="joe",
                lane=str(review.get("target_lane") or report.get("lane")),
                unit=report.get("unit"), status="open", requires_owner=True,
                confidence=round(float(review["confidence"]), 3),
                in_reply_to=report.id, source_commit=report.get("source_commit"),
                now=now,
            )
            efm["title"] = question[:120]
            ebody = (
                "# Owner decision required\n\n"
                f"**Question for Joe:** {question}\n\n"
                f"**Context.** {review['summary']}\n\n"
                f"**Proposed next action (NOT issued).** {review['next_action']}\n\n"
                "**Why this stopped:**\n\n"
                + "\n".join(f"- {r}" for r in verdict["reasons"])
                + f"\n\n- source report: `{report.id}`\n"
                f"- review: `{rfm['id']}`\n"
                f"- reviewer confidence: {float(review['confidence']):.2f} "
                f"(threshold {verdict['threshold']:.2f})\n\n"
                "No executable ticket was published. Answer with:\n\n"
                "```bash\n"
                f"messagesctl resolve-escalation --id {efm['id']} \\\n"
                "  --decision-file /path/to/decision.md \\\n"
                "  --authorized-action \"<exact action>\" --scope \"<scope>\"\n"
                "```\n"
            )
            child_id = efm["id"]
            esc_rel = f"{ml.DIR_ESC_OPEN}/{efm['id']}.md"
            files[esc_rel] = ml.render_message(efm, ebody)

        # 3. the acknowledgement -- same commit, so restart cannot duplicate
        afm = ml.base_frontmatter(
            "receipt", sender="reviewer", to=str(report.get("lane")),
            lane="reviewer", unit=report.get("unit"), status="acknowledged",
            requires_owner=False, in_reply_to=report.id,
            source_commit=report.get("source_commit"), now=now,
        )
        afm.update({
            "receipt_type": "reviewer_ack", "agent": "reviewer",
            "report_id": report.id, "review_of": report.id,
            "reviewer_model": str(self.cfg["reviewer"].get("model")),
            "prompt_sha256": prompt_sha,
        })
        if verdict["mode"] == "ticket":
            afm["ticket_id"] = child_id
        elif verdict["mode"] == "escalation":
            afm["escalation_id"] = child_id
        abody = (
            f"# Reviewer acknowledgement\n\n"
            f"- report: `{report.id}`\n"
            f"- review: `{rfm['id']}`\n"
            f"- outcome: **{verdict['mode']}**"
            + (f" (`{child_id}`)" if child_id else "") + "\n"
            f"- confidence: {float(review['confidence']):.2f}\n"
        )
        files[f"{ml.DIR_RECEIPTS}/{afm['id']}.md"] = ml.render_message(afm, abody)

        if run is not None:
            if cycle_closed:
                cfm = ml.base_frontmatter(
                    "receipt", sender="reviewer", to=str(run.get("lane")),
                    lane=str(run.get("lane")), unit=run.get("unit"),
                    status="completed", in_reply_to=run.id, now=now)
                cfm.update({
                    "receipt_type": "telephone_cycle", "agent": "reviewer",
                    "run_id": run.id, "cycle_index": tel["cycles_after"],
                    "report_id": report.id, "review_of": report.id,
                    "criterion_status": review.get("criterion_status"),
                    "criterion_confidence": (
                        round(float(review["criterion_confidence"]), 3)
                        if review.get("criterion_confidence") is not None else None),
                    "api_calls": api_calls, "spend_usd": spend_usd,
                })
                cbody = (
                    f"# Telephone cycle {tel['cycles_after']}/{tel_state['max_cycles']}\n\n"
                    f"- run: `{run.id}`\n- completion report: `{report.id}`\n"
                    f"- review: `{rfm['id']}`\n"
                    f"- criterion status: {review.get('criterion_status') or '-'}\n"
                    f"- this call: {api_calls} API call, ${spend_usd:.6f}\n")
                files[f"{ml.DIR_RECEIPTS}/{cfm['id']}.md"] = ml.render_message(cfm, cbody)

            if tel["stop"] and tel_state["status"] == "active":
                sfm = ml.base_frontmatter(
                    "receipt", sender="reviewer", to=str(run.get("lane")),
                    lane=str(run.get("lane")), unit=run.get("unit"),
                    status=("completed" if tel["stop_reason"] in tp.SUCCESS_REASONS
                            else "blocked"),
                    requires_owner=bool(tel["escalate"]),
                    in_reply_to=run.id, now=now)
                sfm.update({
                    "receipt_type": "telephone_stop", "agent": "reviewer",
                    "run_id": run.id, "stop_reason": tel["stop_reason"],
                    "cycles_completed": tel["cycles_after"],
                    "criterion_status": review.get("criterion_status"),
                    "criterion_confidence": (
                        round(float(review["criterion_confidence"]), 3)
                        if review.get("criterion_confidence") is not None else None),
                    "api_calls": 0, "spend_usd": 0.0,
                })
                sbody = (
                    f"# Telephone run stopped\n\n"
                    f"- run: `{run.id}`\n"
                    f"- reason: **{tel['stop_reason']}** -- {tp.describe_stop(tel['stop_reason'])}\n"
                    f"- cycles completed: {tel['cycles_after']}/{tel_state['max_cycles']}\n"
                    f"- criterion: {run.get('criterion') or '(none)'}\n"
                    f"- criterion status: {review.get('criterion_status') or '-'}\n\n"
                    + ("> Reaching the cycle limit is exhaustion, not success.\n"
                       if tel["stop_reason"] in tp.EXHAUSTION_REASONS else "")
                    + (f"\n**Evidence.** {review.get('criterion_evidence')}\n"
                       if review.get("criterion_evidence") else ""))
                files[f"{ml.DIR_RECEIPTS}/{sfm['id']}.md"] = ml.render_message(sfm, sbody)
                LOG.info("telephone %s stopped: %s (%d/%d cycles)", run.id,
                         tel["stop_reason"], tel["cycles_after"],
                         tel_state["max_cycles"])

        result = ml.publish(
            self.repo, files,
            f"review({verdict['mode']}): {report.id} -> {rfm['id']}"
            + (f" + {child_id}" if child_id else ""),
            cfg=self.cfg,
        )
        LOG.info("reviewed %s -> %s (%s, conf=%.2f, pushed=%s)",
                 report.id, rfm["id"], verdict["mode"],
                 float(review["confidence"]), result.pushed)

        if esc_rel:
            self._publish_notice_for(child_id, efm["title"], str(efm["lane"]),
                                     efm.get("unit"), esc_rel)

        return {"mode": verdict["mode"], "review_id": rfm["id"], "child_id": child_id,
                "commit": result.commit, "pushed": result.pushed}

    # --- notifications ----------------------------------------------------

    def _publish_notice(self, esc: ml.Message) -> None:
        self._publish_notice_for(esc.id, str(esc.get("title") or "escalation"),
                                 str(esc.get("lane")), esc.get("unit"), esc.rel)

    def _publish_notice_for(self, esc_id: str, summary: str, lane: str,
                            unit, rel: str) -> None:
        status, detail = ml.notify(self.cfg, escalation_id=esc_id, summary=summary,
                                   lane=lane, unit=unit, rel_path=rel)
        nfm = ml.base_frontmatter(
            "receipt", sender="reviewer", to="joe", lane=lane, unit=unit,
            status="open", requires_owner=True, in_reply_to=esc_id,
        )
        nfm.update({"receipt_type": "escalation_notice", "agent": "reviewer",
                    "escalation_id": esc_id, "notification_status": status,
                    "notification_detail": detail[:200]})
        nbody = (f"# Escalation notification receipt\n\n"
                 f"- escalation: `{esc_id}`\n"
                 f"- notification_status: **{status}**\n"
                 f"- detail: {detail}\n")
        ml.publish(self.repo,
                   {f"{ml.DIR_RECEIPTS}/{nfm['id']}.md": ml.render_message(nfm, nbody)},
                   f"receipt(escalation_notice): {esc_id} notification={status} "
                   f"[{nfm['id']}]", cfg=self.cfg)
        LOG.info("escalation %s notification_status=%s (%s)", esc_id, status, detail)


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

_STOP = False


def _handle_signal(signum, _frame):
    global _STOP
    _STOP = True
    LOG.info("received signal %s -- finishing the current pass and stopping", signum)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="reviewer_daemon", description=__doc__.split("\n")[0])
    p.add_argument("--config")
    p.add_argument("--once", action="store_true", help="run a single pass and exit")
    p.add_argument("--poll-seconds", type=float)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    cfg = ml.load_config(args.config)
    try:
        daemon = ReviewerDaemon(cfg)
    except MessageError as exc:
        LOG.error("startup failed: %s", exc)
        return 2

    poll = float(args.poll_seconds or cfg["reviewer"].get("poll_seconds") or 20)
    LOG.info("reviewer online: repo=%s model=%s threshold=%s poll=%ss",
             daemon.repo.path, cfg["reviewer"].get("model"),
             cfg["reviewer"].get("minimum_confidence"), poll)

    # Confirm the credential is loaded. Logs a non-reversible fingerprint only;
    # the key never reaches the journal, a message, or a command line.
    cred = ml.credential_status(cfg)
    if cred["loaded"]:
        LOG.info("credential: loaded from %s (%s)", cred["source"], cred["fingerprint"])
    else:
        LOG.warning("credential: NOT loaded -- reviews will fail closed (%s)",
                    cred["detail"])

    gs = SpendGuard(cfg).status()
    LOG.info("spend guard: day $%.4f/$%.2f, month $%.4f/$%.2f, calls %d/%d, "
             "max_completion_tokens=%d, blocked=%s",
             gs["day_committed_usd"], gs["day_cap_usd"],
             gs["month_committed_usd"], gs["month_cap_usd"],
             gs["calls_today"], gs["max_calls_per_day"],
             gs["max_completion_tokens"], gs["blocked"])
    if gs["blocked"]:
        LOG.warning("spend guard blocked: %s", "; ".join(gs["blocked_reasons"]))

    while True:
        try:
            stats = daemon.run_once()
            if stats["reviewed"] or stats["errors"] or stats["notices"]:
                LOG.info("pass: %s", json.dumps(stats))
        except MessageError as exc:
            LOG.error("pass failed: %s", exc)
        except Exception as exc:  # never let the daemon die on one bad pass
            LOG.exception("unexpected error: %s", exc)
        if args.once or _STOP:
            break
        for _ in range(int(max(1, poll))):
            if _STOP:
                break
            time.sleep(1)
    LOG.info("reviewer stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
