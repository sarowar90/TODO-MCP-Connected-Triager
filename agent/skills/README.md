# Skills directory

The agent loads pre-built skills from here as a **plugin path**, not through
`setting_sources=["project"]`. That distinction matters: enabling the project
setting source would also re-enable the repository's `CLAUDE.md` and `.claude/`
directory, which is the context leak closed in step 8. A plugin path loads
skills from this directory and nothing else.

Expected layout:

```
agent/skills/
└── xlsx/
    └── SKILL.md
```

## Installing the pre-built xlsx skill

Anthropic's document skills are **source-available, not open source**. They are
deliberately *not* vendored into this repository — redistributing them here
would be a licensing decision, and this repo is public. Install them locally
instead:

```bash
git clone https://github.com/anthropics/skills.git /tmp/anthropic-skills
cp -r /tmp/anthropic-skills/skills/xlsx agent/skills/xlsx
```

Check the licence in that repository before redistributing the content
anywhere, including in a container image you publish.

Everything under this directory except this README is gitignored, so an
installed skill will not be committed by accident.

## Verifying it loaded

```powershell
.\.venv\Scripts\python.exe -c "import loop; print(loop.skills_available())"
```

When the directory is empty the agent runs without the skill: the digest step
still produces `digest.md`, and the workbook is simply not part of the goal.
`loop.skills_available()` is what switches that on, so nothing breaks when the
skill is absent.

## What the skill is used for

The digest step asks it to build `workspace/outbox/handover.xlsx` with a
`Tickets` sheet (one row per ticket, fixed headers) and a `Summary` sheet whose
counts are `COUNTIF` formulas rather than typed totals. `loop.check_workbook()`
opens the result with openpyxl and fails the step if it is missing, unopenable,
missing the sheet, missing a column, or missing a ticket.

## Why Bash had to be re-admitted

A document skill builds its file by running Python, so the blanket `Bash` deny
from step 6 would deny the skill. Bash is now admitted only as a single
`python`/`python3` invocation with no shell metacharacters and no path outside
the outbox — see `permissions.py` and the 20 allowlist checks in
`test_permissions.py`.
