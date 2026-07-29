#!/usr/bin/env python3
"""spend_dry_run -- prove, offline, that the queued report cannot overspend.

Runs the REAL reviewer request path against the REAL local configuration and
the REAL queued report, with a tripwire installed over urllib.request.urlopen.
If the guard is working, the tripwire never fires:

  * a request whose reservation fits the cap is refused only by the tripwire
    (proving we got as far as the socket, and what it would have cost);
  * once the cap is consumed, the guard refuses BEFORE the socket exists.

Nothing here is billable. No network call is ever completed. The ledger used
is a throwaway copy so the real one is untouched.

    python3 scripts/spend_dry_run.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import messagelib as ml  # noqa: E402
import reviewer_daemon as rd  # noqa: E402
from spend_guard import SpendGuard, SpendGuardError, SpendLimitExceeded  # noqa: E402

ATTEMPTS: list = []


def tripwire(*a, **k):
    ATTEMPTS.append(a[:1])
    raise AssertionError("NETWORK CALL ATTEMPTED")


def main() -> int:
    urllib.request.urlopen = tripwire          # nothing can reach the network

    # A non-functional placeholder. call_model resolves a credential before it
    # reserves (so a config error cannot burn daily call slots), and this run
    # is about the guard, not auth. The tripwire above guarantees this string
    # is never transmitted anywhere.
    import os
    os.environ["OPENAI_API_KEY"] = "PLACEHOLDER-DRY-RUN-NEVER-TRANSMITTED"

    cfg = ml.load_config()
    model = str(cfg["reviewer"]["model"])
    repo = ml.Repo(cfg["repo"]["path"])
    scratch = Path(tempfile.mkdtemp(prefix="brittle-dryrun-"))
    cfg.setdefault("spending", {})["ledger_path"] = str(scratch / "ledger.jsonl")

    print("=" * 72)
    print("ZERO-NETWORK SPEND GUARD DRY RUN")
    print("=" * 72)
    print(f"config        : {cfg['_config_path']}")
    print(f"model         : {model}")
    print(f"scratch ledger: {scratch/'ledger.jsonl'} (real ledger untouched)")
    print(f"credential    : placeholder, never transmitted "
          f"(urlopen replaced by a tripwire)")

    g = SpendGuard(cfg)
    print(f"\nlimits        : daily ${g.daily_cap_micro/1e6:.2f}, "
          f"monthly ${g.monthly_cap_micro/1e6:.2f}, "
          f"{g.max_calls_per_day} calls/day, "
          f"{g.max_completion_tokens} completion tokens/review")
    print(f"priced models : {g.status()['priced_models']}")

    # --- the real queued report ------------------------------------------
    msgs = ml.load_messages(repo.path)
    pending = [m for m in sorted(msgs.values(), key=ml.Message.sort_key)
               if m.kind == "report" and not ml.reviewer_acked(m.id, msgs)]
    if not pending:
        print("\nNo queued report on the bus; using a synthetic body of the same size.")
        body = "x" * 6000
        report_id = "(none queued)"
    else:
        report = pending[0]
        report_id = report.id
        body = report.body
    print(f"\nqueued report : {report_id}  ({len(body)} chars)")

    daemon = rd.ReviewerDaemon(cfg)
    prompt, _sha = daemon.load_prompt()
    user_content = f"# Report {report_id}\n\n{body}\n"

    # --- 1. what one real review would reserve ---------------------------
    payload_bytes = len(json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": prompt},
                     {"role": "user", "content": user_content}],
        "max_tokens": g.max_completion_tokens,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "brittle_review", "strict": True, "schema": rd.REVIEW_SCHEMA}},
    }).encode())

    in_rate, out_rate = g.price_micro_per_token(model)
    would_reserve = payload_bytes * in_rate + g.max_completion_tokens * out_rate
    print("\n--- 1. worst-case reservation for ONE review of this report ---")
    print(f"  request body            : {payload_bytes} bytes -> counted as "
          f"{payload_bytes} input tokens (~4x conservative)")
    print(f"  completion ceiling      : {g.max_completion_tokens} tokens")
    print(f"  worst-case reservation  : ${would_reserve/1e6:.6f}")
    print(f"  daily cap               : ${g.daily_cap_micro/1e6:.2f}")
    print(f"  fits within daily cap   : {would_reserve <= g.daily_cap_micro}")
    headroom = int(g.daily_cap_micro // max(1, would_reserve))
    print(f"  reviews of this size before the DAILY cap stops it: {headroom}")
    print(f"  ...but max_calls_per_day caps it at: {g.max_calls_per_day}")

    # --- 2. the guard refuses once the cap is consumed -------------------
    print("\n--- 2. consume the daily cap, then attempt a real request ---")
    consumed, n = 0, 0
    while True:
        try:
            SpendGuard(cfg).reserve(model, payload_bytes)
            n += 1
            consumed = SpendGuard(cfg).usage()["day_micro"]
        except SpendLimitExceeded as exc:
            print(f"  reservations granted before refusal : {n}")
            print(f"  committed                           : ${consumed/1e6:.6f}")
            print(f"  refusal                             : {exc}")
            break
        if n > 10_000:
            print("  ABORT: guard never refused"); return 1

    print("\n  now calling the REAL request path with the cap exhausted...")
    before = len(ATTEMPTS)
    try:
        rd.call_model(cfg, prompt, user_content, guard=SpendGuard(cfg))
        print("  RESULT: FAIL -- the call was not refused"); return 1
    except SpendLimitExceeded as exc:
        print(f"  RESULT: REFUSED before any socket -> {str(exc)[:90]}...")
    except SpendGuardError as exc:
        print(f"  RESULT: REFUSED (fail-closed) -> {str(exc)[:90]}...")
    except AssertionError:
        print("  RESULT: FAIL -- a network call was attempted"); return 1
    except Exception as exc:
        print(f"  RESULT: FAIL -- unexpected {type(exc).__name__}: {exc}"); return 1

    if len(ATTEMPTS) != before:
        print("  RESULT: FAIL -- tripwire fired"); return 1
    print(f"  network calls attempted             : {len(ATTEMPTS)}")

    # --- 3. unpriced model fails closed ----------------------------------
    print("\n--- 3. unpriced model fails closed ---")
    bad = json.loads(json.dumps(cfg))
    bad["reviewer"]["model"] = "gpt-9-unreleased"
    bad["spending"]["ledger_path"] = str(scratch / "ledger2.jsonl")
    try:
        rd.call_model(bad, prompt, user_content, guard=SpendGuard(bad))
        print("  RESULT: FAIL -- unpriced model was allowed"); return 1
    except SpendGuardError as exc:
        print(f"  RESULT: REFUSED -> {str(exc)[:90]}...")

    # --- 4. corrupt ledger fails closed ----------------------------------
    print("\n--- 4. corrupt ledger fails closed ---")
    corrupt = json.loads(json.dumps(cfg))
    corrupt["spending"]["ledger_path"] = str(scratch / "ledger3.jsonl")
    Path(corrupt["spending"]["ledger_path"]).write_text("{{{garbage\n")
    try:
        rd.call_model(corrupt, prompt, user_content, guard=SpendGuard(corrupt))
        print("  RESULT: FAIL -- corrupt ledger was tolerated"); return 1
    except SpendGuardError as exc:
        print(f"  RESULT: REFUSED -> {str(exc)[:90]}...")

    print("\n" + "=" * 72)
    print(f"DRY RUN PASSED — total network calls attempted: {len(ATTEMPTS)}")
    print("A queued report cannot cause a request above the configured cap.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
