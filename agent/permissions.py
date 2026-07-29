"""The agent's permission strategy.

One policy engine, consulted at two enforcement points, so there is exactly one
place to read and change what the agent may do.

    classify(tool, input) -> AUTO | ASK | DENY

  * A PreToolUse hook applies it first. Hooks run before every other step in
    the permission flow and a hook deny wins even under `bypassPermissions`,
    so hard denials cannot be configured away by a later change to
    `allowed_tools` or the permission mode.
  * A can_use_tool callback applies it again for anything that reaches the
    approval step, which is where ASK is turned into a real decision.

Both points call the same function, so the hook and the callback can never
disagree.

Why two points rather than one: a tool named in `allowed_tools` is
auto-approved and never reaches `can_use_tool`, so a callback alone would be
silently bypassed for the tools that matter most. Conversely a hook alone
cannot prompt a human. The pairing covers both.

THE STRATEGY
------------
The dividing line is *consequence outside the agent's sandbox*, not risk of
error. Reading a file and misreading it costs a retry. Paging on-call at 3am,
or routing a security report to the wrong queue, costs someone's night or a
missed breach — and cannot be undone by the agent.

| Action                                        | Tier | Why                                        |
|-----------------------------------------------|------|--------------------------------------------|
| Read / Glob / Grep inside the repo            | AUTO | read-only, reversible, no external effect  |
| Customer / order / ticket-history lookups     | AUTO | read-only queries against the mock CRM     |
| Write inside the outbox                       | AUTO | the agent's own scratch space              |
| create_ticket, routine                        | AUTO | the core job; a normal ticket is revisable |
| create_ticket routed to trust_and_safety      | ASK  | security escalation; wrong call is costly  |
| create_ticket routed to retention             | ASK  | churn save; commits a human to outreach    |
| create_ticket at urgency=urgent               | ASK  | pages on-call per triage_spec.md section 1 |
| create_ticket with needs_human_review         | ASK  | the agent has already said it is unsure    |
| Write or edit anywhere outside the outbox     | DENY | would mutate source, the spec, or inputs   |
| Read outside the repository                   | DENY | no reason to reach the wider filesystem    |
| Bash, WebFetch, WebSearch, and anything else  | DENY | unguarded execution / egress; not needed   |

ASK is resolved by an Approver. In production the default is deny-on-unattended
(`DenyApprover`): a run with nobody watching must not silently page on-call. An
interactive operator gets `PromptApprover`; `AutoApprover` exists for tests and
must be opted into explicitly.
"""

import os
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Protocol

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from fs_policy import READ_TOOLS, WRITE_TOOLS, check_access, target_path

CREATE_TICKET = "mcp__triage__create_ticket"

# Read-only context lookups: safe to run unattended.
LOOKUP_TOOLS = (
    "mcp__triage__look_up_customer",
    "mcp__triage__fetch_order",
    "mcp__triage__fetch_recent_tickets",
)

# Teams whose work commits a human to act on the agent's judgement.
SENSITIVE_TEAMS = ("trust_and_safety", "retention")

# Urgency that pages on-call (triage_spec.md section 1).
PAGING_URGENCIES = ("urgent",)

# Never available, whatever else is configured.
DENIED_TOOLS = (
    "BashOutput",
    "KillShell",
    "WebFetch",
    "WebSearch",
    "NotebookEdit",
    "Task",
    "Agent",
)

# --- Bash, narrowly ----------------------------------------------------------
# Step 6 denied Bash outright, as an unguarded write vector sitting next to a
# carefully guarded Write. The xlsx skill changes that calculation: a document
# skill generates its file by running Python, so denying Bash denies the skill.
#
# Rather than drop the denial, Bash is admitted through an allowlist:
#   * the command must invoke the interpreter, nothing else;
#   * no shell metacharacters, so one approved command cannot smuggle a second;
#   * every filesystem-looking argument must sit inside the outbox.
# Anything failing those is denied exactly as before. A blocklist would not be
# sufficient here — this is an allowlist, and unrecognised shapes fail closed.
BASH_ALLOWED_PROGRAMS = ("python", "python3")

# Chaining, substitution, redirection: each turns one vetted command into an
# arbitrary one, so their mere presence is disqualifying.
SHELL_METACHARACTERS = (";", "&&", "||", "|", "`", "$(", ">", "<", "\n", "&")


