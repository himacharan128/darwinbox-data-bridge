# darwinbox-data-bridge

An agent that migrates a client's messy HR exports into a target schema: it works out
the mapping itself, cleans what it can defend, pushes the result to a destination API,
and stops to ask a human only where the evidence genuinely runs out.

Built for the Darwinbox Forward Deployed Engineer take-home.

---

### → **Live: http://dbx-console-alb-1789124705.ap-south-1.elb.amazonaws.com**

**[One-page write-up](docs/WRITEUP.md)** · **[Why the line is there](docs/calibration.md)** ·
**[Demo script](docs/DEMO.md)** · **[Deploying](docs/DEPLOY.md)** ·
**[Test samples](samples/README.md)**

## Run it

Needs Python 3.12 (via [uv](https://docs.astral.sh/uv/)), Node 22 and nothing else —
no database to start, no cloud account.

```bash
uv sync && pnpm install
make run            # builds the UI, starts the destination and the API
```

Or in containers, the same way a deployment runs it: `make stack`.

Open **http://127.0.0.1:8080** and press **New migration**. It loads the bundled
client exports from `tests/fixtures/run1/`.

Other entry points:

```bash
make demo           # run one migration in the terminal
make eval           # measure the escalation boundary against the labelled corpus
make sweep          # show how the boundary moves as thresholds change
make test           # 39 tests, no credentials or network needed
```

The model runs from **recorded replays** committed under `tests/fixtures/model-cache`,
so everything above works offline. Set `OPENAI_API_KEY` and `AWS_REGION` (see
`.env.example`) to call the live model instead.

## What it does

1. **Reads seven files in seven shapes** — CSV, Excel, JSON, YAML, pasted text, a
   native PDF and a scanned one read through OCR — in five naming
   conventions, with overlapping employees and complementary fields — and reconciles
   them into one dataset without being told how.
2. **Maps and cleans on its own.** 66 of 101 source columns map with no human input,
   and it leaves checksums, audit timestamps and bank details alone. Dates are
   normalised, duplicates removed, whitespace trimmed, enums canonicalised.
3. **Escalates only what it cannot settle**, with the evidence and the question
   together, so a case is resolvable without opening the source file.
4. **Delivers to a real stub API** with per-record outcomes, retry, rollback, and an
   append-only audit trail.

## The autonomy boundary

The interesting question is not *can it map columns* but *where does it stop*.

A mapping score is composed in code from six signals. Five are measured from the
actual data — header similarity, type parse rate, constraint fit, uniqueness fit,
pattern-mask fit, and whether the values exist in the referenced lookup. The sixth is
the model's opinion, and it is **capped at 0.25 of the scale**:

```python
score = 0.75 * deterministic_evidence + 0.25 * model_vote
```

So a candidate with no deterministic support cannot exceed 0.25 and can never reach
the 0.75 auto-apply threshold, however certain the model sounds. That is what
"a model's self-reported confidence is not a sufficient basis for the boundary" means
in code rather than in a comment, and `test_model_vote_cannot_decide_alone` asserts it.

Thresholds are **calibrated, not chosen**. `make sweep` reports precision, recall and
error rate across 77 labelled decisions; across every threshold tried, zero mappings
are applied wrongly, zero noise columns are mapped, and zero cases needing a human are
silently resolved. [The full reasoning](docs/calibration.md).

Escalations are typed, eleven classes, each with its own evidence shape — an
ambiguous column shows both candidates' measurements, an ambiguous date shows both
readings it could be, an uncertain identity shows what agrees and what conflicts.

The line between safe and unsafe cleanup is **reversible and evidence-backed** versus
**inventing data**. Stripping punctuation so a value satisfies a declared pattern is
safe. Adding a `+91` country code so it satisfies that same pattern is not, and the
agent escalates instead.

## Architecture

```
apps/web              React console (desktop and mobile compositions): live activity, escalation queue, records, destination
services/api          Run lifecycle, review decisions, delivery, rollback, audit
services/mock-target  The destination HRMS — its own database, reached only over HTTP
packages/contracts    Schema language, evidence, escalation and audit vocabulary
packages/migration-core  Profiling, scoring, cleanup, validation, matching, pipeline
packages/extraction   CSV/XLSX readers with cell-level provenance
packages/agent        Bedrock provider (forced tool use), bounded model stages
tests/fixtures        The client's exports, the target schema, recorded model replays
tests/evaluations     The labelled corpus that makes the boundary measurable
```

**The engine is schema-driven.** Nothing in `migration-core` or `agent` knows what an
employee is — `test_no_hardcoded_fields` fails the build if a domain field name ever
appears in either. The schema under `tests/fixtures/schemas/` is one instance, not
the product's schema.

**Only two things are durable**: what a human decided, and what the destination
accepted. Everything else is derived by replaying the pipeline over the uploaded files
plus those decisions. Reopening a run recomputes the same queue, so resuming and
reprocessing-after-a-decision are the same mechanism rather than two code paths that
can disagree.

**Typed model output uses forced tool use, never `response_format`.** Verified against
gpt-oss-120b: `response_format: json_schema, strict: true` returned reasoning tags, a
markdown preamble and truncated JSON, while `toolChoice` returned pre-parsed,
schema-conformant input. It also separates chain-of-thought structurally, so dropping
it is not a regex over `<reasoning>` tags.

**Prompt injection** is handled in three layers: source values never enter the system
prompt, output is a typed tool call so prose has no channel to become an instruction,
and the policy gate discards any proposal naming a column or field outside the
manifest. One fixture cell carries an injection payload.

## Tech

Python 3.12 · FastAPI · Pydantic · uv — React 18 · TypeScript · Vite · pnpm —
PyMuPDF + docTR (Apache 2.0) for documents —
`openai.gpt-oss-120b` (Apache 2.0, open weights) on Amazon Bedrock via Converse —
SQLite locally, Postgres via `DATABASE_URL` — ruff, pytest.

## Deliberately out of scope

Named here because they are real gaps, not oversights:

- **PII handling.** This is HR data — salary, date of birth, national identifiers.
  Production needs field-level sensitivity in the schema, masked display with
  reveal-on-demand, redaction in the audit trail, and a policy on what may reach a
  hosted model. The design has one chokepoint for it: column profiling is the only
  place source values reach the model.
- **Effective-dated records.** An HRMS models employees temporally — transfers,
  promotions, compensation history. This flattens to a current-state snapshot.
- **Destination upsert.** Runs are independent processing scopes; a natural-key
  collision across runs is detected and surfaced rather than silently creating a
  duplicate, but true update semantics need a stable external-ID contract.
- **Durable queue and worker.** Processing is in-process; production wants the job
  graph, leases and dead-letter queues described in `MASTER_PLAN.md`.

## Notes

The brief suggests roughly 4–6 hours. This is deliberately the production-shaped
version — the ~4-hour core is the vertical slice in `packages/` plus `services/`,
and everything around it (the labelled corpus, the threshold sweep, the recorded
replays, the integration suite) exists to make the autonomy boundary *defensible*
rather than merely demonstrable.

`MASTER_PLAN.md` carries the full design record, including the decisions that were
reversed and why.
