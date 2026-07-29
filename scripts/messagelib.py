#!/usr/bin/env python3
"""messagelib -- shared primitives for the BRITTLE GitHub message bus.

Stdlib-only (Python 3.10+).  Third-party imports are deliberately absent so
that validation runs anywhere -- CI, a bare systemd unit, a rescue shell --
without a virtualenv.

Every gate lives here exactly once.  messagesctl, reviewer_daemon and the
GitHub Action all call these same functions, so a rule can never drift
between entry points.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

PROJECT = "brittle"

KINDS = ("ticket", "report", "review", "receipt", "escalation", "owner_decision")
LANES = ("locomotion", "control", "reviewer", "joe")
AGENT_LANES = ("locomotion", "control")
STATUSES = (
    "open",
    "claimed",
    "completed",
    "blocked",
    "acknowledged",
    "superseded",
    "resolved",
)
RECEIPT_TYPES = (
    "claim",
    "renew",
    "reclaim",
    "complete",
    "block",
    "reviewer_ack",
    "escalation_notice",
    "escalation_resolved",
)

PROJECT_ROOT = f"projects/{PROJECT}"
DIR_TICKETS = f"{PROJECT_ROOT}/tickets"
DIR_REPORTS = f"{PROJECT_ROOT}/reports"
DIR_REVIEWS = f"{PROJECT_ROOT}/reviews"
DIR_RECEIPTS = f"{PROJECT_ROOT}/receipts"
DIR_ESCALATIONS = f"{PROJECT_ROOT}/escalations"
DIR_ESC_OPEN = f"{DIR_ESCALATIONS}/open"
DIR_ESC_RESOLVED = f"{DIR_ESCALATIONS}/resolved"
DIR_DECISIONS = f"{PROJECT_ROOT}/decisions"
DIR_STATE = f"{PROJECT_ROOT}/state"

# Directories whose contents are immutable append-only messages.
MESSAGE_DIRS = (
    DIR_TICKETS,
    DIR_REPORTS,
    DIR_REVIEWS,
    DIR_RECEIPTS,
    DIR_ESCALATIONS,
    DIR_DECISIONS,
)

INDEX_PATH = f"{DIR_STATE}/index.json"

# --- file policy ----------------------------------------------------------

# Message payloads: communication formats only.
ALLOWED_EXT_MESSAGES = {".md", ".json"}

# Whole-repo allow-list (tooling, docs, CI, units).
ALLOWED_EXT_REPO = {
    ".md",
    ".json",
    ".py",
    ".sh",
    ".toml",
    ".yml",
    ".yaml",
    ".txt",
    ".service",
    ".timer",
    ".example",
    ".cfg",
    "",  # LICENSE, .gitignore handled by name
}
ALLOWED_BARE_NAMES = {".gitignore", ".gitattributes", ".gitkeep", "LICENSE", "NOTICE"}

# Explicitly named so rejections produce an actionable message.
FORBIDDEN_EXT = {
    ".npz", ".npy", ".pt", ".pth", ".ckpt", ".safetensors", ".h5", ".pkl",
    ".pickle", ".onnx", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif", ".png", ".jpg", ".jpeg",
    ".bmp", ".tiff", ".svg", ".wav", ".mp3", ".ogg", ".log", ".env", ".pem",
    ".key", ".p12", ".pfx", ".crt", ".der", ".so", ".dylib", ".dll", ".bin",
    ".exe", ".db", ".sqlite", ".parquet", ".usd", ".usda", ".usdc", ".xml",
    ".stl", ".obj", ".dae", ".mjcf", ".urdf",
}

MAX_MESSAGE_BYTES = 256 * 1024
MAX_REPO_FILE_BYTES = 512 * 1024

DEFAULT_LEASE_SECONDS = 45 * 60
DEFAULT_MIN_CONFIDENCE = 0.85

# --- frontmatter ----------------------------------------------------------

REQUIRED_FIELDS = (
    "id",
    "kind",
    "project",
    "from",
    "to",
    "lane",
    "unit",
    "created_at",
    "source_commit",
    "local_source_path",
    "local_source_sha256",
    "in_reply_to",
    "supersedes",
    "requires_owner",
    "confidence",
    "status",
)

# Required *and* may not be null.
NON_NULL_FIELDS = (
    "id",
    "kind",
    "project",
    "from",
    "to",
    "lane",
    "created_at",
    "requires_owner",
    "status",
)

# Whitelisted kind-specific extensions.  Anything outside REQUIRED_FIELDS |
# OPTIONAL_FIELDS is rejected, which keeps a public repo tight.
OPTIONAL_FIELDS = (
    "title",
    "receipt_type",
    "agent",
    "claimed_at",
    "lease_expires_at",
    "brittle_commit",
    "report_id",
    "ticket_id",
    "review_of",
    "escalation_id",
    "decision_id",
    "target_lane",
    "reason",
    "notification_status",
    "notification_detail",
    "authorized_action",
    "scope",
    "expires_at",
    "checksum",
    "autonomy",
    "reviewer_model",
    "prompt_sha256",
    "redacted",
    "truncated",
    "mirror_bytes",
    "next_action",
)

ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

ID_RE = re.compile(r"^BRITTLE-\d{8}T\d{6}Z-[0-9a-f]{8}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")

MIRROR_MARKER = "<!-- brittle:mirrored-content -->"


class MessageError(RuntimeError):
    """Any protocol / policy violation.  Always fail closed."""


# --------------------------------------------------------------------------
# Time + identity
# --------------------------------------------------------------------------


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def iso(dt: _dt.datetime) -> str:
    """RFC3339 UTC with millisecond precision.

    Sub-second resolution is load-bearing, not cosmetic: (created_at, id) is the
    total order the whole bus sorts by. At second resolution two messages written
    in the same second would tie-break on a random id suffix, which could put
    reports out of authorship order for the reviewer, or invert a renew/reclaim
    fold. Milliseconds make the order match what actually happened.
    """
    dt = dt.astimezone(_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def parse_iso(text: str) -> _dt.datetime:
    if not text or not TS_RE.match(text):
        raise MessageError(f"bad RFC3339 UTC timestamp: {text!r}")
    body = str(text)[:-1]
    micro = 0
    if "." in body:
        body, frac = body.split(".", 1)
        micro = int((frac + "000000")[:6])
    return _dt.datetime.strptime(body, "%Y-%m-%dT%H:%M:%S").replace(
        microsecond=micro, tzinfo=_dt.timezone.utc
    )


def new_id(now: _dt.datetime | None = None) -> str:
    """BRITTLE-<UTC timestamp>-<8-char random suffix>."""
    now = now or utc_now()
    return f"BRITTLE-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# Minimal strict YAML frontmatter (flat scalars only -- no nesting, no lists)
# --------------------------------------------------------------------------


# Deliberately strict. A git short-SHA such as `841e285` is valid float syntax
# in Python ("8.41e+287"), so exponent-without-a-decimal-point is NOT treated as
# a number here. A float must carry an explicit decimal point.
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+([eE][+-]?\d+)?$")


def _parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw == "" or raw in ("null", "~"):
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        inner = raw[1:-1]
        if raw[0] == '"':
            inner = (
                inner.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
            )
        return inner
    if raw in ("true", "True"):
        return True
    if raw in ("false", "False"):
        return False
    if _INT_RE.match(raw):
        return int(raw)
    if _FLOAT_RE.match(raw):
        return float(raw)
    return raw


_NEEDS_QUOTE = re.compile(r'^\s|\s$|[:#"\n\']|^$|^[\[\{\-&*!|>%@`]')


def _emit_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    looks_typed = (
        text in ("true", "false", "null", "~", "True", "False")
        or bool(_INT_RE.match(text))
        or bool(_FLOAT_RE.match(text))
    )
    if _NEEDS_QUOTE.search(text) or looks_typed:
        escaped = (
            text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        )
        return f'"{escaped}"'
    return text


def parse_message(text: str) -> tuple[dict, str]:
    """Split a message file into (frontmatter dict, body)."""
    if not text.startswith("---\n"):
        raise MessageError("message must begin with a '---' frontmatter block")
    lines = text.split("\n")
    fm: dict[str, Any] = {}
    end = None
    for idx in range(1, len(lines)):
        line = lines[idx]
        if line.rstrip() == "---":
            end = idx
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            raise MessageError(f"nested/indented frontmatter is not allowed: {line!r}")
        if ":" not in line:
            raise MessageError(f"bad frontmatter line: {line!r}")
        key, _, raw = line.partition(":")
        key = key.strip()
        if key in fm:
            raise MessageError(f"duplicate frontmatter key: {key!r}")
        fm[key] = _parse_scalar(raw)
    if end is None:
        raise MessageError("unterminated frontmatter block")
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return fm, body


def render_message(fm: dict, body: str) -> str:
    ordered = [k for k in ALL_FIELDS if k in fm]
    unknown = [k for k in fm if k not in ALL_FIELDS]
    if unknown:
        raise MessageError(f"unknown frontmatter field(s): {sorted(unknown)}")
    out = ["---"]
    for key in ordered:
        out.append(f"{key}: {_emit_scalar(fm[key])}")
    out.append("---")
    out.append("")
    text = "\n".join(out) + body.rstrip("\n") + "\n"
    return text


# --------------------------------------------------------------------------
# Schema validation -- the single source of truth
# --------------------------------------------------------------------------


def validate_frontmatter(fm: dict, *, rel_path: str = "<mem>") -> None:
    where = f"{rel_path}: "
    missing = [f for f in REQUIRED_FIELDS if f not in fm]
    if missing:
        raise MessageError(where + f"missing required field(s): {missing}")
    unknown = [f for f in fm if f not in ALL_FIELDS]
    if unknown:
        raise MessageError(where + f"unknown field(s): {sorted(unknown)}")
    for f in NON_NULL_FIELDS:
        if fm.get(f) is None:
            raise MessageError(where + f"field {f!r} may not be null")

    if not ID_RE.match(str(fm["id"])):
        raise MessageError(where + f"malformed id: {fm['id']!r}")
    if fm["kind"] not in KINDS:
        raise MessageError(where + f"bad kind: {fm['kind']!r}")
    if fm["project"] != PROJECT:
        raise MessageError(where + f"bad project: {fm['project']!r}")
    if fm["lane"] not in LANES:
        raise MessageError(where + f"bad lane: {fm['lane']!r}")
    if fm["status"] not in STATUSES:
        raise MessageError(where + f"bad status: {fm['status']!r}")
    if not isinstance(fm["requires_owner"], bool):
        raise MessageError(where + "requires_owner must be a boolean")
    if not TS_RE.match(str(fm["created_at"])):
        raise MessageError(where + f"bad created_at: {fm['created_at']!r}")

    conf = fm.get("confidence")
    if conf is not None:
        if not isinstance(conf, (int, float)) or isinstance(conf, bool):
            raise MessageError(where + "confidence must be a number or null")
        if not (0.0 <= float(conf) <= 1.0):
            raise MessageError(where + f"confidence out of range: {conf}")

    sha = fm.get("local_source_sha256")
    if sha is not None and not SHA256_RE.match(str(sha)):
        raise MessageError(where + f"bad local_source_sha256: {sha!r}")

    for key in ("source_commit", "brittle_commit"):
        val = fm.get(key)
        if val is not None and not COMMIT_RE.match(str(val)):
            raise MessageError(where + f"bad {key}: {val!r}")

    for key in ("in_reply_to", "supersedes", "report_id", "ticket_id",
                "review_of", "escalation_id", "decision_id"):
        val = fm.get(key)
        if val is not None and not ID_RE.match(str(val)):
            raise MessageError(where + f"bad reference in {key}: {val!r}")

    for key in ("lease_expires_at", "claimed_at", "expires_at"):
        val = fm.get(key)
        if val is not None and not TS_RE.match(str(val)):
            raise MessageError(where + f"bad timestamp in {key}: {val!r}")

    if fm["kind"] == "receipt":
        rt = fm.get("receipt_type")
        if rt not in RECEIPT_TYPES:
            raise MessageError(where + f"receipt requires receipt_type in {RECEIPT_TYPES}, got {rt!r}")
        if fm.get("in_reply_to") is None:
            raise MessageError(where + "receipt must set in_reply_to")
        if rt in ("claim", "renew", "reclaim"):
            for f in ("agent", "claimed_at", "lease_expires_at", "brittle_commit"):
                if fm.get(f) is None:
                    raise MessageError(where + f"{rt} receipt requires {f!r}")

    if fm["kind"] == "ticket":
        if fm["lane"] not in AGENT_LANES:
            raise MessageError(where + f"ticket lane must be one of {AGENT_LANES}")
        if fm["status"] != "open":
            raise MessageError(where + "a published ticket must have status 'open'")

    if fm["kind"] == "report":
        if fm["lane"] not in AGENT_LANES:
            raise MessageError(where + f"report lane must be one of {AGENT_LANES}")
        if fm.get("local_source_path") is None or fm.get("local_source_sha256") is None:
            raise MessageError(where + "report must record local_source_path and local_source_sha256")

    if fm["kind"] == "owner_decision":
        if fm["from"] != "joe":
            raise MessageError(where + "owner_decision must originate from 'joe'")
        for f in ("authorized_action", "scope", "checksum"):
            if fm.get(f) is None:
                raise MessageError(where + f"owner_decision requires {f!r}")

    if fm["kind"] == "escalation":
        if fm.get("requires_owner") is not True:
            raise MessageError(where + "escalation must set requires_owner: true")


def expected_dir(fm: dict) -> str:
    """Directory a message of this kind must live in."""
    kind = fm["kind"]
    if kind == "ticket":
        return f"{DIR_TICKETS}/{fm['lane']}"
    if kind == "report":
        return f"{DIR_REPORTS}/{fm['lane']}"
    if kind == "review":
        return DIR_REVIEWS
    if kind == "receipt":
        return DIR_RECEIPTS
    if kind == "escalation":
        return DIR_ESC_OPEN
    if kind == "owner_decision":
        return DIR_DECISIONS
    raise MessageError(f"no directory for kind {kind!r}")


# --------------------------------------------------------------------------
# Secret detection
# --------------------------------------------------------------------------

SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("twilio-sid", re.compile(r"\bAC[0-9a-fA-F]{32}\b")),
    ("private-key-block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer-header", re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._\-]{20,}")),
    ("generic-secret-assign", re.compile(
        r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|"
        r"client[_-]?secret|password|passwd)\s*[:=]\s*"
        r"['\"]?[A-Za-z0-9/+=_\-]{16,}['\"]?"
    )),
    ("ssh-private", re.compile(r"\bPRIVATE KEY BLOCK\b")),
    ("cookie-header", re.compile(r"(?i)\bset-cookie\s*:\s*\S{16,}")),
)

# Text that legitimately mentions these words without carrying a secret.
SECRET_ALLOWLIST = re.compile(
    r"(?i)(api_key_env|OPENAI_API_KEY|ANTHROPIC_API_KEY|api[_-]?key\s*[:=]\s*"
    r"['\"]?(?:<[^>]*>|\$\{?[A-Z_]+\}?|REDACTED|xxx+|\.\.\.|null|none|env:[A-Za-z_]+)"
    r"['\"]?)"
)


def scan_secrets(text: str, extra_patterns: Sequence[str] = ()) -> list[str]:
    """Return a list of human-readable findings.  Empty list == clean."""
    findings: list[str] = []
    for name, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            snippet = match.group(0)
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            line = text[line_start : line_end if line_end != -1 else len(text)]
            if SECRET_ALLOWLIST.search(line):
                continue
            findings.append(f"{name}: ...{snippet[:12]}<redacted>")
    for raw in extra_patterns:
        try:
            pat = re.compile(raw)
        except re.error as exc:
            raise MessageError(f"bad private pattern {raw!r}: {exc}") from exc
        if pat.search(text):
            findings.append(f"private-pattern: {raw}")
    return findings


# --------------------------------------------------------------------------
# Path / size policy
# --------------------------------------------------------------------------


def _ext_of(rel: str) -> str:
    name = os.path.basename(rel)
    if name in ALLOWED_BARE_NAMES:
        return name
    return os.path.splitext(name)[1].lower()


def check_path_policy(rel: str, size: int) -> None:
    rel = rel.replace(os.sep, "/")
    if rel.startswith("/") or ".." in rel.split("/"):
        raise MessageError(f"path escapes the repository: {rel!r}")
    name = os.path.basename(rel)
    ext = os.path.splitext(name)[1].lower()

    in_messages = any(rel.startswith(d + "/") for d in MESSAGE_DIRS)
    if in_messages:
        if name == ".gitkeep":
            return  # placeholder so the layout exists in git before first use
        if ext not in ALLOWED_EXT_MESSAGES:
            raise MessageError(
                f"{rel}: message payloads allow only {sorted(ALLOWED_EXT_MESSAGES)}"
            )
        if size > MAX_MESSAGE_BYTES:
            raise MessageError(
                f"{rel}: {size} bytes exceeds the {MAX_MESSAGE_BYTES}-byte message limit"
            )
        return

    if ext in FORBIDDEN_EXT:
        raise MessageError(f"{rel}: forbidden file type {ext!r} for a public bus")
    if name not in ALLOWED_BARE_NAMES and ext not in ALLOWED_EXT_REPO:
        raise MessageError(f"{rel}: extension {ext!r} is not on the repository allow-list")
    if size > MAX_REPO_FILE_BYTES:
        raise MessageError(
            f"{rel}: {size} bytes exceeds the {MAX_REPO_FILE_BYTES}-byte repository limit"
        )


# --------------------------------------------------------------------------
# Config (minimal TOML -- flat sections, scalars and string arrays)
# --------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "brittle-messages" / "config.toml"


def parse_toml(text: str) -> dict:
    data: dict[str, Any] = {}
    section = data
    for lineno, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = data
            for part in line[1:-1].strip().split("."):
                section = section.setdefault(part.strip(), {})
            continue
        if "=" not in line:
            raise MessageError(f"config line {lineno}: expected key = value")
        key, _, raw_val = line.partition("=")
        val = raw_val.split(" #")[0].strip() if not raw_val.strip().startswith(('"', "'")) else raw_val.strip()
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            section[key.strip()] = (
                [_parse_scalar(p) for p in _split_csv(inner)] if inner else []
            )
        else:
            section[key.strip()] = _parse_scalar(val)
    return data


def _split_csv(text: str) -> list[str]:
    out, buf, quote = [], "", None
    for ch in text:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf += ch
        elif ch == ",":
            out.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf.strip())
    return out


DEFAULT_CONFIG: dict[str, Any] = {
    "repo": {
        "path": str(Path.home() / "code" / "messages"),
        "brittle_path": str(Path.home() / "code" / "brittle"),
    },
    "reviewer": {
        "model": "gpt-4o-2024-08-06",
        "prompt_path": str(Path.home() / "code" / "messages" / "prompts" / "brittle-reviewer.md"),
        "poll_seconds": 20,
        "minimum_confidence": DEFAULT_MIN_CONFIDENCE,
        "request_timeout_seconds": 120,
        "max_report_chars": 24000,
    },
    "openai": {
        "api_key_env": "OPENAI_API_KEY",
        "api_key_file": "",
        "base_url": "https://api.openai.com/v1",
    },
    "notification": {"command": "", "timeout_seconds": 20},
    "claims": {"lease_seconds": DEFAULT_LEASE_SECONDS},
    "safety": {"private_patterns": []},
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path or os.environ.get("BRITTLE_MESSAGES_CONFIG") or DEFAULT_CONFIG_PATH)
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        cfg = _deep_merge(cfg, parse_toml(path.read_text(encoding="utf-8")))
    cfg["_config_path"] = str(path)
    return cfg


def state_dir() -> Path:
    root = os.environ.get("BRITTLE_MESSAGES_STATE") or (
        Path.home() / ".local" / "state" / "brittle-messages"
    )
    p = Path(root)
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_api_key(cfg: dict) -> str:
    """Fail closed.  Never log, never echo, never pass on a command line."""
    env_name = cfg.get("openai", {}).get("api_key_env") or "OPENAI_API_KEY"
    key = os.environ.get(env_name, "").strip()
    if key:
        return key
    key_file = str(cfg.get("openai", {}).get("api_key_file") or "").strip()
    if key_file:
        p = Path(os.path.expanduser(key_file))
        if p.exists():
            mode = p.stat().st_mode & 0o777
            if mode & 0o077:
                raise MessageError(
                    f"credential file {p} has permissions {oct(mode)}; require 0600"
                )
            key = p.read_text(encoding="utf-8").strip()
            if key:
                return key
    raise MessageError(
        f"no OpenAI credential: env {env_name} is unset and no readable "
        f"[openai].api_key_file is configured"
    )


# --------------------------------------------------------------------------
# Git repository wrapper
# --------------------------------------------------------------------------

GIT_IDENTITY = ("-c", "user.email=digitalnomadjoe@gmail.com",
                "-c", "user.name=digitalnomadjoe")


@contextmanager
def repo_lock(repo_path: str | Path, timeout: float = 120.0):
    lock_dir = Path(repo_path) / ".git"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / "brittle-messages.lock"
    fh = open(lock_file, "w")
    deadline = None
    try:
        import time as _t

        deadline = _t.time() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if _t.time() > deadline:
                    raise MessageError("timed out acquiring the repository lock")
                _t.sleep(0.2)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


class Repo:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        if not (self.path / ".git").exists():
            raise MessageError(f"{self.path} is not a git repository")

    # --- plumbing ---------------------------------------------------------

    def git(self, *args: str, check: bool = True, identity: bool = False) -> str:
        cmd = ["git", "-C", str(self.path)]
        if identity:
            cmd += list(GIT_IDENTITY)
        cmd += list(args)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise MessageError(
                f"git {' '.join(args)} failed ({proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc.stdout

    def git_ok(self, *args: str) -> bool:
        cmd = ["git", "-C", str(self.path)] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True).returncode == 0

    def has_commits(self) -> bool:
        return self.git_ok("rev-parse", "--verify", "HEAD")

    def has_remote(self) -> bool:
        return bool(self.git("remote", check=False).strip())

    def branch(self) -> str:
        return self.git("rev-parse", "--abbrev-ref", "HEAD").strip() or "main"

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip() if self.has_commits() else ""

    def pull_ff_only(self) -> None:
        if not self.has_remote():
            return
        br = self.branch()
        if not self.git_ok("fetch", "origin", br):
            raise MessageError("git fetch failed (offline or credentials unavailable)")
        if self.git_ok("rev-parse", "--verify", f"origin/{br}"):
            if self.has_commits():
                self.git("merge", "--ff-only", f"origin/{br}")
            else:
                self.git("reset", "--hard", f"origin/{br}")

    def tracked_files(self) -> list[str]:
        if not self.has_commits():
            return []
        return [p for p in self.git("ls-files").split("\n") if p]

    # --- guards -----------------------------------------------------------

    def assert_only_new_files(self, exempt_prefixes: Sequence[str] = (DIR_STATE,),
                              allowed_moves: dict[str, str] | None = None) -> None:
        """Refuse to proceed if published message files were modified/deleted.

        `allowed_moves` permits the one sanctioned relocation -- an escalation
        moving byte-identically from escalations/open/ to escalations/resolved/.
        """
        allowed_moves = allowed_moves or {}
        allowed_paths = set(allowed_moves) | set(allowed_moves.values())
        for line in self.git("status", "--porcelain").split("\n"):
            if not line.strip():
                continue
            code, rel = line[:2], line[3:]
            rel = rel.strip().strip('"')
            if " -> " in rel:
                rel = rel.split(" -> ", 1)[1]
            if any(rel.startswith(p) for p in exempt_prefixes):
                continue
            if not any(rel.startswith(d + "/") for d in MESSAGE_DIRS):
                continue
            if code.strip() in ("??", "A"):
                continue
            if rel in allowed_paths and code.strip() in ("D", "R", "AD", "RM"):
                continue
            raise MessageError(
                f"refusing to publish: '{rel}' is {code.strip()!r}; published "
                f"messages are immutable (append a receipt instead)"
            )


# --------------------------------------------------------------------------
# Message loading + state folding
# --------------------------------------------------------------------------


class Message:
    __slots__ = ("fm", "body", "rel", "id", "kind")

    def __init__(self, fm: dict, body: str, rel: str):
        self.fm, self.body, self.rel = fm, body, rel
        self.id = str(fm["id"])
        self.kind = str(fm["kind"])

    def get(self, key: str, default: Any = None) -> Any:
        return self.fm.get(key, default)

    @property
    def created_at(self) -> _dt.datetime:
        return parse_iso(str(self.fm["created_at"]))

    def sort_key(self) -> tuple:
        return (str(self.fm["created_at"]), self.id)


def iter_message_paths(root: str | Path) -> list[str]:
    root = Path(root)
    out: list[str] = []
    for d in MESSAGE_DIRS:
        base = root / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.md")):
            out.append(str(p.relative_to(root)).replace(os.sep, "/"))
    return out


def load_messages(root: str | Path) -> dict[str, Message]:
    root = Path(root)
    msgs: dict[str, Message] = {}
    for rel in iter_message_paths(root):
        text = (root / rel).read_text(encoding="utf-8")
        fm, body = parse_message(text)
        validate_frontmatter(fm, rel_path=rel)
        mid = str(fm["id"])
        if mid in msgs:
            raise MessageError(
                f"duplicate message id {mid} in {rel} and {msgs[mid].rel}"
            )
        if os.path.basename(rel) != f"{mid}.md":
            raise MessageError(f"{rel}: filename must be <id>.md ({mid}.md)")
        msgs[mid] = Message(fm, body, rel)
    return msgs


def _receipts_for(target: str, msgs: dict[str, Message]) -> list[Message]:
    out = [
        m
        for m in msgs.values()
        if m.kind == "receipt" and m.get("in_reply_to") == target
    ]
    out.sort(key=Message.sort_key)
    return out


def ticket_state(ticket_id: str, msgs: dict[str, Message],
                 now: _dt.datetime | None = None) -> dict:
    """Fold append-only receipts into an effective ticket state.

    Published messages are never edited; status is always derived.
    """
    now = now or utc_now()
    receipts = _receipts_for(ticket_id, msgs)
    superseded_by = next(
        (m.id for m in sorted(msgs.values(), key=Message.sort_key)
         if m.get("supersedes") == ticket_id),
        None,
    )
    completion = next((r for r in receipts if r.get("receipt_type") == "complete"), None)
    block = next((r for r in receipts if r.get("receipt_type") == "block"), None)
    claims = [r for r in receipts if r.get("receipt_type") in ("claim", "renew", "reclaim")]
    active = claims[-1] if claims else None

    lease_expired = True
    if active is not None:
        lease_expired = parse_iso(str(active.get("lease_expires_at"))) <= now

    if superseded_by:
        status = "superseded"
    elif completion is not None:
        status = "completed"
    elif block is not None:
        status = "blocked"
    elif active is not None and not lease_expired:
        status = "claimed"
    else:
        status = "open"

    return {
        "ticket_id": ticket_id,
        "status": status,
        "claim": active,
        "claim_agent": active.get("agent") if active else None,
        "lease_expires_at": active.get("lease_expires_at") if active else None,
        "lease_expired": lease_expired,
        "completion": completion,
        "block": block,
        "superseded_by": superseded_by,
        "receipts": receipts,
    }


def reviewer_acked(report_id: str, msgs: dict[str, Message]) -> bool:
    return any(
        m.kind == "receipt"
        and m.get("receipt_type") == "reviewer_ack"
        and m.get("in_reply_to") == report_id
        for m in msgs.values()
    )


def escalation_state(esc_id: str, msgs: dict[str, Message]) -> str:
    for m in msgs.values():
        if m.kind == "owner_decision" and m.get("escalation_id") == esc_id:
            return "resolved"
        if (m.kind == "receipt" and m.get("receipt_type") == "escalation_resolved"
                and m.get("in_reply_to") == esc_id):
            return "resolved"
    return "open"


def autonomy_state(msgs: dict[str, Message], root: str | Path | None = None) -> dict:
    """Latest committed pause/resume decision, plus a local offline override."""
    decisions = sorted(
        [m for m in msgs.values()
         if m.kind == "owner_decision" and m.get("autonomy") in ("paused", "active")],
        key=Message.sort_key,
    )
    committed = decisions[-1].get("autonomy") if decisions else "active"
    local = (state_dir() / "PAUSED").exists()
    return {
        "committed": committed,
        "local_pause": local,
        "paused": bool(local or committed == "paused"),
        "source": decisions[-1].id if decisions else None,
    }


# --------------------------------------------------------------------------
# Index (disposable cache -- deterministic, no timestamps)
# --------------------------------------------------------------------------


def build_index(msgs: dict[str, Message], now: _dt.datetime | None = None) -> dict:
    now = now or utc_now()
    ordered = sorted(msgs.values(), key=Message.sort_key)

    latest_report: dict[str, str] = {}
    open_ticket: dict[str, str] = {}
    active_claims: dict[str, dict] = {}

    for m in ordered:
        if m.kind == "report" and m.get("lane") in AGENT_LANES:
            latest_report[str(m.get("lane"))] = m.id

    tickets = [m for m in ordered if m.kind == "ticket"]
    for lane in AGENT_LANES:
        for t in tickets:
            if t.get("lane") != lane:
                continue
            st = ticket_state(t.id, msgs, now=now)
            if st["status"] == "open":
                open_ticket.setdefault(lane, t.id)
            if st["status"] == "claimed":
                active_claims[t.id] = {
                    "agent": st["claim_agent"],
                    "lease_expires_at": st["lease_expires_at"],
                }

    open_escalations = sorted(
        m.id for m in ordered
        if m.kind == "escalation" and escalation_state(m.id, msgs) == "open"
    )

    acked_reports = [
        m.id for m in ordered if m.kind == "report" and reviewer_acked(m.id, msgs)
    ]

    return {
        "schema": 1,
        "latest_report_by_lane": dict(sorted(latest_report.items())),
        "open_ticket_by_lane": dict(sorted(open_ticket.items())),
        "active_claims": dict(sorted(active_claims.items())),
        "open_escalations": open_escalations,
        "reviewer_cursor": acked_reports[-1] if acked_reports else None,
        "counts": {
            k: sum(1 for m in ordered if m.kind == k) for k in sorted(KINDS)
        },
    }


def index_text(index: dict) -> str:
    return json.dumps(index, indent=2, sort_keys=False) + "\n"


# --------------------------------------------------------------------------
# Repository-wide validation
# --------------------------------------------------------------------------


def validate_repo(root: str | Path, *, private_patterns: Sequence[str] = (),
                  check_index: bool = True, now: _dt.datetime | None = None) -> list[str]:
    """Return a list of problems.  Empty list == valid."""
    root = Path(root)
    problems: list[str] = []

    # 1. path / size / type policy across every tracked-ish file
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        rel = str(path.relative_to(root)).replace(os.sep, "/")
        if rel.startswith(".git/") or "/__pycache__/" in rel or rel.endswith(".pyc"):
            continue
        if rel.startswith(".venv/"):
            continue
        try:
            check_path_policy(rel, path.stat().st_size)
        except MessageError as exc:
            problems.append(str(exc))

    # 2. schema + uniqueness + filename discipline
    try:
        msgs = load_messages(root)
    except MessageError as exc:
        problems.append(str(exc))
        return problems

    # 3. directory placement + lane consistency
    for m in msgs.values():
        want = expected_dir(m.fm)
        got = os.path.dirname(m.rel)
        if m.kind == "escalation" and got == DIR_ESC_RESOLVED:
            pass  # resolved escalations may be re-filed on resolution
        elif got != want:
            problems.append(f"{m.rel}: kind {m.kind!r}/lane {m.get('lane')!r} belongs in {want}/")

    # 4. reference integrity
    ref_fields = ("in_reply_to", "supersedes", "report_id", "ticket_id",
                  "review_of", "escalation_id", "decision_id")
    for m in msgs.values():
        for f in ref_fields:
            ref = m.get(f)
            if ref and ref not in msgs:
                problems.append(f"{m.rel}: {f} -> unknown message {ref}")

    # 5. one active claim per ticket + lane-correct claims
    for m in msgs.values():
        if m.kind != "ticket":
            continue
        st = ticket_state(m.id, msgs, now=now)
        for r in st["receipts"]:
            if r.get("receipt_type") in ("claim", "renew", "reclaim"):
                if r.get("agent") not in AGENT_LANES:
                    problems.append(f"{r.rel}: unknown agent {r.get('agent')!r}")
                elif r.get("agent") != m.get("lane"):
                    problems.append(
                        f"{r.rel}: {r.get('agent')} agent may not claim a "
                        f"{m.get('lane')} ticket"
                    )
        claims = [r for r in st["receipts"] if r.get("receipt_type") == "claim"]
        if len(claims) > 1:
            problems.append(
                f"{m.rel}: {len(claims)} initial claim receipts; reclaims must "
                f"use receipt_type 'reclaim'"
            )

    # 6. secret scan over every text file
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        rel = str(path.relative_to(root)).replace(os.sep, "/")
        if rel.startswith((".git/", ".venv/")) or "/__pycache__/" in rel:
            continue
        if rel == "scripts/messagelib.py" or rel.startswith("scripts/tests/"):
            continue  # the detector's own corpus
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            problems.append(f"{rel}: not valid UTF-8 text")
            continue
        for finding in scan_secrets(text, private_patterns):
            problems.append(f"{rel}: possible secret -- {finding}")

    # 7. generated index consistency
    if check_index:
        idx_path = root / INDEX_PATH
        if idx_path.exists():
            want = index_text(build_index(msgs, now=now))
            if idx_path.read_text(encoding="utf-8") != want:
                problems.append(
                    f"{INDEX_PATH}: stale -- run `messagesctl rebuild-index`"
                )

    return problems


# --------------------------------------------------------------------------
# Publication
# --------------------------------------------------------------------------


class PublishResult:
    def __init__(self, commit: str, pushed: bool, paths: list[str], detail: str = ""):
        self.commit, self.pushed, self.paths, self.detail = commit, pushed, paths, detail

    def as_dict(self) -> dict:
        return {
            "commit": self.commit,
            "pushed": self.pushed,
            "paths": self.paths,
            "detail": self.detail,
        }


def spool_dir() -> Path:
    p = state_dir() / "spool"
    (p / "outbox").mkdir(parents=True, exist_ok=True)
    return p


def _spool_write(files: dict[str, str], reason: str) -> Path:
    out = spool_dir() / "outbox" / f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}.json"
    out.write_text(
        json.dumps({"reason": reason, "files": files}, indent=2) + "\n",
        encoding="utf-8",
    )
    return out


def _mark_unpushed(repo: Repo, commit: str) -> None:
    marker = spool_dir() / "pending_push.json"
    data = {"repo": str(repo.path), "commits": []}
    if marker.exists():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    if commit not in data.get("commits", []):
        data.setdefault("commits", []).append(commit)
    data["repo"] = str(repo.path)
    marker.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _clear_unpushed() -> None:
    marker = spool_dir() / "pending_push.json"
    if marker.exists():
        marker.unlink()


def publish(repo: Repo, files: dict[str, str], commit_message: str, *,
            cfg: dict | None = None, max_attempts: int = 4,
            rebuild_index_after: bool = True,
            moves: dict[str, str] | None = None,
            now: _dt.datetime | None = None) -> PublishResult:
    """Atomically add brand-new message files, commit, and push.

    Guarantees: new files only, never a force-push, never an amend, never a
    history rewrite.  On a concurrent-append rejection the local (unpushed)
    commit is discarded and the whole cycle is retried against fresh state.
    """
    cfg = cfg or {}
    moves = moves or {}
    private = list(cfg.get("safety", {}).get("private_patterns", []) or [])
    branch = repo.branch()
    last_error = ""

    for src, dst in moves.items():
        if not (src.startswith(DIR_ESC_OPEN + "/") and dst.startswith(DIR_ESC_RESOLVED + "/")
                and os.path.basename(src) == os.path.basename(dst)):
            raise MessageError(
                f"only open->resolved escalation relocation is permitted, got {src} -> {dst}"
            )

    for attempt in range(1, max_attempts + 1):
        # 1. fresh state
        try:
            repo.pull_ff_only()
        except MessageError as exc:
            last_error = f"pull failed: {exc}"
            if attempt == max_attempts:
                path = _spool_write(files, last_error)
                raise MessageError(
                    f"{last_error}; {len(files)} message(s) spooled to {path}"
                ) from exc
            continue

        # 2. repository must already be valid before we add to it
        problems = validate_repo(repo.path, private_patterns=private,
                                 check_index=False, now=now)
        if problems:
            raise MessageError(
                "refusing to publish into an invalid repository:\n  - "
                + "\n  - ".join(problems[:10])
            )

        # 3. new files only
        for rel in files:
            if (repo.path / rel).exists():
                raise MessageError(f"refusing to overwrite existing file: {rel}")

        # 3b. sanctioned relocations: content must survive byte-identically
        written: list[str] = []
        for src, dst in moves.items():
            src_p, dst_p = repo.path / src, repo.path / dst
            if not src_p.exists():
                raise MessageError(f"cannot relocate missing message: {src}")
            if dst_p.exists():
                raise MessageError(f"relocation target already exists: {dst}")
            payload = src_p.read_bytes()
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            dst_p.write_bytes(payload)
            src_p.unlink()
            if sha256_bytes(dst_p.read_bytes()) != sha256_bytes(payload):
                raise MessageError(f"relocation altered {src}; aborting")
            written.extend([src, dst])

        # 4. per-file gates, then write
        for rel, content in files.items():
            data = content.encode("utf-8")
            check_path_policy(rel, len(data))
            findings = scan_secrets(content, private)
            if findings:
                raise MessageError(
                    f"{rel}: secret scan failed -- {findings[0]} "
                    f"({len(findings)} finding(s)); publication aborted"
                )
            if rel.endswith(".md") and any(rel.startswith(d + "/") for d in MESSAGE_DIRS):
                fm, _ = parse_message(content)
                validate_frontmatter(fm, rel_path=rel)
                if os.path.dirname(rel) != expected_dir(fm):
                    raise MessageError(f"{rel}: wrong directory for kind {fm['kind']!r}")
            target = repo.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(rel)

        # 5. refresh the disposable index in the same commit
        if rebuild_index_after:
            msgs = load_messages(repo.path)
            idx_rel = INDEX_PATH
            (repo.path / idx_rel).parent.mkdir(parents=True, exist_ok=True)
            (repo.path / idx_rel).write_text(
                index_text(build_index(msgs, now=now)), encoding="utf-8"
            )
            written.append(idx_rel)

        # 6. immutability guard, then stage explicit paths only (never -A)
        repo.assert_only_new_files(allowed_moves=moves)
        repo.git("add", "--", *written)
        repo.git("commit", "-m", commit_message, identity=True)
        commit = repo.head()

        # 7. push
        if not repo.has_remote():
            return PublishResult(commit, False, written, "no remote configured")
        if repo.git_ok("push", "origin", f"HEAD:{branch}"):
            _clear_unpushed()
            return PublishResult(commit, True, written)

        # 8. concurrent append -- discard *our own unpushed* commit and retry
        last_error = "push rejected (concurrent append)"
        ours = [c for c in repo.git(
            "rev-list", f"origin/{branch}..HEAD", check=False).split("\n") if c]
        if len(ours) == 1 and attempt < max_attempts:
            repo.git("reset", "--hard", f"origin/{branch}")
            continue
        _mark_unpushed(repo, commit)
        return PublishResult(
            commit, False, written,
            f"{last_error}; commit retained locally and spooled for retry",
        )

    path = _spool_write(files, last_error or "unknown")
    raise MessageError(f"publish failed after {max_attempts} attempts: {last_error} "
                       f"(spooled to {path})")


# --------------------------------------------------------------------------
# Message builders
# --------------------------------------------------------------------------


def base_frontmatter(kind: str, *, sender: str, to: str, lane: str,
                     unit: str | None = None, status: str = "open",
                     requires_owner: bool = False,
                     confidence: float | None = None,
                     in_reply_to: str | None = None,
                     supersedes: str | None = None,
                     source_commit: str | None = None,
                     local_source_path: str | None = None,
                     local_source_sha256: str | None = None,
                     now: _dt.datetime | None = None,
                     message_id: str | None = None) -> dict:
    now = now or utc_now()
    return {
        "id": message_id or new_id(now),
        "kind": kind,
        "project": PROJECT,
        "from": sender,
        "to": to,
        "lane": lane,
        "unit": unit,
        "created_at": iso(now),
        "source_commit": source_commit,
        "local_source_path": local_source_path,
        "local_source_sha256": local_source_sha256,
        "in_reply_to": in_reply_to,
        "supersedes": supersedes,
        "requires_owner": requires_owner,
        "confidence": confidence,
        "status": status,
    }


def brittle_commit(brittle_path: str | Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(brittle_path), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
        )
        out = proc.stdout.strip()
        return out if proc.returncode == 0 and COMMIT_RE.match(out) else None
    except OSError:
        return None


# --------------------------------------------------------------------------
# Notification
# --------------------------------------------------------------------------


def notify(cfg: dict, *, escalation_id: str, summary: str, lane: str,
           unit: str | None, rel_path: str) -> tuple[str, str]:
    """Return (status, detail).  status in {sent, failed, unavailable}.

    Never claims success it did not observe.  Secrets are never passed on the
    command line -- only these five well-known environment variables are set.
    """
    command = str(cfg.get("notification", {}).get("command") or "").strip()
    if not command:
        return (
            "unavailable",
            "no [notification].command configured in "
            f"{cfg.get('_config_path', DEFAULT_CONFIG_PATH)}",
        )
    timeout = float(cfg.get("notification", {}).get("timeout_seconds") or 20)
    env = dict(os.environ)
    env.update({
        "BRITTLE_ESCALATION_ID": escalation_id,
        "BRITTLE_ESCALATION_SUMMARY": summary[:400],
        "BRITTLE_ESCALATION_LANE": lane,
        "BRITTLE_ESCALATION_UNIT": unit or "",
        "BRITTLE_ESCALATION_PATH": rel_path,
    })
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return ("failed", f"unparsable [notification].command: {exc}")
    try:
        proc = subprocess.run(argv, env=env, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        return ("failed", f"notification command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return ("failed", f"notification command timed out after {timeout}s")
    except OSError as exc:
        return ("failed", f"notification command error: {exc}")
    if proc.returncode == 0:
        return ("sent", f"exit 0 via {argv[0]}")
    return ("failed", f"{argv[0]} exited {proc.returncode}: {proc.stderr.strip()[:200]}")


__all__ = [n for n in dir() if not n.startswith("_")]