class Tier(str, Enum):
    AUTO = "auto"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    tier: Tier
    reason: str

    @property
    def allowed(self) -> bool:
        return self.tier is Tier.AUTO


def classify(tool_name: str, tool_input: dict[str, Any]) -> Decision:
    """The whole policy, in evaluation order. Deny beats ask beats auto."""

    # 1. Tools that are never available.
    if tool_name in DENIED_TOOLS:
        return Decision(Tier.DENY, f"{tool_name} is not available to this agent")

    # 2. Filesystem containment, for anything naming a path.
    if tool_name in WRITE_TOOLS or tool_name in READ_TOOLS:
        ok, reason = check_access(tool_name, tool_input)
        if not ok:
            return Decision(Tier.DENY, reason)
        if target_path(tool_input) is not None:
            return Decision(Tier.AUTO, reason)
        return Decision(Tier.AUTO, "no filesystem target")

    # 3. Bash, only in the narrow shape a document skill needs.
    if tool_name == "Bash":
        return _classify_bash(str((tool_input or {}).get("command", "")))

    # 4. Read-only context lookups, and invoking a skill. Invoking a skill is
    # itself harmless — it loads instructions. What the skill then *does* is
    # re-checked here call by call, so a skill cannot widen its own access.
    if tool_name in LOOKUP_TOOLS:
        return Decision(Tier.AUTO, "read-only context lookup")
    if tool_name == "Skill":
        return Decision(Tier.AUTO, "loading a skill; its actions are re-checked")

    # 5. The consequential action: filing and routing a ticket.
    if tool_name == CREATE_TICKET:
        return _classify_ticket(tool_input)

    # 6. Anything unrecognised fails closed rather than open.
    return Decision(Tier.DENY, f"{tool_name} is not part of the agent's tool surface")


def _classify_bash(command: str) -> Decision:
    """Admit only `python <args>` with no shell tricks and no paths outside the
    outbox. Everything else is denied, as it was before the skill existed."""
    stripped = command.strip()
    if not stripped:
        return Decision(Tier.DENY, "empty bash command")

    for meta in SHELL_METACHARACTERS:
        if meta in stripped:
            return Decision(
                Tier.DENY,
                f"bash command contains {meta!r}; only a single plain "
                f"{'/'.join(BASH_ALLOWED_PROGRAMS)} invocation is permitted",
            )

    # Split the way the host shell would. posix=True treats a backslash as an
    # escape, which mangles Windows paths into something that then fails the
    # containment check for the wrong reason; posix=False keeps them intact.
    try:
        parts = shlex.split(stripped, posix=(os.name != "nt"))
    except ValueError as exc:
        return Decision(Tier.DENY, f"bash command does not parse: {exc}")
    parts = [p.strip('"').strip("'") for p in parts]
    if not parts:
        return Decision(Tier.DENY, "empty bash command")

    program = PurePosixPath(parts[0]).name
    if program not in BASH_ALLOWED_PROGRAMS:
        return Decision(
            Tier.DENY,
            f"{program!r} is not an allowed program; only "
            f"{'/'.join(BASH_ALLOWED_PROGRAMS)} may be run",
        )

    # Any argument that looks like a path must live inside the outbox. `-c`
    # inline scripts are rejected outright: their contents are not inspectable
    # as paths, so they could write anywhere the process can reach.
    for arg in parts[1:]:
        if arg == "-c":
            return Decision(
                Tier.DENY,
                "inline `python -c` is not permitted; the skill must run a "
                "script file inside the outbox",
            )
        if arg.startswith("-"):
            continue
        if "/" in arg or "\\" in arg or arg.endswith((".py", ".xlsx", ".csv")):
            ok, reason = check_access("Write", {"file_path": arg})
            if not ok:
                return Decision(Tier.DENY, f"bash argument {arg!r}: {reason}")

    return Decision(Tier.AUTO, "python invocation confined to the outbox")


def _classify_ticket(args: dict[str, Any]) -> Decision:
    """Routine tickets file themselves; consequential ones need a human."""
    team = args.get("team")
    urgency = args.get("urgency")

    if args.get("needs_human_review"):
        return Decision(Tier.ASK, "the agent flagged this ticket for human review")
    if team in SENSITIVE_TEAMS:
        return Decision(Tier.ASK, f"routing to {team} commits a human to act")
    if urgency in PAGING_URGENCIES:
        return Decision(Tier.ASK, f"urgency={urgency} pages on-call")
    return Decision(Tier.AUTO, "routine ticket")


