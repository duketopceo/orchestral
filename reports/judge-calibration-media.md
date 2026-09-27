# Judge calibration on live media artifacts — DUK-109

Baseline for comparison: `reports/judge-calibration.md` (DUK-55, text artifacts).
That report's limitation was that no run carried a live judge verdict. This one
closes that gap for `image` tasks.

## Headline

`report.judge` was **non-null for all 18 media runs in this set**. Every one of the
18 verdicts came from a fresh judge API call — 18 `judge` `llm_call` events, 0
`judge_cache_hit` events. This is a live LLM-judge calibration, not a second
publication of validator agreement.

The judge tracks the human on the **score** axis and fails on the **verdict** axis:

| Metric | Strict rubric | Relaxed rubric |
|---|---|---|
| score MAE | 0.148 | 0.204 |
| score Pearson | 0.872 | 0.812 |
| score Spearman | 0.639 | 0.559 |
| human mean / judge mean | 0.698 / 0.658 | 0.802 / 0.658 |
| verdict accuracy | 22.2% | 61.1% |
| Cohen's kappa | 0.000 | 0.308 |
| tp / tn / fp / fn | 0 / 4 / 14 / 0 | 7 / 4 / 7 / 0 |

`fn` is 0 in both columns. The judge never once rejected an artifact a human
accepted. Every single disagreement is a false pass: 14 of 18 under the strict
rubric, 7 of 18 under the relaxed one. The judge means are close to the human means
(0.658 vs 0.698 strict), so the score is not systematically inflated — the
**pass/fail cut applied to that score is what is miscalibrated.**

## Set

- Labels: `labels-media.yaml` (strict, primary) and `labels-media-relaxed.yaml`
  (declared sensitivity variant)
- Source: `runs/`, run group `duk109-media`
- Included: 18 `status=finished`, `config.dry_run=false` runs with an artifact and
  a non-null `report.judge`
- Tasks: `image-hero-coffee` (9 runs), `image-logo-minimal` (9 runs)
- Orchestrator: `deepseek/deepseek-v4-pro`, planner `raw`, `--prompt-variant detailed`
- Image workers: `google/gemini-2.5-flash-image`, `google/gemini-3.1-flash-image`
- Judge under test: `google/gemini-3.1-flash-lite` (single judge, held constant)
- `video` not attempted — no judge path exists for it
- Human labels: QA, blind. 0 pass / 18 fail strict; 7 pass / 11 fail relaxed

`labels.yaml` (the DUK-55 baseline) was not modified. Re-running its documented
command after adding these runs reproduces the published numbers exactly
(MAE 0.000, Pearson 0.9999999999999998, Spearman 1.0, accuracy 72.4%,
kappa 0.4081632653061224, tp 5 / tn 16 / fp 8 / fn 0), so the new runs did not
contaminate the baseline.

## Commands

```text
python3 harness.py calibrate --labels labels-media.yaml --runs-dir runs --json
python3 harness.py calibrate --labels labels-media-relaxed.yaml --runs-dir runs --json
```

Both returned exit 0, `labeled 18 / matched 18 / unmatched 0 / corrupt 0`,
`score_pairs 18`, `verdict_pairs 18`.

## Review method

The rubric was written and committed to the run scratch directory **before any
artifact was opened and before any `report.judge` value was read**. It extracts
the explicit checkable constraints from each task `prompt` (8 for the hero task,
7 for the logo task), scores `score = requirements_met / requirements_total`, and
sets `passed = true` only when every requirement is met.

Blind labelling procedure: each artifact PNG was copied to a neutral filename
(`m01`…`m18`) in an order shuffled by `sha256("duk109" + run_id)`, all 18 were
reviewed, and all 18 labels were written to a draft file. Only then was the
run-id map opened and `report.json` read. Aspect ratio was measured from the PNG
header; PNG alpha was measured from the pixel data rather than judged by eye.

Blinding was procedural, not tool-enforced. It was effective in the sense that no
judge verdict was read before the labels were fixed, but a stronger design would
have a second reviewer or a held-out labelling pass.

