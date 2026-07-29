# Handover workbook format

The exact layout `scripts/build_handover.py` produces. Read this if you need to
verify the output or explain it; you do not need it to run the script.

## `Tickets` sheet

Row 1 is the header, bold on a grey fill, frozen, with an autofilter across
`A1:G<last>`. One row per ticket after that, in the order given in
`tickets.json`.

| Column | Field | Notes |
|---|---|---|
| A | `ticket_id` | e.g. `TICK-5001` |
| B | `urgency` | `urgent` / `high` / `normal` / `low` |
| C | `topic` | the closed topic taxonomy |
| D | `team` | owning team; the `COUNTIF` range on Summary |
| E | `confidence` | number, 0.0–1.0 |
| F | `needs_human_review` | boolean; `TRUE` rows are filled amber |
| G | `summary` | one sentence, wrapped |

Rows with `needs_human_review = TRUE` are highlighted amber across all columns,
so the next shift can see what still needs a person without filtering first.

## `Summary` sheet

Three blocks, separated by a blank row:

1. **Per team** — one row per distinct team, `=COUNTIF(Tickets!D:D,A<row>)`
2. **Per urgency** — only the urgencies actually present, worst first
   (`urgent`, `high`, `normal`, `low`), `=COUNTIF(Tickets!B:B,A<row>)`
3. **Needs human review** — a single `=COUNTIF(Tickets!F:F,TRUE)`

## Why the totals are formulas

Typed totals go stale the moment someone edits a row, and a handover sheet is
something people edit — reassigning a team, clearing a review flag. `COUNTIF`
recalculates, so the summary cannot silently disagree with the rows above it.

`COUNTIF` is an Excel-2007 function, so it also evaluates in LibreOffice and
Google Sheets. Newer functions such as `XLOOKUP`, `FILTER`, and `UNIQUE` do not
evaluate everywhere and are deliberately avoided.

## Failure behaviour

The script exits non-zero and writes nothing if `tickets.json` is missing, is
not a JSON array, is empty, or if any entry lacks one of the seven fields. The
message names the offending entry, so the fix is to correct `tickets.json` and
run it again — never to hand-build the workbook instead.
