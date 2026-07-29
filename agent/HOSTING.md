# Hosting and sandboxing the triage agent

How the agent runs in production, and what each layer actually prevents.

> **Verification status — read this first.**
>
> | Layer | Status |
> |---|---|
> | Offline suite on Linux, off the dev machine | **CI runs it** (`.github/workflows/agent-ci.yml`) |
> | Container image built or run | **Never.** Docker is not installed here |
> | Agent loop against the real API | **Never.** No API key has been available |
> | Deployed to a production environment | **No.** See "What deployment still needs" |
>
> `test_hosting.py` and `test_deploy.py` check *configuration*. "The config
> says read-only" is not "the container came up read-only", and a green CI run
> proves the code imports and its logic holds on Linux — not that the agent
> works. Nothing below should be read as a claim that this has run in
> production.

## Run it

```bash
# From the repository root — the image needs lib/triage/triage_spec.md
docker build -f agent/Dockerfile -t triage-agent .

export ANTHROPIC_API_KEY=sk-ant-...
docker compose -f agent/compose.yaml run --rm agent
```

The image needs only Python 3.12: the `claude-agent-sdk` wheel bundles a native
Claude Code binary, so there is no separate CLI or Node.js to install. That
binary is pinned to the SDK version, so bumping `requirements.txt` is how the
CLI gets updated.

## Why an agent needs isolating at all

The agent decides its own actions. Every control in the layers below assumes
the layer above it can fail: the prompt can be talked out of its instructions
by a support message crafted to do exactly that, and the permission policy is
code that could have a bug. Isolation is what remains true when the agent
behaves in a way nobody designed.

## The layers

| Layer | Prevents | Where |
|---|---|---|
| 1. Tool surface | The agent never has `Bash`, `WebFetch`, `WebSearch`, or subagents to begin with | `permissions.py` → `DENIED_TOOLS` |
| 2. Permission policy | Writes outside the outbox, reads outside the root, consequential tickets filed unattended | `permissions.py`, enforced by a `PreToolUse` hook and `can_use_tool` |
| 3. Settings isolation | Host `CLAUDE.md`, `.claude/`, and auto-memory loading into the agent's context | `setting_sources=[]`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, per-run `CLAUDE_CONFIG_DIR` |
| 4. Container filesystem | Any write outside the workspace volume, and reading application source at all | `read_only: true`, a read-only inbox mount, `.dockerignore` |
| 5. Container privileges | Escalation, capability abuse, fork bombs, runaway memory | non-root uid 10001, `cap_drop: ALL`, `no-new-privileges`, `mem_limit`, `pids_limit` |
| 6. Network | Reaching anything but the Anthropic API | compose network + egress proxy (below) |

Layers 1–3 are the agent's own code and are exercised by the offline suites.
Layers 4–6 are the container and are what this document adds.

### Layer 3 is not cosmetic

The agent's `cwd` is the repository root, which carries a `CLAUDE.md` written
for *this repo's* human contributors and a `.claude/` directory with skills. By
default `query()` loads project settings, so all of that was being pulled into
the triage agent's system prompt: irrelevant instructions, wasted context, and
on a shared host a path for one tenant's files to shape another tenant's run.
`setting_sources=[]` stops filesystem settings loading; auto-memory has to be
disabled separately because it loads regardless.

### The read-root trap

`fs_policy` derives its read root from the file layout — the agent directory's
parent. With the code at `/app` inside the container, that parent is **`/`**,
which would have silently widened the read root from "the repo" to "the entire
filesystem" the moment it was containerised. The image sets
`AGENT_REPO_ROOT=/app` and the derivation is overridable for exactly this
reason. `test_hosting.py` asserts that with the override in place `/etc/passwd`
is not readable.

## What the container does and does not give you

**Does:**

- A crash or a policy bug cannot touch the host filesystem — only the workspace
  volume is writable, and the root filesystem is read-only.
- The image contains the triage spec and the agent's own code. It does **not**
  contain the Flutter application source, so the agent cannot read or leak it.
- Resource caps bound the blast radius of a runaway loop. `max_turns` bounds
  the agent's own iteration; the SDK has no wall-clock session timeout.
- The inbox is mounted read-only, so the agent cannot rewrite its own inputs —
  which also means it cannot hide what it was asked to do.

**Does not:**

- **Restrict egress by domain.** A bridge network permits arbitrary outbound
  traffic. The agent has no network tools, so this is defence in depth rather
  than the primary control, but a compromised dependency would not be stopped
  by it. For that, route egress through a proxy (below).
- **Protect the API key from the agent's own process.** The key is in the
  container's environment because the SDK reads it there. A tool that could
  read environment variables would see it — which is part of why `Bash` is
  denied outright rather than merely sandboxed.
- **Survive a container escape.** Docker is a namespace boundary, not a
  hypervisor. For untrusted input at scale, use gVisor, Firecracker, or a
  sandbox provider.

## Egress control

The SDK needs outbound HTTPS to `api.anthropic.com` and nothing else. To
enforce that, put a proxy in front of the container and let it hold the
credential:

