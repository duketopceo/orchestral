# Task suite critique — gpt-5.6-sol (first principles)

Generated 2026-09-23 · model `openai/gpt-5.6-sol` · cost $0.11552904

## Executive assessment

This is not primarily an orchestration benchmark. It is a small collection of single-shot instruction-following tasks wrapped in an orchestrator→worker execution protocol.

It mostly measures:

- Whether the final model output obeys a format.
- Basic Python and SQLite competence.
- Simple extraction and constrained generation.
- Whether the backend can emit PNG/MP4 bytes.
- The overhead or damage caused by forcing delegation on tasks that do not need it.

It does **not** meaningfully measure decomposition quality, parallel delegation, context allocation, integration, conflict resolution, validation-driven repair, recovery from worker failure, or cost-aware orchestration.

The suite also conflates three distinct things:

1. **Worker capability**: can the worker write a parser or SQL query?
2. **Orchestrator capability**: can the orchestrator plan, delegate, integrate, and repair?
3. **Harness protocol compliance**: can the pairing emit the exact JSON/ZIP/PNG wrapper expected?

Those need separate scores and baselines.

---

# 1. What the suite actually measures versus what it claims

## What it claims to measure

The stated target is which orchestrator→worker pairings produce correct artifacts “cheaply and reliably.” That implies measurement of:

- Task decomposition.
- Subtask dependency planning.
- Delegation granularity.
- Context selection for each worker call.
- Parallel versus sequential execution.
- Artifact assembly.
- Validation and repair.
- Cost and latency.
- Robustness across repeated runs and task variants.

## What it actually measures

### A. Mostly raw worker competence

The following are ordinary single-response tasks:

- `code-expr-parser`
- `code-fizzbuzz`
- `code-lru-cache`
- `code-slugify`
- All five `sql-*` tasks
- `extract-invoice`
- `constraint-product-blurb`
- `needle-deploy-token`
- `api-order-lookup`

A lone model given the original prompt would solve these in exactly the same way. Decomposition adds no useful information and often adds failure opportunities.

For example, `code-fizzbuzz` is approximately ten lines of code. Any “orchestration strategy” is noise. Its score is almost entirely a worker/basic-model score.

### B. Protocol and wrapper compliance

Several tasks substantially measure whether the system returns the right envelope rather than whether the substantive artifact is correct:

- Code tasks require `{"files": [...]}`.
- `swe-patch-rename-key` requires a JSON-escaped unified diff.
- `api-order-lookup` requires a raw JSON list with no prose.
- `multi-file-site` apparently expects a ZIP artifact, although the prompt does not explain the transport representation.
- Image tasks require PNG specifically, although the prompt asks for an image, not a PNG.
- Video requires MP4 specifically, although the prompt asks for a video clip, not explicitly an MP4.

This is useful for production integration testing, but it is not orchestration capability.

### C. Backend availability

- `image-hero-coffee`
- `image-logo-minimal`
- `video-clip`

These mostly measure whether the pairing has access to a compatible media generator and can return bytes in the expected container. The mechanical checks do not test the visual request.

### D. Elementary instruction following

- `constraint-product-blurb`
- `extract-invoice`
- `terminal-config-fix`
- `needle-deploy-token`

These provide useful low-level calibration, but they do not distinguish sophisticated orchestrators.

### E. Fixed-instance memorization or seed fitting

Every SQL task uses one tiny public schema and seed. A model can produce a query tailored to the visible rows, including literal result rows, and pass if the harness only executes against that seed.

Likewise, the code tests are included in metadata. If that metadata reaches the evaluated system, tasks become “write code to these exact tests,” not general implementation tasks.

### F. Almost no reliability measurement

A fixed task run once does not measure reliability. Reliability requires:

- Multiple generated instances.
- Repeated stochastic runs.
- Hidden edge cases.
- Failure injection.
- Confidence intervals.
- Success-at-budget curves.

The suite has none of those in its task definitions.

### G. Almost no “cheaply” measurement in the tasks

