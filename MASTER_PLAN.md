# Master build plan — Darwinbox FDE assignment

Migration agent: multi-file HR data → validated target schema → mock destination API,
with an evidence-based autonomy boundary and human-in-the-loop supervision.

This plan sits on top of decision log D001–D006 / TD001–TD016. Section 1 lists
**amendments** that change the log. Section 2 specifies the parts that were
underspecified. Section 3 is the phase plan. Nothing here is implemented yet.

---

## 0. Confirmed decisions

| # | Decision | Effect |
|---|---|---|
| 1 | Postgres + JSONB, not MongoDB | TD002 data model rewritten |
| 2 | Run-isolated processing + cross-run collision **detection**, no upsert | New escalation class |
| 3 | PII and temporal/effective-dated data = documented future scope | README + write-up |
| 4 | Open-source OCR only | Textract removed entirely |
| H | No agent framework | Structured-output calls + one bounded investigator |
| 5 | AWS: CareerKart account, **hard-isolated** (Option A) | Dedicated IAM principal, `dbx-migration-*` prefix, no link to any CareerKart service |
| 6 | Model access via **Bedrock long-term API key**; typed proposals over **Converse + forced tool use** (A19) | No IAM key pairs; reasoning cleanly separated |
| 7 | Region **ap-south-1** (Mumbai) | Lowest latency; us-east-1 / us-west-2 as fallbacks |
| 8 | Repo `darwinbox-data-bridge`, **public** | Secrets live outside the repo tree; gitleaks in commit #1 |

---

## 1. Amendments to the decision log

Fold these back into the log with revision notes; do not silently overwrite.

### A1 — Datastore (TD001, TD002)
MongoDB Atlas → **PostgreSQL**. Relational tables for `runs`, `files`,
`schema_versions`, `mappings`, `canonical_records`, `review_cases`,
`human_decisions`, `processing_jobs`, `delivery_attempts`, `audit_events`.
`JSONB` for extracted record bodies, column profiles, field-level provenance and
stored payloads.

Rationale: the integrity guarantees already promised — "resolve case + revalidate
record + append audit" as one atomic effect — need real transactions. A partial
write there corrupts the append-only audit trail that is itself a headline feature.
Provenance lineage queries are joins, and SQL does them.

Mock destination keeps a **separate database and separate credentials** (TD007
intent preserved). The main application has no write path to it.

### A2 — OCR (TD001, TD003)
Amazon Textract **removed**. Extraction is: PyMuPDF for native PDF text →
**docTR** (Apache 2.0) for pages needing OCR.

Rationale: the brief constrains you to open-source AI models; Textract is a
proprietary AI service. Removing it entirely — rather than keeping it "optional" —
avoids an untested second code path and gives a clean claim with no asterisk.

docTR returns **per-word confidence and bounding boxes**. Both are load-bearing:
confidence becomes escalation evidence, bounding boxes let the review card render
the cropped scan region beside the extracted value.

TD006's "continue asynchronous Textract work through durable callback/queue events"
is dropped. OCR is now synchronous inside the worker, bounded by page concurrency.

### A3 — Agent execution (TD004)
Strands Agents SDK **removed**. Each stage is a single constrained call returning a
typed Pydantic object. Orchestration lives in Postgres and the queue.

Rationale: TD004 already specifies bounded stages, restricted tools, typed
proposals and external orchestration. Strands' value is the autonomous planning
loop that design deliberately suppresses — roughly 20% usage for a full dependency
plus a duplicate provider abstraction.

**One exception.** A bounded **investigator** loop fires only on ambiguous mapping.
It may take up to 5 tool-calling steps (profile another column, test a candidate
transform, check a lookup, compare a sibling file) to resolve the ambiguity before
escalating. This is the one place a tool loop earns its keep, because the
resolution path is genuinely unknown in advance. Implemented directly against the
OpenAI-compatible tool-calling API.

Everything else in TD004 stands: restricted tool surface, no generic
DB/filesystem/shell/network tools, no direct delivery path, no cross-run memory,
cycle/token/timeout limits, one structured-output repair, no stored chain-of-thought.

