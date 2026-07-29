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
| [`loop.py`](loop.py) | The autonomous loop — phase instrumentation, goal check, retry |
| [`triage_tools.py`](triage_tools.py) | The four custom tools + the closed taxonomy and validation |
| [`fs_policy.py`](fs_policy.py) | Filesystem containment: roots, path checks, the PreToolUse guard |
| [`test_loop.py`](test_loop.py) | Offline checks of the tools and goal logic (21) |
| [`test_fs_policy.py`](test_fs_policy.py) | Offline checks of containment (31) |
| `workspace/inbox/` | Input: support messages the agent reads |
| `workspace/outbox/` | Output: filed tickets the agent writes (gitignored) |

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

## The loop

The Agent SDK runs the **inner** cycle: Claude decides, the SDK executes tools,
results feed back, repeat until it stops calling tools. `loop.py` adds three
things on top:

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
.\.venv\Scripts\python.exe test_loop.py       # 21 checks
.\.venv\Scripts\python.exe test_fs_policy.py  # 31 checks
```

52 checks, no network and no API key. They cover the taxonomy validation, the
confidence gate, each context tool, the goal transition (unmet → rejected →
met), and containment — including traversal, absolute paths outside the repo,
and a sibling directory whose name merely *starts with* `outbox`.

What they cannot cover is the live agent run — that needs a key.

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
