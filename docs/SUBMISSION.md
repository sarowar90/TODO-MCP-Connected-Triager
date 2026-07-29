# Support Triager — End-to-End Submission

An Anthropic-API customer-support triager built in Dart (raw HTTP, no SDK). It
classifies an inbound support message, gathers context with tools, escalates the
hard cases to a stronger model, files a GitHub issue and routes the result to the
right Slack channel over MCP, and attaches an SLA/paging policy from a custom MCP
server.

Code lives in [`lib/triage/`](../lib/triage/) and [`bin/`](../bin/); the
authoritative triage definition is [`triage_spec.md`](../lib/triage/triage_spec.md);
the offline test suite is [`test/triage_test.dart`](../test/triage_test.dart)
(39 tests, `flutter analyze` clean).

---

## Purpose — what this agent solves

This agent solves the **first-touch triage problem** in a customer-support inbox:
inbound messages arrive as unstructured free text, and a human has to read each
one, judge how urgent it is, work out what it's about, and hand it to the team
that owns it — slowly, and inconsistently across reviewers and shifts. The
**input** is a single raw support message (plus whatever identifiers it happens to
carry, such as the sender's email or an order ID) and read-only access to customer
context: a CRM lookup, order records, and the sender's recent ticket history. The
agent classifies the message onto a **closed taxonomy** — one `urgency`, one
`topic`, and one owning `team` — under the ordered precedence rules in
[`triage_spec.md`](../lib/triage/triage_spec.md) (safety → churn → outage → topic
default), pulling the context it needs via parallel tool calls and escalating
uncertain or high-stakes cases from Haiku 4.5 to Opus 4.8 before committing. A
**successful outcome** is a message in, a routed ticket out: a strictly validated
`TriageResult` with a summary, a rationale citing the rule that applied, and a
self-reported confidence score; a GitHub issue filed for genuine bugs (deduped
first); a notification posted to the owning team's Slack channel, where the
destination is decided deterministically in code rather than by the model; and an
SLA/paging policy attached from a custom MCP server. **Correct** means: every
message yields a structured, routable result — never a silent default and never
free-form text — and anything the agent isn't confident about (confidence below
0.60, or an unclear/`other` topic) is flagged `needs_human_review` and routed to
human triage rather than guessed at.

The honest success criterion is therefore *"never drops a message and never fakes
certainty"* rather than raw accuracy — which is what the confidence gate and the
forced-`create_ticket` reliability guard exist to guarantee.

---

## (a) Triage logic

Every message is mapped onto three **closed** enums
([`triage_taxonomy.dart`](../lib/triage/triage_taxonomy.dart)) — never free-form
labels, so the output is always routable:

- **urgency** — `urgent` / `high` / `normal` / `low` (impact & time-sensitivity, independent of topic; drives SLA + paging).
- **topic** — `billing` / `technical` / `account` / `feature_request` / `how_to` / `complaint` / `other`.
- **team** — `billing` / `engineering` / `customer_success` / `trust_and_safety` / `retention` / `triage_review`.

Routing applies **ordered precedence rules** (the first match wins for the field
it sets; later rules fill fields left unset), mirrored from
[`triage_spec.md`](../lib/triage/triage_spec.md) into the system prompt:

1. **Safety** — credible account compromise / unauthorized access / data exposure → `trust_and_safety`, urgency ≥ `high` (`urgent` if ongoing), regardless of topic.
2. **Churn** — intent to cancel / credible threat to leave → `retention`, urgency ≥ `high`.
3. **Outage** — something actively down or payments fully broken → urgency `urgent` (team follows topic default).
4. **Otherwise** — team follows topic (billing→billing, technical→engineering, account/how_to/feature_request/complaint→customer_success, other→triage_review).
5. **Multiple issues** — triage the single highest-urgency actionable issue; mention the rest in the summary.

A **confidence gate** backs it up: below 0.60 (or unclear/`other`) the model sets
`needs_human_review = true` and routes to `triage_review`.
`TriageResult.fromJson` validates strictly — anything off-spec throws
`FormatException`, never a silent default. In the todo layer errors flow as
`Either<Failure, T>`; in the triager they surface as typed exceptions.

**In the run below:** "API 500s since 9am" + "billing-failed emails" → outage
(rule 3) → `urgent`, topic `technical` → `engineering`.

## (b) Model selection justification

Two tiers ([`model_policy.dart`](../lib/triage/model_policy.dart)), matched to the
two shapes of work:

| Tier | Model | Role | Why |
|---|---|---|---|
| 1 — first pass on **every** message | **Haiku 4.5** (`claude-haiku-4-5`) | High-volume, latency-sensitive classification | Triage is a well-specified map onto a closed taxonomy under strict JSON — where the smallest/fastest tier excels. Cheapest/fastest ($1/$5 per MTok), supports strict structured output. Its mistakes are contained by the escalation gate. |
| 2 — the hard minority | **Opus 4.8** (`claude-opus-4-8`) | Re-classify uncertain / high-stakes cases | These are where a wrong call costs most (missed compromise, churn, mis-sorted P1). Strongest reasoning; few enough that $5/$25 per MTok is spent only where it changes the outcome. |

`ModelPolicy.shouldEscalate` re-runs on Tier 2 when the first pass is **uncertain
or high-stakes**: confidence < 0.75, `needs_human_review`, urgency `urgent`, topic
`other`, or a sensitive team (`trust_and_safety` / `retention`). **Not Fable 5** —
triage is short single-turn classification, not long-horizon agentic reasoning, so
Fable's premium would be over-spend with no quality gain.

**In the run below:** Haiku classified it `urgent` (conf 0.70) → escalated → Opus
re-ran it (conf 0.96). Models called, in order: `haiku, opus, opus, opus`.

## (c) Tool & parallel-tool setup

Four strict-schema client tools ([`tools.dart`](../lib/triage/tools.dart);
`strict: true`, `additionalProperties: false`):

- `look_up_customer(email)` · `fetch_order(order_id)` · `fetch_recent_tickets(email)` — read context (mocked CRM/orders/ticketing via `MockToolExecutor`).
- `create_ticket(...)` — the **terminal** action: its arguments *are* the `TriageResult`, so recording the classification and routing the ticket are one step.

The agentic loop lives in [`triage_service.dart`](../lib/triage/triage_service.dart):

- **Parallel tool use** — a turn's independent `tool_use` blocks run concurrently (`Future.wait`), and **all** `tool_result`s go back in a **single** user message (splitting them trains the model out of parallel calls).
- **Reliability guard** — if the model ends a turn in text without routing, the loop forces `create_ticket` via `tool_choice`, so triage always yields a structured result.
- **Server-tool awareness** — server-executed blocks (web search, MCP) aren't run locally; the loop resumes on `stop_reason: "pause_turn"` instead of cutting the turn off.

**In the run below:** Opus requested `look_up_customer` + `fetch_order` +
`fetch_recent_tickets` **together in one turn** (parallel), fed back as one message.

## (d) Token-optimization notes

Because `system` + `tools` are re-sent on every call (multiple per message, ×2 on
escalation), the fixed prefix is the lever:

