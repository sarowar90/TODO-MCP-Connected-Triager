"""Offline checks for the container and isolation setup.

Docker is not required. These verify the things that would silently break
containment inside a container — chiefly that the read root is overridable,
since deriving it from the file layout yields "/" when the code sits at /app —
plus that the build files reference paths that exist and that the isolation
options are actually set on the options object.

What they cannot do is build or run the image. See HOSTING.md.

Run:
    .venv\\Scripts\\python.exe test_hosting.py
"""

import os
import re
import subprocess
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
REPO = AGENT_DIR.parent
DOCKERFILE = AGENT_DIR / "Dockerfile"
COMPOSE = AGENT_DIR / "compose.yaml"
DOCKERIGNORE = REPO / ".dockerignore"

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
    print("read root override (the container-layout trap)")
    # Derived from layout, the read root is the agent dir's parent. At /app
    # that is "/", which would make the whole filesystem readable.
    probe = (
        "import os,sys;"
        "sys.path.insert(0,r'%s');"
        "import fs_policy;"
        "print(fs_policy.REPO_ROOT.as_posix());"
        "print(fs_policy.check_access('Read', {'file_path': '/etc/passwd'})[0])"
    ) % AGENT_DIR

    env = dict(os.environ)
    env["AGENT_REPO_ROOT"] = "/app"
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env
    )
    lines = out.stdout.strip().splitlines()
    check(
        "AGENT_REPO_ROOT overrides the derived root",
        len(lines) >= 1 and lines[0].endswith("/app"),
        out.stdout + out.stderr,
    )
    check(
        "with the root at /app, /etc/passwd is not readable",
        len(lines) >= 2 and lines[1] == "False",
        out.stdout + out.stderr,
    )

    env["AGENT_REPO_ROOT"] = ""
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env
    )
    lines = out.stdout.strip().splitlines()
    check(
        "an empty override falls back to the derived root",
        len(lines) >= 1 and lines[0].endswith(REPO.as_posix().split("/")[-1]),
        out.stdout + out.stderr,
    )

    print("\nbuild context")
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    copies = re.findall(r"^COPY[^\n]*?\s(\S+)\s+/app", dockerfile, re.MULTILINE)
    sources = [c for c in copies if not c.startswith("--")]
    for source in sources:
        if "*" in source:
            matches = list(REPO.glob(source))
            check(f"COPY source {source} matches files", bool(matches))
        else:
            check(f"COPY source {source} exists", (REPO / source).exists())

    check(
        "the build context is the repo root, not agent/",
        "-f agent/Dockerfile" in dockerfile,
    )
    check(
        "no COPY escapes the build context",
        ".." not in " ".join(sources),
        str(sources),
    )
    check("requirements.txt is copied before the source",
          dockerfile.index("requirements.txt") < dockerfile.index("agent/*.py"))

    print("\nimage hardening")
    check("runs as a non-root user", "USER agent" in dockerfile)
    check("the user has no login shell", "nologin" in dockerfile)
    check("the read root is pinned for the container", "AGENT_REPO_ROOT=/app" in dockerfile)
    check("auto memory is disabled", "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1" in dockerfile)
    check("the CLI config dir is per-container", "CLAUDE_CONFIG_DIR=/app/workspace" in dockerfile)
    check("the entrypoint is the production runner", '"runner.py"' in dockerfile)
    check("a healthcheck is defined", "HEALTHCHECK" in dockerfile)
    check("the healthcheck uses preflight", "preflight.py" in dockerfile)
    # Mentioning the variable in a comment is fine; assigning it is not. Look
    # for an actual ENV/ARG assignment rather than any occurrence of the name.
    baked = re.search(
        r"^\s*(ENV|ARG)\s+[^\n]*ANTHROPIC_API_KEY\s*=", dockerfile, re.MULTILINE
    )
    check(
        "no API key is assigned in the image",
        baked is None,
        "the key must be supplied at run time",
    )
    check(
        "no literal key anywhere in the build files",
        not re.search(
            r"sk-ant-[A-Za-z0-9]",
            dockerfile + COMPOSE.read_text(encoding="utf-8"),
        ),
    )

    print("\nrun hardening")
    compose = COMPOSE.read_text(encoding="utf-8")
    for name, needle in [
        ("root filesystem is read-only", "read_only: true"),
        ("all capabilities dropped", "cap_drop"),
        ("privilege escalation blocked", "no-new-privileges:true"),
        ("runs as uid 10001", 'user: "10001:10001"'),
        ("memory is capped", "mem_limit"),
        ("pids are capped", "pids_limit"),
        ("the inbox is mounted read-only", "/app/workspace/inbox:ro"),
        ("tmp is a size-capped tmpfs", "/tmp:size="),
    ]:
        check(name, needle in compose)

    check(
        "the key comes from the environment, not the file",
        "${ANTHROPIC_API_KEY" in compose and "sk-ant-" not in compose,
    )

    print("\nskill packaging")
    check("our own skills ship in the image", "agent/skills /app/skills" in dockerfile)
    check(
        "third-party skills are kept out of the build context",
        "agent/vendor-skills/" in DOCKERIGNORE.read_text(encoding="utf-8"),
        "source-available content must not be baked into a published image",
    )

    print("\nbuild context hygiene")
    ignore = DOCKERIGNORE.read_text(encoding="utf-8")
    for name, needle in [
        ("the host virtualenv is excluded", "agent/.venv/"),
        ("git history is excluded", ".git"),
        ("env files are excluded", ".env"),
        ("keys are excluded", "*.key"),
        ("checkpoint snapshots are excluded", ".checkpoints/"),
    ]:
        check(name, needle in ignore)

    # Anything the image COPYs must not be excluded, or the build breaks.
    for needed in ("agent/requirements.txt", "lib/triage/triage_spec.md"):
        check(
            f"{needed} is not excluded from the context",
            not any(
                line.strip() and not line.startswith("#") and line.strip() in needed
                for line in ignore.splitlines()
            ),
        )

    print("\nsettings isolation in code")
    loop_src = (AGENT_DIR / "loop.py").read_text(encoding="utf-8")
    check("setting_sources is emptied", "setting_sources=[]" in loop_src)
    check("auto memory is disabled in options", "CLAUDE_CODE_DISABLE_AUTO_MEMORY" in loop_src)
    check("a per-run config dir is set", "CLAUDE_CONFIG_DIR" in loop_src)
    check("max_turns bounds the session", "max_turns=MAX_TURNS" in loop_src)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
