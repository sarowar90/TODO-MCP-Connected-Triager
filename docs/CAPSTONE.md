# Capstone submission — Support Triager Agent

An autonomous customer-support triage agent built on the **Claude Agent SDK**
(Python). It reads support messages, classifies them against a written spec,
files routable tickets, and hands the batch over to the next shift as a digest
and a spreadsheet.

Code: [`agent/`](../agent/) · Hosting rationale: [`agent/HOSTING.md`](../agent/HOSTING.md) ·
Step 11 detail: [`STEP-11.md`](STEP-11.md)

> **Honest status up front.** Everything in (a)–(g) is built and covered by 338
> offline checks. **The agent has never run against the Claude API** — no
> `ANTHROPIC_API_KEY` was available at any point in this project — and the
> container image has never been built. Section (h) says exactly what the demo
> transcript does and does not show. Nothing here should be read as a claim
> that the agent has run in production.

---

## (a) Purpose

The agent solves **first-touch triage** in a customer-support inbox. Messages
arrive as unstructured text and a human must read each one, judge its urgency,
work out what it is about, and route it — slowly, and inconsistently across
reviewers and shifts.

**Input:** a raw support message (plus any identifiers it carries, such as an
email or order id) and read-only access to customer context — a CRM lookup,
order records, and the sender's recent tickets.

**Output:** a strictly validated `TriageResult` — one `urgency`, one `topic`,
one owning `team`, a summary, a rationale citing the rule applied, and a
confidence score — written to disk as a ticket, then aggregated into a shift
handover.

**Correct means:** every message yields a structured, routable result — never a
silent default, never free-form text — and anything the agent is not confident
about (confidence below 0.60, or an unclear/`other` topic) is flagged
`needs_human_review` and routed to human triage rather than guessed at. The
honest success criterion is *never drops a message and never fakes certainty*,
not raw accuracy.

The taxonomy and the precedence rules live in
[`lib/triage/triage_spec.md`](../lib/triage/triage_spec.md), which the agent
**reads at runtime** rather than having the rules baked into its prompt — so the
spec stays the single source of truth.

## (b) Core loop

The SDK supplies the inner cycle: Claude decides, the SDK executes tools,
results feed back, repeat until it stops calling tools.
[`loop.py`](../agent/loop.py) adds three things on top, exposed as a reusable
`run_step` that every phase is built from:

1. **Phase instrumentation.** Every message is tagged `DECIDE` (reasoning),
   `ACT` (a tool call) or `OBSERVE` (a result), so the gather → decide → act →
   observe cycle is visible while it runs.
2. **The goal lives in code, not the prompt.** A step is done when a *validated
   ticket exists and has been written* — not when the model says it is done.
3. **An outer loop.** If Claude stops without meeting the goal, the reason is
   fed back and it runs again (`MAX_ATTEMPTS = 2`). This is the reliability
   guard: triage always yields a routable result.

The agent acts through four custom in-process MCP tools: `look_up_customer`,
`fetch_order`, `fetch_recent_tickets` (read-only, so the SDK may run them in
parallel) and the terminal `create_ticket`, whose arguments *are* the
`TriageResult`. `create_ticket` validates against the closed taxonomy and the
confidence gate and returns `is_error` on anything off-spec — so a bad
classification comes back as an observation the agent can correct, rather than
being silently persisted.

Two SDK details worth recording, both learned the hard way:

- **`ResultMessage.subtype` reported `"success"` on a run that failed with HTTP
  401.** `is_error` is the field that reflects the outcome; keying the exit code
  off `subtype` produced a silent false pass.
- **Iterating past `ResultMessage` makes the SDK raise**, and returning from
  inside the loop closes its async generator mid-run. The loop breaks at the
  terminal result, inside a `try`.

## (c) Permission strategy

The dividing line is **consequence outside the sandbox**, not risk of error.
Misreading a file costs a retry; paging on-call at 3am or misrouting a security
report costs someone's night or a missed breach, and the agent cannot undo
either.