Cost can be logged externally, but the suite gives the orchestrator no meaningful cost tradeoff. Most tasks are so small that the cheapest correct policy is “make one worker call” or “solve it directly.” There are no tasks where selective delegation, parallelism, or early stopping creates a meaningful cost-quality frontier.

---

# 2. Weak mechanical validation

## A. Specs with no declared mechanical validation

Under the stated contract, `validation:` is the deterministic check. These tasks declare an empty validator list:

- `api-order-lookup`
- `extract-invoice`
- `sql-cohort-repeat`
- `sql-dedup-latest-price`
- `sql-mom-growth`
- `sql-monthly-revenue`
- `sql-net-revenue`

The code, bugfix, patch, and terminal tasks omit `validation:` entirely and place tests or expected state under `metadata`.

If task-type-specific runners implicitly consume `metadata.tests`, `metadata.expected`, `metadata.schema`, and similar fields, then the suite has two undocumented validation systems. That is a design flaw. A reviewer cannot determine from the declared `validation:` block what is mechanically enforced.

All mechanical checks should be explicit, versioned validators such as:

```yaml
validation:
  - json_schema
  - execute_api_plan
  - compare_api_trace
```

or:

```yaml
validation:
  - parse_sql_single_statement
  - enforce_read_only
  - execute_sql_cases
  - compare_result_schema
  - compare_result_rows
```

## B. Vacuous media validation

### `image-hero-coffee` and `image-logo-minimal`

Mechanical validation is only:

- `non_empty`
- `png_signature`

Any nonempty PNG passes mechanically, including:

- A 1×1 transparent pixel.
- A blank black rectangle.
- The wrong aspect ratio.
- An image containing text.
- A completely unrelated image.

The semantic judge carries the whole task.

The checks also fail valid substantive artifacts on format: a correct photorealistic JPEG fails because the prompt never requires PNG.

Required additions:

- Decode image successfully.
- Minimum dimensions.
- Aspect-ratio tolerance.
- Alpha/background checks where relevant.
- OCR-based “no text” check.
- Perceptual duplicate/blank-image detection.
- Image-text embedding score against positive and negative concepts.

### `video-clip`

`non_empty` plus `mp4_signature` accepts a header-only or unrelated MP4. It does not verify:

- Decodability.
- Duration near four seconds.
- 720p resolution.
- 16:9 aspect ratio.
- No audio.
- No text overlay.
- Actual motion.
- Requested content.

This task currently measures container emission, not video generation.

## C. Vacuous web validation

### `landing-page-coffee`

The checks appear to establish only that:

- Some HTML parses.
- It has a title.
- Something resembles a CTA.
- A form exists.
- The word `coffee` occurs.

A page can pass with:

- No responsive design.
- Three plain strings instead of a usable pricing section.
- A broken form.
- Invisible content.
- No CSS.
- A button labeled “CTA.”
- Poor accessibility.
- Invalid layout on mobile.

The request for a “3-tier pricing section” is not mechanically enforced.

### `multi-file-site`

`has_paths` and token checks are extremely weak. For example:

- `index.html` can contain `<!-- coffee pricing -->`.
- `style.css` can contain `/* pricing */`.
- The HTML need not link `style.css`.
- CSS need not parse.
- Pricing styles need not match any element.
- The signup form need not exist or function.
- The ZIP may contain unsafe or extra paths unless explicitly checked.

This task measures archive packaging more strongly than site integration.

## D. Pipeline validation does not validate a pipeline

### `pipeline-sales-summary`

Only the final output is checked for:

- `$4,700`
- `Enterprise seats`

A system can skip extraction and computation entirely and produce:

> Enterprise seats led revenue, totaling $4,700.

That passes. Nothing validates:

- That all four line items were extracted.
- That step 2 consumed step 1.
- That step 3 consumed step 2.
- That `prior_outputs` were used.
- That intermediate outputs were correct.
- That dependencies were ordered correctly.

This is the only task explicitly framed as a dependency chain, and its validator does not test the chain.

