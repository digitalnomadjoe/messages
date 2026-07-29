# BRITTLE message bus protocol

Version 1. This document is normative. `scripts/messagelib.py` is the single
executable implementation of every rule below — `messagesctl`, the reviewer
daemon and the GitHub Action all call it, so a gate can never drift between
entry points.

## 1. Invariants

1. **Published messages are immutable.** A file under a message directory is
   never edited, never deleted, never renamed. The one sanctioned exception is
   relocating an escalation from `escalations/open/` to `escalations/resolved/`
   **byte-identically**; the validator verifies the content hash is unchanged.
2. **Status is derived, never edited.** To change what a ticket "is", append a
   receipt that references it. Folding receipts over a ticket yields its
   effective status.
3. **Local reports are authoritative.** Everything in this repository is a
   communication mirror. `~/code/brittle/rgl/reports/` is the source of truth.
4. **This repository is public.** Only Markdown, JSON and small text metadata
   are permitted. Every commit is secret-scanned before it is created.
5. **Fail closed.** Any validation failure aborts publication. Nothing is
   published "best effort".

## 2. Identifiers

```
BRITTLE-<UTC timestamp>-<8-char random suffix>
BRITTLE-20260728T193214Z-3f9c1ab2
```

The filename of every message is exactly `<id>.md`. IDs are globally unique
across the repository; the validator rejects collisions.

The bus sorts by `(created_at, id)`. `created_at` is written at microsecond
precision because that ordering is load-bearing — it decides which report the
reviewer reads first and which claim receipt wins a renew/reclaim fold. The id
suffix is random, so it is a tie-break for a stable total order, never a proxy
for authorship order. Timestamps written at coarser precision (`...T10:15:00Z`)
remain valid and parse correctly.

## 3. Frontmatter

Every message begins with a flat YAML frontmatter block. Nesting, lists and
indentation are rejected — the parser accepts scalars only, which keeps the
attack surface of a public bus small.

| field | required | notes |
| --- | --- | --- |
| `id` | yes | matches the ID grammar above |
| `kind` | yes | `ticket` \| `report` \| `review` \| `receipt` \| `escalation` \| `owner_decision` |
| `project` | yes | always `brittle` |
| `from` | yes | author identity (`locomotion`, `control`, `reviewer`, `joe`) |
| `to` | yes | intended recipient |
| `lane` | yes | `locomotion` \| `control` \| `reviewer` \| `joe` |
| `unit` | present, nullable | active BRITTLE unit, e.g. `12U-C11P` |
| `created_at` | yes | RFC3339 UTC, `...Z`, microsecond precision |
| `source_commit` | present, nullable | BRITTLE commit the message describes |
| `local_source_path` | present, nullable | absolute path of the authoritative local file |
| `local_source_sha256` | present, nullable | SHA-256 of that local file |
| `in_reply_to` | present, nullable | message this responds to |
| `supersedes` | present, nullable | message this replaces |
| `requires_owner` | yes | boolean |
| `confidence` | present, nullable | 0.0–1.0 |
| `status` | yes | see below |

All sixteen keys must be present on every message; nullable ones may be
`null`. Unknown keys are rejected outright.

### Kind-specific extensions

A whitelisted set may additionally appear: `title`, `receipt_type`, `agent`,
`claimed_at`, `lease_expires_at`, `brittle_commit`, `report_id`, `ticket_id`,
`review_of`, `escalation_id`, `decision_id`, `target_lane`, `reason`,
`notification_status`, `notification_detail`, `authorized_action`, `scope`,
`expires_at`, `checksum`, `autonomy`, `reviewer_model`, `prompt_sha256`,
`redacted`, `truncated`, `mirror_bytes`, `next_action`.

## 4. Status values

`open`, `claimed`, `completed`, `blocked`, `acknowledged`, `superseded`,
`resolved`.

The `status` field records what the message *asserts at the moment it is
written*. It is never rewritten. The effective status of a ticket is computed
by folding its receipts:

| condition | effective status |
| --- | --- |
| another message sets `supersedes: <ticket>` | `superseded` |
| a `complete` receipt exists | `completed` |
| a `block` receipt exists | `blocked` |
| latest claim receipt's lease is still in the future | `claimed` |
| otherwise | `open` |

## 5. Directory placement

