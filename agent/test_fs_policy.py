"""Offline checks for filesystem containment.

These are the tests that matter most in step 4: they assert the agent *cannot*
write outside the outbox, including via traversal, absolute paths, and
lookalike sibling directories. No network, no API key.

Run:
    .venv\\Scripts\\python.exe test_fs_policy.py
"""

import asyncio

from fs_policy import INBOX, OUTBOX, REPO_ROOT, check_access, target_path

passed = failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}" + (f" - {detail}" if detail else ""))


def allows(tool: str, path: str) -> bool:
    return check_access(tool, {"file_path": path})[0]


async def main() -> int:
    print("writes - permitted")
    check("write into the outbox", allows("Write", str(OUTBOX / "TICK-5001.md")))
    check(
        "write into a subdirectory of the outbox",
        allows("Write", str(OUTBOX / "2026" / "TICK-5001.md")),
    )
    check("edit a file in the outbox", allows("Edit", str(OUTBOX / "TICK-5001.md")))

    print("\nwrites - blocked")
    check(
        "cannot write to the repo root",
        not allows("Write", str(REPO_ROOT / "pubspec.yaml")),
    )
    check(
        "cannot write into lib/",
        not allows("Write", str(REPO_ROOT / "lib" / "main.dart")),
    )
    check(
        "cannot overwrite the triage spec",
        not allows("Edit", str(REPO_ROOT / "lib" / "triage" / "triage_spec.md")),
    )
    check("cannot write into the inbox", not allows("Write", str(INBOX / "note.txt")))
    check(
        "cannot escape the outbox with ..",
        not allows("Write", str(OUTBOX / ".." / ".." / "escaped.txt")),
    )
    check(
        "cannot escape with a deep traversal chain",
        not allows("Write", str(OUTBOX / ".." / ".." / ".." / ".." / "escaped.txt")),
    )
    check(
        "cannot write outside the repo entirely",
        not allows("Write", "C:\\Windows\\Temp\\escaped.txt"),
    )
    check(
        "cannot write to a sibling that merely starts with the outbox name",
        not allows("Write", str(OUTBOX.parent / (OUTBOX.name + "_evil") / "x.txt")),
    )
    check(
        "relative paths resolve against the repo, not the outbox",
        not allows("Write", "notes.txt"),
    )
    check(
        "NotebookEdit is treated as a write",
        not allows("NotebookEdit", str(REPO_ROOT / "x.ipynb")),
    )

    print("\nreads")
    check("can read the triage spec", allows("Read", "lib/triage/triage_spec.md"))
    check("can read from the inbox", allows("Read", str(INBOX / "msg.txt")))
    check("can glob inside the repo", allows("Glob", str(REPO_ROOT / "lib")))
    check(
        "cannot read outside the repo",
        not allows("Read", "C:\\Windows\\System32\\drivers\\etc\\hosts"),
    )

    print("\npath extraction")
    check("finds file_path", target_path({"file_path": "a.txt"}) == "a.txt")
    check("finds notebook_path", target_path({"notebook_path": "a.ipynb"}) == "a.ipynb")
    check("returns None when there is no path", target_path({"pattern": "x"}) is None)
    check("ignores an empty path", target_path({"file_path": "   "}) is None)
    check(
        "a tool with no path is allowed through",
        check_access("Grep", {"pattern": "TODO"})[0],
    )

    # Hook and callback wiring on top of these path checks lives in
    # test_permissions.py, since the policy engine is what consumes them.

    print("\ninbox input resolution")
    from agent import resolve_single

    msg, src = resolve_single(["001-api-outage.txt"])
    check("reads a real inbox file", src == "001-api-outage.txt" and "jane@" in msg)

    msg, src = resolve_single(["not-a-file.txt"])
    check("a missing file falls back to literal text", src == "(command line)")

    # Regression: a traversal argument pointing at a file that really exists
    # must not be read off disk and fed to the model.
    sentinel = REPO_ROOT.parent / "test_fs_policy_sentinel.txt"
    sentinel.write_text("sentinel-secret", encoding="utf-8")
    try:
        import os

        rel = os.path.relpath(sentinel, INBOX)
        msg, src = resolve_single([rel])
        check(
            "traversal cannot read a file outside the inbox",
            src == "(command line)" and "sentinel-secret" not in msg,
            f"got source={src!r}",
        )
    finally:
        sentinel.unlink(missing_ok=True)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
