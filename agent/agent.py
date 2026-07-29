"""Minimal Claude Agent SDK skeleton for the support triager.

Step 2 of the capstone: confirm the Agent SDK runs end to end. This is
deliberately small — one `query()` call, read-only tools, streamed output —
and is the skeleton the production agent grows from in later steps.

Run:
    $env:ANTHROPIC_API_KEY = "sk-ant-..."   # PowerShell
    .venv\\Scripts\\python.exe agent.py
    .venv\\Scripts\\python.exe agent.py "my own support message"
"""

import asyncio
import os
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLINotFoundError,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

# The triage spec lives in the Dart package one level up; the agent reads it
# from disk rather than having the rules baked into the prompt.
REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = "lib/triage/triage_spec.md"

MODEL = "claude-opus-5"

SYSTEM_PROMPT = (
    "You are a customer-support triage agent. Read the triage specification "
    f"at {SPEC_PATH} before classifying anything, and apply its precedence "
    "rules exactly. Answer with the urgency, topic, owning team, a one-line "
    "summary, and the rule number you applied. Be concise."
)

SAMPLE_MESSAGE = (
    "URGENT: our whole team is locked out - the API has returned 500 on every "
    "request since 9am, and we are now getting billing-failed emails too. "
    "This blocks all work."
)


async def triage(message: str) -> int:
    """Run one triage pass. Returns a process exit code."""
    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        # Read-only: this skeleton inspects the repo, it never writes to it.
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="dontAsk",
        cwd=str(REPO_ROOT),
        max_turns=10,
    )

    prompt = f"Triage this support message:\n\n{message}"
    result: ResultMessage | None = None

    # ResultMessage is terminal: stop reading there. Continuing to iterate past
    # it makes the SDK raise ("Claude Code returned an error result"), and
    # returning from inside the loop closes its generator mid-run.
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(block.text)
                    elif isinstance(block, ToolUseBlock):
                        print(f"  [tool] {block.name}")
            elif isinstance(msg, ResultMessage):
                result = msg
                break
    except CLINotFoundError:
        print(
            "Could not start the bundled Claude Code binary. Reinstall with "
            "`pip install --force-reinstall claude-agent-sdk`.",
            file=sys.stderr,
        )
        return 1
    except ClaudeSDKError as exc:
        print(f"Agent SDK error: {exc}", file=sys.stderr)
        return 1

    if result is None:
        print("\nAgent stream ended without a result message.", file=sys.stderr)
        return 1

    # `subtype` reports "success" even for a run that failed on auth — is_error
    # is the signal that actually reflects the outcome.
    if result.is_error:
        print(f"\n--- failed ({result.subtype}) ---", file=sys.stderr)
        if result.api_error_status:
            print(f"    HTTP {result.api_error_status}", file=sys.stderr)
        for err in result.errors or []:
            print(f"    {err}", file=sys.stderr)
        return 1

    cost = f" · ${result.total_cost_usd:.4f}" if result.total_cost_usd else ""
    print(f"\n--- done in {result.num_turns} turns{cost} ---")
    return 0


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set. The SDK reads it from the process "
            "environment and does not load .env files automatically.\n"
            '  PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."',
            file=sys.stderr,
        )
        return 2

    message = " ".join(sys.argv[1:]) or SAMPLE_MESSAGE
    return asyncio.run(triage(message))


if __name__ == "__main__":
    raise SystemExit(main())