| Tier | Actions |
|---|---|
| **auto** | reads inside the repo, the three CRM lookups, writes to the outbox, routine `create_ticket`, invoking a skill |
| **ask** | `create_ticket` routed to `trust_and_safety` or `retention`, at `urgency=urgent` (pages on-call), or carrying `needs_human_review` |
| **deny** | writes outside the outbox, reads outside the repo, `WebFetch`/`WebSearch`/subagents, and **anything unrecognised** |

The ask tier deliberately mirrors `ModelPolicy.shouldEscalate` in the earlier
Dart triager: the cases worth a stronger model are the same ones worth a human.

**Enforced at two points from one `classify()`**, because each alone has a hole:
a tool named in `allowed_tools` is auto-approved and **never reaches
`can_use_tool`**, so a callback alone is bypassed for the tools that matter
most; and a hook cannot prompt a human. A `PreToolUse` hook carries the hard
denials (it runs before every other step and wins even under
`bypassPermissions`); the `can_use_tool` callback resolves `ask`.
`create_ticket` is deliberately **excluded** from `allowed_tools` so every
ticket reaches the gate.

Approval mode is selectable: `--approve=deny` (default, unattended — a
scheduled batch must not page on-call because the model was confident),
`prompt` (an operator), `auto` (tests only). A refusal is fed back as a tool
result so the agent can reclassify rather than simply failing.

**`Bash` is the one deliberate loosening.** It was denied outright until a
document skill needed to run Python. It is now admitted only as a single
`python`/`python3` invocation, with no shell metacharacters, no `-c` inline
script, and every path argument inside the outbox — except the script itself,
which may also come from a skill's `scripts/` directory (version-controlled
code, in a location the agent cannot write to). 26 checks cover the attack
shapes: `;`, `&&`, `|`, backticks, `$( )`, redirection, `&`, newline smuggling,
traversal out of `scripts/`, and a second `.py` argument.

## (d) Hooks and checkpointing

**Six lifecycle hooks** ([`hooks.py`](../agent/hooks.py)) feed one journal —
the run's audit trail, and what you read after an unattended batch to find out
why the outbox looks the way it does. Hooks run in the host process, not the
agent's context window, so they cost no tokens.

| Hook | Purpose |
|---|---|
| `UserPromptSubmit` | a step begins |
| `PreToolUse` | the permission decision **and** journal it |
| `PostToolUse` | record the call; flag file mutations |
| `PostToolUseFailure` | record tool errors |
| `PreCompact` | note compaction, which can drop transcript detail |
| `Stop` | the step finished |

`PreToolUse` carries the policy as well as journalling, because a single
`PreToolUse` hook must return the verdict — splitting it would obscure where
the decision comes from.

**Checkpointing** ([`checkpoints.py`](../agent/checkpoints.py)) snapshots the
outbox before each step, so a step that fails its goal leaves the outbox exactly
as it found it. Restoring makes the outbox match the snapshot *exactly*:
modified files rewritten, deleted files restored, files created afterwards
removed.

This sits **alongside** the SDK's own file checkpointing rather than replacing
it, for a specific reason: SDK checkpoints are **tied to the session that
created them**, and this agent runs one session per step. The single most
valuable rollback — *the digest step trashed what the triage steps produced* —
spans sessions, which `rewind_files()` cannot reach. The SDK's mechanism is
enabled (`enable_file_checkpointing=True` with `replay-user-messages`) and the
per-step UUID captured; invoking `rewind_files()` needs `ClaudeSDKClient` and
this loop uses `query()`, so that path is enabled and captured but not called.

**Demonstrated:** [`demo_rollback.py`](../agent/demo_rollback.py) corrupts one
ticket, deletes another and writes a bad digest, then rolls back and verifies
recovery by re-checking drift.

## (e) Sandbox and hosting

Six layers, each assuming the one above can fail:

