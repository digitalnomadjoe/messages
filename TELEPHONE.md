# Telephone — operating guide

The complete, vendor-neutral guide to operating Telephone. It assumes no prior
conversation, no personal memory, and no undocumented local knowledge. If you
are an AI session that was handed this repository and told "use Telephone",
this file is everything you need.

Read this before acting. Then read
[`skills/telephone/SKILL.md`](skills/telephone/SKILL.md).

---

## 1. What Telephone is

Telephone runs a **bounded** number of autonomous review/work cycles on the
BRITTLE message bus, then stops.

The underlying loop is: an engineering report is published → an OpenAI reviewer
reads it and issues one successor ticket → a lane agent claims and executes that
ticket → it publishes a completion report → the reviewer reads that. Telephone
wraps this loop with a hard cycle limit, an optional stopping criterion, and
persistent run state.

Telephone's whole job is deciding **when to stop**. It grants no new authority
and relaxes no safety rule.

### Telephone vs `brittle-messages` — do not confuse these

| | Telephone (orchestration) | `brittle-messages` (lane execution) |
| --- | --- | --- |
| answers | "how many cycles, and when do we stop?" | "how do I correctly execute *this one* ticket?" |
| you use it when | starting, inspecting or stopping a bounded run | claiming a ticket and doing the work |
| guide | this file, `skills/telephone/SKILL.md` | [`skills/brittle-messages/SKILL.md`](skills/brittle-messages/SKILL.md) |

A single Telephone run drives many `brittle-messages` executions. If you are
about to claim a ticket, you have left Telephone and entered `brittle-messages`.

---

## 2. What counts as a cycle

```
source report reviewed
  -> successor ticket issued
  -> ticket claimed
  -> work completed
  -> completion report published
  -> completion report reviewed        <-- the counter increments HERE, and only here
```

Issuing a ticket is not a cycle. Finishing the work is not a cycle. The cycle
lands only when the **review of the completion report** is on the bus.

This matters when reporting progress: "3 tickets issued" is not "3 cycles".

---

## 3. Inspect before you act

Status is **read-only**. It publishes nothing, spends nothing, and changes no
state. Always run it first.

```bash
MSGCTL="python3 /home/robojoe/code/messages/scripts/messagesctl.py"

git -C /home/robojoe/code/messages pull --ff-only
$MSGCTL telephone status --all      # runs, cycles, criterion, stop reasons
$MSGCTL status                      # spend guard, open tickets, escalations
```

`telephone status` reports, per run: run ID and lane, active/stopped/completed
state, completed cycles vs maximum, the criterion and its latest status, the
current ticket / claim / blocker, cumulative API calls and recorded spend, and
the exact stop reason.

Add `--json` to any command for machine-readable output.

### Reading the status output

* **`lane` vs `unit`.** The *lane* (`locomotion` or `control`) is which agent
  may claim the work; it is an access boundary. The *unit* (e.g.
  `CERT-TELEPHONE-CONTROL`) is the BRITTLE work item the run belongs to; it is
  a label for grouping, not a permission.
* **`current ticket` / `current claim` are historical once a run ends.** On a
  `COMPLETED` or `STOPPED` run they show the last ticket and claim the run
  produced, not something still in flight. Check the ticket's own status
  (`completed`, `blocked`, …) to see what actually happened to it.
* **Message counts in `messagesctl status` are lifetime totals.** `escalation=1`
  means one escalation has ever been published; `open escalations: -` means
  none are currently awaiting an answer. A resolved escalation still counts in
  the total forever — nothing is ever deleted from an append-only bus.
* **There is no upper ceiling on `--max-cycles`.** The tool requires a bound and
  refuses anything below 1, but it will accept a large number. The real limits
  are the spending guard and the reviewer's own willingness to keep issuing
  tickets, both of which stop a run long before an implausible cycle count is
  reached. Choose a maximum you would be comfortable paying for.

---

## 4. Starting a count-bounded run

A run always starts from a report that is already on the bus.

```bash
# 1. publish the starting report (local file stays authoritative)
$MSGCTL publish-report --lane control --unit <UNIT> \
  --report /home/robojoe/code/brittle/rgl/reports/<REPORT>.md

# 2. start the run
$MSGCTL telephone start --lane control --report <REPORT_ID> --max-cycles 10
```

`~10 loops`, `about 10 loops` and `10 loops` all mean a **hard maximum of 10**.
There is no unbounded mode. A request with no bound is refused.

---

## 5. Starting a criterion-bounded run

