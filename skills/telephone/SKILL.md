---
name: telephone
description: Run bounded autonomous BRITTLE report/ticket cycles. Use when the user says "Run Telephone for N loops", "Run Telephone until <criterion>, maximum N loops", "Stop Telephone", or "Telephone status". Orchestration only — for executing a single ticket as a lane agent, use brittle-messages instead.
---

# Telephone — bounded autonomous cycles

Telephone runs the BRITTLE message loop a **bounded** number of times and then
stops. It is orchestration, not execution.

> **Telephone vs brittle-messages.** Telephone decides *how many times the loop
> runs and when it stops*. `brittle-messages` is the protocol a lane agent
> follows to execute *one* ticket. If you are claiming a ticket and doing the
> work, you want `brittle-messages`. If you are starting, watching or stopping a
> bounded run, you want this skill.

Full vendor-neutral operating guide: [`TELEPHONE.md`](../../TELEPHONE.md).

```bash
MSGCTL="python3 /home/robojoe/code/messages/scripts/messagesctl.py"
```

## What one cycle is

```
source report reviewed
  -> successor ticket issued
  -> ticket claimed
  -> work completed
  -> completion report published
  -> completion report reviewed        <-- the counter increments HERE
```

Nothing earlier counts. Issuing a ticket is not a cycle; finishing the work is
not a cycle. A cycle lands only when the review of its completion report is on
the bus.

## Invocations

| the user says | you run |
| --- | --- |
| `Run Telephone for 10 loops` | `telephone start --lane L --report ID --max-cycles 10` |
| `Run Telephone for ~10 loops` | same — **`~10` means a hard maximum of 10** |
| `Run Telephone until <criterion>, maximum 12 loops` | `... --max-cycles 12 --criterion "<criterion>"` |
| `Stop Telephone` | `telephone stop --lane L` |
| `Telephone status` | `telephone status --lane L` |

"About ten" is never licence to run an eleventh. Every run is bounded; there is
no unbounded mode, and a request without a bound is refused.

## Two reviewer modes

| mode | who reviews | how to start |
| --- | --- | --- |
| `api` (default) | the local OpenAI reviewer daemon | `telephone start --reviewer-mode api` |
| `browser` | a browser GPT with a GitHub connector | `telephone start --reviewer-mode browser`, or any browser request |

They never overlap: the API daemon ignores every report governed by a
browser-mode run.

In browser mode **the browser GPT authors every review and ticket; the
workstation bridge only validates and publishes exactly what was submitted.**
The bridge runs no model. Full protocol: [`TELEPHONE.md`](../../TELEPHONE.md) §3b.

## Before starting — always

```bash
$MSGCTL telephone status --all      # read-only
$MSGCTL status                      # spend guard, open tickets, escalations
```

Do not start if the lane already has an active run, or an unrelated open or
claimed ticket. The tool refuses both; do not work around it.

You also need a **start report** already on the bus — a run begins from a
published report, not from nothing:

```bash
$MSGCTL publish-report --lane control --unit <UNIT> --report /abs/path.md
```

## Starting

```bash
$MSGCTL telephone start \
  --lane control --report <REPORT_ID> \
  --max-cycles 12 \
  --criterion "both touchdown speeds are below -100"
```

The criterion is published to a **public** repository. It is secret-scanned
before publication and refused if it carries anything sensitive. Keep it short,
factual and safe to read in public.

## How a run ends

| outcome | meaning |
| --- | --- |
| `criterion_met` | **success** — the criterion was demonstrated at or above the confidence threshold |
| `max_cycles_reached` | exhaustion, **not** success — the budget ran out |
| `criterion_unknown_or_low_confidence` | the reviewer could not judge it confidently → escalated to Joe |
| `owner_decision_required` | a hard gate or low confidence produced an escalation |
| `review_only_no_successor` | the reviewer had no next action |
| `ticket_blocked` | the work could not proceed |
| `manual_stop` | Joe stopped it |
| `spend_guard_refused` | the local spending cap refused further calls |

**Never report a run that hit `max_cycles` as a success.** It means the loop ran
out of budget with the goal unmet. Say so plainly.

## Stopping

```bash
$MSGCTL telephone stop --lane control --reason "<why>"
```

A stop prevents any *new* successor ticket immediately. Work already claimed can
still be completed or explicitly blocked through the normal
`messagesctl complete` / `messagesctl block` path — a stop never strands an
agent mid-task.

## After session loss

Run state lives on the bus, not in your context. A fresh session recovers
everything with:

```bash
git -C /home/robojoe/code/messages pull --ff-only
$MSGCTL telephone status --all
```

Cycle counts, the criterion, the limit, the current ticket and the stop reason
are all folded from append-only messages. Never reconstruct a run from memory.

## What Telephone cannot do

Telephone bounds the loop. It grants no authority. It cannot override the
spending guard, Guard, owner gates, lane isolation, ticket leases, confidence
thresholds, promotion rules, canonical or interface restrictions, or the
public-repository file policy. Every one of those still applies inside a run
exactly as outside it.

The cycle limit is enforced in code, outside the model — no reviewer output can
extend a run.

Watchers never auto-claim. A claim always means a real agent took the work.

## Reporting back

CLI SparkNotes, ≤100 words: run ID, cycles completed / maximum, stop reason in
plain language, cumulative spend, and the report path. The full detail goes to
`rgl/reports/`, never into the terminal.
