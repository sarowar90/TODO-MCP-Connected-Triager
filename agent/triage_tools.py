"""Custom tools the triage agent acts through.

These are the Python counterpart of `MockToolExecutor` in the Dart triager:
three read-only context lookups plus one terminal action. Backing data is an
in-memory stand-in for CRM / orders / ticketing.

`create_ticket` is the terminal tool — its arguments *are* the TriageResult, so
recording the classification and routing the ticket are one step. It validates
strictly against the closed taxonomy and returns `is_error` on anything
off-spec, which lets the agent observe the rejection and correct itself rather
than silently persisting a bad value.
"""

from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

# --- Closed taxonomy (mirrors lib/triage/triage_taxonomy.dart) ----------------

URGENCIES = ("urgent", "high", "normal", "low")
TOPICS = (
    "billing",
    "technical",
    "account",
    "feature_request",
    "how_to",
    "complaint",
    "other",
)
TEAMS = (
    "billing",
    "engineering",
    "customer_success",
    "trust_and_safety",
    "retention",
    "triage_review",
)

REVIEW_THRESHOLD = 0.60

# --- Mock backing data -------------------------------------------------------

CUSTOMERS: dict[str, dict[str, Any]] = {
    "jane@example.com": {
        "customer_id": "CUST-778",
        "name": "Jane Doe",
        "plan": "Pro",
        "account_status": "active",
        "since": "2023-02-11",
    },
    "sam@example.com": {
        "customer_id": "CUST-901",
        "name": "Sam Rivera",
        "plan": "Starter",
        "account_status": "active",
        "since": "2025-08-30",
    },
}

ORDERS: dict[str, dict[str, Any]] = {
    "ORD-1002": {
        "order_id": "ORD-1002",
        "email": "jane@example.com",
        "status": "paid",
        "amount": 120.0,
        "placed_at": "2026-07-01",
    },
    "ORD-1003": {
        "order_id": "ORD-1003",
        "email": "sam@example.com",
        "status": "payment_failed",
        "amount": 40.0,
        "placed_at": "2026-07-20",
    },
}

TICKETS: dict[str, list[dict[str, Any]]] = {
    "jane@example.com": [
        {"ticket_id": "TICK-4820", "subject": "API latency spike", "status": "closed"},
        {"ticket_id": "TICK-4655", "subject": "Invoice question", "status": "closed"},
    ],
    "sam@example.com": [],
}


@dataclass
class TriageSession:
    """Records what the agent did, so the loop can verify the goal was met."""

    ticket: dict[str, Any] | None = None
    tool_calls: list[str] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)

    @property
    def goal_met(self) -> bool:
        """The goal is a filed, structured, routable ticket — nothing less."""
        return self.ticket is not None


