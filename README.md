# BRITTLE messages

## Using Telephone from a new AI session

New here — GPT, Codex, Claude, or any other coding agent? Start with
**[`AGENTS.md`](AGENTS.md)**, then **[`TELEPHONE.md`](TELEPHONE.md)**.

| file | read it before |
| --- | --- |
| [`AGENTS.md`](AGENTS.md) | doing anything at all in this repository |
| [`TELEPHONE.md`](TELEPHONE.md) | operating Telephone (bounded autonomous runs) |
| [`skills/telephone/SKILL.md`](skills/telephone/SKILL.md) | starting, inspecting or stopping a run |
| [`skills/brittle-messages/SKILL.md`](skills/brittle-messages/SKILL.md) | acting as a lane agent on one ticket |

Paste this to bootstrap a fresh session:

> Use the Telephone skill from this repository:
> `https://github.com/digitalnomadjoe/messages`
> Read `TELEPHONE.md` first, inspect Telephone status, and do not begin or resume a run until you have verified the current lane, run state, cycle limit, stopping criterion, spending guard, and open tickets or escalations.

Then instruct it in plain language:

```
Run Telephone on locomotion for 10 loops.

Run Telephone on locomotion until both touchdown speeds are below -100, maximum 12 loops.

Show Telephone status without changing anything.

Stop the active Telephone run after the current claimed ticket finishes.
```

**Repository read access is enough to understand and review Telephone.**
Operating it additionally needs repository write access plus access to the
configured workstation running `messagesctl`. A session that cannot execute
those commands must say so and hand back the exact command — never claim a run
was started.

---

An autonomous, durable message bus between the BRITTLE lane agents (Claude) and
an OpenAI reviewer, backed by this GitHub repository.

Before: Joe moved reports and tickets between ChatGPT and Claude by hand —
downloading, copying, pasting, forwarding. After: an agent publishes a report,
the reviewer reads it and issues the next ticket, an agent claims and executes
it, and the loop closes. No browser, no clipboard, no forwarding.

```
Locomotion / control Claude
        │ publishes report
        ▼
GitHub messages repository
        │ detected by the reviewer daemon
        ▼
OpenAI API reviewer
        │ publishes recap + next ticket
        ▼
GitHub messages repository
        │ claimed by the target Claude agent
        ▼
Claude executes the ticket
        │ publishes report
        └──────────────► repeat
```

The loop stops itself and asks Joe whenever owner permission is required,
architecture or product judgment is required, Guard authorization is missing,
promotion / canonical mutation / `latest` / a policy-card action is proposed,
confidence is below threshold, or several materially different fixes need a
human call.

---

## Architecture

| piece | path | role |
| --- | --- | --- |
| protocol | `protocol/PROTOCOL.md` | normative rules; `message.schema.json` documents the frontmatter |
| library | `scripts/messagelib.py` | **single source of truth** for every gate |
| CLI | `scripts/messagesctl.py` | everything agents and Joe run |
| reviewer | `scripts/reviewer_daemon.py` | polls for unreviewed reports, calls OpenAI, publishes |
| watchers | `scripts/ticket_watcher.py` | read-only lane announcers |
| agent skill | `skills/brittle-messages/SKILL.md` | how a Claude lane agent behaves |
| reviewer skill | `skills/brittle-reviewer/SKILL.md` | how the reviewer behaves |
| reviewer prompt | `prompts/brittle-reviewer.md` | tracked, hashed into every review |
| messages | `projects/brittle/**` | append-only, immutable |
| state | `projects/brittle/state/` | disposable caches, rebuildable |

`messagesctl`, the daemon and the GitHub Action all call the same validators in
`messagelib.py`. A gate implemented twice is a gate that drifts, so there is
exactly one implementation and CI runs it.

### Why GitHub is the durable bus

* **It survives everything else.** Laptop reboots, daemon crashes, expired
  sessions, network outages — the queue is a git history, not a process's
  memory. A restarted daemon picks up exactly where it stopped.
* **Append-only history is the audit trail.** Every ticket, claim, report,
  review and owner decision is a commit with a timestamp and an author. "Who
  authorized this and when" is `git log`, not recollection.
* **Two different vendors' agents can share it.** Claude writes, OpenAI reads,
  neither needs an API into the other.
* **Concurrency is already solved.** Push rejection *is* the conflict signal;
  the CLI pulls, revalidates and retries.
* **Joe can read it from a phone.** No tooling required to see what the robot
  programme is doing.

