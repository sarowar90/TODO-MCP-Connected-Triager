"""The core autonomous loop, and the generic step runner built on it.

The Agent SDK runs the *inner* cycle — Claude decides, the SDK executes tools,
results feed back, repeat until Claude stops calling tools. This module adds:

  1. Phase instrumentation, so the cycle is observable: DECIDE / ACT / OBSERVE.
  2. Goals defined in code rather than in the prompt.
  3. An *outer* loop — if Claude stops without meeting the goal, re-prompt with
     the reason and try again, up to MAX_ATTEMPTS.
  4. Filesystem containment via a PreToolUse hook (see fs_policy.py).

`run_step` is the reusable unit: one instrumented agent run plus the retry loop,
parameterised by system prompt, prompt, and a goal predicate. Both the
per-message triage step and the digest step in plan.py are built from it, which
is what makes multi-step execution composable rather than bespoke.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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

from fs_policy import OUTBOX, REPO_ROOT, ensure_workspace
from permissions import (
    AUTO_APPROVED_TOOLS,
    DENIED_TOOLS,
    Approver,
    DenyApprover,
    PermissionAudit,
    make_can_use_tool,
    make_permission_hook,
)
from triage_tools import TriageSession, build_triage_server

SPEC_PATH = "lib/triage/triage_spec.md"
OUTBOX_REL = OUTBOX.relative_to(REPO_ROOT).as_posix()
DIGEST_NAME = "digest.md"

MODEL = "claude-opus-5"
MAX_ATTEMPTS = 2
MAX_TURNS = 25

TRIAGE_SYSTEM_PROMPT = f"""\
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

DIGEST_SYSTEM_PROMPT = f"""\
You are a support-operations analyst writing a shift handover.

Every ticket from this batch has already been filed as a markdown file in
{OUTBOX_REL}/. Your job is to aggregate them:
  1. Glob {OUTBOX_REL}/ and read every ticket file (skip {DIGEST_NAME} itself).
  2. Write {OUTBOX_REL}/{DIGEST_NAME} containing:
     - a one-line summary of the batch,
     - a markdown table with one row per ticket: id, urgency, topic, team,
       whether it needs human review,
     - counts per team and per urgency,
     - a "Needs attention first" section naming the highest-urgency tickets and
       anything flagged for human review, with one line each on why.

Reference every ticket id exactly as it appears in the files. {OUTBOX_REL}/ is
the only place you may write. Do not re-classify anything — the filed tickets
are the source of truth.
"""


@dataclass
class StepResult:
    """What one step produced."""

    name: str
    goal_met: bool
    session: TriageSession
    attempts: int
    turns: int
    cost_usd: float
    stopped_because: str
    reason: str = ""


@dataclass
class LoopOutcome:
    """Aggregate of every step in a run."""

    steps: list[StepResult] = field(default_factory=list)
    audit: PermissionAudit = field(default_factory=PermissionAudit)
    written_files: list[Path] = field(default_factory=list)
    approver_name: str = DenyApprover.name

    @property
    def goal_met(self) -> bool:
        return bool(self.steps) and all(step.goal_met for step in self.steps)

    @property
    def turns(self) -> int:
        return sum(step.turns for step in self.steps)

    @property
    def cost_usd(self) -> float:
        return sum(step.cost_usd for step in self.steps)

    @property
    def tickets(self) -> list[dict[str, Any]]:
        return [s.session.ticket for s in self.steps if s.session.ticket]


# --- goal predicates ---------------------------------------------------------


def outbox_files(pattern: str = "*") -> list[Path]:
    if not OUTBOX.exists():
        return []
    return sorted(p for p in OUTBOX.glob(pattern) if p.is_file())


def ticket_goal(session: TriageSession) -> tuple[bool, str]:
    """A triage step is done when a validated ticket is filed *and* written."""
    if session.ticket is None:
        if session.rejections:
            return False, f"create_ticket rejected your arguments: {session.rejections[-1]}"
        return False, "you stopped before calling create_ticket"

    ticket_id = session.ticket.get("ticket_id", "")
    if not [p for p in outbox_files("*.md") if ticket_id in p.name]:
        return False, f"you filed {ticket_id} but never wrote {OUTBOX_REL}/{ticket_id}.md"
    return True, ""


def make_digest_goal(ticket_ids: list[str]) -> Callable[[TriageSession], tuple[bool, str]]:
    """The digest is done when it exists and accounts for every ticket."""

    def digest_goal(_session: TriageSession) -> tuple[bool, str]:
        digest = OUTBOX / DIGEST_NAME
        if not digest.is_file():
            return False, f"{OUTBOX_REL}/{DIGEST_NAME} does not exist yet"

        text = digest.read_text(encoding="utf-8", errors="replace")
        missing = [tid for tid in ticket_ids if tid and tid not in text]
        if missing:
            return False, f"{DIGEST_NAME} does not mention {', '.join(missing)}"
        return True, ""

    return digest_goal


# --- instrumented run --------------------------------------------------------