## E. Needle task tests search, not orchestration

### `needle-deploy-token`

`exact_answer` makes the final answer robustly checkable, but the task is still a literal string search over one document. It can be solved with one regex or one model pass.

It does not test decomposition. In fact, splitting the document across calls risks losing the relationship among:

- `deploy complete`
- `region=eu-west`
- `release_token=...`

A genuine orchestration version would distribute evidence across multiple documents or shards and require reconciliation.

## F. SQL validators are vulnerable to the visible seed

All SQL tasks need hidden generated databases, not one public seed.

A read-only query such as this can return the expected fixed answer without solving the problem:

```sql
SELECT '2025-01' AS month, 'Kettle' AS product, 90.0 AS revenue
UNION ALL
SELECT '2025-02', 'Grinder', 80.0;
```

It will pass a single-seed result comparison.

Each SQL task needs multiple hidden cases covering:

- Empty tables.
- Ties.
- Missing related rows.
- Duplicate dates.
- Multiple refunds.
- Zero prior-month revenue.
- Month gaps.
- Customers with only non-shipped orders.
- Products with no history or no shipped sales.
- Adversarial IDs and insertion orders.

### Specific SQL specification defects

#### `sql-monthly-revenue`

The prompt says “one row per month,” but the reference query returns multiple rows when products tie for maximum revenue:

```sql
WHERE revenue = best
```

There is no tie-break rule. The visible seed avoids the defect. The task is underspecified and the reference contradicts “one row per month.”

#### `sql-dedup-latest-price`

The prompt says “current price of every product,” but the reference uses an inner join and omits a product with no price history. Either:

- Require only products with history, or
- Use a left join and define `NULL` price behavior.

#### `sql-net-revenue`

The prompt also says “one row per product,” while the reference begins from shipped order lines and omits products without shipped lines. Again, the wording and reference disagree.

#### `sql-mom-growth`

Zero previous-month revenue is undefined and untested. The task must specify whether `mom_pct` is `NULL`, infinity, or something else.

## G. Code tasks are too shallow and too exposed

### `code-fizzbuzz`

The visible tests almost completely enumerate the behavior. The task has no meaningful implementation space and no orchestration value.

### `code-lru-cache` and `bugfix-lru-evict`

These are redundant. They test the same semantics:

- `get` refreshes recency.
- Updating refreshes recency.
- Overflow evicts the least recent key.

The bugfix version is more diagnostic, but the defect locations are explicitly identified in the prompt. It is not really bug finding; it is guided line editing.

### `code-expr-parser`

This is the strongest standalone code task, but the test set remains narrow. Missing cases include:

- Unary chains such as `--2`.
- Unary minus after multiplication.
- `.5` and `5.` policy.
- Division by zero policy.
- Invalid numeric forms such as `1..2`.
- Trailing tokens.
- Deep nesting.
- Non-string input policy.

A solution can still overfit the listed grammar examples.

### `code-slugify`

“Alphanumeric” is underspecified after Unicode normalization. The suite does not define whether non-Latin letters should be retained, dropped, or transliterated. Only Latin-accent examples are tested.

## H. Patch validation overweights diff syntax

### `swe-patch-rename-key`

This has some integration value, but the requested JSON-escaped patch introduces two independent failure modes:

1. Is the code change correct?
2. Is the unified diff serialized exactly enough for `patch -p1`?

That distinction should be reported separately. A correct edited repository should not receive zero substantive credit because of a hunk-header formatting error.

The task is also tiny enough that no decomposition is needed.

## I. Terminal task contains an unresolved contradiction

### `terminal-config-fix`

The prompt says:

> `deploy.log` — deploy log; the rollback flag line is stale

and asks to “repairs the deploy,” but the required end state says nothing about `deploy.log`. The reference commands leave it untouched.

A careful model may clear or update the stale rollback flag and be semantically reasonable, while an exact validator may ignore or penalize it. Remove the irrelevant statement or define the required log mutation.

The allowed shell also makes diagnosis unnecessary because the prompt gives the exact broken values and exact target state. This is command serialization, not terminal troubleshooting.

