# Data Bridge — An AI agent for client data migration, and where I drew the line between what it decides and what it asks

**Hima Charan** · Live console: http://dbx-console-alb-1789124705.ap-south-1.elb.amazonaws.com · Source and demo recording: [github.com/himacharan128/darwinbox-data-bridge](https://github.com/himacharan128/darwinbox-data-bridge)

## What I built

An agent that reads a client's HR exports, decides where each column belongs, cleans what it can defend, and writes the result to a target API, stopping only where the data does not answer the question. On the messy sample (seven files in five naming conventions, including a native PDF and a scan of the same roster) it maps 85 of 87 mappable columns unaided, leaves 14 noise columns alone, and turns 46 rows into 40 people with about twenty questions.

## Deciding what it does on its own

I did not want the boundary to rest on a model saying it felt confident, so it is arithmetic: **score = 0.75 × measured evidence + 0.25 × model vote**. The evidence comes from the data: header similarity, how much of the column parses as the target type, fit to the field's pattern or allowed values, uniqueness, value shape, and whether the values exist in an uploaded lookup. With nothing measured behind it, a candidate cannot pass 0.25, so it never reaches the 0.70 auto-apply line however sure the model sounds.

The thresholds are calibrated, not chosen: 77 labelled decisions, 18 of them columns that must *not* map. At the chosen line 50 of 57 mappable columns apply unaided, with zero wrong, zero noise mapped and zero genuine ambiguity resolved silently, and those zeros hold at every threshold in the sweep. Two shortcuts were measured and thrown out. Raising the model's cap to 0.30 maps a noise column. Letting the model break near-ties maps `dtProbationEnd` onto `date_of_joining` on a vote of 1.00. The model is exactly as certain when it is wrong.

## What it asks, and keeping the queue short

Eleven typed escalation classes, each with its own evidence: both candidates' measurements, both readings of a date, the cropped scan of what OCR misread. A boundary that is right but unusable has still failed, so every question is asked at the scope of its fact: one per file for a missing column, one per column for an undecidable date, one per value for a code no option allows. Where a rule exists it offers the rule, and "believe the real document" settles eight of nine disagreements at once. Where another file holds the answer it recommends it and shows why ("3 of 3 people in hr_export.csv match day first"), and asks again before a contradicting answer goes through. Pushing to the target is a button, because it is the only action with a consequence outside the tool. Results come back per record, with retry and rollback, and everything lands in the audit trail.

## Things I did differently

- Clients rarely have a target schema, so the agent drafts one (identifier shapes become patterns, lookup-backed columns become references) for a person to edit and approve. A supplied JSON Schema or YAML works too.
- Synonyms ("role" means "designation", which no measurement connects) enter only as aliases a person ticks, never as a vote.
- On a checkable case the agent must use read-only lookups over the run's data before it may suggest anything.
- Prompt injection is handled in three layers, and one fixture carries a live payload.
- Every model call is recorded, so it runs offline and reproduces: 124 tests need no credentials. Only human decisions and destination acceptances are durable; everything else is replayed from the files.

## Scope

The brief suggests four to six hours. I went well past that on purpose: what it grades is where the agent's line sits, and I wanted that line measured rather than asserted. The core (mapping boundary, review queue, push) came first; the rest is built on it.

## What I would build next

PII handling: field sensitivity, masking, audit redaction, and a policy on what reaches a hosted model (profiling is the one chokepoint). Effective-dated records. Deterministic tie-breaks for the remaining near-ties. Calibrated record matching, whose thresholds are still asserted. A durable queue that runs on more than one node.
