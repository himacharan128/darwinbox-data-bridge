# Where the line is, and why it is there

Assignment criterion 3 asks you to explain why the escalation boundary was drawn
where it was. This is the answer, as a measurement rather than an assertion.

Reproduce with `make sweep`. It runs offline from recorded model replays.

## The corpus

77 labelled source-column decisions across **five exports in five naming cultures** —
spaced headers (`Date of Joining`), snake_case (`joining_dt`), terse abbreviations
(`doj`), enterprise-verbose (`hire_dt`), and Hungarian notation (`dtDateOfJoin`).

| Label | Count | Meaning |
|---|---|---|
| `auto` | 57 | must be applied with no human input |
| `escalate` | 2 | genuinely two readings; must raise a case |
| `no_map` | 18 | not entity data at all; must be left alone |

The 18 `no_map` columns matter most. A corpus of only mappable columns measures
nothing about restraint, and real exports are full of row checksums, audit
timestamps, sync markers, internal notes and bank account numbers. **Mapping one of
those is the worst failure available**: it corrupts the dataset silently, with no
case raised for anyone to catch.

Labels are judgments about what each column means, not a rule derived from the
schema. An earlier pass labelled mechanically — anything whose target field declared
no constraint became `escalate` — and that wrongly condemned exact matches like
`Designation → designation`. The mechanical rule was the thing that was wrong.

## The sweep

```
 T_auto  T_gap  auto ok  wrong  noise  over-esc  under-esc   agree
   0.70   0.05       52      0      1         5          0    0.92
   0.70   0.10       51      0      0         7          0    0.91   <- chosen
   0.70   0.15       50      0      0         8          0    0.90
   0.75   0.10       50      0      0         8          0    0.90
   0.80   0.10       50      0      0         8          0    0.90
   0.90   0.10       47      0      0        11          0    0.86
```

Three things this shows that a chosen number could not:

**The safety properties hold everywhere.** Zero wrong mappings and zero
under-escalations at every threshold tried. They are properties of the design — the
capped model vote, the hard vetoes, the gap requirement — not of a lucky cutoff.

**`T_gap` is what protects against noise.** One junk column gets mapped at
`gap = 0.05` and none at `0.10`. A column that fits two fields almost equally well is
usually a column that fits neither, so requiring separation is the specific defence
against confidently mapping a checksum.

**The cost of caution is legible.** Every 0.05 of extra threshold buys nothing in
safety and costs roughly three to five columns a consultant then has to confirm by
hand. That is the trade being made, priced.

## Chosen: `T_auto = 0.70`, `T_gap = 0.10`, `T_decisive = 0.35`

**51 of 57** mappable columns applied unaided, zero wrong, zero noise mapped, zero
genuine ambiguity silently resolved. Agreement with the corpus: 0.91.

Two earlier versions of this boundary escalated far too much, for two separate and
measurable reasons.

### An abbreviated header scored as noise

`dob` scored **0.10** against `date_of_birth` and **0.11** against
`probation_end_date` — it matched the wrong field slightly better than the right
one. Token overlap is empty for an initialism and character similarity on sorted
letters is meaningless at three characters, so every abbreviated header (`doj`,
`mgr`, `loc`, `dept`, `emp_id`) sat indistinguishable from a checksum.

Two structural signals fix it without teaching the engine any domain vocabulary:
an **initialism** (`dob` is the first letters of date-of-birth) and a **contraction**
(`mgr` survives inside *manager* in order). Both ask about shape, never meaning.
`dob → date_of_birth` went 0.10 → 0.90. Guard rails matter here: a two-letter
initialism matched `id → is_deleted` at 0.90, so initialisms require three letters.

### A clear winner with a modest score was still escalated

`site → location_code` scored 0.68 with the runner-up half a scale behind at 0.17.
The absolute score was below `T_auto`, so it went to a human — even though the data
was not remotely ambiguous about which field it was. Synonyms behave this way by
construction: the name carries no signal, so the score stays low while the
separation is enormous.

Hence a second path to auto-apply: **a winner `T_decisive` clear of the runner-up**,
whatever its absolute score. Both paths still require separation. Neither lets the
model carry a column on its own.

| | auto ok | wrong | noise | under-esc |
|---|---|---|---|---|
| baseline | 44 | 0 | 0 | 0 |
| + abbreviation signals | 45 | 0 | 0 | 0 |
| + decisive margin | **51** | **0** | **0** | **0** |

The margin is insensitive between 0.25 and 0.45 — identical results across that
whole range — so 0.35 sits in the middle of a flat region rather than on a cliff.

## Where the model's knowledge does belong

A synonym has no measurable evidence at all. `Designation` and `job_title` share
no tokens, no shape and no values in common with the field name; `role` scores
0.047 against `designation`. No deterministic signal can ever connect them,
because the connection is meaning, and meaning is the one thing the model has
that the code does not.

So the model is asked — but at schema time, not at mapping time, and the answer
is a list a person ticks rather than a score that decides. That is the same gate
a proposed schema already goes through.

Asking it to is not optional, and neither is the gate. Measured on the corpus:

| the model's claim | vote | measured fit | correct? |
|---|---|---|---|
| `dob` → `date_of_birth` | 1.00 | — | yes |
| `role` → `designation` | 1.00 | 0.047 | yes |
| `strAuditUser` → `designation` | 1.00 | 0.061 | **no** |
| `cost_centre` → `department_code` | 1.00 | 0.052 | **no** |
| `dtProbationEnd` → `date_of_joining` | 1.00 | 0.613 | **no** |

**Neither number separates them.** The vote is 1.00 for every row, right and
wrong alike, so raising the threshold to 1.00 still admits all three mistakes.
Measured fit does not separate them either — the correct `role` sits *below* the
incorrect `strAuditUser`, and the incorrect `dtProbationEnd` sits above most
correct ones. Filtering on either number would hide the two suggestions worth
having and keep the one that corrupts data.

What separates them is looking at the values, which is a person's job. So every
suggestion is shown with its samples and the file it came from, nothing is
ticked to begin with, and nothing reaches the schema that a person did not put
there.

## Two things that would raise the rate, and demonstrably must not be done

The remaining escalations are four near-ties and two pure synonyms. Both of the
obvious ways to capture them break the guarantee, and the corpus catches both.

**Raising the model's cap.** At a cap of 0.30 the auto-rate reaches 68% and a noise
column is mapped. The cap is not decoration; it is load-bearing, and this is the
measurement that says so.

**Letting the model break near-ties.** Allowing the model to choose when
deterministic evidence is equal reaches 73% — and maps `dtProbationEnd` to
`date_of_joining` on a model vote of **1.00**. Confidently, completely wrong, and it
would write probation-end dates into the joining-date field with no case raised.

So 51 of 57 is not a tuning ceiling that more effort would lift. The last six need
evidence that is not in the data yet. The principled route for the near-ties is a
*deterministic* separator — a manager reference is a value drawn from the employee-id
column but not unique, which is measurable — not a model guess.

That is the correct outcome, and it comes with an affordance: **declaring an alias
or a constraint on the target field moves the column to auto.** The consultant is
not stuck confirming the same thing forever; they can teach the schema once.

## What is not calibrated yet

Signal weights were compared across three variants and the best adopted, but they
were not swept exhaustively. Record-matching thresholds (0.92 / 0.72) are still
asserted rather than measured — the matching corpus has 8 labelled pairs, and a
meaningful precision/recall curve needs closer to 40.