## J. Constraint validation can be accidentally lexical

### `constraint-product-blurb`

This is mechanically checkable, but validators need explicit tokenization rules:

- Does `best-in-class` violate `best`?
- Does `amazingness` violate `amazing`?
- Is `$249.` recognized as a digit-only price?
- How are em dashes and hyphenated terms counted?
- Are headings included in the word count?

Without defined normalization, correct prose can fail on validator implementation details.

---

# 3. Redundancy and missing capability dimensions

## Redundant groups

### Elementary Python group

- `code-fizzbuzz`
- `code-slugify`
- `code-lru-cache`
- `bugfix-lru-evict`
- `code-expr-parser`

Five tasks overrepresent small Python modules. Keep one implementation task and one diagnosis/repair task. Drop the rest or turn them into generated variants.

### Coffee landing-page group

- `image-hero-coffee`
- `landing-page-coffee`
- `multi-file-site`
- `video-clip`

These repeatedly use the same theme without testing cross-artifact integration. Four separate tasks should become one integrated asset-backed site task.

`image-logo-minimal` is another standalone media-generation task with the same validation weakness.

### SQL group

- `sql-cohort-repeat`
- `sql-dedup-latest-price`
- `sql-mom-growth`
- `sql-monthly-revenue`
- `sql-net-revenue`

These cover useful SQL concepts, but five fixed tiny instances over-weight SQL in a 22-task suite. Convert them into one parameterized SQL benchmark family with hidden generated cases and report subskills.

### Tiny structured-output group

- `api-order-lookup`
- `extract-invoice`
- `constraint-product-blurb`
- `needle-deploy-token`

These are acceptable calibration tasks but provide little incremental orchestration signal.

## Missing capability dimensions

### 1. Parallel fan-out and aggregation

No task requires independent subtasks to be delegated concurrently and then reduced.

Example: extract records from 20 independent documents, deduplicate them, and compute an aggregate.

### 2. Context partitioning

The orchestrator never has to decide which files or document sections each worker needs. Nearly every worker can receive the full prompt cheaply.

### 3. Cross-worker conflict resolution

No task produces two plausible but inconsistent worker outputs requiring adjudication.

### 4. Assembly with cross-file invariants

`multi-file-site` is the closest, but validation does not check linkage. There is no substantial task where independently generated components must agree on:

- Function signatures.
- Schemas.
- Routes.
- Config keys.
- Asset paths.
- Types.
- Protocol versions.

### 5. Validation-driven repair

The benchmark says the orchestrator “assembles and validates,” but no task explicitly gives the orchestrator failing test/compiler/linter feedback and measures whether it repairs the artifact.

### 6. Partial worker failure

No task simulates:

- Timeout.
- Malformed response.
- Incorrect subtask.
- Missing file.
- Contradictory answer.
- Worker refusal.

Recovery is central to reliable orchestration.

### 7. Budget-aware planning

There are no tasks where the orchestrator must choose between:

- One large call.
- Several small calls.
- Verification calls.
- Stopping after sufficient confidence.

### 8. Adaptive delegation

All task requirements are known immediately. Nothing requires discovering a defect and then deciding which specialist subtask to run.

### 9. Long-context synthesis across sources

`needle-deploy-token` searches one long document for one local line. It does not synthesize evidence spread across sources.

### 10. Untrusted worker output and prompt injection

No task tests whether a worker or source document contains instructions that should not override the orchestrator’s task.

### 11. Provenance and traceability

No task requires final claims to be linked to source evidence or prior subtask outputs.

### 12. Stateful tool use

`terminal-config-fix` emits a static command list. It does not require observing command results and adapting.

### 13. Nondeterministic or underspecified requirements

No task tests clarification, assumption tracking, or robust interpretation.

### 14. Repository-scale work

The largest code task is one small module. There is no realistic navigation across a repository, dependency graph, test suite, or API boundary.

### 15. Orchestrator-only and worker-only baselines