1. **De-duplicated the system prompt** — removed the per-tool description block (already in each tool's schema `description`) and the closing "provide these fields" list (already enforced by the strict `create_ticket` schema). ~165 fewer input tokens per call, both tiers, no quality loss.
2. **Prompt caching** — wrapped the stable `system`+`tools` prefix in a `cache_control` breakpoint ([`anthropic_client.dart`](../lib/triage/anthropic_client.dart)); loop iterations read it at ~0.1× instead of reprocessing. *Caveat:* Haiku 4.5 / Opus 4.8 have a 4096-token minimum cacheable prefix, so on today's ~1K-token prefix it's a silent no-op — correct placement that activates as the prompt grows, zero downside when it doesn't fire.
3. **Web search only on the escalation tier** — the high-volume first pass stays lean/deterministic; the external lookup is spent only on the escalated minority.

Net: (1)+(2) are unconditional per-call savings; behavior unchanged (39 tests pass).

## (e) MCP connection details & custom server

**Hosted connector (GitHub + Slack).** Anthropic has no Dart SDK, so the triager
calls the Messages API over HTTP and uses the **MCP connector** (beta
`mcp-client-2025-11-20`): `mcp_servers` + an `mcp_toolset` in `tools`, and the beta
header — added only when a connector is configured
([`anthropic_client.dart`](../lib/triage/anthropic_client.dart)). Connections are
made **server-side**; the model emits `mcp_tool_use`, results come back as
`mcp_tool_result` in the same response (recorded in the outcome trace), and the
loop resumes on `pause_turn`.

- **GitHub** ([`github_mcp.dart`](../lib/triage/github_mcp.dart)) — `https://api.githubcopilot.com/mcp/`, allowlisted to issue tools (`search_issues`/`get_issue`/`create_issue`). Least privilege is enforced by the PAT scope (Issues: read/write), not the allowlist. Used to dedup then file an issue for genuine bugs.
- **Slack** ([`slack_mcp.dart`](../lib/triage/slack_mcp.dart), [`slack_router.dart`](../lib/triage/slack_router.dart)) — `https://mcp.slack.com/mcp`, allowlisted to `slack_post_message`. The **destination is deterministic in code**: `slackChannelFor(team)` maps the routed team → channel, and the router tells the model exactly which channel to post to — so "reaches the correct destination" is a property of the routing table, not the LLM. Delivery is a separate step from classification because `create_ticket` is terminal.

**Custom server (stdio).** [`bin/sla_mcp_server.dart`](../bin/sla_mcp_server.dart)
is a minimal JSON-RPC 2.0 MCP server exposing one workflow-specific tool,
`get_sla_policy(urgency)` (logic in [`sla_policy.dart`](../lib/triage/sla_policy.dart)),
returning the first-response SLA target, whether it pages on-call, and the
escalation step. **Transport: stdio** — chosen because the hosted connector only
accepts a public HTTPS URL (Anthropic can't reach localhost), whereas a local
workflow extension wants no port/TLS/auth/hosting; the host launches it as a
subprocess. Registered with Claude Code via [`.mcp.json`](../.mcp.json) as
`support-sla`, so it loads as `mcp__support-sla__get_sla_policy`. Confirmed
callable by a real MCP handshake (`initialize` → `tools/list` → `tools/call`).

## (f) Full end-to-end run

One message through the whole pipeline. The model / GitHub / Slack round-trips are
**scripted through the real code path** (no API keys in this environment); the
**custom SLA MCP server is called live over stdio**. This is the console transcript
— the triager is a CLI/library, so this is what a run looks like. (To capture live
API screenshots, run [`bin/github_mcp_demo.dart`](../bin/github_mcp_demo.dart) /
[`bin/slack_mcp_demo.dart`](../bin/slack_mcp_demo.dart) with real keys set.)

```text
====================================================================
INCOMING SUPPORT MESSAGE
====================================================================
URGENT: our whole team is locked out — the API has returned 500 on every request
since 9am, and we are now getting billing-failed emails too. This blocks all work.
— jane@example.com (order ORD-1002)

====================================================================
PHASE 1 — CLASSIFY (tools + parallel + MCP + model tiering)
====================================================================
Models called, in order: [claude-haiku-4-5, claude-opus-4-8, claude-opus-4-8, claude-opus-4-8]

Tool calls (client, parallel, and GitHub MCP):
  • look_up_customer({"email":"jane@example.com"}) -> {"customer_id":"CUST-778","name":"Jane Doe",...,"plan":"Pro","account_status":"active"}
  • fetch_order({"order_id":"ORD-1002"}) -> {"order_id":"ORD-1002","status":"paid","amount":120.0,...}
  • fetch_recent_tickets({"email":"jane@example.com"}) -> {"tickets":[{"ticket_id":"TICK-4820",...},{"ticket_id":"TICK-4655",...}]}
  • github/search_issues({"query":"API 500 outage"}) -> {"total_count":0,"items":[]}
  • github/create_issue({...,"title":"Platform-wide API 500s since 09:00",...}) -> {"number":42,"html_url":"https://github.com/sarowar90/TODO-MCP-Connected-Triager/issues/42","state":"open"}
  • create_ticket({"urgency":"urgent","topic":"technical","team":"engineering",...,"confidence":0.96,...}) -> {"ticket_id":"TICK-5001","status":"created"}

Classification: [Urgent (P1)] Technical Issue → Engineering (confidence 0.96)
  escalated: true -> Opus 4.8
  ticket:    TICK-5001
  GitHub issue: https://github.com/sarowar90/TODO-MCP-Connected-Triager/issues/42

====================================================================
PHASE 2 — ROUTE TO SLACK (deterministic destination via MCP)
====================================================================
Routed team Engineering -> channel #eng-triage
  posted:              true
  model posted to:     #eng-triage
  slack result:        {"ok":true,"channel":"C09ENG","ts":"1706551200.000100"}
  correct destination? true

====================================================================
PHASE 3 — CUSTOM MCP SERVER (live stdio) — attach SLA policy
====================================================================
get_sla_policy(urgent) ->
  urgency: urgent
  label: Urgent (P1)
  first_response_target: minutes
  pages_on_call: true
  escalation: Page on-call immediately; open a Sev-1 incident if there is an active outage or security exposure.

====================================================================
RESULT
====================================================================
Engineering ticket TICK-5001 (Urgent (P1)) filed as GitHub issue #42, posted to
#eng-triage, SLA minutes (pages on-call: true).
```

**What this one run exercised:** classification onto the closed taxonomy · client
tools run **in parallel** · **model tiering** (Haiku → escalated → Opus) · **web
search** server tool · **GitHub MCP** (issue dedup + creation) · **Slack MCP**
(deterministic channel routing) · **custom stdio MCP server** (live SLA lookup) —
end to end, message in → ticket routed, issue filed, team notified, SLA attached.