```yaml
environment:
  ANTHROPIC_BASE_URL: http://egress-proxy:8080
  # ANTHROPIC_API_KEY is then not needed in the container at all —
  # the proxy injects it after the request leaves.
```

This is strictly better than the compose file's default: the key stops being
present in the agent's environment, so reading the environment stops being
useful, and the allowlist is enforced somewhere the agent cannot edit.

## Multi-tenant notes

The current design runs one batch per container, which is the simplest safe
shape. To share a container across tenants, the SDK-level requirements are
already met (`setting_sources=[]`, auto-memory off, per-run `CLAUDE_CONFIG_DIR`)
but two more are needed:

- a per-tenant `cwd`, passed on every `query()` — currently every step uses the
  same root;
- per-tenant egress rules at the proxy, so one tenant's credentials cannot be
  used to exfiltrate another's data.

Until both exist, run one tenant per container.

## Orchestration

Two agent roles, with deliberately different tool surfaces:

| Role | Has | Denied | Why |
|---|---|---|---|
| **triage** (×N, concurrent) | Read/Write/Glob/Grep, CRM lookups, `create_ticket` | `Bash`, `Skill` | it classifies; it has no reason to run a shell or build a document |
| **handover** (×1, after) | Read/Write/Glob/Grep, `Bash`, `Skill` | the CRM lookups | it aggregates what is already on disk; re-querying the CRM would let it contradict a filed ticket |

Denials are **bare names in `disallowed_tools`**, which removes the tool
definition from the model's context entirely rather than merely leaving it
unapproved.

Triage messages are independent, so they fan out concurrently, bounded by a
semaphore (`MAX_CONCURRENCY = 4`) — the SDK spawns one CLI subprocess per
session, and a wide fanout is the documented way to hit rate limits. The
handover step runs after, sequentially, because it consumes their output.
`asyncio.gather(..., return_exceptions=True)` means one agent crashing does not
take the batch with it.

### Why not SDK subagents

The SDK can spawn subagents inside one session. Deliberately not used here:

- **Subagent file edits are not tracked by file checkpointing.** That would
  silently void the rollback guarantee from step 7 — the most valuable safety
  property in this agent.
- Subagents inherit the parent's permission mode, which makes per-role least
  privilege harder to reason about, not easier.
- Each message already runs in its own session, so the context isolation
  subagents exist to provide is already there.

What was actually missing was concurrency and least privilege, and neither
needed subagents.

### Concurrency changed checkpointing

Per-step checkpoints are **disabled during the fan-out**. Concurrent steps
share one outbox, so per-step snapshots would race and restoring one would
clobber a sibling's work. The orchestrator takes a single checkpoint around the
whole batch instead; the sequential handover step keeps its own.

## Deploying

### Continuous integration

`.github/workflows/agent-ci.yml` runs the eight offline suites and the three
demonstrations on `ubuntu-latest`. This is the first thing in the project that
executes anywhere other than a Windows laptop, and that is the point: several
code paths are platform-sensitive — shell splitting (`posix=(os.name != "nt")`),
path containment, and the read-root derivation that resolves differently under
`/app`. It uploads the generated workbook as an artifact so the document output
can be downloaded and opened from a machine that never ran the agent.

CI deliberately needs **no API key**, so it verifies the deterministic half and
nothing more.

### Preflight

```bash
python preflight.py              # everything except the model call
python preflight.py --check-api  # one cheap call proving auth end to end
```

Run it on the host before running the agent. It checks the runtime, that a
credential path exists, that the workspace is writable and the read root is not
`/`, that the policy still denies what it should, and that a skill is present.
Exit 0/1, so it works as a deploy gate or container healthcheck. It reports
NOT READY rather than passing when credentials are absent — a preflight that
always passes is worse than none.

### What deployment still needs

Honest list of what stands between this and production, none of which is done:

| Step | Why it is not done here |
|---|---|
| Build and run the image | Docker is not installed on the dev machine |
| Supply `ANTHROPIC_API_KEY` from a secret manager | No key has been available |
| Run `preflight.py --check-api` on the host | Same |
| Run the agent once end to end and read the journal | Same |
| Put an egress proxy in front of it | Documented above, not implemented |
| Add a `SessionStore` so transcripts survive restarts | Not implemented |
| Point OTEL env vars at a collector | Configuration, not code — see below |
| Schedule it (cron / a queue consumer) | `runner.py` is the entrypoint for one; the schedule itself is not configured |

The first four are a single sitting once a key exists. The rest are real work.

## Operational gaps

Honest list of what is designed but not built:

| Gap | Consequence |
|---|---|
| No `SessionStore` adapter | Transcripts live on container disk and are lost on restart. Fine for a one-shot batch, not for resumable sessions. |
| No OTEL export configured | The journal in `hooks.py` is the only observability. The SDK reads OTEL env vars, so this is configuration rather than code. |
| Image never built | Everything above is config-checked, not runtime-verified. |
| Bridge network, no egress proxy | Documented above, not implemented. |