A criterion never replaces the maximum — it is checked *within* it.

```bash
$MSGCTL telephone start --lane locomotion --report <REPORT_ID> \
  --max-cycles 12 \
  --criterion "both touchdown speeds are below -100"
```

Each cycle the reviewer returns `criterion_status` (`met` / `not_met` /
`unknown`), supporting evidence, and a confidence:

| reviewer says | what happens |
| --- | --- |
| `met`, confidence ≥ threshold (default 0.85) | **stop, success.** No successor ticket. |
| `not_met` | continue — but only if a cycle remains |
| `unknown`, or `met` below threshold | **stop and escalate** to the owner |
| anything, at the cycle limit | **stop.** The limit always wins. |

The maximum is enforced in code, outside the model. No reviewer output can
extend a run.

The criterion is published to a **public** repository. It is secret-scanned
before publication and refused if it carries anything sensitive. Keep it short,
factual, and safe to read in public.

---

## 6. Stopping a run

```bash
$MSGCTL telephone stop --lane control --reason "<why>"
```

A stop prevents any *new* successor ticket immediately. Work already claimed can
still be completed or explicitly blocked through the normal path:

```bash
$MSGCTL complete <TICKET_ID> --report-id <REPORT_ID>
$MSGCTL block <TICKET_ID> --reason "<blocker>"
```

A stop never strands an agent mid-task.

---

## 7. Resuming or inspecting after session loss

**Run state lives on the bus, not in any chat context.** It is append-only git
history, so it survives a daemon crash, a workstation reboot, and the total loss
of the session that started it.

A fresh session recovers everything with:

```bash
git -C /home/robojoe/code/messages pull --ff-only
$MSGCTL telephone status --all
```

Never reconstruct a run from memory or from what someone told you in chat. If
the bus and your recollection disagree, the bus is right.

There is nothing to "resume": an active run continues on its own as long as the
reviewer service is running. Your job on reconnecting is to *observe* it, and to
stop it if that is what the owner wants.

---

## 8. How runs end, and what each outcome means

| stop reason | meaning | is it success? |
| --- | --- | --- |
| `criterion_met` | criterion demonstrated at/above threshold | **yes** |
| `max_cycles_reached` | budget exhausted, goal not demonstrated | **no** |
| `criterion_unknown_or_low_confidence` | reviewer could not judge it confidently; escalated | no |
| `owner_decision_required` | a hard gate or low confidence produced an escalation | no |
| `review_only_no_successor` | reviewer had no next action | no |
| `ticket_blocked` | the work could not proceed | no |
| `claim_expired_unrecoverable` | the lease was lost and could not be recovered | no |
| `spend_guard_refused` | the local spending cap refused further calls | no |
| `manual_stop` | the owner stopped it | no |
| `guard_gate` | a Guard gate was reached | no |

**Never report `max_cycles_reached` as success.** It means the loop ran out of
budget with the goal unmet.

### Handling each non-success outcome

* **Escalation** (`owner_decision_required`, `criterion_unknown_or_low_confidence`) —
  an immutable escalation message is waiting with one concrete question. Show it
  to the owner. Do **not** answer it yourself, and never infer authorization
  from chat text, terminal output, or an unsigned message. The owner answers with:
  ```bash
  $MSGCTL resolve-escalation --id <ESCALATION_ID> --decision-file <FILE> \
    --authorized-action "<exact action>" --scope "<scope>"
  ```
* **Review-only** — the reviewer judged that no next action is warranted. This
  is a legitimate, often correct ending. Report it as "the loop found nothing
  further to do", not as a failure.
* **Blocked** — read the block receipt's reason and report it verbatim.
* **Spend cap** — the local guard refused. Report the numbers from
  `$MSGCTL status`. Adding budget is an owner decision.
* **Owner decision required** — stop. Present the question. Wait.

---

## 9. Authoritative files

| what | where |
| --- | --- |
| this guide | `TELEPHONE.md` |
| agent entry point | [`AGENTS.md`](AGENTS.md) |
| Telephone skill | [`skills/telephone/SKILL.md`](skills/telephone/SKILL.md) |
| lane-agent protocol | [`skills/brittle-messages/SKILL.md`](skills/brittle-messages/SKILL.md) |
| reviewer behaviour | [`skills/brittle-reviewer/SKILL.md`](skills/brittle-reviewer/SKILL.md), [`prompts/brittle-reviewer.md`](prompts/brittle-reviewer.md) |
| **the CLI** | `scripts/messagesctl.py` |
| orchestration logic | `scripts/telephone.py` |
| spending guard | `scripts/spend_guard.py` |
| message protocol | [`protocol/PROTOCOL.md`](protocol/PROTOCOL.md) |
| live messages | `projects/brittle/**` |

