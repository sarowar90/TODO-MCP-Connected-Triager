---
name: shift-handover
description: >-
  Build the end-of-batch support shift handover from tickets that have already
  been triaged and filed. Use this after every ticket in a batch has been filed
  and written to the outbox, when the task is to aggregate them into a handover
  digest and workbook for the next shift. Produces digest.md and handover.xlsx
  with one row per ticket, COUNTIF totals per team and urgency, and a
  "needs attention first" section covering urgent tickets and anything flagged
  for human review. Do not use this to classify or re-classify a message, to
  triage a single ticket, or before the tickets exist — it only aggregates
  tickets already on disk.
version: 1.0.0
---

# Shift handover

Aggregate a batch of filed tickets into the two artefacts the next shift reads:
a markdown digest they skim, and a workbook they sort and filter.

## When this applies

Use it when **all** of the following hold:

- every message in the batch has already been triaged and filed;
- one `TICK-*.md` file per ticket exists in the outbox;
- the task is to summarise the batch, not to classify anything.

If any ticket is still unfiled, stop and say so rather than producing a partial
handover — a handover that silently omits a ticket is worse than none, because
the next shift has no way to know something is missing.

## Steps

1. **Collect.** Glob `TICK-*.md` in the outbox and read each one. These are the
   source of truth. Do not re-derive urgency, topic, or team from the original
   message — the filed ticket already settled that.

2. **Write `tickets.json`** in the outbox: a JSON array, one object per ticket,
   with exactly these keys:

   ```json
   [
     {
       "ticket_id": "TICK-5001",
       "urgency": "urgent",
       "topic": "technical",
       "team": "engineering",
       "confidence": 0.96,
       "needs_human_review": false,
       "summary": "Platform-wide API 500s since 09:00."
     }
   ]
   ```

   Keep `urgency`, `topic`, and `team` exactly as they appear in the ticket
   file. `confidence` is a number, `needs_human_review` a boolean.

3. **Build the workbook** by running the bundled script. It is the supported
   way to produce the file — do not hand-write openpyxl code, because the
   script already encodes the column order, the formula style, and the
   formatting that the next shift's filters depend on:

   ```bash
   python <skill_dir>/scripts/build_handover.py <outbox>/tickets.json <outbox>/handover.xlsx
   ```

   It prints a one-line summary and exits non-zero on bad input. If it fails,
   read the error, fix `tickets.json`, and run it again.

4. **Write `digest.md`** in the outbox, covering:
   - one line summarising the batch;
   - a markdown table of every ticket: id, urgency, topic, team, review flag;
   - counts per team and per urgency;
   - a `## Needs attention first` section naming every `urgent` ticket and
     everything with `needs_human_review: true`, one line each on why.

   Reference every ticket id exactly as it appears in the files.

## Constraints

- The outbox is the only writable location. Both arguments to the script must
  be paths inside it.
- Never edit a `TICK-*.md` file. They are the record of what was decided.
- Totals in the workbook are formulas, not typed numbers, so the sheet stays
  correct if someone edits a row. `reference/format.md` has the exact layout.