### A4 — Evidence scoring (TD005) — **the central correction**
TD005's `0.90 / 0.15 / 0.92 / 0.72` are currently undefined numbers, and if they
came from the model they would contradict D003's own rule that model self-report is
not a sufficient basis for the boundary.

Replace with a **deterministically composed score** (spec in §2.2) in which the
model's semantic vote is capped at **0.25 of the total weight**. That cap is the
mechanism that makes D003's principle literally true in code: no model opinion can
by itself push a mapping over the auto-apply line.

Thresholds are **calibrated against the Phase 2 evaluation corpus**, not asserted.
The write-up quotes the sweep, not the number.

### A5 — Idempotency key (TD007)
`run_id + record_id + schema_version + delivery_generation + operation`
→ **`run_id + record_id + delivery_generation`**.

Rationale: with `schema_version` in the key, a failed delivery followed by a schema
edit produces a *different* key on redelivery. A first attempt that returned
"uncertain" but actually succeeded then double-delivers. Schema version belongs in
the payload and the audit event, not the dedupe key.

`delivery_generation` still increments on rollback, which is the intended way to
permit a legitimate redelivery.

### A6 — Cross-run collision detection (TD007, D004)
Mock destination maintains a natural-key index **across runs**, used only for
detection. New endpoint: lookup by natural key returning prior run ID, delivery
time and stored values.

Before delivering a record whose natural key exists in another run, the agent
raises a `CROSS_RUN_COLLISION` review case showing the prior run, its date and a
field-level diff. The human chooses deliver-as-separate or exclude.

Run-scoped processing, retry, rollback and resume are unchanged. This removes the
silent-duplicate defect without introducing update/restore semantics.

### A7 — Match candidate generation (TD005, TD016)
TD005 defines matching semantics but not how pairs are produced. At TD016's stated
limit of 50,000 records per run, naive pairwise comparison is 1.25e9 operations.

Add **blocking** (spec in §2.3). Scoring runs only within candidate blocks.

### A8 — Session cookie topology (TD009, TD012)
`SameSite=Lax` on a cookie issued by an ALB-hosted API will not be sent on XHR from
a Vercel-hosted frontend on a different registrable domain.

Fix: **proxy API calls through Next.js route handlers.** Cookie stays first-party,
CORS disappears, API origin is hidden. Everything else in TD009 stands.

Login rate-limit state moves to Postgres — in-memory does not survive multiple ECS
tasks.

### A9 — Evaluation metrics (TD011)
TD011 has per-case pass/fail and "forbidden automatic actions". Add:

- **Escalation precision** — of cases escalated, the fraction that genuinely needed a human.
- **Escalation recall** — of cases that needed a human, the fraction escalated.
- **Auto-apply error rate** — incorrect mappings/merges applied without review.
- **Threshold sweep** — the above, plotted across candidate thresholds.

Corpus targets: **~80 labelled mapping decisions, ~40 matching decisions,
~25 cleanup decisions**, each labelled `auto-safe` / `must-escalate` / `forbidden`.

"Forbidden automatic actions" are promoted from scored cases to **hard assertions**:
any occurrence fails the build.

### A10 — Network cost (TD012)
Private ECS tasks need NAT or VPC endpoints for ECR pulls and any non-AWS provider.
Bedrock supports PrivateLink. A NAT gateway is a standing ~$32/month charge.
Use VPC endpoints (ECR, S3, Secrets Manager, Bedrock, CloudWatch Logs), or public
subnets with tight security groups.

### A11 — Run workspace layout (TD008)
Thirteen equal-weight routes dilutes the thing criterion 4 measures. The run
workspace **defaults to a single view: live agent activity beside the escalation
queue.** Files, schema, mappings, records, delivery, audit and destination become
secondary tabs. Live activity is rendered from `audit_events` filtered to agent
actions with plain-language summaries — the same append-only table, reused.

### A12 — Proposal cache and demo replay (TD001, TD010)
Cache model proposals keyed by `hash(model_id + prompt_template_version +
serialized_column_profile)`. A cache hit replays the exact prior proposal.

