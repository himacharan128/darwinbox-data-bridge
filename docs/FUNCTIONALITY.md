# What each piece does

Written to be read in order. Each section says what the thing does, why it exists,
and where to look.

---

## 1. Ingestion — `packages/extraction`

**Reads seven input shapes into one record structure**, keeping the address of every
cell: CSV (encoding and delimiter sniffed), Excel (all sheets), JSON and YAML (nested
paths preserved, so provenance reads `employees[3].contact.email`), pasted text, native
PDF, and scanned PDF through OCR.

**It checks what a file *is*, not what it is named.** A `.csv` that is really a PDF is
reported as a PDF, not fed to a CSV parser to explode somewhere deeper.

**Provenance is not decoration.** A review card shows "row 14 of `payroll_staff.xlsx`,
column `grade`" — without that, resolving a case means opening the source file, and the
brief's "resolvable in one glance" is lost.

Two PDF paths, because they are different problems. A page with a text layer goes
through PyMuPDF's table finder. A scan has only words at coordinates, so the column grid
is built from the *data* rows (which split cleanly on gap size) and every row's words
are snapped onto it — which is what a person does with a header like "Date of Joining".

---

## 2. Profiling — `migration-core/profiling.py`

**Summarises a column so the model never sees the column.** Null markers found, distinct
count, what fraction parses as each type, the dominant value *shape* (`EMP-#####`), the
top values, and a stratified sample of 15 — five common, five random, five outliers.

Two reasons it matters:

- **Bounded cost.** ~300 tokens per column *regardless of row count*. 40 columns over
  500,000 rows costs what 40 over 50 costs.
- **It is the only place source values reach the model** — which is exactly where
  field-level PII masking would attach.

---

## 3. The evidence scorer — `migration-core/scoring.py`

**This is the autonomy boundary.** Six signals per (column, field) pair:

| Signal | What it measures |
|---|---|
| `name_sim` | header vs field name and declared aliases |
| `type_fit` | share of values parsing as the target type |
| `constraint_fit` | share satisfying its pattern or enum |
| `unique_fit` | distinct ratio against a `unique` constraint |
| `mask_fit` | dominant value shape vs the expected one |
| `reference_fit` | **share of values that exist in the referenced lookup** |
| `llm_vote` | the model's opinion — capped |

`reference_fit` is the strongest and the least obvious: a column of department codes is
not merely *shaped* like `department_code`, its values are actually in the department
table, and nothing else in the schema can say that.

The composition is what makes the cap real:

```python
score = 0.75 × deterministic_evidence + 0.25 × model_vote
```

A candidate with no measured support cannot exceed 0.25 and never reaches the 0.70
threshold. Auto-apply also requires a **gap** from the runner-up — a column that fits
two fields equally well usually fits neither, and that gap is specifically what stops
a checksum being mapped.

**Signals that cannot discriminate are excluded, not scored neutral.** Scoring
`type_fit = 1.0` for a plain-string target against every column gave irrelevant fields
a free baseline that drowned the real evidence.

---

## 4. Safe cleanup — `migration-core/cleanup.py`

**An exhaustive whitelist**; anything not on it escalates. Every rule is driven by a
constraint the schema declares, so none of them knows what a field means.

The line is **reversible and evidence-backed versus inventing data**:

| Safe | Not safe |
|---|---|
| trim and collapse whitespace | truncating to fit `max_length` |
| Unicode NFC | changing a case-sensitive identifier |
| `"N/A"` → null | guessing an ambiguous date |
| lowercase an email *domain* (not the local part) | adding a `+91` to a bare number |
| strip punctuation to satisfy a declared pattern | inventing a required value |
| canonical enum matching | fuzzy-merging people by name |

**Dates are decided per column, not per value.** `03/04/2024` is undecidable alone. If
anything in its column has a day above 12, that settles the reading for every value in
it. That is why one date column normalises silently and another escalates — a
measurable difference, not a judgement call.

---

## 5. Validation and matching — `validation.py`, `matching.py`

Validation checks required, type, pattern, enum, length, range, uniqueness across the
dataset, and references including manager self-reference.

**Matching is fully deterministic — the model has no say.** That is what makes "no known
incorrect automatic merges" a property of code rather than a behaviour you hope holds.
Candidate pairs come from **blocking keys declared in the schema**, because which fields
identify a person is domain knowledge the consultant has, and guessing it in code is
precisely how a wrong merge happens. Comparing every pair is O(n²) and does not finish
at the stated 50,000-record ceiling.

**Identity and field conflicts are separate decisions.** Matching employee IDs establish
the same person; they say nothing about which job title is right.

---

## 6. The model stages — `packages/agent`

**Typed proposals via forced tool use, never `response_format`.** Verified: with
`response_format: json_schema, strict: true` gpt-oss returned reasoning tags, a markdown
preamble and truncated JSON; `toolChoice` returns pre-parsed, schema-conformant input.
It also separates chain-of-thought *structurally*, so dropping it is not a regex.

Three stages: judge one column against the schema, propose a whole schema (Mode B), and
phrase an escalation a human will read. **The model never decides** — it votes.

