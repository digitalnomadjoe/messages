---
id: BRITTLE-20260808T142359Z-1bccdaed
kind: report
project: brittle
from: locomotion
to: reviewer
lane: locomotion
unit: 12U-C11S15CY
created_at: "2026-08-08T14:23:59.968177Z"
source_commit: fbf7544
local_source_path: /home/robojoe/code/brittle/rgl/reports/UNIFIED_LOCOMOTION_PHASE12UC11S15CY_AUTHORIZATION_20260808T050000Z.md
local_source_sha256: 51eb3a40e1088728f0e30dd6139ff53eee898ce7e36f439c36b001c1ae5c470f
in_reply_to: null
supersedes: null
requires_owner: false
confidence: null
status: open
title: UNIFIED_LOCOMOTION_PHASE12UC11S15CY_AUTHORIZATION_20260808T050000Z
mirror_bytes: 10141
---
# UNIFIED_LOCOMOTION_PHASE12UC11S15CY_AUTHORIZATION_20260808T050000Z

| field | value |
| --- | --- |
| local path | `/home/robojoe/code/brittle/rgl/reports/UNIFIED_LOCOMOTION_PHASE12UC11S15CY_AUTHORIZATION_20260808T050000Z.md` |
| sha256 | `51eb3a40e1088728f0e30dd6139ff53eee898ce7e36f439c36b001c1ae5c470f` |
| brittle commit | `fbf7544` |
| lane | locomotion |
| unit | 12U-C11S15CY |
| mirrored at | 2026-08-08T14:23:59.968177Z |

<!-- brittle:mirrored-content -->

# 12U-C11S15CY — Authorization + Evidence Integrity Closure: **SPEED/CADENCE AUTHORIZATION PACKAGE READY**

**date:** 2026-08-08 · **unit:** 12U-C11S15CY · **lane:** locomotion · **NO PPO · NO Guard mutation · quarantined checkpoint never evaluated**
**constitution:** C1, C2, C3, C5, C6, C7, C9 · **potential violations:** none · **amendment proposed:** no
**return:** **`SPEED/CADENCE AUTHORIZATION PACKAGE READY`**
**changelog:** row 99 · post-append csv `sha256_16` **`3e12e70089cc92f4`**

| artifact | sha256_16 |
|---|---|
| **launcher `launch_acquisition.sh`** | **`9f6fda4b286f6e50`** |
| trainer `rl/mjw/train_tip_reftrack.py` | `b3d1329765f15675` (unchanged — the ticket pins it) |
| estimator `rl/bridge/path_speed_v1.py` | `4127331df42baa8e` |
| member manifest | `dca42548ae633d3e` |
| `Mobs_v1.npy` / `Mact.npy` | `976c327503a092c3` / `23289b73d9b23faa` |

---

## 0. Disclosure, stated plainly

- **One unauthorized 442,368-step PPO run already occurred** in this arc.
- **Its resolved configuration matches the intended acquisition configuration.**
- **Its outputs remain quarantined and have never been evaluated, replayed, compared or used** —
  including by this unit, which touched that directory only to correct its README and hash it.
- **The authorized future run is therefore a fresh rerun, not the discovery of an unknown
  execution path.** Nothing about it is exploratory; the path is known and now interlocked.

## 1. Accidental execution is now impossible

The launcher **refuses by default**. `TC_MODE` is a finite enum and `run` carries a second,
control-plane-backed interlock. All five negatives executed (`proofs/item1_negative_tests.log`):

| test | exit | refusal |
|---|---|---|
| `bash launch_acquisition.sh` (bare) | **2** | `REFUSED: no mode. This launcher never runs by default.` |
| `TC_MODE=train` | **2** | `REFUSED: unknown TC_MODE 'train'. Allowed: dry \| supervision-test \| run` |
| `TC_MODE=run`, no token | **2** | requires `TC_RUN_TOKEN` = the launcher's own sha256_16 |
| `TC_MODE=run` + token, no grant | **2** | requires `TC_GRANT_ID=<owner decision id>` |
| **`TC_MODE=run` + token + the existing broad grant `D-ed52647cd278`** | **2** | `subject_refs ['12U-C11S15','bound-construction-c11s4-walking'] do not name launcher-c11s15tc-acquisition — a grant scoped only to the construction does NOT authorize this launcher` |

