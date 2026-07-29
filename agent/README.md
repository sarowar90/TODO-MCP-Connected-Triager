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

## What the skeleton does

One `query()` call against `claude-opus-5`, streaming messages as they arrive:

- **System prompt** points the agent at `lib/triage/triage_spec.md` rather than
  baking the rules into the prompt — the spec stays the single source of truth.
- **Tools** are read-only (`Read`, `Glob`, `Grep`) with `permission_mode="dontAsk"`,
  so the agent can inspect the repo but never write to it.
- **`cwd`** is the repo root, which is what scopes the agent's file access.

## Notes for later steps

- `ResultMessage.subtype` reports `"success"` even on a failed run — **`is_error`
  is the field that reflects the actual outcome.** The exit code here keys off
  `is_error`, with `api_error_status` and `errors` printed for diagnosis.
- `ResultMessage` is terminal: `break` there. Iterating past it makes the SDK
  raise `Claude Code returned an error result`; returning from inside the loop
  closes its async generator mid-run.
- On the auth-failure path the SDK prints a harmless
  `RuntimeError: aclose(): asynchronous generator is already running` during
  teardown. It does not affect the exit code. Worth re-checking on a successful
  run with a real key.
