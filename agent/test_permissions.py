"""Offline checks for the permission strategy.

Every row of the policy table in permissions.py gets a check here, plus the two
enforcement points (the PreToolUse hook and the can_use_tool callback) and the
three approvers. The point is that the documented strategy and the implemented
one cannot drift apart silently.

Run:
    .venv\\Scripts\\python.exe test_permissions.py
"""

import asyncio

from claude_agent_sdk import PermissionResultDeny
from fs_policy import OUTBOX, REPO_ROOT
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

    print("\ndenied actions")
    check("bash", tier_of("Bash", {"command": "ls"}) is Tier.DENY)
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
        tier_of("Read", {"file_path": "C:\\Windows\\System32\\config\\SAM"}) is Tier.DENY,
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