No `ppo_*.zip` was produced by any of them.

### ★ The silent-sufficiency hazard is real, and measured

`D-ed52647cd278` is an **ACTIVE owner `ppo` grant** with `subject_refs = ["12U-C11S15",
"bound-construction-c11s4-walking"]`. Guard resolves a grant by *(action, ACTIVE unit, declared
subjects)*. **The moment this launcher becomes a member of that construction, a
`run guarded --unit 12U-C11S15 --subject bound-construction-c11s4-walking` would resolve to
`D-ed52647cd278` and ALLOW** — canonical registration would silently have become sufficient for
PPO. Two independent defences are in place and both are needed: the launcher's own interlock
(demonstrated above), and running under unit `12U-C11S15CY`, since Guard's own CP-07 rule is that
authority does not carry across units.

⚠ **An owner ruling is required and I stopped for it.** Whether `D-ed52647cd278` — and the other
eight ACTIVE construction-scoped `ppo` grants — should be **superseded** rather than merely
out-scoped is Joe's call. The supported mechanism is a new decision in the same thread
(`permits.authorization_reasons` then reports the old one as superseded evidence, not a live
grant). **I did not prepare that supersession: narrowing an existing owner grant is an owner
decision.**

## 2. Guard staging — the actual schema

`proofs/item2_targets_three_records.json`, **validated field-by-field against
`rl/guard/schemas/targets.schema.json`**: `schema_version: 1`, three complete records, no field
outside the schema, `target_id` patterns and `hash.algo` enums checked.

| # | target_id | role | value |
|---|---|---|---|
| 1 | `trainer-source-c11s4` | `instrument` | `b3d1329765f15675` |
| 2 | `launcher-c11s15tc-acquisition` | `instrument` | `9f6fda4b286f6e50` |
| 3 | `bound-construction-c11s4-walking` | `construction` | complete record, **all 47 member ids explicitly present** |

⚠ **My previous staging was invented and is withdrawn.** `target register` takes
`--unit --targets-file [--authorized-by]` — there is no `--target-id`, no `--add-member`, and the
role enum contains **no `launcher`** (`plant/reference/episode/family/policy/pointer/instrument/
construction`). The construction record must be complete, not incremental.

`proofs/item2_authority_sequence.txt` holds the two-decision sequence: **Decision A**
(`canonical_mutation`, subjects = the three target ids, bracketed by the
ACQUISITION → POST_ACQUISITION_AUDIT transitions) and **Decision B** (`ppo`, subjects **must**
include `launcher-c11s15tc-acquisition`). They are deliberately separate and B is not implied by A.

## 3. Discriminating receipts

**Plant** — `--expect-plant 81d4e6d8226ed12b --expect-plant-complete c44ad73fbd8b6f45` are now
passed and the run's own resolved plant is checked against them. `out_dry/parity_receipt.json`
verdict **OK**, carrying law + plant + timing + env:

```
law   kp20kd2ap1/ASO1.0/ry20kd1/tau12.5/acd10     ankle-roll kd [0.2, 0.2]
plant sha1 81d4e6d8226ed12b == expect 81d4e6d8226ed12b
timing policy 50 Hz / pd 500 / physics 500 / dt 0.002 / 10 substeps
env   nconmax declared 64, VERIFIED 64 (read back as naconmax 147456 / nworld 2304)
      njmax declared 128, NOT verified — the trainer persists no njmax anywhere; recorded as
        DECLARED BY THE CARD, not read back
      rsi {frac 0.0, path null, mirror 0.0}  et_no_step_s 4.5
      yaw_cmd_range [0.0, 0.0]  yaw_cmd 0.0  yaw_sigma 0.25  reset_roll 2.5
```

**Kd is compared numerically**, element-by-element against the trainer's resolved `kd_mult`, with
an explicit assertion that indices 5 and 11 (ankle-roll) equal **0.2** on both sides.
⚠ The parity gate **fired once on my own rounding** — `kp_mult` is stored to 6 decimals, so
`30 × 0.666667 = 20.00001` against a manifest `20.0`. Fixed with a 1e-4 tolerance, which is
5e-4 % of kp and cannot hide any real law difference (kp 20 vs 30 is 50 %). It fired correctly;
the comparison was too literal, not the law.

