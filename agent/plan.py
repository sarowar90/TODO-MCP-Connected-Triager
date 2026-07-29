"""Multi-step task execution.

The goal — "triage the whole inbox and hand the shift over" — is decomposed
into a plan that is built up front, executed in sequence, and displayed as it
progresses:

    1..N  triage <message>     one step per inbox message
      N+1 write digest         depends on every result before it

The digest step is what makes this genuinely multi-step rather than a loop over
independent items: it cannot run until the triage steps have produced their
tickets, and its own goal check asserts it accounts for every ticket id the
earlier steps returned. Intermediate results are carried in PlanContext and
also on disk, as the ticket files the digest step reads back.

A failed triage step does not abort the batch — the remaining messages are
still processed, and the digest covers whatever was successfully filed. That
matters for a support queue: one unparseable message should not strand the
rest of the shift's work.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from checkpoints import CheckpointStore
from fs_policy import INBOX, REPO_ROOT
from loop import (
    DIGEST_NAME,
    DIGEST_SYSTEM_PROMPT,
    TRIAGE_SYSTEM_PROMPT,
    WORKBOOK_NAME,
    LoopOutcome,
    StepResult,
    make_deliverable_goal,
    outbox_files,
    run_step,
    skills_available,
    ticket_goal,
)
from triage_tools import TriageSession

Status = Literal["pending", "running", "done", "failed", "skipped"]

STATUS_MARK = {
    "pending": " ",
    "running": ">",
    "done": "x",
    "failed": "!",
    "skipped": "-",
}


@dataclass
class PlanStep:
    key: str
    title: str
    status: Status = "pending"
    note: str = ""


@dataclass
class Plan:
    steps: list[PlanStep] = field(default_factory=list)

    def add(self, key: str, title: str) -> PlanStep:
        step = PlanStep(key, title)
        self.steps.append(step)
        return step

    def set(self, key: str, status: Status, note: str = "") -> None:
        for step in self.steps:
            if step.key == key:
                step.status = status
                step.note = note
                break

    def render(self, heading: str = "PLAN") -> None:
        print(f"\n{heading}")
        for index, step in enumerate(self.steps, start=1):
            mark = STATUS_MARK[step.status]
            suffix = f"  {step.note}" if step.note else ""
            print(f"  {index}. [{mark}] {step.title}{suffix}")
        print()


@dataclass
class PlanContext:
    """Intermediate results carried between steps."""

    messages: list[tuple[str, str]] = field(default_factory=list)
    ticket_ids: list[str] = field(default_factory=list)
    results: list[StepResult] = field(default_factory=list)


def load_inbox() -> list[tuple[str, str]]:
    """Read every inbox message. Returns (source name, text) pairs."""
    return [
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted(INBOX.glob("*.txt"))
        if path.is_file()
    ]


def build_plan(messages: list[tuple[str, str]]) -> Plan:
    """Decompose the goal into an ordered plan before any work starts."""
    plan = Plan()
    for source, _ in messages:
        plan.add(f"triage:{source}", f"triage {source}")
    deliverables = (
        f"{DIGEST_NAME} + {WORKBOOK_NAME}" if skills_available() else DIGEST_NAME
    )
    plan.add("digest", f"aggregate the batch into {deliverables}")
    return plan


async def run_plan(outcome: LoopOutcome, approver=None) -> LoopOutcome:
    """Execute the plan in sequence, carrying intermediate results forward."""
    checkpoints = CheckpointStore()
    checkpoints.clear()  # each run starts its own checkpoint history
    context = PlanContext(messages=load_inbox())

    if not context.messages:
        print(
            f"Nothing to triage: {INBOX.relative_to(REPO_ROOT).as_posix()}/ is empty."
        )
        return outcome

    plan = build_plan(context.messages)
    plan.render("PLAN (built before any work starts)")

    # --- steps 1..N: triage each message -------------------------------------
    for index, (source, text) in enumerate(context.messages, start=1):
        key = f"triage:{source}"
        plan.set(key, "running")
        print(f"[step {index}/{len(plan.steps)}] triage {source}")

        result = await run_step(
            name=f"triage {source}",
            system_prompt=TRIAGE_SYSTEM_PROMPT,
            prompt=f"Triage this support message:\n\n{text}",
            goal=ticket_goal,
            audit=outcome.audit,
            session=TriageSession(),
            approver=approver,
            journal=outcome.journal,
            checkpoints=checkpoints,
        )
        outcome.steps.append(result)
        context.results.append(result)

        if result.goal_met and result.session.ticket:
            ticket_id = result.session.ticket.get("ticket_id", "")
            context.ticket_ids.append(ticket_id)
            ticket = result.session.ticket
            plan.set(
                key,
                "done",
                f"-> {ticket_id} {ticket.get('urgency')}/{ticket.get('team')}",
            )
        else:
            # Keep going: one bad message must not strand the rest of the queue.
            plan.set(key, "failed", result.reason)
            if result.checkpoint_id:
                outcome.rolled_back.append(
                    f"{result.name} -> rolled back to {result.checkpoint_id}"
                )

    # --- step N+1: aggregate, using the intermediate results ------------------
    if not context.ticket_ids:
        plan.set("digest", "skipped", "no tickets were filed")
        plan.render("PLAN (final)")
        return outcome

    plan.set("digest", "running")
    print(f"[step {len(plan.steps)}/{len(plan.steps)}] write {DIGEST_NAME}")

    filed = "\n".join(f"- {tid}" for tid in context.ticket_ids)
    digest_result = await run_step(
        name=f"write {DIGEST_NAME}",
        system_prompt=DIGEST_SYSTEM_PROMPT,
        prompt=(
            f"{len(context.ticket_ids)} ticket(s) were filed in this batch:\n"
            f"{filed}\n\n"
            f"Read each ticket file and write the handover digest."
        ),
        goal=make_deliverable_goal(context.ticket_ids),
        audit=outcome.audit,
        session=TriageSession(),
        approver=approver,
        journal=outcome.journal,
        checkpoints=checkpoints,
    )
    outcome.steps.append(digest_result)
    plan.set(
        "digest",
        "done" if digest_result.goal_met else "failed",
        "" if digest_result.goal_met else digest_result.reason,
    )
    if not digest_result.goal_met and digest_result.checkpoint_id:
        # This is the case SDK checkpoints cannot cover: the digest step ran in
        # its own session, so only the workspace checkpoint can undo damage it
        # did to tickets the earlier steps produced.
        outcome.rolled_back.append(
            f"{digest_result.name} -> rolled back to {digest_result.checkpoint_id}"
        )

    plan.render("PLAN (final)")
    outcome.written_files = outbox_files("*.md")
    return outcome


async def run() -> LoopOutcome:
    outcome = LoopOutcome()
    return await run_plan(outcome)
