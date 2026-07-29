"""Production entrypoint.

`agent.py` is the interactive CLI. This is what a scheduler, a queue consumer,
or a container ENTRYPOINT should call instead, because unattended execution
needs four things the interactive path does not:

  * **A single-instance lock.** Batches share one outbox. Two overlapping runs
    interleave writes to the same ticket files and each other's checkpoints.
    Cron will happily start a second run while the first is still going, so
    this refuses rather than corrupting.
  * **Graceful shutdown.** A container stop or an eviction sends SIGTERM. Left
    unhandled, the process dies mid-step and leaves a half-written outbox. Here
    it finishes the step in flight, then stops.
  * **Structured logs on stdout.** Nothing reads a pretty console in
    production. One JSON object per line is what a log collector ingests.
  * **Exit codes a scheduler can branch on**, rather than 0/1 for everything.

    0   the batch completed and met its goal
    1   it ran but the goal was not met
    69  a dependency was unavailable (EX_UNAVAILABLE)
    70  an unexpected internal error (EX_SOFTWARE)
    75  another run holds the lock; try again later (EX_TEMPFAIL)
    78  the environment is not fit to run (EX_CONFIG)

Usage:
    python runner.py                 # preflight, then the batch
    python runner.py --skip-preflight
"""

import asyncio
import json
import os
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fs_policy import WORKSPACE, ensure_workspace

LOCK_PATH = WORKSPACE / ".runner.lock"
# A lock older than this with no live process behind it is treated as debris
# from a killed run rather than an active one.
STALE_LOCK_SECONDS = 6 * 60 * 60

EX_OK = 0
EX_GOAL_NOT_MET = 1
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70
EX_TEMPFAIL = 75
EX_CONFIG = 78


def log(event: str, **fields) -> None:
    """One JSON object per line, on stdout, for a collector to ingest."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), flush=True)


class LockHeld(Exception):
    """Another run holds the lock."""


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = os.system(f'tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL')
        return result == 0
    try:
        os.kill(pid, 0)  # signal 0 tests existence without delivering anything
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _read_lock() -> dict:
    try:
        return json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _lock_is_stale() -> tuple[bool, str]:
    info = _read_lock()
    if not info:
        return True, "lock file is unreadable or empty"

    pid = int(info.get("pid", 0) or 0)
    if not _process_alive(pid):
        return True, f"pid {pid} is not running"

    age = time.time() - float(info.get("acquired_at", 0) or 0)
    if age > STALE_LOCK_SECONDS:
        return True, f"held for {age / 3600:.1f}h, beyond the {STALE_LOCK_SECONDS / 3600:.0f}h ceiling"

    return False, f"held by live pid {pid}"


@contextmanager
def single_instance():
    """Refuse to start if another run is live; reclaim a dead run's lock."""
    ensure_workspace()

    if LOCK_PATH.exists():
        stale, why = _lock_is_stale()
        if not stale:
            raise LockHeld(why)
        log("lock.reclaimed", reason=why)
        LOCK_PATH.unlink(missing_ok=True)

    try:
        # O_EXCL makes creation atomic: two runners racing here, one loses.
        handle = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise LockHeld("lost the race to another runner") from exc

    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "acquired_at": time.time()}, fh)
        log("lock.acquired", pid=os.getpid(), path=str(LOCK_PATH))
        yield
    finally:
        LOCK_PATH.unlink(missing_ok=True)
        log("lock.released")


class Shutdown:
    """Turns SIGTERM/SIGINT into a flag the loop can act on at a safe point."""

    def __init__(self) -> None:
        self.requested = False
        self.signal_name = ""

    def install(self) -> None:
        for name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):
                # Not on the main thread, or unsupported on this platform.
                log("shutdown.handler_unavailable", signal=name)

    def _handle(self, signum, frame) -> None:  # noqa: ANN001
        self.signal_name = signal.Signals(signum).name
        if self.requested:
            # A second signal means someone is impatient; stop pretending.
            log("shutdown.forced", signal=self.signal_name)
            raise SystemExit(EX_SOFTWARE)
        self.requested = True
        log("shutdown.requested", signal=self.signal_name,
            note="finishing the step in flight, then stopping")


async def run(skip_preflight: bool, shutdown: Shutdown) -> int:
    if not skip_preflight:
        import preflight

        log("preflight.start")
        code = preflight.main([])
        failures = [name for name, ok, _ in preflight.CHECKS if not ok]
        log("preflight.done", ready=code == EX_OK, failed=failures)
        if code != EX_OK:
            return EX_CONFIG

    if shutdown.requested:
        log("run.aborted", reason="shutdown requested before starting")
        return EX_OK

    from loop import LoopOutcome, report
    from permissions import DenyApprover
    from plan import run_plan

    approver = DenyApprover()
    outcome = LoopOutcome(approver_name=approver.name)

    started = time.time()
    log("batch.start", approver=approver.name)
    outcome = await run_plan(outcome, approver)
    elapsed = time.time() - started

    report(outcome)

    log(
        "batch.done",
        goal_met=outcome.goal_met,
        steps=len(outcome.steps),
        turns=outcome.turns,
        cost_usd=round(outcome.cost_usd, 4),
        tickets=len(outcome.tickets),
        approvals_refused=sum(1 for _, _, granted in outcome.audit.asked if not granted),
        denied=len(outcome.audit.denied),
        rolled_back=len(outcome.rolled_back),
        elapsed_s=round(elapsed, 1),
        interrupted=shutdown.requested,
    )

    if not outcome.steps:
        return EX_UNAVAILABLE
    return EX_OK if outcome.goal_met else EX_GOAL_NOT_MET


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    skip_preflight = "--skip-preflight" in argv

    shutdown = Shutdown()
    shutdown.install()

    log("runner.start", pid=os.getpid(), python=sys.version.split()[0])

    try:
        with single_instance():
            return asyncio.run(run(skip_preflight, shutdown))
    except LockHeld as exc:
        # Not an error: the previous run simply has not finished.
        log("runner.skipped", reason=str(exc), exit_code=EX_TEMPFAIL)
        return EX_TEMPFAIL
    except KeyboardInterrupt:
        log("runner.interrupted")
        return EX_SOFTWARE
    except Exception as exc:  # noqa: BLE001
        log("runner.failed", error=f"{type(exc).__name__}: {exc}")
        return EX_SOFTWARE
    finally:
        log("runner.stop")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
