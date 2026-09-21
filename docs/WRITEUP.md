# Data Bridge — approach and autonomy boundary

**Hima Charan · [github.com/himacharan128/darwinbox-data-bridge](https://github.com/himacharan128/darwinbox-data-bridge)**

## Approach

The agent reads a client's exports, works out the mapping to the target schema, cleans
what it can defend, and asks a human only where the evidence runs out. On seven files
in five naming conventions it maps **85 of 87 mappable columns unaided** — leaving 14
noise columns alone — reconciles 46 rows into 40 people, and raises **20 cases**.

The design rule is **the model proposes, deterministic code disposes.** That is not a
hedge against a weak model. It is the conclusion of two measurements, below.

## Where the model is used, and what gates it

**Reading a whole file at once.** Judged alone, `Manager ID` scored 1.000 for
`manager_id` on its name and 0.789 for `employee_id` on the shape of its values, and
went to a human as a tie — a question a person could not answer either, in isolation.
Beside `Emp ID` it is obvious. The model now sees every header, its samples and the
schema in one call. *Gate:* its vote is capped, and within a file a field applied to
one column is taken back from any other.

**Naming a synonym.** `role` scores **0.047** against `designation` — no shared tokens,
shape or values. The connection is meaning, the one thing the model has that the code
does not. *Gate:* proposed aliases land in a draft schema a person approves.

**Investigating before asking.** On a checkable case the agent gets four read-only
lookups over the run's own data, and must call one before it may answer. Asked why a
file has no email column, it replies that the file has a `contact` column holding email
addresses. *Gate:* it can only suggest an answer on a case a person still resolves —
never map, clean or send.

## Why the model does not decide

Two obvious ways to raise the auto-apply rate were measured and rejected.

**Raising the cap.** At 0.30 the rate reaches 68% — and a noise column gets mapped.
Mapping a checksum is the worst failure available: it corrupts the dataset silently,
with no case raised for anyone to catch.

**Letting it break near-ties.** Reaches 73% — and maps `dtProbationEnd` onto
`date_of_joining` on a model vote of **1.00**, writing probation-end dates into the
joining-date field.

So `score = 0.75 × deterministic evidence + 0.25 × model vote`, and a candidate with
no measured support cannot exceed 0.25 — never reaching the 0.70 threshold, however
certain the model sounds. The same holds for alias suggestions: it votes **1.00** that
`strAuditUser` means `designation` and **1.00** that `dob` means `date_of_birth`. Its
confidence separates nothing, so a person ticks the list.

## Where the line is

**Calibrated, not chosen.** 77 labelled decisions, 18 of them columns that must *not*
map. At the chosen thresholds: **50 of 57 mappable columns applied unaided, zero wrong,
zero noise mapped, zero genuine ambiguity silently resolved** — and those three zeros
hold at *every* threshold in the sweep, so they are properties of the design, not a
lucky cutoff.

Auto-apply has two routes: a high score clear of the runner-up, or a winner decisively
clear of everything else — `site → location_code` scores only 0.68 but leaves the
runner-up half a scale behind. Both require separation.

For cleanup the line is **reversible and evidence-backed versus inventing data**:
stripping punctuation to satisfy a pattern is safe, adding a `+91` is not.

## Sizing the queue is part of the boundary

A boundary that is correct but unusable has failed.

- **One question per fact, at the fact's own scope.** A required field with no source
  is one question about a *file*; an undecidable date column, one about the *column* —
  it used to be five identical ones, each blocking one employee.
- **Rules, not answers.** Eight of nine conflicts were `roster_export.pdf` against
  `scanned_roster.pdf`: the same roster, once native and once OCR'd. Not eight
  judgements — one rule, *believe the real document*, and nine become one.
- **Sending is a decision.** Everything else is reversible inside the console; writing
  to the client's system is not, so it waits for a push.

## What I'd build next

1. **PII.** Salary, date of birth, national identifiers — field-level sensitivity,
   masked display, audit redaction, and a policy on what reaches a hosted model. The
   chokepoint exists: profiling is the only place values reach it.
2. **Effective dating.** An HRMS models people temporally; this flattens to a snapshot.
3. **Deterministic tie-breaks.** A manager reference is a value drawn from the
   employee-id column but *not* unique — measurable, and it would settle the near-ties
   the model is not allowed to.
4. **Calibrate matching.** Mapping thresholds are measured; matching ones are asserted.