## Why the strict verdict column is degenerate, and what it rests on

All 18 strict labels are `passed: false`, so strict accuracy (22.2%) and kappa
(0.000) are near-meaningless by construction — kappa is 0 because every label is
identical, not because agreement was measured. The strict numbers are published
because they are what the pre-registered rubric produces. The relaxed numbers are
the informative verdict measurement.

Two requirements drive this, and both are **unsatisfiable by construction** rather
than merely failed. That is the measured reason more replicates cannot rescue the
strict column:

1. **`16:9` (hero requirement 7): 0 of 9 hero artifacts are 16:9.** Three are
   1:1; six are 1408x768 = 1.8333, which is 3.1% off 16:9 and outside the
   pre-registered 2% tolerance. One artifact (1376x768 = 1.7917) is within
   tolerance. Root cause: `delegate_image` calls
   `client.images(model=worker.slug, prompt=prompt)`
   (`orchestral/planners.py:425`) and never passes `aspect_ratio`. The Images API
   therefore never receives an aspect parameter; `aspect_ratio` appears in
   `planners.py` only in `_VIDEO_OPTION_KEYS` for the video path. A subtask prompt
   that mentions "16:9" is advisory text to the image model, not a request
   parameter.
2. **Logo background (logo requirement 5): 0 of 18 PNGs carry an alpha channel.**
   All 18 are `mode=RGB` with alpha 255 at every sampled pixel. One logo
   (run `fe0c7825b2f0`) *paints* a transparency checkerboard into opaque RGB
   pixels. No image worker in `models/default.yaml` emits alpha, so the
   "transparent or solid dark background" clause cannot be met.

The relaxed file drops the hero aspect requirement and treats a flat opaque
background as satisfying the logo background clause. Both relaxations are
declared in that file's `selection.review_method`, and both are reproducible with
the command above.

## What the judge got wrong

| Run | Human | Judge | Disagreement |
|---|---|---|---|
| `53b22f20042b` | 0.71 | **1.00** pass | A bare diagonal stroke with a terminal dot. The judge gave the set's only perfect score to the mark that most plainly fails the "geometric baton or conductor motif" clause. |
| `22d5462ee2df` | 0.75 | 0.90 pass | No steam anywhere, and legible text on the carafe ("02" plus graduation marks) against a contract that says "no text". Neither was flagged. |
| `47c17c9849f6` | 0.71 | 0.90 pass | Same stroke-and-dot construction as `53b22f20042b`, scored 0.9 where its near-twin scored 1.0. |
| `e02b3f226a88` | 0.57 | 0.10 fail | Agree on fail; the judge is 0.47 harsher than the human on a flat-vector orchestral scene. |

Direction of error on the logo subset, where the motif requirement is checkable:
the three marks a human judged **not** to carry a recognisable baton/conductor
motif (`53b22f20042b`, `47c17c9849f6`, `76e85c1e0900`) drew judge scores of
1.0 / 0.9 / 0.7, mean 0.87. The six that do carry it drew 0.1 / 0.1 / 0.7 / 0.7 /
0.7 / 0.9, mean 0.53. In this set the judge rewards the marks that miss the brief.
n is 3 versus 6, so this is a direction worth watching, not a proven effect.

The judge did catch the two badly off-contract hero artifacts outright
(`74f45aa95fab` fantasy forest, human 0.25, judge 0.0 fail; `f89a511bc85d` river
valley, human 0.38, judge 0.0 fail). It is not uniformly blind.

## Adjacent defects found while producing the set

These are outside the calibration's scope and are reported, not fixed.

