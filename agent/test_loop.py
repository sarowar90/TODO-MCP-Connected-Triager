"""Offline checks for the loop's tools and goal logic.

Deterministic and network-free, in the spirit of test/triage_test.dart: the
parts of the loop that decide *when the goal is met* are exercised without
touching the API. The live agent run is what test_loop.py cannot cover.

Run:
    .venv\\Scripts\\python.exe test_loop.py
"""

import asyncio

from triage_tools import (
    TriageSession,
    build_triage_tools,
    validate_result,
)

GOOD = {
    "urgency": "urgent",
    "topic": "technical",
    "team": "engineering",
    "summary": "Platform-wide API 500s since 09:00 blocking the whole team.",
    "rationale": "Rule 3: active outage, so urgency is urgent; topic default routes to engineering.",
    "confidence": 0.96,
    "needs_human_review": False,
}

passed = failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}" + (f" - {detail}" if detail else ""))


def _tool(tools: list, name: str):
    """Pull a tool's handler out of the built tool set by name."""
    for candidate in tools:
        if candidate.name == name:
            return candidate.handler
    raise KeyError(f"tool {name!r} not in {[t.name for t in tools]}")


async def main() -> int:
    print("validate_result")
    check("accepts an on-spec result", validate_result(GOOD) is None)
    check(
        "rejects an off-taxonomy team",
        validate_result({**GOOD, "team": "sales"}) is not None,
    )
    check(
        "rejects an off-taxonomy urgency",
        validate_result({**GOOD, "urgency": "P1"}) is not None,
    )
    check(
        "rejects confidence out of range",
        validate_result({**GOOD, "confidence": 1.5}) is not None,
    )
    check(
        "rejects low confidence without the review flag",
        validate_result({**GOOD, "confidence": 0.4}) is not None,
    )
    check(
        "rejects topic 'other' without the review flag",
        validate_result({**GOOD, "topic": "other"}) is not None,
    )
    check(
        "rejects review flag routed away from triage_review",
        validate_result({**GOOD, "needs_human_review": True}) is not None,
    )
    check(
        "accepts a low-confidence result that admits it",
        validate_result(
            {
                **GOOD,
                "confidence": 0.3,
                "needs_human_review": True,
                "team": "triage_review",
            }
        )
        is None,
    )
    check("rejects an empty summary", validate_result({**GOOD, "summary": " "}) is not None)

    print("\ncontext tools")
    session = TriageSession()
    tools = build_triage_tools(session)

    result = await _tool(tools,"look_up_customer")({"email": "jane@example.com"})
    check("look_up_customer finds a known customer", "CUST-778" in str(result))

    result = await _tool(tools,"look_up_customer")({"email": "nobody@example.com"})
    check("look_up_customer errors on an unknown email", result.get("is_error") is True)

    result = await _tool(tools,"fetch_order")({"order_id": "ord-1002"})
    check("fetch_order is case-insensitive on the id", "ORD-1002" in str(result))

    result = await _tool(tools,"fetch_recent_tickets")({"email": "jane@example.com"})
    check("fetch_recent_tickets returns prior tickets", "TICK-4820" in str(result))

    print("\ngoal check")
    check("goal is unmet before any ticket is filed", session.goal_met is False)

    bad = await _tool(tools,"create_ticket")({**GOOD, "team": "sales"})
    check("create_ticket rejects an off-spec result", bad.get("is_error") is True)
    check("a rejection leaves the goal unmet", session.goal_met is False)
    check("the rejection reason is recorded for the retry", len(session.rejections) == 1)

    ok = await _tool(tools,"create_ticket")(dict(GOOD))
    check("create_ticket accepts a valid result", ok.get("is_error") is None)
    check("filing a ticket meets the goal", session.goal_met is True)
    check(
        "the filed ticket carries an id",
        (session.ticket or {}).get("ticket_id", "").startswith("TICK-"),
    )
    check(
        "the tool sequence was recorded in order",
        session.tool_calls
        == [
            "look_up_customer",
            "look_up_customer",
            "fetch_order",
            "fetch_recent_tickets",
            "create_ticket",
            "create_ticket",
        ],
        str(session.tool_calls),
    )

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