def _log(phase: str, detail: str) -> None:
    print(f"    {phase:<8} {detail}", flush=True)


def _short(value: Any, limit: int = 92) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _run_once(
    system_prompt: str,
    prompt: str,
    session: TriageSession,
    permissions: PermissionAudit,
    approver: Approver,
) -> tuple[str, int, float]:
    """One pass of the SDK's inner loop, with each phase logged."""
    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=system_prompt,
        mcp_servers={"triage": build_triage_server(session)},
        # create_ticket is deliberately not pre-approved: leaving it out is what
        # guarantees every ticket reaches can_use_tool and the ASK tier.
        allowed_tools=AUTO_APPROVED_TOOLS,
        tools=["Read", "Glob", "Grep", "Write"],
        disallowed_tools=list(DENIED_TOOLS),
        # No matcher: the policy sees every tool call, not just file tools.
        hooks={"PreToolUse": [HookMatcher(hooks=[make_permission_hook(permissions)])]},
        can_use_tool=make_can_use_tool(approver, permissions),
        # "default" means nothing is auto-approved beyond allowed_tools, so
        # anything unrecognised reaches the callback and fails closed there.
        permission_mode="default",
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
                        _log("ACT", f"{name}({_short(block.input, 60)})")

            elif isinstance(msg, UserMessage):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        flag = "rejected " if block.is_error else ""
                        _log("OBSERVE", f"{flag}{_short(block.content, 76)}")

            elif isinstance(msg, ResultMessage):
                subtype = msg.subtype
                turns = msg.num_turns
                cost = msg.total_cost_usd or 0.0
    except Exception as exc:  # noqa: BLE001 - query() raises after an error result
        _log("ERROR", _short(exc))
        if subtype == "no_result":
            subtype = "error_during_execution"

    return subtype, turns, cost


async def run_step(
    name: str,
    system_prompt: str,
    prompt: str,
    goal: Callable[[TriageSession], tuple[bool, str]],
    audit: PermissionAudit,
    session: TriageSession | None = None,
    approver: Approver | None = None,
) -> StepResult:
    """Run one step to completion: the inner loop plus the retry guard."""
    ensure_workspace()
    session = session or TriageSession()
    approver = approver or DenyApprover()
    attempts = turns = 0
    cost = 0.0
    subtype = "no_result"
    reason = ""
    current_prompt = prompt

    while attempts < MAX_ATTEMPTS:
        attempts += 1
        if attempts > 1:
            _log("RETRY", f"attempt {attempts}/{MAX_ATTEMPTS}")

        subtype, step_turns, step_cost = await _run_once(
            system_prompt, current_prompt, session, audit, approver
        )
        turns += step_turns
        cost += step_cost

        met, reason = goal(session)
        if met:
            return StepResult(name, True, session, attempts, turns, cost, subtype)

        _log("GOAL", f"not met - {reason}")
        current_prompt = (
            f"The previous attempt did not finish, because {reason}. "
            f"Complete the task now.\n\n{prompt}"
        )

    return StepResult(name, False, session, attempts, turns, cost, subtype, reason)


def report(outcome: LoopOutcome) -> int:
    """Print the final state. Returns a process exit code."""
    print("\n" + "=" * 70)
    print("RUN SUMMARY")
    print("=" * 70)

    for step in outcome.steps:
        mark = "OK  " if step.goal_met else "FAIL"
        retried = f" ({step.attempts} attempts)" if step.attempts > 1 else ""
        print(f"  [{mark}] {step.name}{retried}")
        if not step.goal_met:
            print(f"         {step.reason}", file=sys.stderr)

    tickets = outcome.tickets
    if tickets:
        print("\n  tickets filed")
        for ticket in tickets:
            print(
                f"    {ticket.get('ticket_id'):<10} "
                f"{ticket.get('urgency'):<7} {ticket.get('topic'):<15} "
                f"-> {ticket.get('team')}"
                + ("  [review]" if ticket.get("needs_human_review") else "")
            )

    print("\n  filesystem")
    for path in outcome.written_files:
        print(f"    wrote   {path.relative_to(REPO_ROOT).as_posix()}")
    if not outcome.written_files:
        print("    wrote   (nothing)")

    print(f"\n  permissions  (approver: {outcome.approver_name})")
    for tool, reason, granted in outcome.audit.asked:
        verdict = "APPROVED" if granted else "REFUSED "
        print(f"    {verdict} {tool.replace('mcp__triage__', '')} - {reason}")
    for tool, reason in outcome.audit.denied:
        print(f"    DENIED   {tool.replace('mcp__triage__', '')} - {reason}")
    if not outcome.audit.asked and not outcome.audit.denied:
        print("    nothing required approval and nothing was denied")
    print(f"    {len(outcome.audit.auto)} call(s) auto-approved")

    print(
        f"\n  {len(outcome.steps)} steps · {outcome.turns} turns · "
        f"${outcome.cost_usd:.4f}"
    )
    print("=" * 70)
    print("GOAL MET" if outcome.goal_met else "GOAL NOT MET")

    return 0 if outcome.goal_met else 1
