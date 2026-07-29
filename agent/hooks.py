"""Lifecycle hooks.

The permission hook in permissions.py answers "may this run?". These answer
"what happened?" — they observe rather than gate, and they run in your process
rather than in the agent's context window, so they cost no tokens.

    UserPromptSubmit  a step begins          -> open a journal entry
    PreToolUse        a tool is requested    -> policy decision (permissions.py)
    PostToolUse       a tool returned        -> record it; note file mutations
    PostToolUseFailure a tool errored        -> record the failure
    PreCompact        history is summarising -> note it, since compaction can
                                                drop detail from the transcript
    Stop              the agent finished     -> close the entry

The journal is the run's audit trail: what the agent did, in order, with the
file mutations called out. It is what you read after an unattended batch to
find out why the outbox looks the way it does.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import HookMatcher

from fs_policy import target_path
from permissions import make_permission_hook

MUTATING_TOOLS = ("Write", "Edit", "NotebookEdit", "MultiEdit")


@dataclass
class JournalEntry:
    at: str
    event: str
    detail: str


@dataclass
class Journal:
    """An ordered record of everything the hooks observed."""

    entries: list[JournalEntry] = field(default_factory=list)
    mutations: list[str] = field(default_factory=list)
    compactions: int = 0
    step: str = ""

    def record(self, event: str, detail: str = "") -> None:
        self.entries.append(
            JournalEntry(
                at=datetime.now(timezone.utc).strftime("%H:%M:%S"),
                event=event,
                detail=detail,
            )
        )

    def render(self, limit: int = 0) -> None:
        shown = self.entries[-limit:] if limit else self.entries
        for entry in shown:
            detail = f" {entry.detail}" if entry.detail else ""
            print(f"    {entry.at}  {entry.event:<18}{detail}")


def _tool_of(input_data: dict[str, Any]) -> str:
    return str(input_data.get("tool_name", "")).replace("mcp__triage__", "")


def build_hooks(journal: Journal, permission_audit) -> dict[str, list[HookMatcher]]:
    """Every lifecycle hook, wired to one journal.

    PreToolUse carries the permission policy as well as journalling, because a
    single PreToolUse hook must return the permission decision — splitting it
    across two hooks would mean one of them returning `{}` and (per the SDK's
    precedence rules) the deny still winning, but the intent would be muddier.
    """

    policy_hook = make_permission_hook(permission_audit)

    async def on_prompt(input_data, tool_use_id, context):
        journal.record("step.begin", journal.step or "(unnamed step)")
        return {}

    async def on_pre_tool(input_data, tool_use_id, context):
        decision = await policy_hook(input_data, tool_use_id, context)
        verdict = (
            decision.get("hookSpecificOutput", {}).get("permissionDecision", "allow")
            if decision
            else "allow"
        )
        tool = _tool_of(input_data)
        path = target_path(input_data.get("tool_input") or {}) or ""
        suffix = f" {path}" if path else ""
        journal.record(f"tool.{verdict}", f"{tool}{suffix}")
        return decision

    async def on_post_tool(input_data, tool_use_id, context):
        tool = str(input_data.get("tool_name", ""))
        path = target_path(input_data.get("tool_input") or {}) or ""
        if tool in MUTATING_TOOLS and path:
            journal.mutations.append(path)
            journal.record("file.write", path)
        else:
            journal.record("tool.done", _tool_of(input_data))
        return {}

    async def on_tool_failure(input_data, tool_use_id, context):
        journal.record("tool.failed", _tool_of(input_data))
        return {}

    async def on_pre_compact(input_data, tool_use_id, context):
        journal.compactions += 1
        journal.record("context.compact", str(input_data.get("trigger", "")))
        return {}

    async def on_stop(input_data, tool_use_id, context):
        journal.record("step.end", journal.step or "")
        return {}

    return {
        "UserPromptSubmit": [HookMatcher(hooks=[on_prompt])],
        "PreToolUse": [HookMatcher(hooks=[on_pre_tool])],
        "PostToolUse": [HookMatcher(hooks=[on_post_tool])],
        "PostToolUseFailure": [HookMatcher(hooks=[on_tool_failure])],
        "PreCompact": [HookMatcher(hooks=[on_pre_compact])],
        "Stop": [HookMatcher(hooks=[on_stop])],
    }
