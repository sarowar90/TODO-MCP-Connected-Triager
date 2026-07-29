# Capstone Agent — Claude Agent SDK skeleton

Step 2 of the capstone: the Claude Agent SDK set up and confirmed running end
to end. This is the skeleton the production triage agent grows from.

The agent lives here in Python; the Dart triager in [`../lib/triage/`](../lib/triage/)
is the earlier implementation and stays as-is. Both read the same authoritative
spec, [`../lib/triage/triage_spec.md`](../lib/triage/triage_spec.md).

## Setup

Already done, but to recreate from scratch:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The SDK bundles its own Claude Code binary — no separate CLI install needed.

## Run

The SDK reads the key from the process environment and does **not** load `.env`
files automatically.

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
.\.venv\Scripts\python.exe agent.py

# or triage your own message
.\.venv\Scripts\python.exe agent.py "I was charged twice this month"
```

Exit codes: `0` success, `1` agent/API failure, `2` no API key set.

## Layout

| File | Role |
|---|---|
| [`agent.py`](agent.py) | Entry point: input resolution, API-key check, exit codes |
| [`plan.py`](plan.py) | Multi-step execution: decompose the goal, sequence it, carry results |
| [`loop.py`](loop.py) | The step runner — phase instrumentation, goal check, retry |
| [`triage_tools.py`](triage_tools.py) | The four custom tools + the closed taxonomy and validation |
| [`permissions.py`](permissions.py) | The permission strategy: tiers, enforcement, approvers |
| [`fs_policy.py`](fs_policy.py) | Filesystem roots and path containment checks |
| [`test_loop.py`](test_loop.py) | Offline checks of the tools and goal logic (21) |
| [`test_fs_policy.py`](test_fs_policy.py) | Offline checks of path containment (25) |
| [`test_plan.py`](test_plan.py) | Offline checks of plan decomposition and sequencing (22) |
| [`test_permissions.py`](test_permissions.py) | Offline checks of every policy row and both enforcement points (37) |
| `workspace/inbox/` | Input: support messages the agent reads |
| `workspace/outbox/` | Output: filed tickets the agent writes (gitignored) |

## Permission strategy

The dividing line is **consequence outside the agent's sandbox**, not risk of
error. Misreading a file costs a retry. Paging on-call at 3am, or routing a
security report to the wrong queue, costs someone's night or a missed breach —
and the agent cannot undo either.

| Action | Tier | Why |
|---|---|---|
| Read / Glob / Grep inside the repo | **auto** | read-only, reversible, no external effect |
| Customer / order / ticket-history lookups | **auto** | read-only queries against the mock CRM |
| Write inside the outbox | **auto** | the agent's own scratch space |
| `create_ticket`, routine | **auto** | the core job; a normal ticket is revisable |
| `create_ticket` → `trust_and_safety` | **ask** | security escalation; a wrong call is costly |
| `create_ticket` → `retention` | **ask** | churn save; commits a human to outreach |
| `create_ticket` at `urgency=urgent` | **ask** | pages on-call, per the spec's §1 |
| `create_ticket` with `needs_human_review` | **ask** | the agent has already said it is unsure |
| Write or edit outside the outbox | **deny** | would mutate source, the spec, or its own inputs |
| Read outside the repository | **deny** | no reason to reach the wider filesystem |
| Bash, WebFetch, WebSearch, subagents, anything unrecognised | **deny** | unguarded execution or egress; not needed here |

The ask tier deliberately mirrors the escalation triggers already in
`ModelPolicy.shouldEscalate` in the Dart triager — the cases worth spending a
stronger model on are the same ones worth spending a human on.

### How it's enforced

[`permissions.py`](permissions.py) is one `classify()` function consulted at
**two** points, so the hook and the callback can never disagree:

- A **`PreToolUse` hook** applies it first. Hooks run before every other step
  and a hook deny wins even under `bypassPermissions`, so denials can't be
  configured away later by an edit to `allowed_tools` or the permission mode.
- A **`can_use_tool` callback** applies it again at the approval step, which is
  where `ask` becomes a real decision.

Two points rather than one because each alone has a hole: a tool named in
`allowed_tools` is auto-approved and **never reaches `can_use_tool`**, so a
callback alone would be bypassed for the tools that matter most; and a hook
alone cannot prompt a human. `create_ticket` is deliberately **left out** of
`allowed_tools` so every ticket reaches the callback.

Unrecognised tools **fail closed** — a new tool is denied until the policy
names it, rather than inheriting the permission mode.

### Resolving an approval

| `--approve=` | Behaviour | Use |
|---|---|---|
| `deny` *(default)* | refuse anything needing approval, and tell the model why | unattended and scheduled runs |
| `prompt` | ask on the terminal, showing the ticket fields | an operator working the queue |
| `auto` | approve everything reaching `ask` | tests and trusted automation only |

Defaulting to `deny` is the point: a batch running on a schedule with nobody
watching must not be able to page on-call just because the model was confident.
The refusal is fed back as a tool result, so the agent can reclassify or leave
the ticket for a human rather than simply failing.

## Filesystem access

The agent reads inputs and writes outputs through a workspace:

| | Root | Why |
|---|---|---|
| Read | the repo | it must reach [`triage_spec.md`](../lib/triage/triage_spec.md) and the inbox |
| Write | `workspace/outbox/` only | the single place output belongs |

Containment is a **`PreToolUse` hook** ([`fs_policy.py`](fs_policy.py)), not a
permission rule or a `can_use_tool` callback. Two reasons, both from the SDK's
permission model:

- **Auto-approved tools never reach `can_use_tool`.** `Write` is in
  `allowed_tools` so the agent isn't prompted, which would silently bypass a
  callback guard. Hooks run *before* every other step, and a hook deny wins
  even under `bypassPermissions`.
- **A `Write(path)` permission rule is never matched** by the file permission
  checks — `Edit(path)` is what governs `Write` — so rule-based path scoping is
  easy to get subtly wrong.

Paths are resolved with `Path.resolve()` before the check, which collapses `..`
and follows symlinks, so traversal is normalised away rather than pattern-matched.
`Bash` is in `disallowed_tools`: it would be an unguarded write vector sitting
next to a carefully guarded `Write`.

## Multi-step execution

Running with no arguments triages the **whole inbox**. The goal is decomposed
into a plan before any work starts, then executed in sequence:

```
PLAN (built before any work starts)
  1. [ ] triage 001-api-outage.txt
  2. [ ] triage 002-double-charge.txt
  3. [ ] aggregate the batch into digest.md