1. **The image subtask prompt is a process instruction, not an image prompt.**
   `delegate_image` sends `subtask["prompt"] or subtask["description"]` verbatim
   to the Images API (`orchestral/planners.py:394`; the same one-line lookup recurs at
   `planners.py:481` and `:589` for the video and text-media paths). The default `raw` planner
   asks for subtasks a worker executes, and orchestrators comply by writing process
   steps. Across the 18 runs, 31 of 48 subtasks (65%) began with an imperative
   process verb, and 6 of 18 runs had *every* subtask in that form. Those 6 runs
   averaged a human score of 0.54 against 0.83 for the other 12, and they contain
   both off-contract artifacts. With the default prompt variant the very first
   live attempt failed outright: the image API received the literal string
   "Define the composition and lighting setup" and returned
   `400 Gemini could not generate an image (STOP)`. `--prompt-variant detailed`
   is required to get usable image prompts; the flag is undocumented for this.
2. **Image cost is under-reported ~14x.** Actual API cost for the 27 live media
   invocations was **$2.9486**; the runs recorded **$0.2085**.
   `compute_image_cost` (`orchestral/costs.py:136`) prefers token-derived cost
   whenever usage is present, so an image's ~1300 output tokens are priced at the
   text output rate. `models/default.yaml` also lists
   `google/gemini-3.1-flash-image` at `price_per_image: 0.03` against a measured
   **$0.0672** per image. Per-run ledger figures understate by more than an order
   of magnitude, so any budget based on them is wrong.
3. **33% of live media invocations fail before producing an artifact.** 9 of 27
   failed, 8 with `exception:provider_error` (Google content moderation
   `block_reason: SAFETY`, or `Gemini could not generate an image (STOP)`) and 1
   with `exception:rate_limit`. The safe-fail costs real money: failed runs still
   spent $0.1162, $0.2018 and $0.1348 on images before dying.
4. **Only one image per run is kept.** Each subtask generates a separate image and
   `assemble_media` retains one, selected by an orchestrator call that never sees
   the images. On a 3-subtask plan that is 3 images bought and 1 kept.

## Residual risk and what I did not verify

- **One judge, one orchestrator, two image workers, two tasks, one planning
  prompt.** These numbers describe `google/gemini-3.1-flash-lite` judging
  artifacts produced by this pipeline. They do not establish that a different
  judge model would behave the same way. The cheapest vision-capable candidate
  was chosen; a stronger judge may be better calibrated.
- **n = 18** is small. Kappa on 18 items has a wide confidence interval; treat
  0.308 as indicative.
- **Labels are one reviewer's.** No second reviewer, no inter-rater agreement. The
  requirements that drove the strict verdicts (aspect, alpha) were measured
  objectively, but the subjective ones — is the pour-over ceramic, is the mark a
  baton, is it a scene rather than a mark — are one opinion.
- **Not verified:** whether the judge is similarly lenient on `video` (no judge
  path exists, out of scope), on `html`/`code`/`multi-file` artifacts with a real
  judge attached (the DUK-55 set has no live judge verdicts either), or whether
  `--prompt-variant` changes the judge's verdict distribution. None of those were
  run.
- **Blinding was procedural.** See the method note above.

## What this means for DUK-97

The decision to keep the LLM judge as the content gate for `image` tasks rests on
the judge being a meaningful gate. On live media artifacts it is not, yet. It
passed 14 of 18 artifacts that a human reviewer rejected against the task
contract, and its errors are one-directional: zero false rejections, all false
acceptances. A gate that never rejects is not a gate.

Two things should be settled before the judge is trusted on `image`:

1. The **pass threshold** is the defect, not the score. Judge and human score means
   agree within 0.04 while verdicts disagree on 14 of 18. Tightening the prompt or
   the scale without fixing the cut will not help.
2. The judge is scoring against a **contract the pipeline cannot currently meet**.
   Every artifact in this set fails its own task yaml, so a well-calibrated judge
   should have failed all 18. It passed 14. Either the judge is not reading the
   explicit constraints, or the task contracts are wrong about what the workers can
   deliver. Both are worth knowing before the judge is wired in as a gate; the
   second is arguably a task-specification problem, not a judge problem.

The evidence is reproducible: `labels-media.yaml` and
`labels-media-relaxed.yaml` are committed, the two commands above regenerate every
number in this report, and the 18 runs are in `runs/` under group `duk109-media`.