**Prompt injection is handled in three layers**: source values never enter the system
prompt, output is a typed tool call so prose has no channel to become an instruction,
and the policy gate discards any proposal naming a column or field outside the manifest.

**Every call is cached by content hash.** That makes the audit trail reproducible —
MoE inference means `temperature=0` is not a determinism guarantee — and it is the
offline path, so a rate limit never takes the demo down.

---

## 7. The pipeline — `migration-core/pipeline.py`

Extract → map → build → reconcile → validate → escalate.

**The queue is sized for a person.** A required field with no source is one question
about a *file*, not one per row. The first end-to-end run raised 98 cases and readied
nothing; asking once collapsed it to 18. A file supplying almost nothing is one case
saying so, not one per missing field.

**Human decisions become content-addressed overrides and the run is replayed**, not
patched. Reprocess-and-revalidate falls out of the design rather than being a second
code path that can leave the dataset half-corrected.

---

## 8. Escalations — eleven typed classes

Each carries its own evidence shape, because "needs review" without evidence is not
resolvable:

`AMBIGUOUS_MAPPING` · `UNMAPPED_REQUIRED` · `AMBIGUOUS_VALUE` · `UNCERTAIN_IDENTITY` ·
`CONFLICTING_FACTS` · `MISSING_REQUIRED` · `VALIDATION_UNRESOLVED` ·
`UNRESOLVED_REFERENCE` · `LOW_CONFIDENCE_EXTRACTION` · `CROSS_RUN_COLLISION` ·
`DELIVERY_PERMANENT_FAILURE`

`DELIVERY_PERMANENT_FAILURE` is shown apart from the rest, under **Failures**: the
receiver refusing is not the agent asking, and it calls for a different action.

A record blocked only because its manager or department points at a *blocked* record
has nothing of its own to decide — the neighbour is the decision. It becomes a
**child** of that case rather than a queue entry nobody can act on, counts toward
"needs review" from the first screen, and is revalidated when the parent is answered.

`LOW_CONFIDENCE_EXTRACTION` is deliberately **two-sided**: low OCR confidence alone is
not enough (OCR is routinely unsure about text it got right) and a bad value alone is
not enough (the file may contain one). Both together say the problem is in the reading —
and the card shows **the cropped image**, which a consultant can act on in a way no
confidence score allows.

---

## 9. Schema — both modes, versioned

**Mode A**: the client's schema as YAML, JSON or an object, normalised into one internal
representation while the original text is kept — someone who uploaded YAML should see
what they uploaded.

**Mode B**: the agent proposes a schema from the data. From 101 columns it produced 22
fields and collapsed `emp_id` / `staff_code` / `code` / `emp_ref` / `strEmployeeCode`
into one `employee_id`. **A proposal is never self-approving.**

**Versions are immutable.** Approving supersedes; already-delivered records keep the
version they were sent under, because a payload was shaped by a particular schema and
editing it afterwards would make the history describe something never sent.

---

## 10. Delivery — `services/api/delivery.py` + `services/mock-target`

The destination is a **real service reached over HTTP**, with its own database and no
write path from the migration. A destination you can reach around is not one you have
integrated with.

Delivery **follows readiness**: a record that passes validation with nothing open
against it goes on its own, as part of processing. A click before the fact only
delayed work already judged safe. Rollback is the undo, and it **pauses** automatic
sending — otherwise the next pass would resend immediately and the undo would mean
nothing.

**Three failure classes, three responses:**

- **transient** → retry with backoff
- **permanent** → never retry; the same bytes earn the same refusal
- **uncertain** → the write may have landed. **Reconcile by identity first** — retrying
  blind turns a possible success into a certain duplicate

Delivery identity is `(run_id, natural_key, generation)`. Schema version is deliberately
**excluded**: with it in the key, a failed delivery followed by a schema edit produces a
different key, and an uncertain-but-successful first attempt double-delivers.

**Rollback tombstones, never deletes**, and reports partial as partial.

---

## 11. The console — `apps/web`

Live activity beside the escalation queue, one case at a time with Previous/Next and
"Case 2 of 14". **Navigating never submits anything** — a consultant can read
everything, answer the easy ones and leave a hard one for a supervisor.

**Backend state is authoritative.** Nothing reports optimistic success: a record is
delivered when the destination says it holds it.

**Approve is only offered where a proposal exists** — it must never be a way past a
failed check. Mobile is a different composition, not a shrunken desktop.

---

## 12. What makes it measurable — `tests/evaluations`

**77 labelled decisions**, of which **18 are columns that must not map**. A corpus of
only mappable columns measures nothing about restraint, and mapping a checksum is the
worst failure available: it corrupts the data silently with no case raised.

`make sweep` shows zero wrong mappings and zero under-escalations **at every threshold
tried** — properties of the design, not of a chosen cutoff.

Plus two guards: a test that **fails the build if any domain field name appears in the
engine packages**, and tests that keep the fixtures honest, so a corpus claiming a
column is ambiguous over a column that isn't cannot silently pass.
