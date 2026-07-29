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
| [`hooks.py`](hooks.py) | Lifecycle hooks and the run journal |
| [`checkpoints.py`](checkpoints.py) | Workspace snapshots and rollback |
| [`demo_rollback.py`](demo_rollback.py) | Offline demonstration of recovering from a bad change |
| [`demo_xlsx.py`](demo_xlsx.py) | Offline demonstration that the handover workbook opens and is correct |
| [`demo_skill.py`](demo_skill.py) | Offline demonstration of the custom skill's workflow |
| [`preflight.py`](preflight.py) | Production readiness gate — run before deploying |
| [`runner.py`](runner.py) | Production entrypoint: lock, signals, JSON logs, exit codes |
| [`skills/`](skills/shift-handover/SKILL.md) | Our own skills — committed |
| [`vendor-skills/`](vendor-skills/README.md) | Third-party skills — installed locally, gitignored |
| [`Dockerfile`](Dockerfile) / [`compose.yaml`](compose.yaml) | Container image and hardened run configuration |
| [`HOSTING.md`](HOSTING.md) | How it is sandboxed and hosted, and what each layer prevents |
| [`fs_policy.py`](fs_policy.py) | Filesystem roots and path containment checks |
| [`test_loop.py`](test_loop.py) | Offline checks of the tools and goal logic (21) |
| [`test_fs_policy.py`](test_fs_policy.py) | Offline checks of path containment (25) |
| [`test_plan.py`](test_plan.py) | Offline checks of plan decomposition and sequencing (22) |
| [`test_permissions.py`](test_permissions.py) | Offline checks of every policy row and both enforcement points (37) |
| `workspace/inbox/` | Input: support messages the agent reads |
| `workspace/outbox/` | Output: filed tickets the agent writes (gitignored) |

## Running it sandboxed

```bash
docker build -f agent/Dockerfile -t triage-agent .      # from the repo root
export ANTHROPIC_API_KEY=sk-ant-...
docker compose -f agent/compose.yaml run --rm agent
```

Non-root, read-only root filesystem, all capabilities dropped, capped memory
and pids, read-only inbox mount, and no API key in the image. The image ships
the triage spec and the agent's code but **not** the Flutter application
source, so the agent cannot read or leak it.

Full rationale, the layer-by-layer threat model, and the known gaps (no egress
proxy, no `SessionStore`, image never built) are in [`HOSTING.md`](HOSTING.md).

## The custom skill: `shift-handover`

[`skills/shift-handover/`](skills/shift-handover/SKILL.md) is our own skill —
version-controlled and committed, unlike the third-party ones.

```
skills/shift-handover/
├── SKILL.md                      frontmatter + workflow
├── scripts/build_handover.py     bundled builder (the supported way to make the workbook)
└── reference/format.md           the exact layout, for verification
```

```powershell
.\.venv\Scripts\python.exe demo_skill.py   # offline: walk the whole workflow
.\.venv\Scripts\python.exe test_skill.py   # 38 checks
```

**The description is written to prevent over-triggering as much as to enable
triggering.** It says when to use the skill (after every ticket in a batch is
filed, to aggregate them), what it produces (`digest.md`, `handover.xlsx`), and
explicitly when *not* to — not for classifying, not for a single ticket, not
before the tickets exist. A description that only says what a skill does will
get it invoked on everything vaguely adjacent.

The bundled script exists so the agent doesn't re-invent spreadsheet code each
run: column order, `COUNTIF` formulas, the frozen header, the autofilter, and
the amber fill on rows needing review are fixed rather than improvised. It
fails loudly — non-zero exit, a message naming the offending ticket and field,
and **no half-written file** — so a failure tells the agent what to fix.

### It forced a permission change

The skill's script lives in `skills/shift-handover/scripts/`, not the outbox,
so the step-9 Bash allowlist would have denied it. The rule is now: the
*script* may come from a skill's `scripts/` directory (version-controlled code
we ship, in a location the agent cannot write to), but **every other path
argument must still be inside the outbox** — so a trusted script cannot be
pointed at an untrusted destination. Six further checks in
[`test_permissions.py`](test_permissions.py) cover that boundary, including
traversal out of `scripts/` and a second `.py` argument being treated as an
ordinary path rather than a second script.

## Document output (pre-built xlsx skill)

