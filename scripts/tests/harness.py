"""Shared fixtures for the BRITTLE message-bus tests.

Every test runs against a real git repository with a real bare remote, so push
rejection, fast-forward pulls and spooling are exercised for real rather than
mocked.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import messagelib as ml  # noqa: E402

SCAFFOLD_DIRS = (
    f"{ml.DIR_TICKETS}/locomotion",
    f"{ml.DIR_TICKETS}/control",
    f"{ml.DIR_REPORTS}/locomotion",
    f"{ml.DIR_REPORTS}/control",
    ml.DIR_REVIEWS,
    ml.DIR_RECEIPTS,
    ml.DIR_ESC_OPEN,
    ml.DIR_ESC_RESOLVED,
    ml.DIR_DECISIONS,
    ml.DIR_STATE,
)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=test@example.com",
         "-c", "user.name=test", *args],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout


class BusTestCase(unittest.TestCase):
    """A temp bus: bare remote + working clone + isolated operator state."""

    def setUp(self) -> None:
        import logging

        logging.disable(logging.CRITICAL)  # daemons log intentionally; keep runs quiet
        self.addCleanup(logging.disable, logging.NOTSET)

        self.tmp = Path(tempfile.mkdtemp(prefix="brittle-bus-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self._env_backup = dict(os.environ)
        self.addCleanup(self._restore_env)
        os.environ["BRITTLE_MESSAGES_STATE"] = str(self.tmp / "state")
        os.environ.pop("BRITTLE_REVIEWER_MOCK", None)
        os.environ.pop("OPENAI_API_KEY", None)

        self.remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)],
                       capture_output=True, check=True)

        self.repo_path = self._make_clone("messages")
        self.repo = ml.Repo(self.repo_path)

        self.brittle = self.tmp / "brittle"
        (self.brittle / "rgl" / "reports").mkdir(parents=True)
        subprocess.run(["git", "init", "-b", "main", str(self.brittle)],
                       capture_output=True, check=True)
        (self.brittle / "README.md").write_text("brittle\n", encoding="utf-8")
        _git(self.brittle, "add", "README.md")
        _git(self.brittle, "commit", "-m", "init")

        self.cfg = {
            "repo": {"path": str(self.repo_path), "brittle_path": str(self.brittle)},
            "reviewer": {
                "model": "test-model",
                "prompt_path": str(self.repo_path / "prompts" / "brittle-reviewer.md"),
                "poll_seconds": 1,
                "minimum_confidence": 0.85,
                "max_report_chars": 24000,
            },
            "openai": {"api_key_env": "OPENAI_API_KEY", "api_key_file": "",
                       "base_url": "https://api.openai.com/v1"},
            "notification": {"command": "", "timeout_seconds": 5},
            "claims": {"lease_seconds": 2700},
            "safety": {"private_patterns": []},
            # A properly configured system: the spend guard is armed and priced,
            # so tests exercise the real gate order rather than tripping over an
            # unpriced model. Per-test ledger, generous caps.
            "spending": {
                "monthly_cap_usd": 100.0,
                "daily_cap_usd": 100.0,
                "max_calls_per_day": 1000,
                "max_completion_tokens": 1000,
                "ledger_path": str(self.tmp / "spend_ledger.jsonl"),
                "pricing_input_usd_per_1m": {"test-model": 2.50},
                "pricing_output_usd_per_1m": {"test-model": 10.00},
            },
            "_config_path": str(self.tmp / "config.toml"),
        }

    def _restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _make_clone(self, name: str) -> Path:
        path = self.tmp / name
        subprocess.run(["git", "clone", str(self.remote), str(path)],
                       capture_output=True, check=True)
        _git(path, "checkout", "-B", "main")
        for d in SCAFFOLD_DIRS:
            (path / d).mkdir(parents=True, exist_ok=True)
            (path / d / ".gitkeep").write_text("", encoding="utf-8")
        (path / "prompts").mkdir(exist_ok=True)
        (path / "prompts" / "brittle-reviewer.md").write_text(
            "# reviewer\nBe skeptical. Return the structured object.\n", encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-m", "scaffold")
        _git(path, "push", "-u", "origin", "main")
        return path

    def second_clone(self) -> ml.Repo:
        """A competing agent's checkout of the same bus."""
        path = self.tmp / "messages-2"
        subprocess.run(["git", "clone", str(self.remote), str(path)],
                       capture_output=True, check=True)
        cfg = json.loads(json.dumps(self.cfg))
        cfg["repo"]["path"] = str(path)
        return ml.Repo(path), cfg

    # --- convenience publishers ------------------------------------------

    def write_local_report(self, name: str = "SYNTHETIC_REPORT.md",
                           text: str | None = None) -> Path:
        path = self.brittle / "rgl" / "reports" / name
        path.write_text(text or SYNTHETIC_REPORT, encoding="utf-8")
        return path

    def publish_report(self, lane: str = "locomotion", unit: str = "12U-SYNTH",
                       local: Path | None = None, cfg: dict | None = None) -> str:
        cfg = cfg or self.cfg
        local = local or self.write_local_report()
        sha = ml.sha256_file(local)
        fm = ml.base_frontmatter(
            "report", sender=lane, to="reviewer", lane=lane, unit=unit,
            status="open", source_commit=ml.brittle_commit(self.brittle),
            local_source_path=str(local), local_source_sha256=sha,
        )
        fm["title"] = local.stem
        body = f"{ml.MIRROR_MARKER}\n\n" + local.read_text(encoding="utf-8")
        rel = f"{ml.DIR_REPORTS}/{lane}/{fm['id']}.md"
        ml.publish(ml.Repo(cfg["repo"]["path"]), {rel: ml.render_message(fm, body)},
                   f"report({lane}): synthetic [{fm['id']}]", cfg=cfg)
        return fm["id"]

    def publish_ticket(self, lane: str = "locomotion", unit: str = "12U-SYNTH",
                       title: str = "Synthetic ticket",
                       in_reply_to: str | None = None) -> str:
        fm = ml.base_frontmatter(
            "ticket", sender="reviewer", to=lane, lane=lane, unit=unit,
            status="open", in_reply_to=in_reply_to,
            source_commit=ml.brittle_commit(self.brittle),
        )
        fm["title"] = title
        rel = f"{ml.DIR_TICKETS}/{lane}/{fm['id']}.md"
        ml.publish(self.repo, {rel: ml.render_message(fm, f"# {title}\n\nDo the thing.\n")},
                   f"ticket({lane}): {title} [{fm['id']}]", cfg=self.cfg)
        return fm["id"]

    def msgs(self) -> dict:
        return ml.load_messages(self.repo_path)

    def assertValid(self) -> None:
        problems = ml.validate_repo(self.repo_path,
                                    private_patterns=self.cfg["safety"]["private_patterns"])
        self.assertEqual(problems, [], f"repository invalid: {problems}")


