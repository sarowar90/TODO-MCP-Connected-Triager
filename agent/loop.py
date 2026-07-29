"""The core autonomous loop.

The Agent SDK runs the *inner* cycle — Claude decides, the SDK executes tools,
results feed back, repeat until Claude stops calling tools. This module does
four things on top of it:

  1. Instruments each phase so the cycle is observable: DECIDE / ACT / OBSERVE.
  2. Defines the goal in code, not in the prompt: a validated ticket exists
     *and* has been written to the outbox.
  3. Adds the *outer* loop — if Claude stops without meeting the goal,
     re-prompt with the reason and try again, up to MAX_ATTEMPTS.
  4. Contains the agent's filesystem access with a PreToolUse hook, so writes
     land in the outbox and nowhere else (see fs_policy.py).
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from fs_policy import (
    INBOX,
    OUTBOX,
    READ_TOOLS,
    REPO_ROOT,
    WRITE_TOOLS,
    FsAudit,
    ensure_workspace,
    make_fs_guard,
)
from triage_tools import TOOL_NAMES, TriageSession, build_triage_server

SPEC_PATH = "lib/triage/triage_spec.md"
OUTBOX_REL = OUTBOX.relative_to(REPO_ROOT).as_posix()

MODEL = "claude-opus-5"
MAX_ATTEMPTS = 2
MAX_TURNS = 25

SYSTEM_PROMPT = f"""\
You are a customer-support triage agent.

Work autonomously through this cycle until the task is done:
  1. Gather context. Read {SPEC_PATH} for the precedence rules. If the message
     names an email or order id, look up the customer, the order, and their
     recent tickets before deciding.
  2. Decide the urgency, topic, and owning team by applying the spec's ordered
     precedence rules (safety -> churn -> outage -> topic default).
  3. Act: call create_ticket exactly once with your classification.
  4. Write the filed ticket to {OUTBOX_REL}/<ticket_id>.md as a short markdown
     summary: the ticket id, urgency, topic, team, confidence, whether it needs
     human review, the summary, and the rationale.
  5. Observe each result. If create_ticket rejects your arguments, read the
     reason, fix them, and call it again.

You may read anywhere in the repository, but {OUTBOX_REL}/ is the only place
you may write. Do not attempt to modify source files, the spec, or the inbox.

You are not done until create_ticket succeeds and the markdown file exists. Do
not stop to ask questions — if the message is genuinely unclear, file it with
needs_human_review=true and team='triage_review'. Keep any text you write to
one short line per step.
"""


@dataclass
class LoopOutcome:
    """What one full run of the outer loop produced."""

    session: TriageSession
    audit: FsAudit
    attempts: int
    turns: int
    cost_usd: float
    stopped_because: str
    written_files: list[Path] = field(default_factory=list)

    @property
    def goal_met(self) -> bool:
        return self.session.goal_met and bool(self.written_files)


def goal_state(session: TriageSession) -> tuple[bool, str]:
    """Is the task actually finished? Returns (met, reason-if-not)."""
    if session.ticket is None:
        if session.rejections:
            return False, f"create_ticket rejected your arguments: {session.rejections[-1]}"
        return False, "you stopped before calling create_ticket"

    ticket_id = session.ticket.get("ticket_id", "")
    if not _outbox_files(ticket_id):
        return False, f"you filed {ticket_id} but never wrote {OUTBOX_REL}/{ticket_id}.md"
    return True, ""


def _outbox_files(ticket_id: str = "") -> list[Path]:
    if not OUTBOX.exists():
        return []
    found = sorted(p for p in OUTBOX.glob("**/*") if p.is_file())
    if ticket_id:
        found = [p for p in found if ticket_id in p.name]
    return found


def _log(phase: str, detail: str) -> None:
    print(f"  {phase:<8} {detail}", flush=True)


def _short(value: Any, limit: int = 96) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _run_once(
    prompt: str, session: TriageSession, audit: FsAudit
) -> tuple[str, int, float]:
    """One pass of the SDK's inner loop, with each phase logged."""
    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"triage": build_triage_server(session)},
        allowed_tools=["Read", "Glob", "Grep", "Write", *TOOL_NAMES],
        # Only these built-ins exist for the agent. Bash is deliberately absent:
        # it would be an unguarded write vector alongside Write/Edit.
        tools=["Read", "Glob", "Grep", "Write"],
        disallowed_tools=["Bash"],
        # The real containment. Hooks run before allow rules, so this holds
        # even though Write is auto-approved above.
        hooks={
            "PreToolUse": [
                HookMatcher(
                    matcher="|".join([*WRITE_TOOLS, *READ_TOOLS]),
                    hooks=[make_fs_guard(audit)],
                )
            ]
        },
        permission_mode="acceptEdits",
        cwd=str(REPO_ROOT),
        max_turns=MAX_TURNS,
    )

    subtype, turns, cost = "no_result", 0, 0.0

    # Iterate to completion rather than breaking on ResultMessage: trailing
    # system events can follow it. query() then raises by design once an error
    # result has been yielded, so the whole loop sits in a try.
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, SystemMessage) and msg.subtype == "compact_boundary":
                _log("CONTEXT", "history compacted")

            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        _log("DECIDE", _short(block.text))
                    elif isinstance(block, ToolUseBlock):
                        name = block.name.replace("mcp__triage__", "")
                        _log("ACT", f"{name}({_short(block.input, 64)})")

            elif isinstance(msg, UserMessage):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        flag = "rejected " if block.is_error else ""
                        _log("OBSERVE", f"{flag}{_short(block.content, 80)}")

            elif isinstance(msg, ResultMessage):
                subtype = msg.subtype
                turns = msg.num_turns
                cost = msg.total_cost_usd or 0.0
    except Exception as exc:  # noqa: BLE001 - query() raises after an error result
        _log("ERROR", _short(exc))
        if subtype == "no_result":
            subtype = "error_during_execution"

    return subtype, turns, cost


