---
id: BRITTLE-20260728T101500Z-a1b2c3d4
kind: ticket
project: brittle
from: reviewer
to: locomotion
lane: locomotion
unit: 12U-C11P
created_at: 2026-07-28T10:15:00Z
source_commit: 841e285
local_source_path: null
local_source_sha256: null
in_reply_to: BRITTLE-20260728T100000Z-0011aabb
supersedes: null
requires_owner: false
confidence: 0.91
status: open
title: "Re-measure the C7 clean-parent collapse under the repaired ankle-roll law"
next_action: "Re-run the C7 verifier with kd=0.2 on plant v2 and report the survival delta"
---

# Re-measure the C7 clean-parent collapse under the repaired ankle-roll law

> Issued autonomously by the BRITTLE reviewer from report
> `BRITTLE-20260728T100000Z-0011aabb` at confidence 0.91 (threshold 0.85).

## Objective

Determine whether the C7 clean-parent collapse is the ankle-roll PD instability
rather than genuine plant incompatibility.

## Preconditions

- BRITTLE commit `841e285`, plant v2 (`81d4e6d8226ed12b`).
- Existing C7 verifier configuration, unmodified.

## Steps

1. Re-run the C7 verifier twice: once at the recorded `kd=1.0`, once at `kd=0.2`.
2. Hold every other flag identical; diff the flag sets and paste the diff.
3. Record survival, tracking error and P99 ankle-roll torque for both.

## Acceptance criteria

- Both runs complete with ≥16 evaluation episodes.
- The report states, with numbers, whether the collapse survives `kd=0.2`.

## Prohibitions

- Do not promote anything, move `latest`, or touch a policy card.
- Do not modify the plant.
- Do not overwrite the canonical reference.

## Report

Write the full report to `rgl/reports/`, mirror it with
`messagesctl publish-report`, then `messagesctl complete` this ticket.