This delivers audit reproducibility *and* doubles as `DEMO_MODE=cached`: the
system runs end-to-end with zero network calls.

Honesty constraint from TD010 preserved and sharpened — a **replay of a recorded
real run** is distinct from a **hand-written fixture**. Both are visibly labelled in
the UI; only the former is used for demonstration.

Also set `temperature=0` and pin the model version, but state plainly in the
write-up that this does not guarantee bitwise determinism (gpt-oss-120b is MoE and
provider-side batching is non-deterministic). Reproducibility comes from the cache
and the deterministic gate, not from the model.

### A13 — Documented future scope (D003, TD002)
**PII**: field-level sensitivity classification, masked display with reveal-on-demand,
redaction in audit and logs, and a policy on what may leave the perimeter to a model
endpoint. Noted at the column-profiling chokepoint — the single place source values
reach the model — so the hook is visible in the design.

**Temporal HR data**: effective-dated records (transfers, promotions, compensation
history), which an HRMS models natively and this system flattens to current-state.

Both land in README "Out of scope" and in the write-up's *"what I'd build next"*,
which criterion 6 explicitly asks for.

### A14 — Deploy automation (TD015)
"Documented checklist" → a `make deploy` target. Three containers, Vercel, Terraform
and an index migration will be deployed many times; a 20-line script removes the
class of errors that eats an evening.

### A15 — Model access (TD001)
Access `gpt-oss-120b` through a **Bedrock long-term API key** on the
**OpenAI-compatible endpoint**, not an IAM access key pair with SigV4:

```
OPENAI_BASE_URL = https://bedrock-runtime.ap-south-1.amazonaws.com/openai/v1
OPENAI_API_KEY  = <bedrock long-term api key>
MODEL_ID        = openai.gpt-oss-120b-1:0
```

Three consequences:

- **TD001's provider abstraction becomes nearly free.** Bedrock, Groq, Cerebras,
  Together and local Ollama are the same client code with two env vars swapped.
  If Bedrock access stalls, change `OPENAI_BASE_URL` and keep moving.
- **Structured outputs via `response_format` are NOT reliably enforced** on the
  OpenAI-compatible shim — see **A19**, which supersedes this point. Typed proposals
  use the Converse API with forced tool use instead.
- Region **ap-south-1**. Model is also in us-east-1, us-east-2, us-west-2 and
  several EU/APAC regions. Context window 128K, max output 16K.

Bedrock API keys are IAM **service-specific credentials**, so they are created and
rotated via `aws iam create-service-specific-credential` — scriptable and auditable,
no console clicking required.

### A19 — Typed proposals use Converse + forced tool use (TD004) — **verified**

Empirically established against `openai.gpt-oss-120b-1:0` in ap-south-1:

| Mechanism | Result |
|---|---|
| OpenAI-compat `response_format: json_schema, strict: true` | **Not enforced.** `finish_reason: stop`, content returned as `<reasoning>…</reasoning>` + a markdown preamble + truncated JSON |
| Converse API + `toolChoice: {tool: {name}}` | **Reliable.** `stopReason: tool_use`; `reasoningContent` and `toolUse` as separate blocks; `toolUse.input` pre-parsed and schema-conformant |

**Decision:** every typed-proposal stage uses **forced tool use**, never `response_format`.

Three problems solved at once:

1. **Typed output is structurally guaranteed** — the proposal arrives as parsed JSON
   matching the Pydantic schema. No string extraction, no brace balancing.
2. **TD004's "do not store or present hidden chain-of-thought" becomes trivial** —
   reasoning is its own content block, so it is dropped rather than regex-stripped.
   On the OpenAI-compat endpoint reasoning is *inlined* in `content` as
   `<reasoning>` tags, which would have made that requirement fragile.
3. **Provider portability is preserved.** Forced tool use is the same pattern on
   OpenAI-compatible providers (`tool_choice: {type: "function", function: {name}}`),
   and Groq/Cerebras return reasoning in a separate `message.reasoning` field.

The provider adapter interface is therefore `propose(schema, messages) -> ParsedProposal`,
with each adapter responsible for (a) forcing the tool call and (b) separating reasoning
from answer. Two adapter shapes, one call site.

