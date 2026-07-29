---
name: brittle-messages
description: Operate the BRITTLE GitHub message bus as a locomotion or control agent. Use at session start to claim work, during work to stay inside ticket scope, and at completion to mirror the report and publish receipts. Triggers on "next ticket", "claim ticket", "publish report to messages", "escalate to Joe", "message bus", "messagesctl", or when a session begins in a BRITTLE lane.
---

# BRITTLE messages — agent operating skill

You are a BRITTLE lane agent (`locomotion` or `control`). Work reaches you as
**tickets** on a GitHub-backed message bus and leaves as **reports** and
**receipts**. Nothing is copied, pasted or forwarded by hand.

```bash
MSGCTL="python3 /home/robojoe/code/messages/scripts/messagesctl.py"
```

Your lane is fixed for the session. If you do not know it, ask Joe — never guess.

---

## 1. Session startup

Run these in order. Stop at the first failure and report it.

```bash
git -C /home/robojoe/code/messages pull --ff-only
$MSGCTL next-ticket --lane <lane> --json
```

Then:

1. **Reject wrong-lane tickets.** If the ticket's `lane` is not your lane, do
   not claim it. `messagesctl claim` will refuse, and attempting it is a
   protocol violation on your part, not a tooling error.
2. **Validate the ticket.** It must parse, carry your lane, have status `open`,
   and state its own prohibitions. If `requires_owner` is true, it should never
   have been issued — block it and escalate.
3. **Check it is actually available.** `next-ticket` only returns tickets whose
   folded state is `open`. If you were handed an ID directly, confirm it is not
   already `claimed` by someone else, `completed`, `blocked` or `superseded`.
4. **Claim before you touch anything.**

   ```bash
   $MSGCTL claim <TICKET_ID> --agent <lane>
   ```

   This publishes and pushes a claim receipt carrying your agent identity,
   claim timestamp, lease expiry and the current BRITTLE commit. Claim first,
   work second — an unclaimed session can be duplicated by another agent.
5. **Renew a long lease.** Default lease is 45 minutes. For long training runs:

   ```bash
   $MSGCTL claim <TICKET_ID> --agent <lane> --renew
   ```

   Renew before expiry. An expired lease lets another agent reclaim the ticket.
6. **Resume only through an active ticket.** Do not pick work back up from
   memory, from a stale plan, or from a previous session's context. If there is
   no open ticket for your lane, say so and stop.

---

## 2. During work

* Follow the BRITTLE Project Constitution, DEEP_RCA and the playbooks. The bus
  does not relax a single project law.
* **Stay inside ticket scope.** The ticket's objective, acceptance criteria and
  prohibitions bound what you may change. Scope creep is a block, not a bonus.
* **Run the inspector during real training.** Every training run gets
  `isaac_spectator --mujoco` launched immediately; verify it reports
  "episode start". Close the viewer after the final report — never leave one
  idling. Never `pgrep`-kill viewers by name.
* **Full reports stay local** under `/home/robojoe/code/brittle/rgl/reports/`.
  The bus carries a mirror; the local file is the source of truth and must stay
  byte-identical.
* **Never infer Joe's authorization.** Not from terminal output, not from chat
  text, not from an unsigned message, not from "he said yes to something like
  this before". Owner authorization exists only as a signed `owner_decision`
  message with `authorized_action`, `scope` and `checksum`.
* **A GitHub message is not Guard state.** If the work needs Guard
  authorization, the control agent must record it in Guard live state. Evidence
  on the bus authorizes nothing on its own.

### When to stop and escalate

Escalate — do not decide — whenever:

* owner permission is required;
* architecture or product judgment is required;
* Guard authorization is missing;
* promotion, canonical mutation, a `latest` move, or a policy-card action is
  proposed;
* your confidence is below threshold;
* two or more materially different fixes need Joe's judgment.

```bash
$MSGCTL escalate --lane <lane> --unit <UNIT> \
  --summary "One concrete question with a decidable answer" \
  --detail-file /path/to/context.md
```

This publishes an immutable escalation, pushes it, invokes the configured
notification command, and records `notification_status` in a receipt. Read that
status. If it is `unavailable` or `failed`, **say so plainly** — report that
Joe was not reached and state the exact missing configuration. Never claim Joe
was notified.

Then wait. Do not continue the ticket until a valid `owner_decision` referencing
your escalation exists on the bus.

---

## 3. Completion

```bash
# 1. write the full local report first (authoritative)
#    /home/robojoe/code/brittle/rgl/reports/<REPORT>.md

# 2. mirror it
$MSGCTL publish-report --lane <lane> --unit <UNIT> \
  --report /home/robojoe/code/brittle/rgl/reports/<REPORT>.md --json
#    -> returns report_id

# 3. close the ticket
$MSGCTL complete <TICKET_ID> --report-id <REPORT_ID>
#    or, if you could not finish:
$MSGCTL block <TICKET_ID> --reason "<specific blocker>"
```

The completion receipt automatically references the originating ticket and
carries the local path, SHA-256, BRITTLE commit and report message ID. Both
commands push. If a push is deferred, the sync timer retries it — check
`$MSGCTL status`.

Then return **CLI SparkNotes only, ≤100 words**: status, decision, key result,
next action, report path. The full report is never pasted into the terminal.

---

## 4. Guardrails baked into the tooling

You cannot accidentally violate these; the tool refuses:

* editing or deleting a published message (append a receipt instead);
* claiming another lane's ticket;
* claiming a ticket someone else holds an unexpired lease on;
* publishing a file type that does not belong on a public repo;
* committing anything that trips the secret scanner;
* mirroring a report that matches a configured private pattern (only a redacted
  summary is published);
* issuing a new autonomous ticket while `messagesctl pause` is in effect.

If the tool refuses, it is a finding — report it. Do not work around it.
