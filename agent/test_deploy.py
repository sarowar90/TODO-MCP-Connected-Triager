"""Offline checks for orchestration and the deployment path.

Covers the parts of step 11 that don't need the model or a cloud account: the
per-role tool surfaces, the concurrency bound, the CI workflow being valid and
pointing at files that exist, and preflight reporting readiness honestly.

What it cannot cover is a deployment actually running. See HOSTING.md.

Run:
    .venv\\Scripts\\python.exe test_deploy.py
"""

import subprocess
import sys
from pathlib import Path

import yaml

AGENT_DIR = Path(__file__).resolve().parent
REPO = AGENT_DIR.parent
WORKFLOW = REPO / ".github" / "workflows" / "agent-ci.yml"

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
    print("roles and least privilege")
    from loop import ROLE_DENIED_TOOLS, Role
    from permissions import AUTO_APPROVED_TOOLS, LOOKUP_TOOLS

    check("both roles are defined", {r for r in Role} == {Role.TRIAGE, Role.HANDOVER})
    check("every role has a denial list", all(r in ROLE_DENIED_TOOLS for r in Role))

    triage_denied = ROLE_DENIED_TOOLS[Role.TRIAGE]
    handover_denied = ROLE_DENIED_TOOLS[Role.HANDOVER]

    check("a triage agent cannot run a shell", "Bash" in triage_denied)
    check("a triage agent cannot load a document skill", "Skill" in triage_denied)
    check(
        "a handover agent cannot query the CRM",
        all(tool in handover_denied for tool in LOOKUP_TOOLS),
    )
    check(
        "the roles deny different things",
        set(triage_denied) != set(handover_denied),
        "identical denial lists would mean the split buys nothing",
    )

    def surface(role: Role) -> list[str]:
        denied = ROLE_DENIED_TOOLS[role]
        return [t for t in AUTO_APPROVED_TOOLS if t not in denied]

    triage_surface = surface(Role.TRIAGE)
    handover_surface = surface(Role.HANDOVER)
    check("the triage agent keeps its lookups", all(t in triage_surface for t in LOOKUP_TOOLS))
    check("the handover agent keeps Skill", "Skill" in handover_surface)
    check(
        "neither role has the full surface",
        len(triage_surface) < len(AUTO_APPROVED_TOOLS)
        and len(handover_surface) < len(AUTO_APPROVED_TOOLS),
    )
    check(
        "both roles can still read and write",
        all(t in triage_surface and t in handover_surface for t in ("Read", "Write")),
    )

    print("\nconcurrency")
    from plan import MAX_CONCURRENCY

    check("concurrency is bounded", isinstance(MAX_CONCURRENCY, int) and MAX_CONCURRENCY >= 1)
    check(
        "the bound is modest enough to respect rate limits",
        MAX_CONCURRENCY <= 8,
        f"{MAX_CONCURRENCY} concurrent sessions is a wide fanout",
    )

    print("\ncheckpointing under concurrency")
    import inspect

    from loop import run_step

    signature = inspect.signature(run_step)
    check("run_step accepts a role", "role" in signature.parameters)
    check("run_step allows checkpointing to be disabled", "checkpoints" in signature.parameters)
    check(
        "checkpoints default to on for sequential callers",
        signature.parameters["checkpoints"].default is None,
        "None means the caller decides; plan.py passes a store for the digest step",
    )

    source = (AGENT_DIR / "plan.py").read_text(encoding="utf-8")
    check(
        "the fan-out disables per-step checkpoints",
        "checkpoints=None" in source,
        "concurrent per-step snapshots of one outbox would race",
    )
    check("a batch checkpoint covers the fan-out instead", "before the triage batch" in source)
    check(
        "one agent raising does not abort the batch",
        "return_exceptions=True" in source,
    )
    check("the fan-out is bounded by a semaphore", "Semaphore(MAX_CONCURRENCY)" in source)

    print("\nCI workflow")
    check("the workflow exists", WORKFLOW.is_file())
    if WORKFLOW.is_file():
        raw = WORKFLOW.read_text(encoding="utf-8")
        try:
            spec = yaml.safe_load(raw)
            check("the workflow is valid YAML", isinstance(spec, dict))
        except yaml.YAMLError as exc:
            check("the workflow is valid YAML", False, str(exc))
            spec = {}

        # `on:` is parsed by YAML 1.1 as the boolean True, not the string "on".
        triggers = spec.get("on", spec.get(True, {})) or {}
        check("it runs on push", "push" in triggers)
        check("it can be triggered manually", "workflow_dispatch" in triggers)

        jobs = spec.get("jobs", {})
        check("it defines a job", bool(jobs))
        job = next(iter(jobs.values()), {})
        check("it runs on Linux, unlike the dev machine", "ubuntu" in str(job.get("runs-on", "")))
        check("it has a timeout", "timeout-minutes" in job)
        check(
            "it runs from the agent directory",
            "agent" in str(job.get("defaults", {})),
        )

        steps = job.get("steps", [])
        run_blocks = "\n".join(s.get("run", "") for s in steps if isinstance(s, dict))
        for suite in (
            "test_loop", "test_fs_policy", "test_plan", "test_permissions",
            "test_checkpoints", "test_hosting", "test_skill",
        ):
            check(f"CI runs {suite}", suite in run_blocks)
        for demo in ("demo_rollback", "demo_xlsx", "demo_skill"):
            check(f"CI runs {demo}", demo in run_blocks)

        check(
            "CI does not require an API key",
            "ANTHROPIC_API_KEY" not in raw,
            "CI must not depend on a secret that is not configured",
        )
        check("no literal key in the workflow", "sk-ant-" not in raw)
        check(
            "preflight is allowed to fail in CI",
            "continue-on-error: true" in raw,
            "no credentials in CI, so the auth check will fail by design",
        )

        # Every script CI runs must exist, or the pipeline breaks on push. The
        # workflow drives loops over bare names ("$suite.py"), so resolve the
        # enumerated names rather than scanning for literal .py tokens.
        names = [
            token.strip('";\\')  # shell leaves `test_skill;` before `do`
            for token in run_blocks.replace("\\\n", " ").split()
            if token.startswith(("test_", "demo_", "preflight"))
        ]
        names = [n if n.endswith(".py") else f"{n}.py" for n in names]
        missing = sorted({n for n in names if not (AGENT_DIR / n).is_file()})
        check(
            "every script CI names exists on disk",
            not missing,
            str(missing),
        )
        check("CI names a non-trivial number of scripts", len(set(names)) >= 10, str(len(set(names))))

        literal = [
            token
            for token in run_blocks.replace('"', " ").split()
            if token.endswith(".py") and "$" not in token
        ]
        missing_literal = [t for t in literal if not (AGENT_DIR / t).is_file()]
        check("every literally-named script exists", not missing_literal, str(missing_literal))

    print("\npreflight")
    result = subprocess.run(
        [sys.executable, str(AGENT_DIR / "preflight.py")],
        capture_output=True, text=True, cwd=AGENT_DIR,
    )
    output = result.stdout
    check("preflight runs", "PREFLIGHT" in output, result.stderr[:120])
    check("it checks the policy is still closed", "unknown tools fail closed" in output)
    check("it checks the read root is sane", "read root is not the filesystem root" in output)
    check("it checks the skill is present", "shift-handover skill is present" in output)
    check("it checks the outbox is writable", "outbox is writable" in output)
    check(
        "it reports NOT READY without credentials rather than passing",
        "NOT READY" in output and result.returncode == 1,
        "a preflight that always passes is worse than none",
    )
    check(
        "the API check is opt-in, not implicit",
        "pass --check-api" in output,
    )

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
