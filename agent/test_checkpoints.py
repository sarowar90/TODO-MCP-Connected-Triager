"""Offline checks for checkpointing and lifecycle hooks.

Uses a scratch directory rather than the real outbox, so running the tests
cannot destroy work an agent run produced.

Run:
    .venv\\Scripts\\python.exe test_checkpoints.py
"""

import asyncio
import shutil
from pathlib import Path

from checkpoints import CheckpointStore
from fs_policy import WORKSPACE
from hooks import Journal, build_hooks
from permissions import CREATE_TICKET, PermissionAudit

SCRATCH = WORKSPACE / ".test-scratch"
SCRATCH_STORE = WORKSPACE / ".test-checkpoints"

passed = failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}" + (f" - {detail}" if detail else ""))


def fresh_store() -> CheckpointStore:
    for path in (SCRATCH, SCRATCH_STORE):
        if path.exists():
            shutil.rmtree(path)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    return CheckpointStore(root=SCRATCH, store=SCRATCH_STORE)


def write(name: str, text: str) -> Path:
    path = SCRATCH / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def main() -> int:
    print("snapshot and restore")
    store = fresh_store()
    write("a.md", "alpha")
    write("b.md", "beta")
    cp = store.create("two files")

    check("a checkpoint records every file", len(cp.files) == 2)
    check("the checkpoint is listed", [c.id for c in store.list()] == [cp.id])
    check("latest() returns it", (store.latest() or cp).id == cp.id)

    write("a.md", "CORRUPTED")
    report = store.restore(cp.id)
    check("a modified file is rewritten", (SCRATCH / "a.md").read_text() == "alpha")
    check("the restore reports it", report.restored == ["a.md"])
    check("an untouched file is left alone", report.unchanged == ["b.md"])

    print("\ndeletions and additions")
    store = fresh_store()
    write("keep.md", "keep")
    write("gone.md", "gone")
    cp = store.create("before damage")

    (SCRATCH / "gone.md").unlink()
    write("junk.md", "junk")
    report = store.restore(cp.id)

    check("a deleted file is recreated", (SCRATCH / "gone.md").read_text() == "gone")
    check("recreation is reported", report.recreated == ["gone.md"])
    check("a file created after the snapshot is removed", not (SCRATCH / "junk.md").exists())
    check("removal is reported", report.removed == ["junk.md"])

    print("\nnested paths")
    store = fresh_store()
    write("2026/q3/deep.md", "nested")
    cp = store.create("nested")
    (SCRATCH / "2026" / "q3" / "deep.md").unlink()
    store.restore(cp.id)
    check(
        "a nested file is restored with its directories",
        (SCRATCH / "2026" / "q3" / "deep.md").read_text() == "nested",
    )

    print("\ndrift detection")
    store = fresh_store()
    write("x.md", "one")
    write("y.md", "two")
    cp = store.create("baseline")
    check("a fresh snapshot shows no drift", not any(store.drift(cp).values()))

    write("x.md", "changed")
    (SCRATCH / "y.md").unlink()
    write("z.md", "new")
    drift = store.drift(cp)
    check("modification is detected", drift["modified"] == ["x.md"])
    check("deletion is detected", drift["deleted"] == ["y.md"])
    check("addition is detected", drift["added"] == ["z.md"])
    store.restore(cp.id)
    check("drift clears after a restore", not any(store.drift(cp).values()))

    print("\nmultiple restore points")
    store = fresh_store()
    write("doc.md", "v1")
    first = store.create("v1")
    write("doc.md", "v2")
    second = store.create("v2")
    write("doc.md", "v3")

    store.restore(second.id)
    check("can roll back to the most recent point", (SCRATCH / "doc.md").read_text() == "v2")
    store.restore(first.id)
    check("can roll back further to an earlier point", (SCRATCH / "doc.md").read_text() == "v1")
    check("both checkpoints remain listed", len(store.list()) == 2)

    print("\nerrors")
    try:
        store.restore("cp99-nope")
        check("restoring an unknown checkpoint raises", False)
    except KeyError:
        check("restoring an unknown checkpoint raises", True)
    check("get() returns None for an unknown id", store.get("cp99-nope") is None)

    store = fresh_store()
    empty = store.create("nothing yet")
    check("an empty snapshot is valid", empty.files == {})
    write("late.md", "late")
    report = store.restore(empty.id)
    check("restoring an empty snapshot clears the tree", not (SCRATCH / "late.md").exists())
    check("and reports the removal", report.removed == ["late.md"])

    print("\nlifecycle hooks")
    journal = Journal()
    audit = PermissionAudit()
    wired = build_hooks(journal, audit)

    for event in (
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PreCompact",
        "Stop",
    ):
        check(f"{event} is registered", event in wired and bool(wired[event]))

    journal.step = "triage demo.txt"
    await wired["UserPromptSubmit"][0].hooks[0]({}, None, None)
    check("the step start is journalled", journal.entries[-1].event == "step.begin")

    await wired["PreToolUse"][0].hooks[0](
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "lib/main.dart"},
        },
        None,
        None,
    )
    check("an allowed tool is journalled", journal.entries[-1].event == "tool.allow")

    denied = await wired["PreToolUse"][0].hooks[0](
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
        },
        None,
        None,
    )
    check(
        "the policy still denies through the lifecycle hook",
        denied.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
    )
    check("the denial is journalled", journal.entries[-1].event == "tool.deny")

    await wired["PreToolUse"][0].hooks[0](
        {
            "hook_event_name": "PreToolUse",
            "tool_name": CREATE_TICKET,
            "tool_input": {"urgency": "urgent", "team": "engineering"},
        },
        None,
        None,
    )
    check("an approval-needing call is journalled as ask", journal.entries[-1].event == "tool.ask")

    await wired["PostToolUse"][0].hooks[0](
        {"tool_name": "Write", "tool_input": {"file_path": "out/TICK-1.md"}}, None, None
    )
    check("a file write is recorded as a mutation", journal.mutations == ["out/TICK-1.md"])
    check("and journalled", journal.entries[-1].event == "file.write")

    await wired["PostToolUse"][0].hooks[0](
        {"tool_name": "Read", "tool_input": {"file_path": "x.md"}}, None, None
    )
    check("a read is not counted as a mutation", journal.mutations == ["out/TICK-1.md"])

    await wired["PostToolUseFailure"][0].hooks[0]({"tool_name": "Write"}, None, None)
    check("a tool failure is journalled", journal.entries[-1].event == "tool.failed")

    await wired["PreCompact"][0].hooks[0]({"trigger": "auto"}, None, None)
    check("compaction is counted", journal.compactions == 1)

    await wired["Stop"][0].hooks[0]({}, None, None)
    check("the step end is journalled", journal.entries[-1].event == "step.end")

    for path in (SCRATCH, SCRATCH_STORE):
        if path.exists():
            shutil.rmtree(path)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
