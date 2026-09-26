# Judge calibration — DUK-55

## Set

- Labels: `labels.yaml`
- Source: `runs/`
- Included: 29 completed, non-dry-run artifacts
- Distribution: 5 pass, 24 fail
- Selection rule: `status=finished`, `config.dry_run=false`, with `report.json` and an artifact
- Review: QA compared each artifact with its task contract; router, extraction, and landing-page artifacts were reviewed from the stored files

Three live runs were excluded because they failed before producing artifacts. Dry-run outputs were excluded because they are synthetic placeholders rather than model results.

## Command

```text
python3 harness.py calibrate --labels labels.yaml --runs-dir runs --json
```

## Agreement output

```text
Labeled: 29 | matched to runs: 29 | unmatched: 0

Score agreement (n=10):
  MAE 0.000 | Pearson 0.9999999999999998 | Spearman 1.0
  human mean 0.500 | judge mean 0.500

Verdict agreement (n=29):
  accuracy 72.4% | Cohen's kappa 0.4081632653061224
  tp 5 | tn 16 | fp 8 | fn 0
```

## Limitation

No selected run has a non-null `report.judge` result. `collect_pairs` therefore uses the stored run-level `score` and `passes` fallback. These results are reproducible human-label versus validator agreement, not a live LLM-judge calibration. A real judge calibration requires new judged runs; this work does not invent judge outputs.

## Reproducibility ceiling

The figures above are **not recomputable from this repository alone.** The input store is `runs/`,
which `.gitignore` ignores, and `runs/` is not committed.

What a reviewer *can* verify from the repo:

- `labels.yaml` parses, and contains the 29 labels the report claims
- `labels_sha256` matches `shasum -a 256 labels.yaml`
- the confusion matrix sums to 29, and accuracy and kappa recompute from it
- the JSON schema contract the tool expects

What a reviewer **cannot** verify without the original run store: `matched: 29`, and therefore the
selection of which 29 completed runs were included. If you are checking these numbers, obtain the
run store or treat the agreement figures as historical record rather than a reproducible
measurement.

## Reading the commit subject

The commit that introduced this file is titled `chore: record judge calibration baseline`. That
subject overstates what was measured. As the Limitation section above says, there is no live
LLM-judge output in this data: `non_null_judge_outputs` is 0, and the score side of the comparison
is `collect_pairs` falling back to the stored run-level `score` and `passes`. This is a
human-label-versus-validator-fallback agreement baseline, not a judge-quality calibration.