Without these, a pairing score cannot be attributed. Every task should be compared against:

- Orchestrator alone.
- Worker alone.
- One unstructured worker call.
- The full orchestration protocol.

Otherwise, the benchmark cannot tell whether orchestration helps.

---

# 4. Which tasks actually exercise orchestration?

## Meaningful, but still weak

### `pipeline-sales-summary`

It explicitly requires an ordered dependency chain and exposes `prior_outputs`. This is the right structural idea, but the arithmetic is trivial and only the final keywords are validated. It exercises the harness pathway, not decomposition quality.

### `multi-file-site`

It requires two files and artifact packaging. It weakly tests assembly, but the files are simple enough for one call and cross-file consistency is barely validated.

### `swe-patch-rename-key`

It requires coordinated edits across two files. It tests a small cross-file invariant, but the exact locations and replacement are stated. A single worker call is optimal.

### `terminal-config-fix`

It requires a sequence of state mutations. However, every command is directly implied by the prompt and no observation or adaptation is needed.

## Superficially decomposable, but single-shot in substance

### `landing-page-coffee`

One could delegate hero, pricing, form, and styling separately, but that is likely worse than one coherent generation call. The task does not reward or verify decomposition.

### `api-order-lookup`

The four calls are already enumerated. There is nothing to decompose or infer.

### `bugfix-lru-evict`

The prompt names both bugs and their required fixes. Delegating diagnosis and implementation separately adds no value.

### Media tasks

An orchestrator can rewrite the prompt for an image/video worker, but there is no assembly or validation beyond checking the file signature.

## Not orchestration tasks

- `code-expr-parser`
- `code-fizzbuzz`
- `code-lru-cache`
- `code-slugify`
- `constraint-product-blurb`
- `extract-invoice`
- `needle-deploy-token`
- All `sql-*` tasks

These are worker-quality or direct-generation tasks.

## Core design problem

The orchestrator receives the full solvable task. Nothing prevents it from:

- Solving the task itself.
- Sending the entire prompt to one worker.
- Ignoring decomposition.
- Fabricating intermediate reasoning.
- Calling workers redundantly.

Therefore, the benchmark cannot infer decomposition quality from final accuracy.

To measure orchestration, the harness must either:

1. **Instrument and score the trace**, including task graph, context sent, worker calls, retries, cost, and dependency use; or
2. **Enforce information separation**, where no single call has all necessary information and the orchestrator must combine worker outputs.

The second is much stronger.

---

# 5. Concrete revamped suite

A tighter suite should contain approximately 10 high-signal tasks plus a small calibration set. Every task should have generated hidden instances and explicit validators.

## Proposed 10-task suite

| Proposed spec | Action | What it uniquely measures |
|---|---|---|
| `calibration-structured-extract` | Merge `extract-invoice` and `api-order-lookup` patterns | Baseline worker instruction following and strict schema compliance, isolated from orchestration. |
| `calibration-code-parser` | Keep and strengthen `code-expr-parser` | Baseline implementation capability using hidden grammar/property tests; establishes worker-alone competence. |
| `sql-generated-analytics` | Merge all five `sql-*` tasks into a generated family | SQL reasoning across joins, windows, deduplication, and aggregation, with hidden database mutations that defeat seed hardcoding. |
| `repo-bugfix-regression` | Replace `bugfix-lru-evict`, `code-lru-cache`, and `swe-patch-rename-key` | Repository navigation, diagnosis, coordinated multi-file edits, and regression-safe repair under hidden tests. |
| `fanout-document-ledger` | Replace `needle-deploy-token` and `pipeline-sales-summary` | Parallel extraction from partitioned documents, provenance-preserving aggregation, deduplication, and exact reconciliation. |
| `dependency-dag-build` | Add | Correct planning of a nontrivial dependency DAG with parallel branches and downstream steps that genuinely consume prior outputs. |
| `contract-integration-service` | Replace `api-order-lookup` as an orchestration task | Delegating client, server, schema, and tests separately, then resolving interface mismatches during assembly. |
| `validate-repair-loop` | Add | Running tests or compilation, interpreting failures, selectively redelegating fixes, and stopping when validation passes. |
| `stateful-terminal-recovery` | Strengthen `terminal-config-fix` | Observe-act-observe terminal troubleshooting where the correct next command depends on command output and hidden filesystem state. |
| `asset-backed-site` | Merge `landing-page-coffee`, `multi-file-site`, and `image-hero-coffee`; drop standalone video | Cross-modal delegation and assembly: generate an asset, build linked HTML/CSS, verify dimensions, references, rendering, accessibility, and responsive layout. |