```

The digest is what makes this multi-step rather than a loop over independent
items: it **cannot** run until the triage steps have produced tickets, it reads
those tickets back off disk, and its goal check asserts it accounts for every
ticket id the earlier steps returned. Intermediate results travel two ways —
in `PlanContext` (ticket ids, step results) and on disk (the ticket files).

A failed triage step does **not** abort the batch. The remaining messages are
still processed and the digest covers whatever was filed, because one
unparseable message shouldn't strand the rest of the queue. The run as a whole
still reports failure.

Pass a filename or a literal message to triage just that one — the single-step
path, no digest.

## The loop

The Agent SDK runs the **inner** cycle: Claude decides, the SDK executes tools,
results feed back, repeat until it stops calling tools. `loop.py` adds three
things on top, exposed as a reusable `run_step` that both the triage and digest
steps are built from:

1. **Phase instrumentation** — every message is tagged `DECIDE` (Claude's
   reasoning), `ACT` (a tool call), or `OBSERVE` (the tool result), so the
   gather → decide → act → observe cycle is visible as it runs.
2. **The goal lives in code, not the prompt.** The task is done when a
   *validated* ticket exists (`TriageSession.goal_met`) — not when the model
   says it is done.
3. **An outer loop.** If Claude stops without meeting the goal, the reason is
   fed back and it runs again (`MAX_ATTEMPTS = 2`). This is the reliability
   guard from the Dart triager: triage always yields a routable result.

Tools are the Python counterpart of `MockToolExecutor`: `look_up_customer`,
`fetch_order`, `fetch_recent_tickets` (read-only, so the SDK may run them in
parallel) and the terminal `create_ticket`, whose arguments *are* the
TriageResult. `create_ticket` validates against the closed taxonomy and the
confidence gate, returning `is_error` on anything off-spec — so a bad
classification comes back as an observation the agent can correct, rather than
being silently persisted.

The agent also gets `Read` and `Glob` to fetch
[`triage_spec.md`](../lib/triage/triage_spec.md) itself; `tools=["Read","Glob"]`
drops every other built-in from its context.

## Tests

```powershell
.\.venv\Scripts\python.exe test_loop.py         # 21 checks
.\.venv\Scripts\python.exe test_fs_policy.py    # 25 checks
.\.venv\Scripts\python.exe test_plan.py         # 22 checks
.\.venv\Scripts\python.exe test_permissions.py  # 37 checks
```

105 checks, no network and no API key. They cover the taxonomy validation, the
confidence gate, each context tool, the goal transitions, plan decomposition
and sequencing, containment (traversal, absolute paths outside the repo, and a
sibling directory whose name merely *starts with* `outbox`), and every row of
the permission table above — including that an auto-approver still cannot grant
a denied tool.

**What they cannot cover is the live agent run.** Every goal check, hook, and
plan transition here is verified against synthetic inputs; none of it has yet
been exercised by the model actually driving the SDK. That needs a key.

## Notes for later steps

- `ResultMessage.subtype` reports `"success"` even on a failed run — **`is_error`
  is the field that reflects the actual outcome.** The exit code here keys off
  `is_error`, with `api_error_status` and `errors` printed for diagnosis.
- **Iterate the stream to completion; do not `break` on `ResultMessage`.**
  Trailing system events can arrive after it. `query()` then raises by design
  once an error result has been yielded, so the loop belongs inside a `try`.
  (Step 2 broke on the result instead — `loop.py` corrects that.)
- On the auth-failure path the SDK prints a harmless
  `RuntimeError: aclose(): asynchronous generator is already running` during
  teardown. It does not affect the exit code. Worth re-checking on a successful
  run with a real key.
