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
   0.70   0.05       45      0      1        12          0    0.83
   0.70   0.10       44      0      0        14          0    0.82   <- chosen
   0.70   0.15       43      0      0        15          0    0.81
   0.75   0.10       38      0      0        20          0    0.74
   0.80   0.10       35      0      0        23          0    0.70
   0.85   0.10       32      0      0        26          0    0.66
   0.90   0.10       24      0      0        34          0    0.56
   0.95   0.10       19      0      0        39          0    0.49
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

## Chosen: `T_auto = 0.70`, `T_gap = 0.10`

44 of 57 mappable columns applied unaided, zero wrong, zero noise mapped, zero
genuine ambiguity silently resolved.

## The 14 over-escalations

Not a tuning failure. Almost all are abbreviated headers (`mgr`, `loc`, `dept`,
`cell`, `office`) pointing at target fields that declare nothing measurable — no
type beyond string, no pattern, no enum, no uniqueness. The only available evidence
is header similarity plus the model's opinion, and the model is capped precisely so
it cannot decide alone.

That is the correct outcome, and it comes with an affordance: **declaring an alias
or a constraint on the target field moves the column to auto.** The consultant is
not stuck confirming the same thing forever; they can teach the schema once.

## What is not calibrated yet

Signal weights were compared across three variants and the best adopted, but they
were not swept exhaustively. Record-matching thresholds (0.92 / 0.72) are still
asserted rather than measured — the matching corpus has 8 labelled pairs, and a
meaningful precision/recall curve needs closer to 40.