## Optional 11th task if security matters

| Proposed spec | Action | What it uniquely measures |
|---|---|---|
| `untrusted-worker-synthesis` | Add | Resistance to prompt injection and malicious instructions embedded in source documents or worker responses while preserving valid evidence. |

---

## Detailed changes by current task

### Keep, but strengthen

#### `code-expr-parser`

Keep as a **calibration task**, not an orchestration task.

Changes:

- Generate expressions and compare against a trusted parser.
- Add malformed-input fuzzing.
- Explicitly define numeric grammar and division-by-zero behavior.
- Score code correctness separately from JSON-wrapper correctness.
- Run worker-only and orchestrated baselines.

#### `terminal-config-fix`

Keep only after converting it to interactive troubleshooting:

- Do not state exact defects.
- Let the system inspect files.
- Make one symptom depend on another.
- Require clearing or updating the stale rollback flag if it matters.
- Validate final filesystem and command budget.
- Return command observations to the orchestrator after each step.

### Merge

#### Merge `bugfix-lru-evict`, `code-lru-cache`, and `swe-patch-rename-key`

Replace them with a five-to-ten-file repository task containing:

- A failing integration test.
- One misleading local symptom.
- A config-key migration across producer, consumer, docs/schema, and tests.
- A genuine behavioral bug.
- Hidden tests for backward incompatibility and stale references.

Require a final repository artifact or patch, but report:

- Patch parse/application success.
- Build success.
- Test success.
- Stale-reference scan.
- Behavioral correctness.

#### Merge all SQL tasks

Use a parameterized SQL family instead of five static tasks. Each run selects one semantic pattern:

- Latest-row selection.
- Cohort/repeat analysis.
- Month-over-month windows.
- Top-per-group with explicit tie rules.
- Aggregation without join multiplication.

Run each submitted query against at least 20 hidden generated databases. Validate:

- Single read-only statement.
- Required column names and order.
- Row ordering.
- Exact result semantics.
- Empty and adversarial cases.

This retains SQL breadth without spending five suite slots.

#### Merge the coffee site and image tasks

Replace:

- `image-hero-coffee`
- `landing-page-coffee`
- `multi-file-site`

with one artifact bundle:

- `index.html`
- `style.css`
- `hero.png`
- optional `script.js`

Mechanical checks should include:

- Safe archive paths.
- HTML and CSS parsing.
- Correct stylesheet and asset references.
- Three distinct pricing tiers.
- Form labels and email input.
- Mobile and desktop browser render.
- No overflow at target widths.
- Image dimensions and aspect ratio.
- OCR “no text” check.
- Screenshot-based visibility checks.
- Broken-link scan.

This actually tests delegation and assembly across modalities.

Drop `video-clip` unless video orchestration is a product requirement. Video generation is expensive and the current task produces little signal. If retained, make it part of a multimedia package and validate duration, streams, dimensions, frame motion, OCR, and content embeddings.

### Drop

#### `code-fizzbuzz`

Drop completely. It is too easy, fully saturated, and has no orchestration value.

#### `code-slugify`

Drop as a dedicated task. It contributes little beyond the parser calibration task. If Unicode handling matters, create a generated text-normalization benchmark with a precise Unicode policy.

#### `code-lru-cache`

Drop as a standalone task because `bugfix-lru-evict` covers the same behavior more diagnostically.

#### `constraint-product-blurb`

