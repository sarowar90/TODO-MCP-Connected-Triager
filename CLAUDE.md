# CLAUDE.md

Guidance for working in this repository.

## What this repo is

Two things live side by side in one Flutter/Dart package (`todo_app`):

1. **A Flutter todo app** — offline task manager (`lib/main.dart`, `lib/core/`, `lib/features/todo/`).
2. **An Anthropic customer-support triager** — classifies support messages and routes them to teams, calling Claude via the Messages API with tool use (`lib/triage/`, `bin/`).

The two are independent; the triager was built incrementally on top of the todo app and does not depend on it.

Dart SDK: `^3.12.2` (uses null-aware map elements like `'key': ?value`).

## Commands

```bash
flutter pub get                      # install dependencies
flutter analyze                      # static analysis — keep it clean (zero issues)
flutter test                         # run the whole suite
flutter test test/triage_test.dart   # run just the triager tests
flutter run                          # launch the todo app

# Triager CLI scripts (need a key: set ANTHROPIC_API_KEY first)
dart run bin/api_check.dart          # Step 1 — confirm API connectivity
dart run bin/triage_demo.dart        # end-to-end triage of a sample message (streams live)
```

Set the key per shell before running the scripts:
- PowerShell: `$env:ANTHROPIC_API_KEY="sk-ant-..."`
- Bash: `export ANTHROPIC_API_KEY="sk-ant-..."`

## Architecture — todo app (`lib/features/todo/`)

Feature-first **clean architecture**, dependencies point inward (presentation → domain ← data):

- `domain/` — pure Dart: `Todo` entity, `TodoRepository` interface, use cases. No Flutter/sqflite.
- `data/` — `TodoModel` (⇄ DB rows), `TodoLocalDataSource` (raw sqflite CRUD), `TodoRepositoryImpl` (converts exceptions → typed `Failure`s).
- `presentation/` — Riverpod DI + `TodoListNotifier`/`TodoListState`, pages, widgets.
- `core/` — `AppDatabase` (sqflite singleton + schema), `Failure`/`DatabaseException`.

Errors flow as `Either<Failure, T>` (dartz); the UI never sees raw exceptions. State management is **Riverpod**; persistence is **sqflite**.

## Architecture — triager (`lib/triage/`)

Anthropic has no official Dart SDK, so the triager calls the **REST API directly over HTTP** (`http` package). Key files:

- `triage_taxonomy.dart` — closed enums `Urgency` / `Topic` / `Team` + `TriageResult`. `TriageResult.fromJson` validates strictly (throws `FormatException` on anything off-spec — never silently defaults).
- `triage_spec.md` — **the authoritative definition of correct triage**: category meanings + ordered precedence rules (safety → churn → outage → topic-default) + worked examples. Source of truth; keep enums and the system prompt in sync with it.
- `model_policy.dart` — two-tier model policy: **Haiku 4.5** (`claude-haiku-4-5`) first-pass on every message; escalate to **Opus 4.8** (`claude-opus-4-8`) via `ModelPolicy.shouldEscalate` when uncertain/high-stakes.
- `tools.dart` — three strict-schema tools (`look_up_customer`, `fetch_order`, `fetch_recent_tickets`, `create_ticket`) + `MockToolExecutor` (in-memory stand-in for CRM/orders/ticketing). **`create_ticket` is the terminal tool — its arguments ARE the `TriageResult`.**
- `anthropic_client.dart` — `MessagesApi` (abstract) + `AnthropicClient` (real HTTP). Supports non-streaming (`createMessage`) and **SSE streaming** (`createMessageStreaming`), which rebuilds the same `{content, stop_reason}` map so the loop is streaming-agnostic.
- `triage_service.dart` — `TriageService.triage()` runs the agentic tool-use loop, applies the tiering, and returns a `TriageOutcome` (result + tool-call trace + ticket id). `buildTriageSystemPrompt()` mirrors `triage_spec.md`, pulling allowed values from the enums so the prompt can't drift.

### Triager conventions & invariants

- **Model IDs** are exact strings: `claude-haiku-4-5`, `claude-opus-4-8`. Don't append date suffixes.
- **API key** always from the `ANTHROPIC_API_KEY` env var — never hardcode it.
- **Parallel tool use**: a turn's independent `tool_use` blocks run concurrently (`Future.wait`), and all `tool_result`s go back in a **single** user message (splitting them trains the model out of parallel calls).
- **Reliability guard**: if the model ends a turn in text without routing, the loop forces `create_ticket` via `tool_choice`, so triage always yields a structured, routable result.
- Tools use **strict schemas** (`strict: true`, `additionalProperties: false`) so tool inputs validate exactly.

## Testing

- `test/triage_test.dart` — offline, deterministic. The tool-use loop is driven by a scripted `FakeMessagesApi` (extends `MessagesApi`); SSE reconstruction is tested with `MockClient.streaming` delivering the body in tiny chunks. No network/API key needed.
- `test/widget_test.dart` — todo app widget test; overrides `todoRepositoryProvider` with an in-memory fake.
- Prefer testing against the `MessagesApi` interface with fakes over hitting the real API.

## Conventions

- Keep `flutter analyze` at zero issues. The `prefer_initializing_formals` lint fires spuriously on private named constructor params → suppressed with a file-level `// ignore_for_file` where needed.
- Match the surrounding style: doc comments on public types, `Either`/typed failures in the todo layer, strict JSON validation in the triager.
- Temporary/scratch files do not belong in the repo.
