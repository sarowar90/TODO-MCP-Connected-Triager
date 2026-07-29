# Step 11 — Orchestration and deployment

Submission notes. Code lives in [`agent/`](../agent/); the hosting rationale and
the full gap list are in [`agent/HOSTING.md`](../agent/HOSTING.md).

**Status in one line:** orchestration is complete and verified; the agent is
**not** deployed — it has never run against the API, and the container image has
never been built.

---

## (a) Orchestration

The task benefits from it, so it was added. Triage of N support messages is
embarrassingly parallel — each message is independent — and two agent roles with
genuinely different needs already existed, running sequentially.

**Two roles, each with a minimal tool surface:**

| Role | Instances | Has | Denied | Why |
|---|---|---|---|---|
| **triage** | N, concurrent | Read/Write/Glob/Grep, CRM lookups, `create_ticket` | `Bash`, `Skill` | it classifies; no reason to run a shell or build documents |
| **handover** | 1, after | Read/Write/Glob/Grep, `Bash`, `Skill` | the three CRM lookups | it aggregates what is already on disk; re-querying the CRM would let it contradict a filed ticket |

Denials are bare names in `disallowed_tools`, which removes the tool definition
from the model's context entirely rather than leaving it merely unapproved.

**Fan-out / fan-in.** Triage steps run concurrently under a semaphore
(`MAX_CONCURRENCY = 4`); the handover step runs afterwards, sequentially,
because it consumes their output. `asyncio.gather(..., return_exceptions=True)`
means one agent crashing does not take the batch down.

**SDK subagents were deliberately not used.** Three reasons:

1. Subagent file edits are **not tracked by file checkpointing**, so using them
   would silently void the rollback guarantee built in step 7 — the most
   valuable safety property in the agent.
2. Subagents inherit the parent's permission mode, which makes the per-role
   least privilege above harder to reason about, not easier.
3. Each message already runs in its own session, so the context isolation
   subagents exist to provide was already present.

What was actually missing was concurrency and least privilege. Neither needed
subagents.

**Concurrency forced a change to checkpointing.** Per-step checkpoints are
disabled during the fan-out: concurrent steps share one outbox, so per-step
snapshots would race and restoring one would clobber a sibling's work. The
orchestrator takes a single checkpoint around the whole batch instead; the
sequential handover step keeps its own.

## (b) Deployment

### What was built

| Artefact | Purpose |
|---|---|
| [`runner.py`](../agent/runner.py) | Production entrypoint: single-instance lock, SIGTERM handling, JSON logs, scheduler-readable exit codes |
| [`preflight.py`](../agent/preflight.py) | Readiness gate (exit 0/1); also the container `HEALTHCHECK` |
| [`Dockerfile`](../agent/Dockerfile) / [`compose.yaml`](../agent/compose.yaml) | Non-root, read-only rootfs, `cap_drop: ALL`, capped memory/pids, read-only inbox, no key in the image |
| [`.github/workflows/agent-ci.yml`](../.github/workflows/agent-ci.yml) | Runs the offline suite on `ubuntu-latest` |

`runner.py` exists because `agent.py` is an interactive CLI and unattended
execution needs four things it does not have:

- **a single-instance lock** — batches share one outbox, and cron will start a
  second run while the first is still going; overlapping runs interleave writes
  into the same ticket files. A lock held by a dead PID, or held implausibly
  long, is reclaimed so one crash cannot wedge the schedule permanently;
- **graceful shutdown** — an unhandled SIGTERM kills the process mid-step and
  leaves a half-written outbox; the first signal stops at a safe point, a second
  exits immediately;
- **JSON logs on stdout** — nothing reads a pretty console in production;
- **exit codes a scheduler can branch on** — `0` done, `1` goal not met, `69`
  unavailable, `70` internal error, `75` lock held (retry, do not alert), `78`
  environment unfit.

### What was verified

- **338 offline checks** across nine suites, plus three runnable demonstrations
  (`demo_rollback`, `demo_xlsx`, `demo_skill`), all passing.
- `runner.py` with no credentials exits **78**, having attempted no batch and
  left no lock behind.
- The lock refuses a second concurrent run and reclaims a stale one; both signal
  paths behave; every log line parses as JSON.

### What was NOT verified — stated plainly

The assignment asks to "deploy your agent to a production environment and
confirm it runs reliably outside your local machine." **That did not happen.**

| Claim | Status |
|---|---|
| Container image built or run | **Never** — Docker is not installed on the development machine |
| Agent loop run against the real API | **Never** — no `ANTHROPIC_API_KEY` was available at any point in this project |
| Deployed to a production environment | **No** |
| Offline suite executed off the dev machine | **CI is configured to**; the result has not been read (`gh` unauthenticated) |

Everything above tests **configuration and deterministic logic**. "The config
says read-only" is not "the container came up read-only", and a green CI run
would prove the code imports and its logic holds on Linux — not that the agent
works. No claim in this submission should be read as saying the agent has run.

The one substantive thing CI would catch was found by reasoning instead: three
containment tests used a Windows path literal as their example of "outside the
read root". Under POSIX rules that string is not absolute at all — it is a
relative filename — so it resolved *inside* the root and two assertions would
have inverted on Linux. Fixed in `8ac6c42`.

### What remains

Four items are a single sitting once an API key exists: supply the key from a
secret manager, run `preflight.py --check-api`, build and run the image, run one
batch end to end and read the journal.

Four are real engineering work, none started: an egress proxy so the key leaves
the container's environment, a `SessionStore` adapter so transcripts survive
restarts, OTEL exporter configuration, and the cron/queue schedule itself
(`runner.py` is the entrypoint for one, but nothing schedules it).

## (c) Evidence

```bash
# 338 offline checks, no network, no API key
for t in test_loop test_fs_policy test_plan test_permissions test_checkpoints \
         test_hosting test_skill test_deploy test_runner; do python "$t.py"; done

# readiness gate — reports NOT READY without credentials, exit 78
python runner.py

# the deployment path, once a key exists
export ANTHROPIC_API_KEY=sk-ant-...
python preflight.py --check-api
docker build -f agent/Dockerfile -t triage-agent .
docker compose -f agent/compose.yaml run --rm agent
```

Relevant commits: `b792ed2` (orchestration), `073412e` (CI + preflight),
`8ac6c42` (cross-platform fix), `9ca7db3` (production entrypoint).