The cost is that it is public — see [Privacy](#repository-privacy-risks).

### Local reports remain authoritative

`/home/robojoe/code/brittle/rgl/reports/` is the source of truth. This
repository holds mirrors. Every mirrored report records the original local
path, its SHA-256, the BRITTLE commit, the originating lane, the active unit
and the timestamp. `publish-report` hashes the local file before and after
mirroring and aborts if a single byte moved.

---

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/digitalnomadjoe/messages.git ~/code/messages
mkdir -p ~/.config/brittle-messages
cp ~/code/messages/config/config.example.toml ~/.config/brittle-messages/config.toml
chmod 600 ~/.config/brittle-messages/config.toml
```

Edit `[repo].path`, `[repo].brittle_path`, `[reviewer].model` and
`[notification].command`.

### 2. Credentials

The OpenAI key is read from the environment (`[openai].api_key_env`, default
`OPENAI_API_KEY`) or from a 0600 file named in `[openai].api_key_file`. It never
appears in git, service files, logs, reports, command lines or escalations. If
it is missing the reviewer **fails closed** and says so.

For the systemd unit, put it in a file systemd reads but git never sees:

```bash
install -m 600 /dev/null ~/.config/brittle-messages/reviewer.env
printf 'OPENAI_API_KEY=%s\n' "$YOUR_KEY" > ~/.config/brittle-messages/reviewer.env
```

That path is referenced by `EnvironmentFile=-` in the unit and lives outside
this repository.

### 3. Agent setup

Install the skill so every Claude session in a BRITTLE lane picks it up:

```bash
mkdir -p ~/.claude/skills
ln -sfn ~/code/messages/skills/brittle-messages ~/.claude/skills/brittle-messages
```

An agent session then starts with `messagesctl next-ticket --lane <lane>`,
claims before working, and completes with `publish-report` + `complete`.

### 4. Services

```bash
sh ~/code/messages/scripts/install_services.sh
systemctl --user enable --now brittle-messages-locomotion.service
systemctl --user enable --now brittle-messages-control.service
systemctl --user enable --now brittle-message-sync.timer
systemctl --user enable --now brittle-message-reviewer.service   # billable — see below
loginctl enable-linger "$USER"   # survive logout
```

Re-run `install_services.sh` after editing anything under `systemd/`.

> **Do not symlink the units into `~/.config/systemd/user/`.** When a unit file
> in the search path is itself a symlink, `systemctl --user disable <unit>`
> removes that symlink along with the `wants/` entry. The unit then disappears:
> a later `enable --now` fails with "Unit ... could not be found", and a plain
> `restart` silently leaves the service inactive. `install_services.sh` copies
> instead, which makes `disable` safe and reversible.

The reviewer is the only unit that spends money. It is gated by the local
spending guard (see below), but if you want it off entirely,
`systemctl --user disable --now brittle-message-reviewer.service` is now safe.

No sudo anywhere in this stack.

---

## Service management

```bash
systemctl --user status  brittle-message-reviewer.service
systemctl --user restart brittle-message-reviewer.service
systemctl --user stop    brittle-messages-locomotion.service
systemctl --user list-timers brittle-message-sync.timer
journalctl --user -u brittle-message-reviewer.service -f
```

Services log concise operational status only — never report bodies, never
message contents, never secrets. They restart on failure, stop on SIGTERM
within 45s, and are idempotent across restarts.

---

## Escalation workflow

1. An agent or the reviewer publishes an immutable `escalation` with one
   concrete question, and pushes it.
2. The configured notification command runs.
3. A receipt records `notification_status`: `sent`, `failed`, or `unavailable`.
   **The system never claims Joe was notified when it did not observe success.**
   With no notification command configured, the escalation is still published
   and the status is `unavailable` with the exact missing configuration named.
4. Polling continues for a tracked owner decision. The ticket does not proceed
   until one is present and valid.

```bash
messagesctl open-escalations
messagesctl notify-test          # verify the configured command works
```

## Owner-decision workflow

```bash
cat > /tmp/decision.md <<'EOF'
Lower ankle-roll kd to 0.2 control-side. Do not touch the plant.
Re-run the C7 verifier and report the survival delta.
EOF

messagesctl resolve-escalation \
  --id BRITTLE-20260728T121200Z-9f0e1d2c \
  --decision-file /tmp/decision.md \
  --authorized-action "Lower ankle-roll kd to 0.2 and re-run the C7 verifier" \
  --scope "12U-C11P, control-side PD only, no plant mutation"
```

This publishes an immutable `owner_decision` from `joe` carrying the decision
ID, the exact authorized action, the related escalation, the unit, the scope,
the timestamp, an optional expiration and a SHA-256 checksum of the decision
file — and relocates the escalation to `escalations/resolved/` byte-identically.

> **Guard-critical decisions.** A message here is communication evidence. It is
> not Guard state. The control agent must still record the authorization in
> Guard live state. Nothing on this bus unlocks a Guard gate by itself.

Terminal text, chat text and unsigned messages are never owner decisions.

---

## Repository privacy risks

**Assume this repository is public and permanent.**

* Anything committed is world-readable and may be cached or indexed after
  deletion. Deleting a message does not unpublish it — and the protocol forbids
  deletion anyway.
* Message directories accept only `.md` and `.json`, ≤256 KiB. Checkpoints,
  tensors, videos, images, logs, env files, keys, tokens and cookies are
  rejected before the commit is created.
* Every new file is secret-scanned (OpenAI/Anthropic/GitHub/AWS/Slack/Google/
  Twilio keys, private-key blocks, JWTs, bearer headers, generic
  `api_key = ...` assignments) and the whole tree is rescanned by
  `messagesctl validate` and by CI.
* Reports matching a configured `[safety].private_patterns` regex are **not
  mirrored**. Only a redacted communication summary is published; the full
  report stays local.
* Mirrored reports do record absolute local paths and BRITTLE commit hashes.
  That is deliberate — it is what makes a mirror traceable — but it does reveal
  directory structure. Do not put anything in a report you would not put on a
  billboard.

If something sensitive does land here, treat it as disclosed: rotate the
credential. Do not attempt to rewrite history — the protocol forbids it and it
would not help.

---

## Recovery after network or API failure

The system is designed to degrade, not to lose work.

| failure | behaviour | recovery |
| --- | --- | --- |
| push rejected (concurrent append) | local unpushed commit discarded, re-pull, retry ×4 | automatic |
| push still failing (offline) | commit retained locally, recorded in the durable spool | `brittle-message-sync.timer` retries every 5 min, or `messagesctl sync` |
| pull fails before a commit exists | message content written to `~/.local/state/brittle-messages/spool/outbox/` | `messagesctl sync` replays it |
| OpenAI unreachable / HTTP error | report left unacknowledged, no partial publication | next poll retries |
| malformed model response | rejected, nothing published | next poll retries |
| daemon killed mid-review | review + child + acknowledgement are one commit, so either all or none landed | restart; no duplicates |
| daemon killed after escalation, before notifying | escalation exists with no notice receipt | restart detects and sends it |
| index drifts | it is a disposable cache | `messagesctl rebuild-index` |

```bash
messagesctl status     # deferred pushes, spool depth, awaiting review
messagesctl sync       # force a retry now
messagesctl validate   # full integrity check
```

---

## How Joe pauses and resumes autonomy

```bash
messagesctl pause    # stop issuing new autonomous tickets
messagesctl resume   # start again
```

`pause` takes effect immediately via a local marker (works offline) **and**
publishes an immutable `owner_decision` recording the pause. It deletes
nothing and modifies nothing: queued tickets, reports, receipts and escalations
stay exactly as they are. While paused, the reviewer leaves incoming reports
unacknowledged, so `resume` picks up with no lost work and no duplicate review.

Agents already holding a claim finish their ticket — pause stops *issuance*,
not work in flight.

---

## Command reference

```bash
messagesctl validate                    # full repository integrity check
messagesctl validate --diff-base <REF>  # + immutability against a git ref (CI)
messagesctl sync                        # pull, retry deferred pushes, replay spool
messagesctl rebuild-index               # regenerate state/ from append-only history

messagesctl publish-report --lane locomotion --unit 12U-C11P \
  --report /home/robojoe/code/brittle/rgl/reports/<REPORT>.md
messagesctl publish-ticket --lane locomotion --ticket <FILE>

messagesctl next-ticket --lane locomotion
messagesctl claim <MESSAGE_ID> --agent locomotion
messagesctl claim <MESSAGE_ID> --agent locomotion --renew
messagesctl complete <MESSAGE_ID> --report-id <REPORT_ID>
messagesctl block <MESSAGE_ID> --reason "<TEXT>"

messagesctl escalate --lane locomotion --unit <UNIT> --summary "<QUESTION>"
messagesctl resolve-escalation --id <MESSAGE_ID> --decision-file <FILE> \
  --authorized-action "<ACTION>" --scope "<SCOPE>"

messagesctl pause
messagesctl resume
messagesctl status
messagesctl open-escalations
messagesctl tail -n 20
messagesctl notify-test
```

Add `--json` to any command for machine-readable output.

## Tests

```bash
python3 -m unittest discover -s scripts/tests -v
```

Covers schema validation, duplicate IDs, immutable publication, report
mirroring without touching the source, SHA-256 verification, wrong-lane claim
rejection, leases and renewal, expired-lease reclamation, duplicate-reviewer
prevention, mocked OpenAI responses, malformed-response rejection,
low-confidence and hard-gate escalation, owner decisions, the Guard
communication/state distinction, failed-push spool preservation, concurrent
append retry, secret rejection, forbidden files, notification success and
failure, index rebuilding, and daemon-restart idempotency — plus a full
synthetic end-to-end run of the loop against a temporary bare remote.