SYNTHETIC_REPORT = """# SYNTHETIC — 12U-SYNTH smoke

**status:** synthetic test artifact, not a real BRITTLE result
**decision:** none
**key result:** survival 812 steps over 16 held-out episodes, seed-averaged
**next action:** re-measure with the repaired control law
**report path:** synthetic

## Numbers

| metric | value |
| --- | --- |
| survival (steps) | 812 |
| tracking RMSE (mm) | 11.4 |
| ankle-roll P99 torque (Nm) | 8.9 |

This file exists only to exercise the message bus. It describes no real run.
"""


def mock_response(tmp: Path, **overrides) -> str:
    """Write a canned reviewer response and return its path."""
    payload = {
        "summary": "Survival 812 steps at 11.4 mm RMSE; the control law repair holds.",
        "target_lane": "locomotion",
        "next_action": "Re-run the smoke at three seeds and report the spread.",
        "ticket_title": "Three-seed repeat of the 12U-SYNTH smoke",
        "ticket_markdown": (
            "## Objective\nRepeat the smoke at three seeds.\n\n"
            "## Steps\n1. Run seeds 0, 1, 2.\n2. Report the spread.\n\n"
            "## Acceptance criteria\nAll three seeds complete 16 episodes.\n"
        ),
        "requires_owner": False,
        "owner_question": None,
        "confidence": 0.93,
        "reasoning_summary": "Single-seed result; a three-seed repeat is the "
                             "standard next step and needs no owner input.",
    }
    payload.update(overrides)
    path = tmp / f"mock-{ml.secrets.token_hex(4)}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def raw_mock(tmp: Path, raw: str) -> str:
    path = tmp / f"rawmock-{ml.secrets.token_hex(4)}.json"
    path.write_text(raw, encoding="utf-8")
    return str(path)


def hours(n: float) -> _dt.timedelta:
    return _dt.timedelta(hours=n)