**`reasoning_effort` is a supported per-request knob** (`low` / `high` both verified).
Make it per-stage configuration: `low` for mechanical extraction, higher for ambiguous
mapping. Reasoning tokens are a real cost — a trivial mapping proposal spent 162 output
tokens, most of them reasoning.

### A20 — Verified environment

| | |
|---|---|
| AWS account | `ACCOUNT_ID` (CareerKart), profile `careerkart` |
| Project principal | `arn:aws:iam::ACCOUNT_ID:user/dbx-migration-agent` |
| IAM policy | `dbx-bedrock-invoke` — `bedrock:CallWithBearerToken` on `*`, `InvokeModel*` on the two gpt-oss model ARNs only |
| Credential | Bedrock long-term API key (`ABSK…`, 132 chars), expires **2026-12-18** |
| Region / model | `ap-south-1` / `openai.gpt-oss-120b-1:0` (fallback `-20b-1:0`) |
| Latency | ~225 ms simple call; ~800 ms structured proposal |
| Secrets | `~/.darwinbox-agent/.env`, mode 600, outside the repo tree |
| Project context | `.envrc` (direnv) exporting the careerkart AWS profile + `dotenv_if_exists ~/.darwinbox-agent/.env` |

`bedrock:CallWithBearerToken` **must be scoped to `Resource: "*"`** — scoping it to a
model ARN yields a 401. IAM propagation took ~15 s.

**`.envrc` must be gitignored in commit #1** — it contains no secrets, but it carries
absolute paths into the CareerKart credentials directory. A `.envrc.example` is committed
in its place.

### A16 — AWS account isolation (TD001, TD012)
**Option A: the CareerKart account, hard-isolated.** TD001's "never connect this
application to CareerKart production resources" is honoured as *no link to any
running CareerKart service* — not as a prohibition on the account.

Enforced by:

- A dedicated IAM user `dbx-migration-agent`, with an inline policy granting only
  `bedrock:InvokeModel*` on the gpt-oss model ARN for Phases 1–6.
- Every resource created by this project prefixed **`dbx-migration-*`**.
- A **separate Terraform state file/backend**, so `terraform destroy` at teardown
  can never reach an existing CareerKart resource.
- No VPC peering, no shared security groups, no reads from existing buckets,
  queues or databases.
- Phase 3/7 IAM additions (S3, SQS, ECS, RDS) scoped by ARN to the
  `dbx-migration-*` prefix only.

### A17 — Secret handling for a public repository
`github.com/himacharan128/darwinbox-data-bridge` is public; a leaked key is scraped
within seconds.

- Secrets live at **`~/.darwinbox-agent/.env`**, outside the repo tree entirely, so
  `git add -A` cannot physically reach them.
- The repo carries `.env.example` with placeholder **names** only.
- `.gitignore` covering `.env*` is in **commit #1**, before any key exists.
- **gitleaks** pre-commit hook.
- The Bedrock key is inference-scoped, so a leak is rotatable rather than an incident.

### A18 — No browser automation for console operations
Credential and console operations are performed via CLI/API or by the user directly.
Blind AppleScript automation of an authenticated AWS console holding production
resources is not an acceptable mechanism: state cannot be verified before acting.

---

## 2. Specifications the log left open

### 2.0 Two schemas — do not confuse them

**The runtime target schema is fully dynamic.** Whatever the user supplies (Mode A)
or the agent recommends and the user approves (Mode B). The engine contains **zero
hardcoded field names**. Every signal in §2.2 — `type_fit`, `constraint_fit`,
`mask_fit`, `unique_fit` — is computed from whatever constraints the *supplied*
schema declares. This is exactly why the scorer is built from schema-derived
signals rather than a lookup table of known HR fields: it generalises to any schema
a client brings.

Field **aliases** are likewise user input: a schema may optionally declare
`aliases: [...]`. Where absent, generic string similarity plus the model's semantic
vote covers it. No HR vocabulary is baked into the code.