| Layer | Prevents |
|---|---|
| 1. Tool surface | the agent never has subagents or web tools to begin with |
| 2. Permission policy | out-of-bounds writes, unattended consequential tickets |
| 3. Settings isolation | host `CLAUDE.md`, `.claude/` and auto-memory loading into context |
| 4. Container filesystem | any write outside the workspace volume |
| 5. Container privileges | escalation, capability abuse, fork bombs, runaway memory |
| 6. Network | reaching anything but the Anthropic API |

The image runs **non-root (uid 10001, no login shell)** with a **read-only root
filesystem**, `cap_drop: ALL`, `no-new-privileges`, capped memory/CPU/pids, a
**read-only inbox mount**, and **no API key baked in**. It ships the triage spec
and the agent's code but deliberately **not** the Flutter application source — a
container that cannot see it cannot leak it.

Two bugs this surfaced:

- **The read-root trap.** `fs_policy` derived its read root as the agent
  directory's parent — which is **`/`** once the code sits at `/app`. Containerising
  would have silently widened containment to the entire filesystem. Now
  overridable via `AGENT_REPO_ROOT`, with a test asserting `/etc/passwd` stays
  unreadable under that layout.
- **A settings leak.** `query()` loads project settings by default, and the
  agent's `cwd` is the repo root — so a `CLAUDE.md` written for *human
  contributors* was being pulled into the triage agent's prompt. Closed with
  `setting_sources=[]` plus `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (which loads
  regardless) and a per-run `CLAUDE_CONFIG_DIR`.

**What the container does not give**, stated in `HOSTING.md` rather than glossed:
no domain-level egress restriction on a bridge network; no protection of the API
key from the agent's own process (part of why `Bash` is constrained rather than
merely sandboxed); and no defence against a container escape — Docker is a
namespace boundary, not a hypervisor.

**Production entrypoint.** [`runner.py`](../agent/runner.py) provides what
unattended execution needs and the interactive CLI does not: a single-instance
lock (batches share one outbox; cron will start a second run while the first is
going), graceful SIGTERM, JSON logs on stdout, and exit codes a scheduler can
branch on (`75` = lock held, retry; `78` = environment unfit).
[`preflight.py`](../agent/preflight.py) is the readiness gate and the container
`HEALTHCHECK`.

## (f) Skills

**Pre-built — `xlsx`.** Wired via `plugins=[{"type": "local", "path": …}]`
rather than `setting_sources=["project"]`, because the project source would
re-open the `CLAUDE.md` leak closed above. **Deliberately not vendored:**
Anthropic's document skills are *source-available, not open source*, so
`agent/vendor-skills/` is gitignored and excluded from the Docker build context
— installing them is a licensing decision left to the repository owner, and
baking them into a published image would be redistribution.

**Custom — `shift-handover`** ([`SKILL.md`](../agent/skills/shift-handover/SKILL.md)),
committed, with YAML frontmatter and two bundled resources:

```
skills/shift-handover/
├── SKILL.md                     frontmatter + workflow
├── scripts/build_handover.py    the builder — column order, COUNTIF totals,
│                                frozen header, autofilter, amber review rows
└── reference/format.md          exact layout, for verification
```

The description is written to **prevent over-triggering as much as to enable
triggering**: it states when to use the skill, what it produces, and explicitly
when *not* to — not for classifying, not for a single ticket, not before the
tickets exist. A description that only says what a skill does gets it invoked on
everything adjacent.

The bundled script exists so the agent does not re-invent spreadsheet code each
run, and it **fails loudly** — non-zero exit, a message naming the offending
ticket and field, and no half-written file.

`check_workbook()` **opens** the result with openpyxl rather than checking that
a file exists: a document that will not open is not a deliverable, and "the
agent said it wrote it" is not evidence.

## (g) Orchestration

Two agent roles with deliberately different tool surfaces:

| Role | Instances | Denied | Why |
|---|---|---|---|
| **triage** | N, concurrent | `Bash`, `Skill` | it classifies; no reason to run a shell or build documents |
| **handover** | 1, after | the CRM lookups | it aggregates what is on disk; re-querying could contradict a filed ticket |

Denials are bare names in `disallowed_tools`, removing the definition from the
model's context entirely. Triage messages are independent so they **fan out
concurrently** under a semaphore (`MAX_CONCURRENCY = 4`); one agent crashing
does not take the batch down.

**SDK subagents were deliberately not used**, because subagent file edits are
**not tracked by file checkpointing** — using them would silently void the
rollback guarantee in (d). Each message already runs in its own session, so the
context isolation subagents provide was already present. What was missing was
concurrency and least privilege, and neither needed subagents.

Concurrency forced one change: **per-step checkpoints are disabled during the
fan-out**, since concurrent steps share one outbox and restoring one would
clobber a sibling. The orchestrator takes a single batch checkpoint instead.

## (h) Demo Day run

**Transcript: [`demo-run.txt`](demo-run.txt)** — generated by
[`agent/demo.py`](../agent/demo.py), which adapts to the environment.

**What the committed transcript shows.** It was recorded with **no
`ANTHROPIC_API_KEY` present**, so **the agent loop was not exercised**. It
covers the deterministic parts end to end:

| Segment | Shows |
|---|---|
| Preflight | the readiness gate correctly reporting NOT READY without credentials |
| Checkpoint rollback | a later step corrupting one ticket, deleting another and writing a bad digest — then full recovery, verified by re-checking drift |
| Document output | the handover workbook built, validated by the agent's own goal check, and re-opened as a real `.xlsx` |
| Custom skill workflow | discovery, the skill's documented command passing the permission policy while three abuse variants are refused, the bundled script running, and the goal check accepting the result |

**What it does not show:** the model reading a support message, choosing tools,
classifying against the spec, or invoking the skill. That requires a key.

**To produce the live transcript**, the same command adapts automatically:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python agent/demo.py          # runs preflight --check-api, then a full batch
```

