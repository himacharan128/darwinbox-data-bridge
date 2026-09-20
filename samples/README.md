# Test samples

Five sets, each aimed at a different behaviour. Upload a folder through **New
migration**, or drop it into `tests/fixtures/` and start a run from it.

What each one should do — if it does something else, that is a bug worth reporting.

## `01-clean/` — the happy path

Four employees, correct formats, every required field present.

**Expect:** 4 records, **0 cases**, status *Ready to send*. Press send; all four land
in the destination.

The point: an agent that escalates on clean data is useless. This is the control.

## `02-messy/` — the real one

Seven files in five naming conventions: CSV, Excel, JSON-ish, a native PDF export and
a scanned roster. Overlapping employees, complementary fields, contradictions.

**Expect:** 40 records, ~35 cases across **8 of the 11 escalation classes**, 66 of 101
columns mapped unaided.

Worth doing in order:
1. Answer *"no column for status"* → 13 records go ready from one decision.
2. Open the `contact` case → 3 emails, 3 phones, no winner.
3. Open a date case → both readings shown, because nothing in that column settles it.
4. Open the scan case → the cropped image of what OCR misread.

## `03-conflicts/` — two systems that disagree

The same two people in two exports, disagreeing on job title, joining date and
location. Plus a **namesake**: same name, same birthday, different employee ID.

**Expect:** conflicts escalate per field with both values and their sources. The
namesake must **not** merge — identity and field conflicts are separate decisions,
and matching names never justify an automatic merge.

## `04-edge-cases/` — things that should not crash it

Empty file · header with no rows · a single column · duplicate header names · Greek
and Chinese names · 300-character values · impossible dates (`2020-13-45`,
`0000-00-00`) · a future birth date · a lowercase ID where the schema is
case-sensitive · a `.csv` that is really a PDF · malformed JSON · nested YAML ·
multi-sheet Excel with a prose sheet.

**Expect:** no crashes. Unreadable files are reported by name with a reason. The
lowercase ID escalates rather than being "helpfully" uppercased. The prose sheet does
not become employee records. Long values fail `max_length` per record rather than
disqualifying the whole column.

## `05-adversarial/` — hostile input

A prompt injection in a job title, a SQL fragment, a spreadsheet formula, and a file
of pure system noise (checksums, ETL batch ids, bank account numbers).

**Expect:** the injection is preserved as *data* and obeyed by nothing — other records
keep their real employment type. The noise file raises **one** case saying it does not
look like employee data, not one per missing field. No column from it is ever mapped.

---

## Quick automated pass

```bash
make samples     # runs all five sets and prints what each produced
```