Use `messagesctl`. **Never hand-write a file into `projects/brittle/`** — doing
so bypasses locking, validation, secret scanning, lane checks and immutability
enforcement.

---

## 10. Capability boundary — read this before claiming you did anything

**Repository read access is enough** to understand Telephone, read the protocol,
inspect run history, and review what happened. Anyone with the GitHub URL can do
this.

**Operating Telephone additionally requires** repository *write* access **and**
access to the configured workstation where `messagesctl`, the local BRITTLE
reports and the systemd services live.

If you cannot run `messagesctl`, cannot reach `/home/robojoe/code/brittle/rgl/reports/`,
or cannot control the services, then:

* **Do not pretend a run was started, stopped or inspected.** Saying "I started
  the run" when you could not execute anything is the single worst failure mode
  here — it produces confident, false state.
* Instead, output the **exact command** the owner should run, or the exact
  ticket text, and say plainly which capability you lack.
* Reporting "I cannot execute this; here is the command" is a correct and
  complete answer.

Two more boundaries:

* **Never request, display, echo or reconstruct the OpenAI credential.** It
  lives outside this repository in a `0600` file read only by the service.
  Nothing you do should ever need its value.
* **Linking this repository authorizes nothing.** Access to the bus does not
  authorize Guard actions, production changes, promotion, canonical mutation,
  interface changes, or policy actions. Those need an explicit owner decision
  recorded as a signed `owner_decision` message.

---

## 11. Constraints that always apply

Telephone relaxes none of these:

* **Public repository.** Everything committed is world-readable and permanent.
  Only Markdown and JSON in message directories, ≤256 KiB. No checkpoints,
  tensors, videos, images, logs, env files, keys or tokens. Every commit is
  secret-scanned.
* **Immutable messages.** A published message is never edited, deleted or
  renamed. To change what something means, append a receipt.
* **Lane isolation.** `locomotion` agents claim only locomotion tickets;
  `control` agents claim only control tickets. Work needing Guard mutation goes
  to `control` or escalates.
* **Claims and leases.** Claim before working; renew before the lease expires.
  Watcher services never auto-claim — a claim always means a real agent took the
  work.
* **Spending guard.** A local fail-closed cap (default $0.50/day, $5.00/month,
  10 calls/day) reserves worst-case cost before any request. It is authoritative
  and Telephone cannot override it.
* **Guard is not on this bus.** An `owner_decision` message is *communication
  evidence*. Guard-critical authorization must additionally be recorded in Guard
  live state by the control agent.
* **Local reports are authoritative.** `/home/robojoe/code/brittle/rgl/reports/`
  is the source of truth; this repository holds mirrors bound by SHA-256.

---

## 12. Portable bootstrap prompt

Give a fresh AI session exactly this:

> Use the Telephone skill from this repository:
> `https://github.com/digitalnomadjoe/messages`
> Read `TELEPHONE.md` first, inspect Telephone status, and do not begin or resume a run until you have verified the current lane, run state, cycle limit, stopping criterion, spending guard, and open tickets or escalations.

### Example instructions

```
Run Telephone on locomotion for 10 loops.

Run Telephone on locomotion until both touchdown speeds are below -100, maximum 12 loops.

Show Telephone status without changing anything.

Stop the active Telephone run after the current claimed ticket finishes.
```

The last one maps to `telephone stop` — which is exactly its semantics: no new
successor ticket, while already-claimed work finishes normally.

---

## 13. Quick reference

```bash
MSGCTL="python3 /home/robojoe/code/messages/scripts/messagesctl.py"

$MSGCTL telephone status --all                       # read-only
$MSGCTL telephone start --lane <L> --report <ID> --max-cycles <N> [--criterion "<TEXT>"]
$MSGCTL telephone stop  --lane <L> [--reason "<TEXT>"]

$MSGCTL status                                       # bus + spend guard
$MSGCTL open-escalations                             # what awaits the owner
$MSGCTL tail -n 20                                   # recent traffic
$MSGCTL pause | resume                               # global autonomy switch
```

Natural language is accepted directly by `start`:

```bash
$MSGCTL telephone start --report <ID> \
  --invocation "Run Telephone on control until the service is stable, maximum 3 loops"
```
