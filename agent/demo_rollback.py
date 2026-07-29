"""Demonstration: recovering from a deliberately bad change.

Runs offline — no API key, no network. The "agent" here is simulated so the
damage is deterministic and the recovery is provable, but the CheckpointStore
being exercised is the same one loop.run_step uses in a live run.

The scenario is the one the SDK's own checkpointing cannot cover: a *later*
step corrupts what *earlier* steps produced. Because this agent runs one
session per step, an SDK checkpoint from the triage steps is not rewindable
from the digest step's session.

    .venv\\Scripts\\python.exe demo_rollback.py
"""

import sys

from checkpoints import CheckpointStore
from fs_policy import OUTBOX, ensure_workspace

GOOD_TICKET = """\
# {tid}

- urgency: {urgency}
- topic: {topic}
- team: {team}
- confidence: {confidence}
- needs_human_review: false

## Summary
{summary}
"""

TICKETS = [
    dict(
        tid="TICK-5001",
        urgency="urgent",
        topic="technical",
        team="engineering",
        confidence="0.96",
        summary="Platform-wide API 500s since 09:00 blocking the whole team.",
    ),
    dict(
        tid="TICK-5002",
        urgency="high",
        topic="billing",
        team="billing",
        confidence="0.91",
        summary="Charged twice for this month's subscription; second payment failed.",
    ),
]


def rule(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def show_outbox(note: str) -> None:
    print(f"\n  outbox {note}:")
    files = sorted(p for p in OUTBOX.rglob("*") if p.is_file() and p.name != ".gitkeep")
    if not files:
        print("    (empty)")
    for path in files:
        first = path.read_text(encoding="utf-8").splitlines()[:1]
        preview = first[0] if first else "(empty file)"
        print(f"    {path.name:<20} {len(path.read_bytes()):>5} bytes  {preview}")


def main() -> int:
    ensure_workspace()
    store = CheckpointStore()
    store.clear()

    # Start from a clean outbox so the demo is reproducible.
    for path in list(OUTBOX.rglob("*")):
        if path.is_file() and path.name != ".gitkeep":
            path.unlink()

    rule("1. EARLIER STEPS PRODUCE GOOD WORK")
    for spec in TICKETS:
        (OUTBOX / f"{spec['tid']}.md").write_text(
            GOOD_TICKET.format(**spec), encoding="utf-8"
        )
    print("  two triage steps filed their tickets")
    show_outbox("after triage")

    rule("2. CHECKPOINT TAKEN BEFORE THE NEXT STEP")
    checkpoint = store.create("before digest step")
    print(f"  {checkpoint.summary()}")
    for rel, digest in checkpoint.files.items():
        print(f"    {rel:<20} sha256:{digest[:16]}…")

    rule("3. THE DIGEST STEP GOES WRONG")
    # Three distinct kinds of damage, to prove the restore is not just a copy.
    (OUTBOX / "TICK-5001.md").write_text(
        "CORRUPTED - the step overwrote this ticket with garbage\n", encoding="utf-8"
    )
    print("  overwrote TICK-5001.md with garbage")

    (OUTBOX / "TICK-5002.md").unlink()
    print("  deleted TICK-5002.md entirely")

    (OUTBOX / "digest.md").write_text(
        "# Handover\n\nTICK-5001: ???\n(this digest is wrong and incomplete)\n",
        encoding="utf-8",
    )
    print("  wrote a bad digest.md")
    show_outbox("after the bad step")

    drift = store.drift(checkpoint)
    print("\n  drift detected against the checkpoint:")
    for kind in ("modified", "deleted", "added"):
        if drift[kind]:
            print(f"    {kind:<9} {', '.join(drift[kind])}")

    rule("4. ROLL BACK")
    report = store.restore(checkpoint.id)
    print(f"  restored to {checkpoint.id}: {report.describe()}")
    for rel in report.restored:
        print(f"    rewritten  {rel}")
    for rel in report.recreated:
        print(f"    recreated  {rel}")
    for rel in report.removed:
        print(f"    removed    {rel}")
    show_outbox("after rollback")

    rule("5. VERIFY")
    checks = []

    restored_1 = (OUTBOX / "TICK-5001.md").read_text(encoding="utf-8")
    checks.append(("TICK-5001 content is back", "CORRUPTED" not in restored_1))
    checks.append(("TICK-5001 says urgent again", "urgency: urgent" in restored_1))

    checks.append(("TICK-5002 exists again", (OUTBOX / "TICK-5002.md").is_file()))
    if (OUTBOX / "TICK-5002.md").is_file():
        checks.append(
            (
                "TICK-5002 content is intact",
                "Charged twice" in (OUTBOX / "TICK-5002.md").read_text(encoding="utf-8"),
            )
        )

    checks.append(("the bad digest.md is gone", not (OUTBOX / "digest.md").exists()))

    after = store.drift(checkpoint)
    checks.append(
        ("no drift remains against the checkpoint", not any(after.values()), )
    )

    print()
    ok = True
    for name, condition in checks:
        print(f"  {'PASS' if condition else 'FAIL'}  {name}")
        ok = ok and condition

    print("\n" + "=" * 68)
    print("RECOVERED" if ok else "RECOVERY FAILED")
    print("=" * 68)

    store.clear()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
