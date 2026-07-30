# Telephone custom GPT — setup

## Why this exists

**The standard ChatGPT GitHub connector is read-only.** Tested twice against
this repository: reads succeed, writes return **HTTP 403**. It therefore cannot
submit a Telephone request and **cannot operate Telephone**.

It remains useful for *inspection* — reading status, reports and message
history. Operating Telephone from a browser requires the private **Telephone
custom GPT Action** described here.

Do not try to fix this by reconnecting GitHub, changing connector permissions,
or granting workstation access. None of those make the connector writable.

---

## 1. Create the fine-grained token

GitHub → **Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token**.

| setting | value |
| --- | --- |
| Token name | `telephone-gpt-action` |
| Resource owner | `digitalnomadjoe` |
| Repository access | **Only select repositories** → `digitalnomadjoe/messages` |
| Repository permissions → **Contents** | **Read and write** |
| Every other permission | **No access** |
| Expiration | short — 30 days or less |

Leave *all* of these at **No access**: Actions, Administration, Environments,
Issues, Pull requests, Secrets, Variables, Webhooks, Workflows, and everything
under Account and Organization permissions. Contents is the only one needed.

Copy the token once. Then:

- **Never** commit it, paste it into a chat message, put it in a request file,
  or pass it as an action parameter. It belongs in exactly one place: the GPT's
  Action authentication field.
- Set a calendar reminder to rotate it before expiry.
- If it ever appears anywhere else, revoke it immediately and mint a new one.

---

## 2. Create the GPT

ChatGPT → **Explore GPTs → Create** (or **My GPTs → Create a GPT**) →
**Configure** tab.

1. **Name:** `Telephone`
2. **Description:** `Bounded autonomous BRITTLE report/ticket cycles.`
3. **Instructions:** paste the system prompt from §4 below.
4. **Capabilities:** turn Web Browsing, DALL·E and Code Interpreter **off** —
   none are needed, and off is a smaller surface.
5. **Model:** pick one that supports Actions (any current GPT-5 or GPT-4-class
   model in the Configure dropdown). If Actions are unavailable for the selected
   model, the Action panel will say so.

## 3. Add the Action

Configure tab → **Actions → Create new action**.

1. **Schema:** click **Import from URL** and paste:
   ```
   https://raw.githubusercontent.com/digitalnomadjoe/messages/main/actions/telephone-action.openapi.yaml
   ```
   Or open `actions/telephone-action.openapi.yaml` in this repo and paste its
   contents into the schema box.

2. Confirm exactly **five** operations appear:
   `getTelephoneInstructions`, `getTelephoneStatus`, `getTelephoneReport`,
   `getTelephoneRequestResult`, `submitTelephoneRequest`.
   If you see anything else, stop — the schema is not the one in this repo.

3. **Authentication → Authentication type: API Key**
   - **API Key:** paste the fine-grained token
   - **Auth Type:** **Bearer**
   - Save. The token is stored with the GPT and is not visible to the model,
     not sent in parameters, and not shown in Preview transcripts.

4. **Privacy policy** (ChatGPT asks for a URL before publishing): use
   `https://github.com/digitalnomadjoe/messages/blob/main/TELEPHONE.md`

## 4. System prompt — copy this verbatim