**The fixture schema (§D1) is one fixed file** in `tests/fixtures/`, used only for
the demo, the acceptance checks and the labelled eval corpus. A threshold cannot be
calibrated against "any possible schema" — calibration needs a corpus, and a corpus
needs one fixed schema to label against.

**Enforced, not asserted:** a lint test fails the build if any domain field name
(`employee_id`, `work_email`, ...) appears anywhere in `packages/migration-core` or
`packages/agent`. If the engine is genuinely schema-driven the test passes; if
anyone special-cases a field, the build breaks. This is the answer to a panelist
asking "is this hardcoded for your demo data?"

### 2.1 Column profile

Built by streaming the full column. Sent to the model **in place of** the column.
Never send raw column data.

- Raw header + normalized tokens
- Non-null / null counts; which null-markers occurred (`""`, `-`, `N/A`, `NULL`, `na`, `--`)
- Distinct count, cardinality ratio
- Type-candidate parse rates: int, float, bool, email, phone, date **with the specific format candidates that parsed and at what rate**
- Length min/max/mean; min/max for numerics and dates
- Top-10 values with counts
- **Pattern-mask signature**: values collapsed to masks (`EMP-####`, `####`, `@@@-##`), top 3 with coverage %
- **Stratified 15-value sample**: 5 most frequent, 5 random, 5 deliberate outliers (longest, shortest, rarest, pattern-violating)

Two properties that matter:

- **Bounded regardless of file size.** ~300 tokens per column, so 40 columns × 500k rows costs the same as 40 × 50. This is what makes TD016's 50k-record limit safe for the model path.
- **The mask signature is the highest-information feature per token.** `EMP-####` at 98% coverage says more than fifty sample values.

The profile also feeds the evidence scorer directly — parse rates and constraint-fit
rates come straight out of it. Built once, serves both sides of the boundary.

### 2.2 Mapping evidence score

For each (source column, target field) pair, signals in `[0,1]`:

| Signal | Definition |
|---|---|
| `name_sim` | Max over target field name + declared aliases of a token-set / Jaro-Winkler blend |
| `type_fit` | Fraction of non-null values parsing as the target type |
| `constraint_fit` | Fraction satisfying the field's regex / enum / range / length |
| `unique_fit` | Distinct ratio vs a `unique: true` constraint; neutral when not required |
| `mask_fit` | Dominant pattern mask vs the field's expected mask, where declared |
| `llm_vote` | Model's semantic judgment — **capped at 0.25 of total weight** |

`score = Σ(weight × signal)`, with **hard vetoes** that bypass the sum entirely:

- `type_fit < 0.5` → capped below the review floor
- target is `unique` and distinct ratio `< 0.9` → veto
- proposal names a column or field absent from the manifest → rejected at the gate

Auto-apply requires `score ≥ T_auto` **and** `score − runner_up ≥ T_gap` **and** no
veto. `T_auto` and `T_gap` come from the Phase 2 sweep.

The escalation card renders **the signal breakdown**, never the number:

> **`contact` is ambiguous.** 42% of values parse as email, 51% as phone.
> Header name matches neither `work_email` nor `mobile_number` strongly.
> Investigated: no sibling column disambiguates. No clear winner — your call.

### 2.3 Record matching

**Blocking** — candidate pairs come only from these blocks:

1. Normalized `employee_id` exact
2. Normalized email exact
3. Normalized `last_name` + date of birth
4. Last 8 digits of normalized phone

Candidates = union of blocks. Pairwise scoring runs **only inside candidates**.

**Pair signals**: identifier match, email match, name similarity, DOB match, join-date
match, department match, count of directly conflicting fields.

**Hard rules** (override score, per TD005):
- Name-only similarity never permits an automatic merge.
- Any hard conflict on an identifying field forces review.
- Exact-duplicate rows are removed automatically; contributing source references preserved.

### 2.4 Escalation taxonomy

Eleven classes, each with its own evidence shape. This taxonomy is the core
intellectual artifact — it is what "defensible boundary" means concretely.

