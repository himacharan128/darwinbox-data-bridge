# Evaluation corpus

This is what makes the escalation boundary a measurement rather than an assertion.
Assignment criterion 3 asks you to explain *why the line was drawn where it was*.
The answer is the threshold sweep produced from this corpus, not a chosen number.

## Files

| File | What it labels |
|---|---|
| `corpus/mapping_decisions.yaml` | source column → target field; `auto` vs `escalate`, plus which evidence signal should carry it |
| `corpus/matching_decisions.yaml` | record identity; `auto_merge` / `escalate` / `no_match` |
| `corpus/safe_cleanup.yaml` | transformations that MUST apply with no review case |
| `corpus/escalation_cases.yaml` | one labelled instance per escalation class (MASTER_PLAN §2.4) |
| `corpus/forbidden_actions.yaml` | hard assertions — any occurrence fails the build |

## Metrics (Phase 2)

- **Escalation precision** — of cases escalated, the fraction that genuinely needed a human.
- **Escalation recall** — of cases that needed a human, the fraction escalated.
- **Auto-apply error rate** — incorrect mappings or merges applied without review.
- **Threshold sweep** — all of the above across candidate `T_auto` / `T_gap` values.

Escalating a `safe_cleanup` or `auto` case costs precision. Silently resolving an
`escalate` case costs recall. Both directions are failures; the corpus measures the
trade-off instead of asserting a number.

## Current coverage

Phase 0 skeleton: 36 mapping decisions, 8 matching decisions, 8 safe-cleanup cases,
12 escalation cases (11 of 12 with Phase-1 fixtures; `LOW_CONFIDENCE_EXTRACTION`
needs the scanned PDF authored in Phase 4), 9 forbidden actions.

Phase 2 expands mapping to ~80 and matching to ~40, adding adversarial negatives.

## Alias independence

Only 8 of 35 source columns are resolvable through an alias declared in the target
schema. The remaining 27 must map on measured evidence — name similarity, type parse
rate, constraint fit, pattern-mask signature and the capped model vote. This is
deliberate: it proves the engine is not an alias lookup table.
