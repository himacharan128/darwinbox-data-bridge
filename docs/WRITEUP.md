# Data Bridge — approach and autonomy boundary

**Hima Charan · [github.com/himacharan128/darwinbox-data-bridge](https://github.com/himacharan128/darwinbox-data-bridge)**

## Approach

The agent reads a client's exports, works out the mapping to the target schema,
cleans what it can defend, delivers to a stub destination, and asks a human only
where the evidence runs out. Across seven files in five naming conventions it maps
**66 of 101 columns unaided** and raises **35 cases for 40 employees**.

The design rule is **the model proposes, deterministic code disposes.** The model
never decides — it nominates candidates, code scores them against measured evidence,
and a policy gate makes the call.

## Where the line is, and why there

A mapping score comes from six signals. Five are measured from the data: header
similarity, what fraction of values parse as the target type, how many satisfy its
pattern or enum, whether a unique field is actually unique, the dominant value shape,
and — the strongest — whether values exist in the lookup the field references. The
sixth is the model's opinion, capped at a quarter of the scale:

```
score = 0.75 × deterministic evidence + 0.25 × model vote
```

A candidate with no measured support cannot exceed 0.25 and never reaches the 0.70
auto-apply threshold, however certain the model sounds. "A confidence score is not a
sufficient basis for the boundary" is enforced by arithmetic, not a comment. An
earlier version renormalised the vote alongside the other signals and its share
reached **87%** on a field declaring no constraints — the opposite of a cap.

**Thresholds are calibrated, not chosen.** 77 labelled decisions, 18 of them columns
that must *not* map: checksums, audit timestamps, bank details. A corpus of only
mappable columns measures nothing about restraint, and mapping a checksum is the
worst failure available — it corrupts the data silently with no case raised. The
sweep (`make sweep`) shows **zero wrong mappings and zero under-escalations at every
threshold**, so those are properties of the design, not a lucky cutoff. It also shows
the gap threshold is what prevents noise-mapping: one junk column maps at gap 0.05,
none at 0.10. A column fitting two fields equally well usually fits neither.

The cleanup line is **reversible and evidence-backed versus inventing data**.
Stripping punctuation to satisfy a declared pattern is safe; adding a `+91` country
code to satisfy that same pattern is not, and escalates. Dates resolve per *column*:
`03/04/2024` is undecidable alone, but if anything in its column has a day above 12,
that settles the whole column. One date column auto-normalises and another escalates
— a measurable difference, not a judgement call.

Escalations are eleven typed classes, each carrying its own evidence. An ambiguous
column shows both candidates' measurements; an ambiguous date shows both readings; a
low-confidence OCR read shows **the cropped image of the scan**, which a consultant
can act on in a way no confidence score allows.

The queue is sized for a person. A required field with no source is one question
about a *file*, not one per row — the first end-to-end run raised 98 cases and
readied nothing, exactly the failure the brief warns about. Answering one question
readies 13 records at once.

## What I'd build next

1. **PII.** Salary, date of birth, national identifiers. Needs field-level
   sensitivity, masked display, audit redaction, and a policy on what reaches a hosted
   model. The chokepoint already exists: profiling is the only place values reach it.
2. **Effective dating.** An HRMS models people temporally; this flattens to a snapshot.
3. **Destination upsert.** Cross-run key collisions are surfaced, not silently
   duplicated, but real update semantics need a stable external-ID contract.
4. **Calibrate matching.** Mapping thresholds are measured; matching ones are still
   asserted. Needs ~40 labelled pairs.
5. **Durable queue.** Replay-based recovery is correct but single-node.

## On scope

The brief suggests 4–6 hours. I built the production-shaped version deliberately: the
interesting question isn't whether an LLM can guess column names, it's whether you can
*defend* where the machine stops — and that needs a labelled corpus, a threshold
sweep, and adversarial negatives. The ~4-hour core is the vertical slice; everything
else makes the boundary measurable rather than merely demonstrable.
