---
id: BRITTLE-20260728T121200Z-9f0e1d2c
kind: escalation
project: brittle
from: reviewer
to: joe
lane: locomotion
unit: 12U-C11P
created_at: 2026-07-28T12:12:00Z
source_commit: 841e285
local_source_path: null
local_source_sha256: null
in_reply_to: BRITTLE-20260728T120000Z-5566ddee
supersedes: null
requires_owner: true
confidence: 0.62
status: open
title: "Two materially different repairs for the ankle-roll instability -- which lane?"
---

# Owner decision required

**Question for Joe:** The report supports two materially different repairs —
lower `kd` to 0.2 control-side, or re-derive the armature and re-baseline the
plant. They have different blast radii. Which do you want pursued?

**Context.** Survival collapses at 4M steps on plant v2; ankle-roll P99 torque
sits at the clip for 31% of stance.

**Proposed next action (NOT issued).** Lower ankle-roll `kd` to 0.2 and re-run
the C7 verifier.

**Why this stopped:**

- reviewer set requires_owner
- confidence 0.62 < threshold 0.85
- hard gate(s): canonical-mutation

No executable ticket was published.

```bash
messagesctl resolve-escalation --id BRITTLE-20260728T121200Z-9f0e1d2c \
  --decision-file /path/to/decision.md \
  --authorized-action "<exact action>" --scope "<scope>"
```
