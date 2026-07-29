"""Filesystem containment for the agent.

The agent needs to read inputs (the triage spec, the inbox) and write outputs
(filed tickets). Those are different privileges, so they get different roots:

    READ_ROOTS   repo root  - the spec and the inbox live here
    WRITE_ROOTS  outbox     - the only place the agent may create or edit files

Enforcement is a `PreToolUse` hook rather than an allow/deny rule or a
`can_use_tool` callback, for one reason: hooks run *before* every other step in
the permission flow, and a hook deny wins even in `bypassPermissions`. A tool
auto-approved by `allowed_tools` never reaches `can_use_tool`, so a callback
guard would be silently bypassed the moment someone bare-allows `Write`.

Note also that a `Write(path)` permission rule is never matched by the file
permission checks — `Edit(path)` is what governs `Write` — which makes
rule-based path scoping easy to get wrong. The hook sidesteps that entirely.
"""

import os
from pathlib import Path
from typing import Any

AGENT_DIR = Path(__file__).resolve().parent

# The read root. Derived from the layout by default, but overridable, because
# the derivation is wrong in a container: with the code at /app, the parent is
# "/" and the read root would silently become the entire filesystem. The image
# sets AGENT_REPO_ROOT=/app so containment holds there too.
REPO_ROOT = Path(os.environ.get("AGENT_REPO_ROOT") or AGENT_DIR.parent).resolve()
WORKSPACE = AGENT_DIR / "workspace"
INBOX = WORKSPACE / "inbox"
OUTBOX = WORKSPACE / "outbox"

# Reading is broad (the agent must reach the spec); writing is narrow.
READ_ROOTS = (REPO_ROOT,)
WRITE_ROOTS = (OUTBOX,)

# Tools that create or modify files. Bash is not in the agent's tool set at
# all, so it is not a write vector here; if it is ever added, add it below.
WRITE_TOOLS = ("Write", "Edit", "NotebookEdit", "MultiEdit")
READ_TOOLS = ("Read", "Glob", "Grep")

_PATH_KEYS = ("file_path", "notebook_path", "path")


def target_path(tool_input: dict[str, Any]) -> str | None:
    """Pull the filesystem target out of a tool's input, if it has one."""
    for key in _PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _resolve(raw: str) -> Path:
    """Resolve a model-supplied path to a canonical absolute path.

    `resolve()` collapses `..` and follows symlinks, so traversal and link
    tricks are normalised away before the containment check runs. Relative
    paths are anchored at the repo root, matching the agent's cwd.
    """
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    return candidate.resolve()


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            if path == root or path.is_relative_to(root.resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


def check_access(tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether a tool call may touch the filesystem.

    Returns (allowed, reason). Tools with no path are allowed through — this
    guard is about *where* files are touched, not which tools may run.
    """
    raw = target_path(tool_input)
    if raw is None:
        return True, "no filesystem target"

    resolved = _resolve(raw)

    if tool_name in WRITE_TOOLS:
        if _within(resolved, WRITE_ROOTS):
            return True, f"write inside {OUTBOX.name}/"
        return False, (
            f"writes are confined to {OUTBOX.relative_to(REPO_ROOT).as_posix()}/; "
            f"{resolved} is outside it"
        )

    if tool_name in READ_TOOLS:
        if _within(resolved, READ_ROOTS):
            return True, "read inside the repo"
        return False, f"reads are confined to the repo; {resolved} is outside it"

    return True, "tool is not filesystem-scoped"


def ensure_workspace() -> None:
    """Create the inbox/outbox the agent works through."""
    INBOX.mkdir(parents=True, exist_ok=True)
    OUTBOX.mkdir(parents=True, exist_ok=True)