| Class | Triggered when | Evidence shown |
|---|---|---|
| `AMBIGUOUS_MAPPING` | Two+ targets within `T_gap` | Both signal breakdowns; investigator's attempts |
| `UNMAPPED_REQUIRED` | Required target has no candidate above floor | Best candidates and why each failed |
| `AMBIGUOUS_VALUE` | e.g. `03/04/2024` with no column-level format evidence | Format candidates + column format inference |
| `UNCERTAIN_IDENTITY` | Match score in review band | Matched and conflicting fields side by side |
| `CONFLICTING_FACTS` | Identity established, values conflict, no authority rule | Both values with sources |
| `MISSING_REQUIRED` | Required value absent, not derivable | Record + the rule it violates |
| `VALIDATION_UNRESOLVED` | Failed validation twice | Full attempt history |
| `UNRESOLVED_REFERENCE` | Lookup key unknown or multiply matched | Candidate lookup rows |
| `LOW_CONFIDENCE_EXTRACTION` | OCR confidence low **and** value fails field pattern | **Cropped scan region** + per-word confidence |
| `CROSS_RUN_COLLISION` | Natural key delivered in another run | Prior run, date, field-level diff |
| `DELIVERY_PERMANENT_FAILURE` | Destination rejected permanently | Payload + API error |

Every case carries: affected record/field, source references, the rule involved,
attempted corrections, plausible alternatives, and the decision needed in plain
language.

### 2.5 Forbidden automatic actions

Hard assertions. Any occurrence fails the build (A9).

- Invent a required value
- Truncate data to satisfy a length constraint
- Alter an identifier (case, padding, punctuation, leading zeros)
- Guess an ambiguous date
- Merge two people on name similarity alone
- Auto-resolve a conflict where both sources have equal authority
- Deliver a record carrying any unresolved case
- Auto-retry a failure classified permanent

### 2.6 Safe cleanup whitelist

Per TD005, exhaustive — anything not listed escalates:

boundary and repeated whitespace · Unicode NFC normalization · unambiguous dates
(format established at column level) · canonical enum matching against allowed
values · exact duplicate removal · known null-marker → null · email domain-part
lowercasing (local part preserved) · safe phone punctuation

---

## 3. Phase plan

Every phase ends with a system that is **demoable and submittable as-is**. The
submission is never blocked on an unfinished phase.

**Governing heuristic:** write the one-page write-up at the end of Phase 1 and
revise it after every phase. *If a phase doesn't change the write-up, question
whether it earned its place.* The one-page cap is the only hard constraint the
brief gives that unlimited time does not relax — use it as the scope governor.

### Phase 0 — Fixtures, target schema, eval corpus skeleton `[S]` — ✅ COMPLETE
Highest leverage work in the build. The demo is only as good as this.

- `git init`; **`.gitignore` + gitleaks hook in commit #1**, before any key exists.
- Monorepo scaffold per TD010, root `darwinbox-data-bridge`; remote set to the public GitHub repo.
- Toolchain pinned: uv + Python 3.12, `.nvmrc` at Node 22 LTS, Postgres 16 in Compose.
- `target_schema.yaml`, deliberately **adversarial** to the sources: different names, stricter types, enums the sources violate, a case-sensitive identifier, a `unique` constraint.
- 3 employee files with different headers/formats, overlapping IDs, complementary fields, exact duplicates.
- 1 department lookup + manager references (including a self-reference and a cycle).
- **Exactly one clean instance of each escalation class** from §2.4, plus plenty of unambiguous cases so the agent visibly does *not* over-escalate.
- One cell containing a prompt-injection attempt.
- Labelled corpus skeleton per A9 targets.

**Gate:** every §2.4 class has a labelled instance; schema is adversarial, not derived.
**Result: PASSED.** 11/11 escalation classes labelled (ESC-012 `LOW_CONFIDENCE_EXTRACTION`
carries its label; its scanned-PDF fixture is authored in Phase 4). 28 employees across
three file shapes, 9 of 13 target fields with no alias, 25 integrity tests green.
Commits: `931cdc7` secret guards → `329fb82` scaffold → `07ce9a3` fixtures → `142f8f9` corpus.

