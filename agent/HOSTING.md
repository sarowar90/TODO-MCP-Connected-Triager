# Hosting and sandboxing the triage agent

How the agent runs in production, and what each layer actually prevents.

> **Verification status.** Docker is not installed on the machine this was
> written on, so **the image has never been built or run.** The configuration
> is checked offline by [`test_hosting.py`](test_hosting.py) (36 checks: the
> read-root override, COPY sources, hardening directives, context hygiene),
> but "the config says read-only" is not the same as "the container came up
> read-only". Treat the build as unverified until you run the commands below.

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

## Operational gaps

Honest list of what is designed but not built:

| Gap | Consequence |
|---|---|
| No `SessionStore` adapter | Transcripts live on container disk and are lost on restart. Fine for a one-shot batch, not for resumable sessions. |
| No OTEL export configured | The journal in `hooks.py` is the only observability. The SDK reads OTEL env vars, so this is configuration rather than code. |
| Image never built | Everything above is config-checked, not runtime-verified. |
| Bridge network, no egress proxy | Documented above, not implemented. |