**Inspector** — receipts are required **after actual viewer initialisation**; an empty-watch-loop
line is explicitly insufficient for the preflight, which demands all three of
`EXACT-SYMMETRY matrices Mobs_v1.npy / Mact.npy`, `RELOADED_CHECKPOINT`, `MANIFEST PARITY OK`,
plus `episode start`. Cleanup is now an `EXIT` trap and every `wait` is bracketed by `set +e`, so
`set -e` can no longer skip it; the trap closes the exact PIDs on any exit path. Trainer failure
closes the exact spectator PID; spectator failure terminates the trainer (both re-verified at
exit 5 in `proofs/item3_supervision.log`).

**g_v1** — the retained **real-state action battery** is graded automatically
(`proofs/gv1_battery_grade.py` → Red's `c2_gv1_oracle.py --compare`):

```
rows compared     : 22 / 22
worst per-dim dev : 6.291e-07   (tol 2.0e-05; noise floor 9.5e-07)
VERDICT: ACCEPT
```

★ **The equivariance residual is explicitly not used as proof.** It is 0.000e+00 for *any*
involution, including a wrong one — it shows the wrapper is self-consistent, not that `Mobs` is
correct. The chain is: wrapper **identity** (the trainer's own step-zero actions matched this
reconstruction at 0.000e+00, with bare `f` differing by 0.426) + **semantics** on 22 real rollout
states against an independent oracle. The trainer is not modified to do the grading, because this
package pins it at `b3d1329765f15675`.

**Dry verification** through the real launcher: exit 0, `===== NO OPTIMIZER STEP =====`, no
`ppo_*.zip`, and **100.0000 % cyclic sampling** (147456/147456) with the ±2 anchors at exactly
±0.3060 obs45.

## 4. Evidence integrity

**Quarantine README root cause corrected.** The earlier wording said the flag reached the command
line but the early exit did not fire. That was wrong. The correct cause: **the `sed` edit removed
the line-continuation backslash, the shell terminated the trainer command early and parsed
`--dry-proof-out` as a separate command (`--dry-proof-out: command not found`), so the trainer
never received the flag at all** and no early exit was ever possible.

- `SHA256SUMS` added and committed for **all 19** retained incident files; `sha256sum -c` verifies.
- Quarantined checkpoint hash recorded explicitly in `QUARANTINED_CHECKPOINT.txt` and in
  `SHA256SUMS`: `ppo_442368.zip` = `524e30b9b7c42b2a…`.
- **Renamed banked fixtures are labelled.** Any `inspector_preflight/ppo_*.zip` is a byte-for-byte
  copy of the banked actor renamed so the watch loop loads it; a `FIXTURE_README.txt` sits beside
  it and the launcher now writes that file itself whenever it stages one. Zero training steps —
  never to be read as a trained `ppo_442368`.
- Stale root `tc_dryproof.json` renamed **`SUPERSEDED_tc_dryproof_root.json`** and excluded from
  the index; the authoritative telemetry is `out_dry/tc_dryproof.json`.
- **`EVIDENCE_INDEX.md`** is the authoritative index and lists only current receipts, with an
  explicit *NOT evidence* section for the quarantine, the superseded dry proof, the synthetic
  fixtures and the scratch output dirs from superseded card revisions.

## 5. Artifacts

`rgl/artifacts/c11s15tc_contract_20260807T230000Z/` — `EVIDENCE_INDEX.md`,
`launch_acquisition.sh` `9f6fda4b286f6e50`, `proofs/` (item1 negatives + dry, item2 targets +
authority, item2 tamper, item3 supervision + g_v1 grade/actions/oracle, item4 guard + norecord,
item5 reward parity, `gv1_battery_grade.py`), `out_dry/` (tc_dryproof.json, parity_receipt.json,
resolved_config.json, train.log; no checkpoint), and the preserved quarantine.

**No PPO. No Guard mutation. Nothing promoted, banked or made canonical. The quarantined
checkpoint was never evaluated or replayed. DFL thresholds, the banked actor, `latest.txt` and the
historical reference families are untouched.**
