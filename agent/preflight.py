"""Production readiness check.

Run this on the host that will run the agent, before running it. It answers
"will this environment work?" rather than "does the code compile?", so it
checks the things that differ between a laptop and a deployment: credentials,
writability, the skill being present, the policy still denying what it should,
and — with --check-api — whether the model is actually reachable.

    python preflight.py              # everything except the API call
    python preflight.py --check-api  # also spend one cheap call to prove auth

Exit codes: 0 ready, 1 not ready. Suitable as a container healthcheck or a
deploy gate.
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

CHECKS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, ok, detail))
    return ok


def section(title: str) -> None:
    print(f"\n{title}")


def report() -> int:
    failures = [c for c in CHECKS if not c[1]]
    print("\n" + "=" * 66)
    for name, ok, detail in CHECKS:
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f"  — {detail}" if detail and not ok else ""))
    print("=" * 66)
    print("READY" if not failures else f"NOT READY — {len(failures)} check(s) failed")
    return 0 if not failures else 1


def check_runtime() -> None:
    section("runtime")
    major, minor = sys.version_info[:2]
    record(
        f"python {major}.{minor} meets the 3.10+ floor",
        (major, minor) >= (3, 10),
        f"found {major}.{minor}",
    )

    try:
        import claude_agent_sdk

        record(
            "claude-agent-sdk importable",
            True,
            getattr(claude_agent_sdk, "__version__", "?"),
        )
    except ImportError as exc:
        record("claude-agent-sdk importable", False, str(exc))
        return

    try:
        import openpyxl  # noqa: F401

        record("openpyxl importable (needed for the workbook)", True)
    except ImportError as exc:
        record("openpyxl importable (needed for the workbook)", False, str(exc))


def check_credentials() -> None:
    section("credentials")
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    proxied = bool(os.environ.get("ANTHROPIC_BASE_URL"))
    record(
        "an auth path is configured",
        bool(key) or proxied,
        "set ANTHROPIC_API_KEY, or ANTHROPIC_BASE_URL to route via a proxy",
    )
    if key:
        record(
            "the key looks like a key, not a placeholder",
            key.startswith("sk-ant-") and len(key) > 20 and "..." not in key,
            "value does not look like a real key",
        )
    if proxied and not key:
        record("key is held by the egress proxy, not the container", True)


def check_workspace() -> None:
    section("workspace")
    try:
        from fs_policy import INBOX, OUTBOX, REPO_ROOT, ensure_workspace

        ensure_workspace()
    except Exception as exc:  # noqa: BLE001
        record("workspace importable and creatable", False, str(exc))
        return

    record("workspace importable and creatable", True)
    record("read root is not the filesystem root", str(REPO_ROOT) not in ("/", "\\"),
           f"AGENT_REPO_ROOT resolves to {REPO_ROOT}")
    record("inbox exists", INBOX.is_dir())
    record("outbox exists", OUTBOX.is_dir())

    probe = OUTBOX / ".preflight-write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        record("outbox is writable", True)
    except OSError as exc:
        record("outbox is writable", False, str(exc))

    messages = list(INBOX.glob("*.txt"))
    record(
        "inbox has at least one message",
        bool(messages),
        "nothing to triage; mount inputs into the inbox",
    )

    free_mb = shutil.disk_usage(OUTBOX).free // (1024 * 1024)
    record("at least 200 MB free disk", free_mb >= 200, f"{free_mb} MB free")


def check_policy() -> None:
    """The policy is code; a bad edit could silently open it up. Assert the
    invariants that matter here, where a deploy gate can catch them."""
    section("permission policy")
    try:
        from fs_policy import OUTBOX, REPO_ROOT
        from permissions import CREATE_TICKET, Tier, classify
    except Exception as exc:  # noqa: BLE001
        record("policy importable", False, str(exc))
        return

    record("policy importable", True)
    record(
        "writes outside the outbox are denied",
        classify("Write", {"file_path": str(REPO_ROOT / "x.py")}).tier is Tier.DENY,
    )
    record(
        "writes inside the outbox are allowed",
        classify("Write", {"file_path": str(OUTBOX / "x.md")}).tier is Tier.AUTO,
    )
    record(
        "arbitrary shell is denied",
        classify("Bash", {"command": "rm -rf /"}).tier is Tier.DENY,
    )
    record(
        "unknown tools fail closed",
        classify("SomeFutureTool", {}).tier is Tier.DENY,
    )
    record(
        "urgent tickets still require approval",
        classify(
            CREATE_TICKET,
            {
                "urgency": "urgent", "topic": "technical", "team": "engineering",
                "summary": "x", "rationale": "y", "confidence": 0.9,
                "needs_human_review": False,
            },
        ).tier
        is Tier.ASK,
    )


def check_skills() -> None:
    section("skills")
    try:
        from loop import CUSTOM_SKILL, custom_skill_available, skills_available
    except Exception as exc:  # noqa: BLE001
        record("skill loader importable", False, str(exc))
        return

    record("skill loader importable", True)
    record(
        f"the {CUSTOM_SKILL} skill is present",
        custom_skill_available(),
        "the workbook cannot be built without a skill",
    )
    record("at least one skill root is populated", skills_available())


async def check_api() -> None:
    section("model reachability")
    try:
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
    except ImportError as exc:
        record("api reachable", False, str(exc))
        return

    try:
        subtype = "no_result"
        async for message in query(
            prompt="Reply with the single word: ready",
            options=ClaudeAgentOptions(
                model="claude-opus-5",
                max_turns=1,
                tools=[],
                permission_mode="dontAsk",
                setting_sources=[],
            ),
        ):
            if isinstance(message, ResultMessage):
                subtype = message.subtype
                if message.total_cost_usd:
                    print(f"  (cost ${message.total_cost_usd:.4f})")
        record("api reachable and authenticated", subtype == "success", subtype)
    except Exception as exc:  # noqa: BLE001
        record("api reachable and authenticated", False, str(exc)[:120])


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    print("=" * 66)
    print("PREFLIGHT")
    print("=" * 66)

    check_runtime()
    check_credentials()
    check_workspace()
    check_policy()
    check_skills()

    if "--check-api" in argv:
        asyncio.run(check_api())
    else:
        print("\nmodel reachability")
        print("  skipped — pass --check-api to spend one call proving auth")

    return report()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
