# Triage agent container.
#
# Only the Python runtime is needed: the claude-agent-sdk wheel bundles a native
# Claude Code binary for the platform, so there is no separate CLI or Node.js to
# install. That binary is pinned to the SDK version, so bumping the pin in
# requirements.txt is how the CLI gets updated.
#
# Build from the REPOSITORY ROOT, not from agent/, because the image needs the
# triage spec from lib/:
#
#   docker build -f agent/Dockerfile -t triage-agent .
#
# No secret is baked in. ANTHROPIC_API_KEY is supplied at run time.

FROM python:3.12-slim

# Dependencies first, so a code change does not invalidate the wheel layer.
WORKDIR /app
COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && find /usr/local -name '__pycache__' -type d -prune -exec rm -rf {} +

# Unprivileged, no login shell. The agent writes only to its workspace, so
# every other path can stay read-only at run time.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin agent

COPY --chown=agent:agent agent/*.py /app/

# Copy only the spec the agent reads, not the repository. The agent has no
# reason to see application source, and a container that cannot see it cannot
# leak it — which also keeps the read root small and meaningful.
COPY --chown=agent:agent lib/triage/triage_spec.md /app/lib/triage/triage_spec.md

RUN mkdir -p /app/workspace/inbox /app/workspace/outbox /app/workspace/.claude-config \
    && chown -R agent:agent /app/workspace

USER agent

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Without this the read root would derive to "/" inside the container,
    # silently widening containment to the whole filesystem.
    AGENT_REPO_ROOT=/app \
    # Belt and braces: the code sets these too, but the env vars hold even if
    # someone edits the options.
    CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 \
    CLAUDE_CONFIG_DIR=/app/workspace/.claude-config

# Unattended by default: refuse anything needing a human rather than silently
# paging on-call. Override at run time for an interactive operator.
ENTRYPOINT ["python", "agent.py"]
CMD ["--approve=deny"]
