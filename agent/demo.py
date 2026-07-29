"""Demo Day driver.

Runs the agent's demonstrations in one pass and writes a timestamped transcript
to docs/demo-run.txt, which is the artefact submitted for Step 12 (h).

It adapts to what the environment can actually do:

  * **With ANTHROPIC_API_KEY set** it runs the real thing — preflight against
    the live API, then a full batch through runner.py: read the inbox, triage
    each message concurrently, file tickets, build the handover workbook.
  * **Without a key** it runs the deterministic demonstrations that need no
    model — checkpoint rollback, the workbook format, and the custom skill's
    workflow — and says clearly, in the transcript itself, that the agent loop
    was not exercised.

The second mode is not a substitute for the first. It is a demonstration of the
parts that can be demonstrated, labelled as such.

    python demo.py
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
TRANSCRIPT = AGENT_DIR.parent / "docs" / "demo-run.txt"

OFFLINE_SEGMENTS = [
    (
        "Checkpoint rollback",
        "demo_rollback.py",
        "A later step corrupts one ticket, deletes another and writes a bad "
        "digest; the run rolls back to the pre-step checkpoint and every "
        "change is undone.",
    ),
    (
        "Document output",
        "demo_xlsx.py",
        "The handover workbook is built, validated with the agent's own goal "
        "check, and re-opened to confirm it is a real .xlsx.",
    ),
    (
        "Custom skill workflow",
        "demo_skill.py",
        "The shift-handover skill end to end: discovery, its documented "
        "command passing the permission policy while abuse variants are "
        "refused, the bundled script running, and the goal check accepting "
        "the result.",
    ),
]


class Tee:
    """Write to the console and the transcript at once."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("w", encoding="utf-8")

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
        self.handle.write(text)
        self.handle.flush()

    def close(self) -> None:
        self.handle.close()


def run(out: Tee, script: str, args: list[str] | None = None) -> int:
    """Run a script, streaming its output into the transcript."""
    process = subprocess.Popen(
        [sys.executable, script, *(args or [])],
        cwd=AGENT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        out.write(line)
    return process.wait()


def banner(out: Tee, title: str, note: str = "") -> None:
    out.write("\n" + "=" * 74 + "\n")
    out.write(title + "\n")
    if note:
        out.write("-" * 74 + "\n" + note + "\n")
    out.write("=" * 74 + "\n\n")


def main() -> int:
    live = bool(os.environ.get("ANTHROPIC_API_KEY"))
    started = time.time()
    out = Tee(TRANSCRIPT)

    try:
        out.write("SUPPORT TRIAGER — DEMO DAY RUN\n")
        out.write(f"recorded  {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
        out.write(f"python    {sys.version.split()[0]}\n")
        out.write(f"mode      {'LIVE — real API calls' if live else 'OFFLINE — no API key present'}\n")

        if not live:
            out.write(
                "\nNOTE: ANTHROPIC_API_KEY is not set, so the agent loop is NOT\n"
                "exercised in this transcript. What follows demonstrates the\n"
                "deterministic parts: checkpoint rollback, the document output,\n"
                "and the custom skill's workflow. Set the key and re-run to\n"
                "produce a live end-to-end transcript.\n"
            )

        failures: list[str] = []

        if live:
            banner(out, "PREFLIGHT (live)", "Confirms the environment and that the API answers.")
            if run(out, "preflight.py", ["--check-api"]) != 0:
                out.write("\npreflight failed — stopping.\n")
                failures.append("preflight")
            else:
                banner(
                    out,
                    "END-TO-END BATCH",
                    "runner.py: read the inbox, triage each message concurrently,\n"
                    "file tickets, then build the handover digest and workbook.",
                )
                code = run(out, "runner.py", ["--skip-preflight"])
                out.write(f"\nrunner.py exit code: {code}\n")
                if code != 0:
                    failures.append(f"runner (exit {code})")
        else:
            banner(out, "PREFLIGHT", "Readiness gate. Expected to report NOT READY without a key.")
            code = run(out, "preflight.py")
            out.write(f"\npreflight exit code: {code}  (78/1 expected without credentials)\n")

            for title, script, note in OFFLINE_SEGMENTS:
                banner(out, title.upper(), note)
                if run(out, script) != 0:
                    failures.append(title)

        banner(out, "RESULT")
        elapsed = time.time() - started
        if failures:
            out.write(f"FAILED: {', '.join(failures)}\n")
        else:
            out.write("All demonstrated segments passed.\n")
        out.write(f"elapsed {elapsed:.1f}s\n")

        if not live:
            out.write(
                "\nScope of this transcript: the agent loop was not run. The\n"
                "segments above exercise checkpointing, the document pipeline\n"
                "and the skill workflow without the model.\n"
            )

        out.write(f"\ntranscript: {TRANSCRIPT}\n")
        return 1 if failures else 0
    finally:
        out.close()


if __name__ == "__main__":
    raise SystemExit(main())