# --- resolving ASK -----------------------------------------------------------


class Approver(Protocol):
    """How an ASK decision becomes a yes or a no."""

    name: str

    def approve(self, tool_name: str, tool_input: dict[str, Any], reason: str) -> bool: ...


class DenyApprover:
    """Unattended default: refuse anything needing a human, and say so.

    Failing closed is the point. A batch running on a schedule with nobody
    watching must not be able to page on-call or open a security escalation
    just because the model was confident.
    """

    name = "deny (unattended)"

    def approve(self, tool_name: str, tool_input: dict[str, Any], reason: str) -> bool:
        return False


class AutoApprover:
    """Approve everything that reaches ASK. Tests and trusted automation only."""

    name = "auto-approve (explicitly enabled)"

    def approve(self, tool_name: str, tool_input: dict[str, Any], reason: str) -> bool:
        return True


class PromptApprover:
    """Ask a human on the terminal."""

    name = "interactive"

    def approve(self, tool_name: str, tool_input: dict[str, Any], reason: str) -> bool:
        short = tool_name.replace("mcp__triage__", "")
        print(f"\n    APPROVAL NEEDED: {short}")
        print(f"      because: {reason}")
        for key in ("urgency", "topic", "team", "confidence", "summary"):
            if key in tool_input:
                print(f"      {key:<11} {tool_input[key]}")
        try:
            answer = input("      approve? [y/N] ").strip().lower()
        except EOFError:
            print("      no input available - denying")
            return False
        return answer in ("y", "yes")


APPROVERS = {
    "deny": DenyApprover,
    "auto": AutoApprover,
    "prompt": PromptApprover,
}


@dataclass
class PermissionAudit:
    """Every decision the policy made, for the run report."""

    auto: list[tuple[str, str]] = None  # type: ignore[assignment]
    asked: list[tuple[str, str, bool]] = None  # type: ignore[assignment]
    denied: list[tuple[str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.auto = self.auto or []
        self.asked = self.asked or []
        self.denied = self.denied or []


def make_permission_hook(audit: PermissionAudit):
    """PreToolUse hook: enforce DENY, and record everything.

    ASK is not resolved here — a hook cannot prompt. It is passed through to
    the callback below, which is the only place a human is consulted.
    """

    async def permission_hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input") or {}
        decision = classify(tool_name, tool_input)

        if decision.tier is Tier.DENY:
            audit.denied.append((tool_name, decision.reason))
            return {
                "hookSpecificOutput": {
                    "hookEventName": input_data.get("hook_event_name", "PreToolUse"),
                    "permissionDecision": "deny",
                    "permissionDecisionReason": decision.reason,
                }
            }

        if decision.tier is Tier.ASK:
            return {
                "hookSpecificOutput": {
                    "hookEventName": input_data.get("hook_event_name", "PreToolUse"),
                    "permissionDecision": "ask",
                    "permissionDecisionReason": decision.reason,
                }
            }

        return {}

    return permission_hook


def make_can_use_tool(approver: Approver, audit: PermissionAudit):
    """The approval gate: turns ASK into an actual decision."""

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any,
    ) -> PermissionResultAllow | PermissionResultDeny:
        decision = classify(tool_name, tool_input)

        if decision.tier is Tier.DENY:
            audit.denied.append((tool_name, decision.reason))
            return PermissionResultDeny(message=decision.reason)

        if decision.tier is Tier.AUTO:
            audit.auto.append((tool_name, decision.reason))
            return PermissionResultAllow()

        granted = approver.approve(tool_name, tool_input, decision.reason)
        audit.asked.append((tool_name, decision.reason, granted))
        if granted:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=(
                f"Denied: {decision.reason}. Approval was required and not "
                f"granted ({approver.name}). If this ticket does not truly need "
                f"escalation, reclassify it; otherwise leave it for a human."
            )
        )

    return can_use_tool


# Tools pre-approved in `allowed_tools`. create_ticket is deliberately absent:
# leaving it out guarantees every ticket reaches can_use_tool, so the ASK tier
# is enforced by the callback and does not depend on hook `ask` semantics.
AUTO_APPROVED_TOOLS = ["Read", "Glob", "Grep", "Write", "Skill", *LOOKUP_TOOLS]