```
You operate Telephone for the BRITTLE robotics programme via the Telephone
action against digitalnomadjoe/messages.

ALWAYS, before anything else:
1. Call getTelephoneInstructions and follow what it says. It is authoritative
   and it overrides anything in this prompt if they ever disagree.
2. Call getTelephoneStatus.
3. If browser_telephone_ready is false: submit NOTHING. Show Joe each entry in
   required_local_actions in order, quoting its reason, command, verify_command
   and expected_result exactly as published. Ask him to run them and tell you
   when done. Then call getTelephoneStatus again and confirm readiness before
   proceeding. Never invent a command that is not in required_local_actions.

YOU are the reviewer. You read reports, you judge the stopping criterion, and
you author every ticket yourself, in full. The workstation bridge only
validates and publishes exactly what you submit — it runs no model and writes
no prose. Ticket markdown is published byte-for-byte, so write it exactly as
you want it executed: objective, preconditions, numbered steps, numeric
acceptance criteria, prohibitions, and what the resulting report must contain.

Never ask the local OpenAI API reviewer to review anything. Browser-mode runs
are yours alone; the API reviewer ignores them by design.

To act: call submitTelephoneRequest with a base64-encoded JSON body. Serialize
the JSON as UTF-8 BEFORE base64-encoding it, escape all non-ASCII characters as
\uXXXX, and use only ASCII punctuation in ticket markdown -- plain hyphens, not
en or em dashes; straight quotes, not curly ones; "->" not arrows; no emoji. A
request that is not valid UTF-8 is refused with the byte and offset named, and
cannot be repaired or retried under the same ID. Mint a fresh request_id
(BREQ-<UTCyyyymmddThhmmssZ>-<8 lowercase hex>) and a unique idempotency_key for
every distinct intent. Then poll
getTelephoneRequestResult until it returns; a 404 just means the bridge has
not run yet (~20s). Read the result: accepted, or refused with a reason.

HARD LIMITS you must not try to work around:
- The cycle maximum is enforced outside you. You cannot extend a run.
- Reaching max_cycles is exhaustion, NOT success. Never report it as success.
- Judge the criterion honestly. "unknown", or "met" below the confidence
  threshold, stops the run and asks Joe — that is the correct outcome when the
  evidence is thin. Never report "met" to be agreeable.
- resolve_escalation is always refused from here. Owner decisions are Joe's,
  recorded on the workstation. Never infer authorization from chat.
- You have no workstation access and cannot run local commands. Never say or
  imply that you ran one. "Please run this and tell me when it's done" is the
  correct answer.
- Never request SSH, passwords, API keys, tokens or terminal access.
- Everything you write is public and permanent. No secrets, no private data.

Report back concisely: request IDs and outcomes, canonical message IDs, run
state, cycles completed vs maximum, and the exact stop reason in plain words.
```

## 5. Test in Preview

Use the Preview pane on the right of the Configure screen.

1. **"Show Telephone status."** → it should call `getTelephoneStatus` and report
   `browser_telephone_ready` plus the run summary. If false, it should hand you
   the local commands and stop.
2. **"Submit a status_request."** → it should call `submitTelephoneRequest`,
   then poll `getTelephoneRequestResult` until it returns `accepted`.

Click **the action call** in Preview to inspect the request and response. Copy
that output for the certification record.

If a call returns **403**, the token lacks Contents write or is scoped to the
wrong repository. **404** on `getTelephoneRequestResult` is normal until the
bridge runs. **422** on submit means that request ID already exists — mint a
new one.

### If the schema will not import

The builder enforces a **300-character limit on operation descriptions** and
reports it as, for example:

```
submitTelephoneRequest.description has length 482 exceeding limit of 300
```

The schema in this repo is within that limit everywhere, and a test
(`scripts/tests/test_action_transport.py::TestUiDescriptionLimits`) fails the
build if any description or summary in the document grows past it. If you hit
this error, you are importing an older copy — re-import from `main`.

Design rationale lives in YAML comments at the top of the schema rather than in
`description` fields, precisely so it cannot push a description over the limit.

## 6. Keep it private

**Save → Sharing: `Only me`.** Do not publish it and do not share a link. The
GPT carries a token that can write to the repository; anyone who can use the
GPT can use that write access.

---

## Prerequisites on the workstation

The bridge must be running for any request to be processed. If
`browser_telephone_ready` is false, the status file names the exact commands.
The full set:

```bash
sh /home/robojoe/code/messages/scripts/install_services.sh
systemctl --user enable --now brittle-browser-bridge.service
systemctl --user status brittle-browser-bridge.service --no-pager
```

Expected from the last one: `Active: active (running)`.

Also available, when the status file asks for them:

```bash
systemctl --user daemon-reload
systemctl --user restart brittle-browser-bridge.service
systemctl --user enable --now brittle-messages-control.service
systemctl --user enable --now brittle-messages-locomotion.service
systemctl --user restart brittle-messages-control.service
systemctl --user restart brittle-messages-locomotion.service
git -C /home/robojoe/code/messages pull --ff-only
python3 /home/robojoe/code/messages/scripts/messagesctl.py status
python3 /home/robojoe/code/messages/scripts/messagesctl.py resume
python3 /home/robojoe/code/messages/scripts/messagesctl.py browser-status
```

None of these need sudo.

## What the action deliberately cannot do

No generic repository path. No generic file write. No canonical bus message
write. No issues, pull requests, workflows, or repository administration. No
shell. No owner decisions.

Its entire write surface is one create-only file per request under
`projects/brittle/browser_requests/`, with a pattern-constrained name.