The digest step produces a shift-handover workbook, `workspace/outbox/handover.xlsx`,
using Anthropic's pre-built **xlsx** skill: a `Tickets` sheet with one row per
ticket and fixed headers, plus a `Summary` sheet whose counts are `COUNTIF`
formulas rather than typed totals.

```powershell
.\.venv\Scripts\python.exe demo_xlsx.py   # offline: build, validate, read back
```

The pre-built xlsx skill is **not vendored** — Anthropic's document skills are
source-available, not open source, so installing them is a licensing decision
left to you. See [`vendor-skills/README.md`](vendor-skills/README.md) for the
one-line install; that directory is gitignored and excluded from the Docker
build context, so source-available content can't be committed or baked into a
published image by accident.

It is not required: the custom `shift-handover` skill above builds the workbook
on its own.

Two things this forced, both deliberate:

- **Loaded as a plugin path, not a project setting source.** `setting_sources=["project"]`
  would load skills *and* re-enable the repo's `CLAUDE.md` and `.claude/` — the
  leak closed in step 8. `plugins=[{"type":"local","path":…}]` loads the skill
  directory and nothing else.
- **`Bash` had to come back, narrowly.** A document skill builds its file by
  running Python, so the blanket deny from step 6 would deny the skill. Bash is
  now admitted only as a single `python`/`python3` invocation, with no shell
  metacharacters and no path outside the outbox. `python -c` is rejected
  outright, since an inline script's contents can't be checked as paths. 20
  allowlist checks in [`test_permissions.py`](test_permissions.py) cover the
  attack shapes: `;`, `&&`, `|`, backticks, `$( )`, redirection, `&`, newline
  smuggling, and scripts outside the outbox.

`loop.check_workbook()` **opens** the result with openpyxl rather than checking
that a file exists — a document that won't open isn't a deliverable, and "the
agent said it wrote it" isn't evidence. It fails the step if the workbook is
missing, unopenable, missing the `Tickets` sheet, missing a column, or missing
a ticket.

## Checkpointing and rollback

```powershell
.\.venv\Scripts\python.exe demo_rollback.py   # offline, no API key
```

`run_step` takes a checkpoint of the outbox **before** each step, so a step
that fails its goal leaves the outbox exactly as it found it rather than
half-written. [`demo_rollback.py`](demo_rollback.py) shows recovery from a
deliberately bad change: a later step overwrites one ticket with garbage,
deletes another, and writes a wrong digest. The restore rewrites the corrupted
file, recreates the deleted one, and removes the file that was added after the
snapshot — verified by re-checking drift, which comes back empty.

### Why not just the SDK's checkpointing

The SDK tracks `Write`/`Edit`/`NotebookEdit` and can `rewind_files()` to a
checkpoint UUID. Two properties make it insufficient **on its own here**:

- **Checkpoints are tied to the session that created them.** This agent runs
  *one session per step*, so a checkpoint from triaging message 1 cannot be
  rewound from the digest step's session. The single most valuable rollback —
  "the digest step trashed the tickets the earlier steps produced" — spans
  sessions, and is precisely what the SDK's mechanism cannot reach.
- **File content only.** Creating and deleting files is not fully undone.

So `enable_file_checkpointing=True` is set (with
`extra_args={"replay-user-messages": None}`, which is what makes the UUIDs
appear) and the first user-message UUID is captured per step for *within*-step
rewind, while [`checkpoints.py`](checkpoints.py) covers the whole run. They are
complementary. **Caveat:** calling `rewind_files()` requires `ClaudeSDKClient`,
and this loop uses `query()` — so the SDK-side restore path is enabled and
captured but not yet invoked. The workspace store is what actually performs
rollbacks today.

## Lifecycle hooks

[`hooks.py`](hooks.py) wires six events to one journal — the run's audit trail,
and what you read after an unattended batch to find out why the outbox looks
the way it does. Hooks run in your process, not the agent's context window, so
they cost no tokens.

| Hook | Purpose |
|---|---|
| `UserPromptSubmit` | a step begins — open a journal entry |
| `PreToolUse` | the permission decision (see below) **and** journal it |
| `PostToolUse` | record the call; flag file mutations |
| `PostToolUseFailure` | record tool errors |
| `PreCompact` | note compaction, which can drop detail from the transcript |
| `Stop` | the step finished — close the entry |

