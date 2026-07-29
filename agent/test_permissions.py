"""Offline checks for the permission strategy.

Every row of the policy table in permissions.py gets a check here, plus the two
enforcement points (the PreToolUse hook and the can_use_tool callback) and the
three approvers. The point is that the documented strategy and the implemented
one cannot drift apart silently.

Run:
    .venv\\Scripts\\python.exe test_permissions.py
"""

import asyncio
import os

from claude_agent_sdk import PermissionResultDeny
from fs_policy import OUTBOX, REPO_ROOT

# Absolute and outside the read root on both platforms. A Windows literal is
# only a relative filename under POSIX rules, so it would resolve *inside* the
# read root on Linux and invert this assertion.
SYSTEM_FILE = (
    "C:\\Windows\\System32\\config\\SAM" if os.name == "nt" else "/etc/passwd"
)
from permissions import (
    AUTO_APPROVED_TOOLS,
    CREATE_TICKET,
    AutoApprover,
    DenyApprover,
    PermissionAudit,
    PromptApprover,
    Tier,
    classify,
    make_can_use_tool,
    make_permission_hook,
)

passed = failed = 0

ROUTINE = {
    "urgency": "normal",
    "topic": "how_to",
    "team": "customer_success",
    "summary": "How do I export to CSV?",
    "rationale": "Product works as designed; routine.",
    "confidence": 0.92,
    "needs_human_review": False,
}


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}" + (f" - {detail}" if detail else ""))


def tier_of(tool: str, args: dict) -> Tier:
    return classify(tool, args).tier