async def run_triage(message: str) -> LoopOutcome:
    """Run the outer loop until the goal is met or attempts are exhausted."""
    ensure_workspace()
    session = TriageSession()
    audit = FsAudit()
    prompt = f"Triage this support message:\n\n{message}"

    attempts = turns = 0
    cost = 0.0
    subtype = "no_result"

    while attempts < MAX_ATTEMPTS:
        attempts += 1
        print(f"\n[attempt {attempts}/{MAX_ATTEMPTS}]", flush=True)

        subtype, attempt_turns, attempt_cost = await _run_once(prompt, session, audit)
        turns += attempt_turns
        cost += attempt_cost

        met, reason = goal_state(session)
        if met:
            ticket_id = (session.ticket or {}).get("ticket_id", "")
            return LoopOutcome(
                session, audit, attempts, turns, cost, subtype, _outbox_files(ticket_id)
            )

        # Goal unmet: feed the reason back and go round again. This is the
        # reliability guard — triage must always yield a routable result.
        _log("GOAL", f"not met - {reason}")
        prompt = (
            f"The previous attempt did not finish, because {reason}. "
            f"Complete the task now for this message:\n\n{message}"
        )

    return LoopOutcome(session, audit, attempts, turns, cost, subtype, _outbox_files())


def report(outcome: LoopOutcome) -> int:
    """Print the final state. Returns a process exit code."""
    session = outcome.session
    print("\n" + "=" * 68)

    if outcome.goal_met:
        ticket = session.ticket or {}
        print("GOAL MET - ticket filed and written")
        print(f"  ticket      {ticket.get('ticket_id')}")
        print(f"  urgency     {ticket.get('urgency')}")
        print(f"  topic       {ticket.get('topic')}")
        print(f"  team        {ticket.get('team')}")
        print(f"  confidence  {ticket.get('confidence')}")
        print(f"  review?     {ticket.get('needs_human_review')}")
        print(f"  summary     {ticket.get('summary')}")
    else:
        print("GOAL NOT MET", file=sys.stderr)
        print(f"  stopped because: {outcome.stopped_because}", file=sys.stderr)
        print(f"  reason: {goal_state(session)[1]}", file=sys.stderr)

    print("\n  filesystem")
    for path in outcome.written_files:
        print(f"    wrote   {path.relative_to(REPO_ROOT).as_posix()}")
    if not outcome.written_files:
        print("    wrote   (nothing)")
    for tool, path, reason in outcome.audit.denied:
        print(f"    BLOCKED {tool} -> {path} ({reason})")
    if not outcome.audit.denied:
        print("    blocked (nothing - the agent stayed in bounds)")

    print(
        f"\n  {outcome.attempts} attempt(s) · {outcome.turns} turns · "
        f"{len(session.tool_calls)} tool calls · ${outcome.cost_usd:.4f}"
    )
    print(f"  tool sequence: {' -> '.join(session.tool_calls) or '(none)'}")
    print("=" * 68)

    return 0 if outcome.goal_met else 1