Drop from the core suite. It measures lexical compliance, not orchestration. Keep it only in a cheap protocol-smoke-test set.

#### `image-logo-minimal`

Drop from the core suite. A standalone image with signature-only validation is a backend smoke test.

#### `video-clip`

Drop from the core suite unless media generation is central to the product. Its cost, nondeterminism, and weak validation make it low-signal.

### Replace

#### `needle-deploy-token` → `fanout-document-ledger`

Construct 12–30 document shards. No worker receives all of them. Relevant facts are distributed:

- One shard maps deployment IDs to regions.
- Another records completion status.
- Another maps deployment IDs to tokens.
- Others contain drafted rollbacks and conflicting stale records.

Require:

- Parallel shard extraction.
- Evidence references.
- A join over deployment ID.
- Exact final token.
- Validator checks on intermediate evidence and final reconciliation.

Now decomposition and context partitioning matter.

#### `pipeline-sales-summary` → `dependency-dag-build`

Use a real DAG rather than a three-step arithmetic chain:

1. Parse two independent source formats in parallel.
2. Normalize records to a shared schema.
3. Deduplicate using a supplied identity rule.
4. Compute separate financial and operational aggregates.
5. Reconcile them against a control total.
6. Produce a final report citing discrepancies.

Mechanically validate every node’s output schema and hashes linking downstream inputs to upstream outputs.

#### `api-order-lookup` → `contract-integration-service`

Provide:

- An OpenAPI fragment.
- A partially implemented client.
- A stub server with edge cases.
- A consumer expecting a specific normalized result.

Separate workers can handle client code, schema interpretation, and tests. The orchestrator must assemble them and repair mismatched field names, error behavior, and pagination. Validate against hidden server scenarios.

---

# Required benchmark-level changes

## 1. Separate scores

Report at least:

- **Artifact correctness**
- **Protocol correctness**
- **Orchestration efficiency**
- **Repair effectiveness**
- **Reliability across repetitions**
- **Cost**
- **Latency**

Do not collapse these immediately into one opaque score.

## 2. Add baselines

For every task, run:

- Worker alone with the full prompt.
- Orchestrator alone if supported.
- Orchestrator with exactly one worker call.
- Unrestricted orchestration.

The useful orchestration metric is not raw accuracy. It is improvement over the best single-call baseline at a comparable budget.

## 3. Score the trace

Record and evaluate:

- Number of worker calls.
- Sequential depth.
- Parallel width.
- Tokens and cost per call.
- Context sent to each worker.
- Dependency declarations.
- Validation attempts.
- Retries.
- Which worker output contributed to the final artifact.
- Redundant or unused calls.

A final artifact alone cannot reveal decomposition quality.

## 4. Use hidden generated instances

Static visible fixtures invite overfitting. Generate task instances from templates and keep expected outputs hidden.

This is especially important for:

- SQL data.
- Invoice fields.
- Repository symbol names.
- Filesystem layouts.
- Logs and distractors.
- API responses.
- Cross-file contracts.

## 5. Validate substance before format

Use layered results rather than all-or-nothing failure:

1. Transport decoded.
2. Artifact parsed.
3. Artifact executed or rendered.
4. Functional behavior correct.
5. Semantic quality acceptable.

A valid implementation with a malformed wrapper should lose protocol points, not all substantive credit.

## 6. Make orchestration necessary

At least half the core suite should satisfy one of these:

- Information is partitioned across workers.
- Parallelism reduces cost or latency.
- Components have independently delegated contracts.
- Validation feedback forces selective repair.
- Worker outputs conflict and require adjudication.
- One worker failure must be recovered from.
- The artifact cannot be produced correctly from any single subtask context.

Without that property, the suite evaluates models placed behind an orchestrator, not orchestration.

---

## Final recommended composition

Use:

- **2 calibration tasks**: structured extraction and code implementation.
- **1 generated SQL family**.
- **2 repository/tool tasks**: repo repair and stateful terminal recovery.
- **3 true orchestration tasks**: fan-out aggregation, dependency DAG,