| kind | directory |
| --- | --- |
| `ticket` | `projects/brittle/tickets/<lane>/` |
| `report` | `projects/brittle/reports/<lane>/` |
| `review` | `projects/brittle/reviews/` |
| `receipt` | `projects/brittle/receipts/` |
| `escalation` | `projects/brittle/escalations/open/` → `resolved/` |
| `owner_decision` | `projects/brittle/decisions/` |

`projects/brittle/state/` holds generated caches only. They are disposable and
rebuildable from history with `messagesctl rebuild-index`; source messages and
receipts are authoritative.

## 6. Receipts

`receipt_type` ∈ `claim`, `renew`, `reclaim`, `complete`, `block`,
`reviewer_ack`, `escalation_notice`, `escalation_resolved`.

Claim-family receipts (`claim`/`renew`/`reclaim`) must carry `agent`,
`claimed_at`, `lease_expires_at` and `brittle_commit`.

### Leases

A claim grants a bounded lease (default 45 min, `[claims].lease_seconds`).
Another agent may reclaim a ticket only when **all** hold:

* the lease has expired,
* no completion receipt exists,
* no newer renewal exists,
* the reclaim is appended as a `reclaim` receipt.

Exactly one initial `claim` receipt may exist per ticket; later takeovers must
use `reclaim`. Only one claim may be active at a time.

## 7. Lane separation

* A `locomotion` agent may claim only tickets under `tickets/locomotion/`.
* A `control` agent may claim only tickets under `tickets/control/`.

The reviewer assigns a lane explicitly. A locomotion ticket that would require
Guard mutation must instead target the `control` lane, or an owner escalation
must be raised first. `messagesctl claim` rejects cross-lane claims, and the
repository validator rejects them retroactively.

## 8. Escalations and owner decisions

An escalation is immutable, must set `requires_owner: true`, and asks exactly
one concrete question. After it is pushed, the configured notification command
runs and the outcome is recorded in an `escalation_notice` receipt as
`notification_status` ∈ `sent` | `failed` | `unavailable`. The system never
claims Joe was notified when it did not observe a success.

An `owner_decision` must come `from: joe` and carry `authorized_action`,
`scope` and `checksum` (SHA-256 of the decision source file). It may carry
`expires_at`.

> A Guard-critical authorization recorded here is **communication evidence
> only**. It does not authorize anything by itself: the control agent must
> still record it in Guard live state. A GitHub message is not a Guard state
> mutation.

Terminal text, chat text and unsigned messages are never owner decisions.

## 9. Publication procedure

Every mutating command performs, in order:

1. acquire the repository lock (`flock` on `.git/brittle-messages.lock`);
2. `git pull --ff-only`;
3. validate the current repository;
4. create **only new files** (existing paths are never overwritten);
5. run secret and schema checks on each new file;
6. commit (explicit paths only — never `git add -A`);
7. push;
8. on a concurrent-append rejection, discard the local unpushed commit, re-pull
   and retry, bounded to 4 attempts;
9. if the push still cannot land, retain the commit locally and record it in a
   durable spool for the sync timer.

Never: force-push, amend a published commit, rewrite history, delete a message,
or overwrite another agent's file.

Immutability is enforced at two layers:

* **Locally, before the commit** — the working tree is inspected and any
  modification or deletion under a message directory aborts publication.
* **In CI, after the fact** — `messagesctl validate --diff-base <ref>` compares
  the whole range `<ref>...HEAD` and rejects any `M`, `D` or non-sanctioned `R`
  under a message directory.

The CI comparison is cumulative, so a message created *and* edited inside the
same pull request reads as a single addition. That is intended: the invariant
being defended is *"once a message is on `main`, it never changes"*, and the
local layer already blocks editing during the session that wrote it.

## 10. Public-repository safety

Permitted in message directories: `.md`, `.json`, ≤256 KiB each.
Permitted elsewhere: docs, scripts, units, CI, ≤512 KiB each.

Rejected: checkpoints, `.npz`/`.npy` tensors, videos, images, logs, env files,
credentials, private keys, tokens, cookies, archives, binaries, and any path
outside the approved roots.

Secret detection runs over every new file before every commit, and over the
whole tree during `messagesctl validate`. A report matching a configured
private pattern is **not mirrored**; only a redacted communication summary is
published, and the full report stays local.
