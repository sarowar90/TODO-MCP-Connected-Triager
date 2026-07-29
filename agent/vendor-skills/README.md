# Third-party skills (installed, not vendored)

Anthropic's pre-built document skills are installed here. Everything in this
directory except this README is gitignored.

They are **source-available, not open source**. Redistributing them — including
inside a container image you publish — is a licensing decision, so they are
deliberately not committed to this repository. Install them locally:

```bash
git clone https://github.com/anthropics/skills.git /tmp/anthropic-skills
cp -r /tmp/anthropic-skills/skills/xlsx agent/vendor-skills/xlsx
```

Check the licence in that repository before redistributing.

Our own skills live in [`../skills/`](../skills/) and *are* committed — see
[`../skills/shift-handover/SKILL.md`](../skills/shift-handover/SKILL.md).

Both directories are loaded as plugin paths, so a skill in either is available
to the agent without enabling project setting sources (which would re-open the
`CLAUDE.md` leak closed in step 8).

## Verifying

```powershell
.\.venv\Scripts\python.exe -c "import loop; print(loop._skill_plugins())"
```

With this directory empty the agent still runs: the custom `shift-handover`
skill alone is enough to build the workbook.