async def main() -> int:
    print("auto-approved actions")
    check("read inside the repo", tier_of("Read", {"file_path": "lib/main.dart"}) is Tier.AUTO)
    check("glob inside the repo", tier_of("Glob", {"path": str(REPO_ROOT)}) is Tier.AUTO)
    check(
        "grep with no path",
        tier_of("Grep", {"pattern": "TODO"}) is Tier.AUTO,
    )
    check(
        "write inside the outbox",
        tier_of("Write", {"file_path": str(OUTBOX / "TICK-1.md")}) is Tier.AUTO,
    )
    for lookup in (
        "mcp__triage__look_up_customer",
        "mcp__triage__fetch_order",
        "mcp__triage__fetch_recent_tickets",
    ):
        check(f"{lookup.split('__')[-1]} is auto", tier_of(lookup, {"email": "a@b.c"}) is Tier.AUTO)
    check("a routine ticket files itself", tier_of(CREATE_TICKET, ROUTINE) is Tier.AUTO)

    print("\nactions requiring approval")
    check(
        "urgent pages on-call",
        tier_of(CREATE_TICKET, {**ROUTINE, "urgency": "urgent"}) is Tier.ASK,
    )
    check(
        "routing to trust_and_safety",
        tier_of(CREATE_TICKET, {**ROUTINE, "team": "trust_and_safety"}) is Tier.ASK,
    )
    check(
        "routing to retention",
        tier_of(CREATE_TICKET, {**ROUTINE, "team": "retention"}) is Tier.ASK,
    )
    check(
        "the agent's own uncertainty flag",
        tier_of(CREATE_TICKET, {**ROUTINE, "needs_human_review": True}) is Tier.ASK,
    )
    check(
        "the ask reason names why",
        "pages on-call"
        in classify(CREATE_TICKET, {**ROUTINE, "urgency": "urgent"}).reason,
    )

    print("\nbash allowlist (needed by the xlsx skill)")
    script = str(OUTBOX / "build_handover.py")
    check(
        "a plain python invocation inside the outbox is allowed",
        tier_of("Bash", {"command": f"python {script}"}) is Tier.AUTO,
    )
    check(
        "python3 is allowed too",
        tier_of("Bash", {"command": f"python3 {script}"}) is Tier.AUTO,
    )
    check(
        "flags are allowed alongside the script",
        tier_of("Bash", {"command": f"python -B {script}"}) is Tier.AUTO,
    )

    for name, command in [
        ("a non-python program", "ls -la /"),
        ("rm", "rm -rf /"),
        ("curl", "curl https://evil.example.com"),
        ("command chaining with ;", f"python {script} ; rm -rf /"),
        ("command chaining with &&", f"python {script} && curl evil.com"),
        ("piping", f"python {script} | sh"),
        ("backtick substitution", "python `whoami`"),
        ("dollar substitution", "python $(which sh)"),
        ("output redirection", f"python {script} > /etc/passwd"),
        ("input redirection", f"python {script} < /etc/shadow"),
        ("background execution", f"python {script} &"),
        ("newline smuggling", f"python {script}\nrm -rf /"),
        ("inline -c script", "python -c 'import os; os.system(\"rm -rf /\")'"),
        ("a script outside the outbox", "python /app/agent.py"),
        ("a script escaping via traversal", f"python {OUTBOX / '..' / '..' / 'x.py'}"),
        ("an empty command", "   "),
        ("an interpreter by absolute path outside the allowlist", "/bin/sh script.sh"),
    ]:
        check(f"denied: {name}", tier_of("Bash", {"command": command}) is Tier.DENY, command)

    check(
        "the denial explains itself",
        "not an allowed program" in classify("Bash", {"command": "ls"}).reason,
    )

    print("\nbundled skill scripts (step 10)")
    from permissions import SKILL_SCRIPT_ROOTS

    bundled = SKILL_SCRIPT_ROOTS[0] / "shift-handover" / "scripts" / "build_handover.py"
    tickets = OUTBOX / "tickets.json"
    workbook = OUTBOX / "handover.xlsx"

    check(
        "a bundled skill script may be executed",
        tier_of("Bash", {"command": f"python {bundled} {tickets} {workbook}"}) is Tier.AUTO,
    )
    check(
        "but its output path must still be in the outbox",
        tier_of("Bash", {"command": f"python {bundled} {tickets} /app/lib/evil.xlsx"})
        is Tier.DENY,
        "a trusted script must not be pointable at an untrusted destination",
    )
    check(
        "a script elsewhere under the skill dir is not executable",
        tier_of(
            "Bash",
            {"command": f"python {SKILL_SCRIPT_ROOTS[0] / 'shift-handover' / 'SKILL.md.py'}"},
        )
        is Tier.DENY,
        "only files directly inside a scripts/ directory count",
    )
    check(
        "a script outside both the skill roots and the outbox is denied",
        tier_of("Bash", {"command": "python /tmp/payload.py"}) is Tier.DENY,
    )
    check(
        "traversal out of a scripts/ directory is denied",
        tier_of(
            "Bash",
            {"command": f"python {SKILL_SCRIPT_ROOTS[0] / 'x' / 'scripts' / '..' / '..' / '..' / 'agent.py'}"},
        )
        is Tier.DENY,
    )
    check(
        "only the first .py argument is treated as the script",
        tier_of(
            "Bash",
            {"command": f"python {bundled} {SKILL_SCRIPT_ROOTS[0] / 'y' / 'scripts' / 'other.py'}"},
        )
        is Tier.DENY,
        "a second bundled path must still be checked as an ordinary argument",
    )

    print("\ndenied actions")
    check("web fetch", tier_of("WebFetch", {"url": "https://example.com"}) is Tier.DENY)
    check("web search", tier_of("WebSearch", {"query": "x"}) is Tier.DENY)
    check(
        "write outside the outbox",
        tier_of("Write", {"file_path": str(REPO_ROOT / "lib" / "main.dart")}) is Tier.DENY,
    )
    check(
        "write escaping via traversal",
        tier_of("Write", {"file_path": str(OUTBOX / ".." / ".." / "x.txt")}) is Tier.DENY,
    )
    check(
        "read outside the repo",
        tier_of("Read", {"file_path": SYSTEM_FILE}) is Tier.DENY,
    )
    check(
        "an unrecognised tool fails closed",
        tier_of("SomeFutureTool", {"x": 1}) is Tier.DENY,
    )

    print("\nthe documented surface matches the code")
    check(
        "create_ticket is NOT pre-approved, so it always reaches the callback",
        CREATE_TICKET not in AUTO_APPROVED_TOOLS,
    )
    check(
        "every pre-approved tool classifies as auto",
        all(
            classify(t, {"file_path": str(OUTBOX / "x.md")} if t == "Write" else {}).tier
            is Tier.AUTO
            for t in AUTO_APPROVED_TOOLS
        ),
    )

    print("\nhook enforcement (deny is hard)")
    audit = PermissionAudit()
    hook = make_permission_hook(audit)

    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(REPO_ROOT / "lib" / "main.dart")},
        },
        None,
        None,
    )
    check(
        "hook denies an out-of-bounds write",
        out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
    )
    check("hook records the denial", len(audit.denied) == 1)

    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": CREATE_TICKET,
            "tool_input": {**ROUTINE, "urgency": "urgent"},
        },
        None,
        None,
    )
    check(
        "hook routes a consequential ticket to ask",
        out.get("hookSpecificOutput", {}).get("permissionDecision") == "ask",
    )

    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "lib/main.dart"},
        },
        None,
        None,
    )
    check("hook passes an auto action through", out == {})

    print("\napproval gate")
    audit = PermissionAudit()
    gate = make_can_use_tool(DenyApprover(), audit)

    result = await gate(CREATE_TICKET, dict(ROUTINE), None)
    check("routine ticket is allowed without asking", result.behavior == "allow")
    check("auto decisions are audited", len(audit.auto) == 1)

    result = await gate(CREATE_TICKET, {**ROUTINE, "urgency": "urgent"}, None)
    check("unattended run refuses an urgent ticket", isinstance(result, PermissionResultDeny))
    check("the refusal explains itself to the model", "pages on-call" in result.message)
    check("the ask is audited as refused", audit.asked and audit.asked[-1][2] is False)

    audit = PermissionAudit()
    gate = make_can_use_tool(AutoApprover(), audit)
    result = await gate(CREATE_TICKET, {**ROUTINE, "team": "trust_and_safety"}, None)
    check("auto-approver grants the same call", result.behavior == "allow")
    check("the ask is audited as approved", audit.asked and audit.asked[-1][2] is True)

    result = await gate("Bash", {"command": "rm -rf /"}, None)
    check(
        "auto-approver still cannot grant a denied tool",
        isinstance(result, PermissionResultDeny),
        "DENY must beat the approver",
    )

    print("\napprover defaults")
    check("the unattended default is deny", DenyApprover().approve("x", {}, "r") is False)
    check("auto-approver approves", AutoApprover().approve("x", {}, "r") is True)
    check("prompt approver exists for operators", hasattr(PromptApprover, "approve"))

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
