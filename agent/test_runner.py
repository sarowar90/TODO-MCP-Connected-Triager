"""Offline checks for the production entrypoint.

The lock, the shutdown flag, the log format and the exit codes are all
deterministic, so they are testable without a key. The one thing not covered is
a real batch running, which needs the API.

Run:
    .venv\\Scripts\\python.exe test_runner.py
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import runner
from runner import (
    EX_CONFIG,
    EX_TEMPFAIL,
    LOCK_PATH,
    LockHeld,
    Shutdown,
    _lock_is_stale,
    _process_alive,
    single_instance,
)

AGENT_DIR = Path(__file__).resolve().parent

passed = failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}" + (f" - {detail}" if detail else ""))


def main() -> int:
    LOCK_PATH.unlink(missing_ok=True)

    print("single-instance lock")
    with single_instance():
        check("the lock file is created", LOCK_PATH.is_file())
        info = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        check("it records this pid", info.get("pid") == os.getpid())
        check("it records when it was taken", isinstance(info.get("acquired_at"), (int, float)))

        stale, why = _lock_is_stale()
        check("a live lock is not considered stale", not stale, why)

        # The whole point: a second runner must refuse rather than interleave
        # writes into the same outbox.
        try:
            with single_instance():
                check("a second run is refused while the first holds the lock", False)
        except LockHeld:
            check("a second run is refused while the first holds the lock", True)

    check("the lock is released on exit", not LOCK_PATH.exists())

    print("\nstale lock reclamation")
    LOCK_PATH.write_text(json.dumps({"pid": 999999, "acquired_at": time.time()}), encoding="utf-8")
    stale, why = _lock_is_stale()
    check("a lock held by a dead pid is stale", stale, why)
    with single_instance():
        check("and is reclaimed rather than blocking forever", LOCK_PATH.is_file())
        info = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        check("the reclaimed lock belongs to us now", info.get("pid") == os.getpid())

    LOCK_PATH.write_text(
        json.dumps({"pid": os.getpid(), "acquired_at": time.time() - (7 * 60 * 60)}),
        encoding="utf-8",
    )
    stale, why = _lock_is_stale()
    check("a lock held far too long is stale even if the pid is alive", stale, why)

    LOCK_PATH.write_text("not json at all", encoding="utf-8")
    stale, _ = _lock_is_stale()
    check("an unreadable lock is stale rather than fatal", stale)
    LOCK_PATH.unlink(missing_ok=True)

    check("this process is detected as alive", _process_alive(os.getpid()))
    check("an absent pid is detected as dead", not _process_alive(999999))
    check("pid 0 is not treated as alive", not _process_alive(0))

    print("\nshutdown handling")
    shutdown = Shutdown()
    check("it starts un-requested", not shutdown.requested)
    shutdown._handle(getattr(__import__("signal"), "SIGTERM", 15), None)
    check("one signal requests a graceful stop", shutdown.requested)
    check("the signal name is recorded", "SIG" in shutdown.signal_name)
    try:
        shutdown._handle(getattr(__import__("signal"), "SIGTERM", 15), None)
        check("a second signal exits immediately", False)
    except SystemExit:
        check("a second signal exits immediately", True)

    print("\nstructured logging")
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'%s'); import runner; "
         "runner.log('demo.event', a=1, b='two')" % AGENT_DIR],
        capture_output=True, text=True,
    )
    line = result.stdout.strip()
    try:
        record = json.loads(line)
        check("each log line is a JSON object", isinstance(record, dict), line)
        check("it carries a timestamp", "ts" in record)
        check("it carries the event name", record.get("event") == "demo.event")
        check("it carries the extra fields", record.get("a") == 1 and record.get("b") == "two")
    except json.JSONDecodeError:
        check("each log line is a JSON object", False, line)

    print("\nexit codes")
    check("distinct codes for distinct outcomes",
          len({runner.EX_OK, runner.EX_GOAL_NOT_MET, runner.EX_UNAVAILABLE,
               runner.EX_SOFTWARE, runner.EX_TEMPFAIL, runner.EX_CONFIG}) == 6)
    check("a held lock is temporary, not a failure", EX_TEMPFAIL == 75)
    check("a bad environment is a config error", EX_CONFIG == 78)

    # End to end: no API key, so preflight must stop it before any model call.
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    result = subprocess.run(
        [sys.executable, str(AGENT_DIR / "runner.py")],
        capture_output=True, text=True, cwd=AGENT_DIR, env=env, timeout=180,
    )
    check(
        "without credentials it exits EX_CONFIG rather than trying to run",
        result.returncode == EX_CONFIG,
        f"exit {result.returncode}",
    )

    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("{")]
    events = []
    for ln in lines:
        try:
            events.append(json.loads(ln)["event"])
        except (json.JSONDecodeError, KeyError):
            pass
    check("it logs a start event", "runner.start" in events, str(events[:6]))
    check("it logs the preflight result", "preflight.done" in events)
    check("it releases the lock even on failure", "lock.released" in events)
    check("it logs a stop event", "runner.stop" in events)
    check("no batch was attempted", "batch.start" not in events)
    check("the lock file is not left behind", not LOCK_PATH.exists())

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