def _text(payload: str, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": payload}]}
    if is_error:
        result["is_error"] = True
    return result


def build_triage_tools(session: TriageSession) -> list[Any]:
    """Build the tool set, bound to one session's recorder.

    Returned as `SdkMcpTool` objects so tests can invoke `.handler` directly
    without going through the API.
    """

    read_only = ToolAnnotations(readOnlyHint=True)

    @tool(
        "look_up_customer",
        "Look up a customer's CRM record by email address.",
        {"email": str},
        annotations=read_only,
    )
    async def look_up_customer(args: dict[str, Any]) -> dict[str, Any]:
        session.tool_calls.append("look_up_customer")
        record = CUSTOMERS.get(args["email"].strip().lower())
        if record is None:
            return _text(f"No customer found for {args['email']}.", is_error=True)
        return _text(str(record))

    @tool(
        "fetch_order",
        "Fetch a single order by its order id (e.g. ORD-1002).",
        {"order_id": str},
        annotations=read_only,
    )
    async def fetch_order(args: dict[str, Any]) -> dict[str, Any]:
        session.tool_calls.append("fetch_order")
        record = ORDERS.get(args["order_id"].strip().upper())
        if record is None:
            return _text(f"No order found for {args['order_id']}.", is_error=True)
        return _text(str(record))

    @tool(
        "fetch_recent_tickets",
        "Fetch a customer's recent support tickets by email address.",
        {"email": str},
        annotations=read_only,
    )
    async def fetch_recent_tickets(args: dict[str, Any]) -> dict[str, Any]:
        session.tool_calls.append("fetch_recent_tickets")
        found = TICKETS.get(args["email"].strip().lower())
        if found is None:
            return _text(f"No customer found for {args['email']}.", is_error=True)
        return _text(str({"tickets": found}))

    @tool(
        "create_ticket",
        "Terminal action: file the triaged ticket and route it to a team. Call "
        "this exactly once, after gathering context. Its arguments are the "
        "triage result.",
        {
            "type": "object",
            "properties": {
                "urgency": {"type": "string", "enum": list(URGENCIES)},
                "topic": {"type": "string", "enum": list(TOPICS)},
                "team": {"type": "string", "enum": list(TEAMS)},
                "summary": {
                    "type": "string",
                    "description": "One sentence restating the core issue.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why this urgency/topic/team; cite the precedence rule applied.",
                },
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "needs_human_review": {"type": "boolean"},
            },
            "required": [
                "urgency",
                "topic",
                "team",
                "summary",
                "rationale",
                "confidence",
                "needs_human_review",
            ],
            "additionalProperties": False,
        },
    )
    async def create_ticket(args: dict[str, Any]) -> dict[str, Any]:
        session.tool_calls.append("create_ticket")
        problem = validate_result(args)
        if problem:
            session.rejections.append(problem)
            return _text(f"Rejected: {problem}", is_error=True)

        session.ticket = dict(args)
        ticket_id = f"TICK-{5000 + len(session.tool_calls)}"
        session.ticket["ticket_id"] = ticket_id
        return _text(str({"ticket_id": ticket_id, "status": "created"}))

    return [look_up_customer, fetch_order, fetch_recent_tickets, create_ticket]


def build_triage_server(session: TriageSession):
    """Wrap the tool set in the in-process MCP server the agent connects to."""
    return create_sdk_mcp_server(
        name="triage",
        version="1.0.0",
        tools=build_triage_tools(session),
    )


def validate_result(args: dict[str, Any]) -> str | None:
    """Return a rejection reason, or None if the result is on-spec.

    Enforced beyond the JSON schema: the confidence gate from triage_spec.md
    section 4 rule 6. A low-confidence or unclear result must admit it.
    """
    if args["urgency"] not in URGENCIES:
        return f"urgency {args['urgency']!r} is not one of {URGENCIES}"
    if args["topic"] not in TOPICS:
        return f"topic {args['topic']!r} is not one of {TOPICS}"
    if args["team"] not in TEAMS:
        return f"team {args['team']!r} is not one of {TEAMS}"

    confidence = args["confidence"]
    if not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
        return f"confidence {confidence!r} is not between 0.0 and 1.0"

    low_confidence = confidence < REVIEW_THRESHOLD
    unclear = args["topic"] == "other"
    if (low_confidence or unclear) and not args["needs_human_review"]:
        return (
            "confidence below 0.60 or topic 'other' requires "
            "needs_human_review=true and team='triage_review'"
        )
    if args["needs_human_review"] and args["team"] != "triage_review":
        return "needs_human_review=true requires team='triage_review'"

    if not args["summary"].strip():
        return "summary must not be empty"
    if not args["rationale"].strip():
        return "rationale must not be empty"
    return None


# Fully-qualified names, per the mcp__{server}__{tool} convention.
TOOL_NAMES = [
    "mcp__triage__look_up_customer",
    "mcp__triage__fetch_order",
    "mcp__triage__fetch_recent_tickets",
    "mcp__triage__create_ticket",
]