### Phase 1 — Vertical slice `[L]`
Single process. Postgres. No queue, no OCR, no schema versioning, no mobile.
CSV + XLSX only. Ingest → profile → map → clean → validate → escalate → resolve in
UI → deliver to mock API → audit.

**Gate:** acceptance checks for criteria 1–5 pass on Phase 0 fixtures.
**Record the demo here.** Draft the write-up here.

### Phase 2 — Evidence scorer + evaluation harness `[M]` — *the differentiator*
Full §2.2 scorer. Corpus labelled to A9 targets. Automated precision/recall/error-rate.
Threshold sweep. `T_auto`, `T_gap` and the matching bands set **from the data**.

**Gate:** the sweep exists; every threshold in the log traces to a number in it;
zero forbidden actions; zero incorrect automatic merges.

Deliberately ahead of Phase 3 — this is what distinguishes the submission, while
durable job processing is table stakes. If momentum dies, this must already be done.

### Phase 3 — Durability and recovery `[L]`
SQS + DLQ, dependency job graph, leases, outbox/dispatcher, reconciliation,
retry classification, rollback with generations, resume.

**Gate:** kill the worker mid-run → resume with no duplicates, no lost decisions,
no re-delivery of an accepted record.

### Phase 4 — Format breadth `[M]`
XLSX multi-sheet, JSON/YAML, pasted text, native PDF, OCR + `LOW_CONFIDENCE_EXTRACTION`
with cropped-region evidence.

**Gate:** scanned PDF produces an extraction escalation showing the image crop.

### Phase 5 — Schema modes and versioning `[M]`
Mode A (supplied) and Mode B (recommended). Form + JSON/YAML upload, normalization
to canonical internal representation, immutable versions, remap-on-approve,
delivered records retain their version.

**Demo discipline:** criteria 1 and 2 are *always* demonstrated through Mode A
against the fixed adversarial schema. Mode B is a separate showcase. A schema the
agent derived from the source makes the mapping self-consistent by construction and
removes the difficulty criterion 2 exists to test.

### Phase 6 — UI depth `[M]`
Mobile composition, live view polish, non-sequential review navigation, keyboard and
touch accessibility, plain-language statuses.

### Phase 7 — Deploy, write-up, recording `[S]`
Terraform apply, manual deploy via `make deploy`, hosted smoke test, one-page
write-up final, recording cut, teardown documented.

---

## 4. Deliverables

- [ ] **Working prototype** — hosted link *and* local run instructions
- [ ] **Recording** showing at least one escalation resolved through the UI *(named requirement)*
- [ ] **Repo** with README covering setup and tech stack
- [ ] **One-page write-up** — approach, the autonomy boundary, what's next *(hard cap)*
- [ ] Acceptance-criteria traceability table (TD011)
- [ ] Decision log in `docs/decisions/`

**The write-up must explicitly address the 4–6 hour guidance.** State that you
deliberately built the production-shaped version, say why, and point at the
~4-hour core (Phase 1) inside it. Unstated, a large submission reads as having
missed the scoping question — which *is* the graded competency. Stated, it reads as
having answered it and chosen. That sentence converts the single largest risk in
this plan into its strongest signal.

---

## 5. Risk register

| Risk | Mitigation |
|---|---|
| Model endpoint unavailable during panel demo | `DEMO_MODE=cached` (A12) — zero network calls |
| Hosted link taken down for cost before the panel | Recording is the durable artifact — cut it at Phase 1 |
| Cookie/CORS failure at deploy | Next.js route-handler proxy (A8) — designed in, not debugged later |
| Panel can't find the autonomy story under the infrastructure | Write-up and demo lead with the boundary; infra mentioned second |
| Scope never closes | Phase gates; write-up-change heuristic |
| OCR slow at TD016's 200-page limit | docTR on CPU ≈ 2–5 s/page → bound concurrency; lower the page limit for demo fixtures |
| NAT gateway standing cost | VPC endpoints (A10) |

---

## 6. Still open

- Weight values in §2.2 before calibration (hand-set in Phase 1, replaced in Phase 2)
- Source-authority configuration format for conflict resolution (TD005)
- Whether Mode B ships at all, if Phase 5 runs long — it is the most cuttable item