`PreToolUse` carries the permission policy as well as journalling, because a
single `PreToolUse` hook has to return the permission decision — splitting it
would leave one hook returning `{}` and muddy where the verdict comes from.

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

## Orchestration

Two agent roles with different tool surfaces, not one agent doing everything:

| Role | Denied | Why |
|---|---|---|
| **triage** (×N, concurrent) | `Bash`, `Skill` | it classifies; no reason to run a shell or build a document |
| **handover** (×1, after) | the CRM lookups | it aggregates what is on disk; re-querying could contradict a filed ticket |

Triage messages are independent so they **fan out concurrently**, bounded to
`MAX_CONCURRENCY = 4` — the SDK spawns one subprocess per session and a wide
fanout is the documented way to hit rate limits. One agent crashing doesn't take
the batch down (`return_exceptions=True`).

**Not SDK subagents, deliberately:** subagent file edits aren't tracked by file
checkpointing, which would void the step-7 rollback guarantee. Each message
already gets its own session, so the context isolation subagents provide is
already there. What was missing was concurrency and least privilege.

Concurrency forced one change: **per-step checkpoints are disabled during the
fan-out**, since concurrent steps share one outbox and restoring one would
clobber a sibling. The orchestrator takes a single batch checkpoint instead.

## Deployment

`agent.py` is the interactive CLI. **`runner.py` is what a scheduler or a
container entrypoint should call**, because unattended execution needs four
things the interactive path doesn't:

- **a single-instance lock** — batches share one outbox, and cron will happily
  start a second run while the first is going; overlapping runs interleave
  writes into the same ticket files. A lock held by a dead process is
  reclaimed, so one crash doesn't wedge the schedule forever;
- **graceful SIGTERM** — a container stop finishes the step in flight instead
  of dying mid-write; a second signal exits immediately;
- **JSON logs on stdout** — nothing reads a pretty console in production;
- **exit codes a scheduler can branch on**: `0` done, `1` goal not met, `69`
  unavailable, `70` internal error, `75` another run holds the lock (retry
  later), `78` environment unfit.

```powershell
.\.venv\Scripts\python.exe runner.py                 # preflight, then the batch
.\.venv\Scripts\python.exe preflight.py              # readiness gate alone
.\.venv\Scripts\python.exe preflight.py --check-api  # also prove auth works
```

`preflight.py` is also the container `HEALTHCHECK`, so the container goes
unhealthy if credentials are revoked, the outbox stops being writable, or the
policy is edited open.

[`.github/workflows/agent-ci.yml`](../.github/workflows/agent-ci.yml) runs all
eight suites and three demos on `ubuntu-latest` — the first thing here that
executes off a Windows laptop, which matters because several code paths are
platform-sensitive. It needs no API key and uploads the generated workbook as
an artifact.

**The agent has not been deployed.** See [`HOSTING.md`](HOSTING.md) for the
verification-status table and the list of what deployment still needs.

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
.\.venv\Scripts\python.exe test_plan.py         # 27 checks
.\.venv\Scripts\python.exe test_permissions.py  # 63 checks
.\.venv\Scripts\python.exe test_checkpoints.py  # 41 checks
.\.venv\Scripts\python.exe test_hosting.py      # 39 checks
.\.venv\Scripts\python.exe test_skill.py        # 38 checks
.\.venv\Scripts\python.exe test_deploy.py       # 50 checks
.\.venv\Scripts\python.exe test_runner.py       # 32 checks
```

338 checks, no network and no API key. They cover the taxonomy validation, the
confidence gate, each context tool, the goal transitions, plan decomposition
and sequencing, containment (traversal, absolute paths outside the repo, and a
sibling directory whose name merely *starts with* `outbox`), and every row of
the permission table above — including that an auto-approver still cannot grant
a denied tool — plus checkpoint snapshot/restore/drift and every lifecycle hook.

Checkpoint rollback is additionally **demonstrated end to end** by
[`demo_rollback.py`](demo_rollback.py), which needs no key: it is the one part
of this agent whose behaviour has actually been observed rather than asserted.

**What the tests cannot cover is the live agent run.** Every goal check, hook,
permission tier, and plan transition here is verified against synthetic inputs;
none of it has yet been exercised by the model actually driving the SDK. That
needs a key.

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