It then records the real run: inbox read, messages triaged concurrently,
tickets filed, digest and workbook produced — replacing `docs/demo-run.txt`
with a live transcript.

---

## Evidence

```bash
# 338 offline checks across nine suites — no network, no API key
for t in test_loop test_fs_policy test_plan test_permissions test_checkpoints \
         test_hosting test_skill test_deploy test_runner; do python "$t.py"; done

# the Demo Day transcript
python demo.py
```

| Suite | Checks | Covers |
|---|---|---|
| `test_loop` | 21 | tool surface, taxonomy validation, goal transitions |
| `test_fs_policy` | 25 | path containment, traversal, sibling-prefix directories |
| `test_plan` | 27 | plan decomposition, sequencing, workbook goal |
| `test_permissions` | 63 | every policy row, both enforcement points, the Bash allowlist |
| `test_checkpoints` | 41 | snapshot/restore/drift, all six hooks |
| `test_hosting` | 41 | read-root override, image hardening, context hygiene |
| `test_skill` | 38 | frontmatter, bundled script, failure behaviour |
| `test_deploy` | 50 | roles, concurrency bound, CI workflow, preflight |
| `test_runner` | 32 | lock, stale reclamation, signals, JSON logs, exit codes |

CI (`.github/workflows/agent-ci.yml`) runs all of it on `ubuntu-latest` — the
only execution of this code off the development machine. It needs no API key.

## Known gaps

| Gap | Status |
|---|---|
| Agent run against the live API | **Never** — no key available |
| Container image built or run | **Never** — Docker not installed |
| Deployed to production | **No** |
| CI result read | Configured; not read (`gh` unauthenticated) |
| Egress proxy | Documented, not implemented |
| `SessionStore` for transcript durability | Not implemented |
| OTEL exporter configuration | Not configured |
| Cron/queue schedule | `runner.py` is the entrypoint; nothing schedules it |

The first three need only an API key and Docker; the last four are genuine
outstanding engineering work.
