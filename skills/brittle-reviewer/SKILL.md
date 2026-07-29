---
name: brittle-reviewer
description: Operating rules for the autonomous BRITTLE reviewer that reads mirrored reports and issues the next ticket or an owner escalation. Use when running, debugging, tuning or hand-simulating the reviewer daemon, or when auditing why a review produced a ticket rather than an escalation.
---

# BRITTLE reviewer — operating skill

This skill is loaded alongside `prompts/brittle-reviewer.md` and appended to the
reviewer's system prompt. It describes the mechanics; the prompt describes the
stance.

## What one pass does

1. Lock the repository and `git pull --ff-only`.
2. Validate the whole repository. An invalid repo aborts the pass — never
   review into a broken tree.
3. Find every `report` with no `reviewer_ack` receipt, oldest first.
4. Verify each report's schema, checksum and source relationship. If the local
   source file exists and its SHA-256 no longer matches what the message
   recorded, **refuse to review it** and surface the mismatch.
5. Load the tracked prompt (`prompts/brittle-reviewer.md`) plus this skill and
   hash them; the hash goes into every review and acknowledgement so a verdict
   can always be traced to the exact instructions that produced it.
6. Call the OpenAI API with a strict structured-output schema.
7. Validate the response by hand — missing field, unknown field, wrong type,
   bad lane, or out-of-range confidence all fail closed.
8. Publish, in **one commit**: the review, at most one ticket *or* one
   escalation, and the acknowledgement receipt.
9. For an escalation, invoke the notification command and append a receipt
   recording `notification_status`.

## Idempotency

The acknowledgement is written in the same commit as the review and its child
message. A crash before the commit leaves the report unacknowledged and it is
retried; a crash after the commit leaves the report acknowledged and it is
skipped. There is no window that yields a duplicate review or duplicate ticket.

On restart the daemon also looks for reviewer escalations that carry no
`escalation_notice` receipt and sends the missing notification — the only
sanctioned retry.

## Ticket vs escalation

Issue a ticket only when **all** hold:

* `confidence >= [reviewer].minimum_confidence` (default 0.85);
* the action stays inside already-approved scope;
* no hard gate is touched;
* nothing canonical, plant-level, observation/action-interface or architectural
  is introduced;
* no promotion, `main`/`latest` update or policy-card action is involved;
* no Joe authorization is legally or procedurally required;
* exactly one clearly preferred implementation exists.

Otherwise: `requires_owner = true`, no executable ticket, one escalation with
one concrete question, and ping Joe.

**Confidence never overrides a hard gate.** The daemon enforces this
independently of the model: it re-scans the model's own summary, next action,
ticket title and ticket body for gate language (promotion, canonical, `latest`,
policy card, crown, Guard mutation, interface change, architecture, gate
override, owner authorization) and converts a proposed ticket into an
escalation if any of it appears. A model that forgets the rule cannot bypass it.

It also enforces lane law: a Guard mutation routed to `locomotion` becomes an
escalation rather than a ticket.

## Pause

When autonomy is paused (`messagesctl pause`), the daemon reviews nothing and
acknowledges nothing. Reports stay queued exactly as they are, so `resume`
picks up cleanly with no lost work and no duplicate.

## Secrets

The API key is read from the environment or a 0600 credential file, and appears
in no message, no log line, no service file and no command line. If it is
missing, the daemon fails closed and reports it — it never degrades to an
unauthenticated call.

## Operating

```bash
python3 /home/robojoe/code/messages/scripts/reviewer_daemon.py --once --verbose
systemctl --user status brittle-message-reviewer.service
journalctl --user -u brittle-message-reviewer.service -n 50
```

Set `BRITTLE_REVIEWER_MOCK=/path/to/response.json` to feed a canned model
response instead of calling the API — this is how the test suite exercises
valid, malformed, low-confidence and hard-gate paths without spending tokens.
