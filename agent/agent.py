"""Entry point for the capstone triage agent.

Step 2 stood the Agent SDK up, step 3 grew the autonomous loop, step 4 gave it
an inbox to read and an outbox to write. This file handles argv, the API key
check, and exit codes.

Run:
    $env:ANTHROPIC_API_KEY = "sk-ant-..."   # PowerShell

    .venv\\Scripts\\python.exe agent.py                       # first inbox file
    .venv\\Scripts\\python.exe agent.py 002-double-charge.txt  # a named one
    .venv\\Scripts\\python.exe agent.py "I was charged twice"  # literal text
"""

import asyncio
import os
import sys

from fs_policy import INBOX, REPO_ROOT, ensure_workspace
from loop import report, run_triage


def resolve_input(argv: list[str]) -> tuple[str, str] | None:
    """Return (message, source label), or None if there is nothing to triage."""
    ensure_workspace()

    if argv:
        # Resolve before testing: a traversal argument like ../../secrets.txt
        # would otherwise be read straight off disk and fed to the model.
        candidate = (INBOX / argv[0]).resolve()
        inside_inbox = candidate == INBOX or candidate.is_relative_to(INBOX.resolve())
        if inside_inbox and candidate.is_file():
            return candidate.read_text(encoding="utf-8"), candidate.name
        return " ".join(argv), "(command line)"

    inbox_files = sorted(p for p in INBOX.glob("*.txt") if p.is_file())
    if not inbox_files:
        return None
    first = inbox_files[0]
    return first.read_text(encoding="utf-8"), first.name


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set. The SDK reads it from the process "
            "environment and does not load .env files automatically.\n"
            '  PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."',
            file=sys.stderr,
        )
        return 2

    resolved = resolve_input(sys.argv[1:])
    if resolved is None:
        print(
            f"Nothing to triage: {INBOX.relative_to(REPO_ROOT).as_posix()}/ is "
            "empty. Add a .txt file, or pass a message as an argument.",
            file=sys.stderr,
        )
        return 2

    message, source = resolved
    print("=" * 68)
    print(f"INCOMING SUPPORT MESSAGE  ({source})")
    print("=" * 68)
    print(message.strip())

    outcome = asyncio.run(run_triage(message))
    return report(outcome)


if __name__ == "__main__":
    raise SystemExit(main())
