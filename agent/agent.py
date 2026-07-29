"""Entry point for the capstone triage agent.

Step 2 stood the Agent SDK up, step 3 grew the autonomous loop, step 4 gave it
an inbox and an outbox, step 5 made it plan a multi-step batch, step 6 put a
permission strategy around it (see permissions.py).

Run:
    $env:ANTHROPIC_API_KEY = "sk-ant-..."   # PowerShell

    .venv\\Scripts\\python.exe agent.py                       # the whole inbox
    .venv\\Scripts\\python.exe agent.py 002-double-charge.txt  # one message
    .venv\\Scripts\\python.exe agent.py "I was charged twice"  # literal text

    --approve=deny    refuse anything needing approval (default, unattended)
    --approve=prompt  ask on the terminal
    --approve=auto    approve everything that reaches ASK (tests only)
"""

import asyncio
import os
import sys

from fs_policy import INBOX, ensure_workspace
from loop import (
    TRIAGE_SYSTEM_PROMPT,
    LoopOutcome,
    outbox_files,
    report,
    run_step,
    ticket_goal,
)
from permissions import APPROVERS, DenyApprover
from plan import run_plan
from triage_tools import TriageSession

APPROVE_FLAG = "--approve="


def parse_approver(argv: list[str]) -> tuple[object, list[str]] | None:
    """Pull --approve=MODE out of argv. Returns (approver, rest) or None."""
    mode, rest = "deny", []
    for arg in argv:
        if arg.startswith(APPROVE_FLAG):
            mode = arg[len(APPROVE_FLAG) :].strip().lower()
        else:
            rest.append(arg)

    factory = APPROVERS.get(mode)
    if factory is None:
        print(
            f"Unknown approval mode {mode!r}. Choose one of: "
            f"{', '.join(sorted(APPROVERS))}.",
            file=sys.stderr,
        )
        return None
    return factory(), rest


def resolve_single(argv: list[str]) -> tuple[str, str] | None:
    """Resolve a single-message request, or None to run the whole inbox."""
    if not argv:
        return None

    # Resolve before testing: a traversal argument like ../../secrets.txt
    # would otherwise be read straight off disk and fed to the model.
    candidate = (INBOX / argv[0]).resolve()
    inside_inbox = candidate == INBOX or candidate.is_relative_to(INBOX.resolve())
    if inside_inbox and candidate.is_file():
        return candidate.read_text(encoding="utf-8"), candidate.name
    return " ".join(argv), "(command line)"


async def run_single(message: str, source: str, approver) -> LoopOutcome:
    """Triage one message — the single-step path, no digest."""
    outcome = LoopOutcome(approver_name=approver.name)
    print("=" * 70)
    print(f"INCOMING SUPPORT MESSAGE  ({source})")
    print("=" * 70)
    print(message.strip())
    print()

    result = await run_step(
        name=f"triage {source}",
        system_prompt=TRIAGE_SYSTEM_PROMPT,
        prompt=f"Triage this support message:\n\n{message}",
        goal=ticket_goal,
        audit=outcome.audit,
        session=TriageSession(),
        approver=approver,
    )
    outcome.steps.append(result)
    outcome.written_files = outbox_files("*.md")
    return outcome


def main() -> int:
    parsed = parse_approver(sys.argv[1:])
    if parsed is None:
        return 2
    approver, argv = parsed

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set. The SDK reads it from the process "
            "environment and does not load .env files automatically.\n"
            '  PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."',
            file=sys.stderr,
        )
        return 2

    if isinstance(approver, DenyApprover) and sys.stdin.isatty():
        print(
            "note: running with --approve=deny; consequential tickets will be "
            "refused. Use --approve=prompt to decide interactively.\n"
        )

    ensure_workspace()
    single = resolve_single(argv)

    if single is None:
        outcome = LoopOutcome(approver_name=approver.name)
        outcome = asyncio.run(run_plan(outcome, approver))
    else:
        message, source = single
        outcome = asyncio.run(run_single(message, source, approver))

    if not outcome.steps:
        return 2
    return report(outcome)


if __name__ == "__main__":
    raise SystemExit(main())
