---
name: commit
description: Stage all changes, write a Conventional Commits message derived from the actual diff, commit, and push to the remote. Use when the user asks to commit, "commit and push", "push this", "save my work to GitHub", or wants a properly formatted commit message written for them.
---

# Commit & Push (Conventional Commits)

Stage the working tree, write a Conventional Commits message from the **actual diff**, commit, and push to the current branch's remote.

## Steps

1. **Inspect the repo state** — run these together:
   - `git status --porcelain` — is there anything to commit?
   - `git branch --show-current` — the branch to push.
   - `git log --oneline -5` — recent style to match.
   If the working tree is clean, stop and tell the user there is nothing to commit.

2. **Stage everything**: `git add -A`.

3. **Read the actual change** — `git diff --staged`. Base the message on what the diff really does; never guess from filenames or the conversation alone. For a large diff, `git diff --staged --stat` first, then read the meaningful hunks.

4. **Write the message** in Conventional Commits format:

   ```
   <type>(<scope>): <subject>

   <optional body — what & why, wrapped ~72 cols>

   <optional footer — BREAKING CHANGE:, Refs: #123>
   ```

   - **type** (required): `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`.
   - **scope** (optional): the affected area — a feature/module/dir (e.g. `triage`, `todo`, `deps`). Omit if the change is broad.
   - **subject**: imperative mood, lowercase start, no trailing period, ≤ 72 chars ("add streaming", not "Added streaming.").
   - **body**: add only when the *why* isn't obvious from the subject. Bullet points are fine.
   - **Breaking changes**: put `!` after the type/scope (`feat(api)!: ...`) **and** a `BREAKING CHANGE: <description>` footer.
   - Pick the single type that best describes the primary change. If the diff is genuinely several unrelated changes, say so and suggest splitting into separate commits — but if the user wants one commit, choose the dominant type.

5. **Commit** with the message, ending with the co-author trailer:

   ```
   git commit -m "<type>(<scope>): <subject>" -m "<body if any>" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
   ```

6. **Push** the current branch:
   - If it already tracks an upstream: `git push`.
   - If not (new branch): `git push -u origin <branch>`.
   Report the result. If the push is rejected (e.g. non-fast-forward), stop and report — do not force-push unless the user explicitly asks.

## Rules

- Derive the message from the diff, not from assumptions.
- Never use `--no-verify` or skip hooks. If a pre-commit/pre-push hook fails, stop, report the failure, and fix the underlying issue rather than bypassing it.
- Never force-push unless the user explicitly requests it.
- Do not commit secrets (API keys, tokens, `.env`). If the staged diff contains one, stop and warn instead of committing.
- One logical change per commit is ideal — flag when the staged set is clearly several unrelated things.

## Examples

- `feat(triage): add SSE streaming for live agent UX`
- `fix(todo): prevent duplicate ids on rapid add`
- `docs: add CLAUDE.md with project overview`
- `refactor(triage): run independent tool calls in parallel`
- `chore(deps): add http dependency`
- `test(triage): cover fragmented tool_use JSON reassembly`
