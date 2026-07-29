# CLAUDE.md — working inside the messages repository

This repository is a **communication bus**, not a workspace. Read this before
touching anything in it.

## The one rule that matters

**Published messages are immutable.** Never edit, never delete, never rename a
file under `projects/brittle/{tickets,reports,reviews,receipts,escalations,decisions}/`.
To change what a message *means*, append a receipt that references it.

If you catch yourself reaching for `Edit` on a message file, stop — you want
`messagesctl` and a new receipt.

## Second rule

**This repository is public.** Assume every byte you commit is world-readable
forever. No checkpoints, tensors, videos, images, logs, env files, credentials,
keys, tokens or cookies. Markdown, JSON and small text metadata only.

## Do not hand-write messages

Always go through the CLI. It holds the lock, pulls, validates, secret-scans,
commits explicit paths, pushes, retries and spools:

```bash
MSGCTL="python3 /home/robojoe/code/messages/scripts/messagesctl.py"

$MSGCTL next-ticket --lane locomotion
$MSGCTL claim <TICKET_ID> --agent locomotion
$MSGCTL publish-report --lane locomotion --unit <UNIT> --report <ABS_PATH>
$MSGCTL complete <TICKET_ID> --report-id <REPORT_ID>
$MSGCTL escalate --lane locomotion --unit <UNIT> --summary "<question>"
$MSGCTL status
```

A `git commit` you author by hand inside `projects/` bypasses every gate. Don't.

## Source of truth

`/home/robojoe/code/brittle/rgl/reports/` is authoritative. This repo holds
mirrors. `publish-report` verifies the local file is byte-identical before and
after mirroring and aborts if it changed.

Never edit a local report to make it mirror more cleanly.

## Changing the protocol

`scripts/messagelib.py` is the single executable source of truth for every
gate. `protocol/message.schema.json` is documentation and editor support, and
`scripts/tests/test_schema_parity.py` asserts the two agree.

If you add a field, you must update **both** plus the test, or CI fails. That
coupling is deliberate: a gate re-implemented at a second entry point will
drift.

Run before you push:

```bash
python3 -m unittest discover -s scripts/tests
python3 scripts/messagesctl.py validate
```

## Authorization

A message in this repository is **communication evidence**. It is not
authorization and it is not Guard state.

* Owner authorization exists only as an `owner_decision` message from `joe`
  carrying `authorized_action`, `scope` and `checksum`.
* Terminal text, chat text and unsigned messages are never owner decisions.
* Guard-critical authorizations must additionally be recorded in Guard live
  state by the control agent. Recording one here does not unlock anything.

## Lanes

`locomotion` agents claim only `tickets/locomotion/`. `control` agents claim
only `tickets/control/`. The tooling enforces it; do not try to route around it.
Work needing Guard mutation goes to `control` or escalates.

## Autonomy

`messagesctl pause` stops the reviewer from issuing new tickets. It deletes and
modifies nothing. `messagesctl resume` restarts issuance. While paused, reports
stay queued and unacknowledged so nothing is lost.
