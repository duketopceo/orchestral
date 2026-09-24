"""Replay mechanical validation on stored run artifacts.

The validators are deterministic — no model calls — so a stored artifact can
be re-scored at any time. Two consumers:

- Repairing the score axis on runs written before mechanical and judge
  scores were separated (the judge score overwrote ``report.score`` and
  ``meta.score``; ``meta.passes`` on pre-U5 runs could carry the judge
  verdict ANDed in).
- Re-checking a corpus after a validator bugfix.

The pass is idempotent: a run whose stored verdict already matches the
replayed one is left untouched. A divergence restores ``report.score`` /
``report.passes`` / ``meta.score`` / ``meta.passes`` and stamps
``report["revalidated"]`` with the old values so the repair is auditable
rather than silent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestral.config import TaskSpec, find_task, load_task
from orchestral.fileset import read_zip
from orchestral.logger import EventLogger
from orchestral.runner import _CANDIDATE_TASKS, Runner, _artifact_ext

# artifact filename per task type when the type is not in _CANDIDATE_TASKS —
# mirrors the runner's dispatch: media bytes, zips for file-set tasks, the
# generic artifact for everything else
def _load_artifact(run_dir: Path, task: TaskSpec) -> tuple[Any, str]:
    """Return (validator_input, kind) for a run's stored artifact."""
    if task.type in _CANDIDATE_TASKS:
        name = _CANDIDATE_TASKS[task.type][0]
        path = run_dir / name
        if not path.exists():
            raise FileNotFoundError(name)
        return path.read_text(encoding="utf-8", errors="replace"), "text"
    if task.type in ("multi-file", "code", "bugfix"):
        path = run_dir / "artifact.zip"
        if not path.exists():
            raise FileNotFoundError("artifact.zip")
        return path.read_bytes(), "zip"
    if task.type in ("image", "video"):
        path = run_dir / f"artifact{_artifact_ext(task.type)}"
        if not path.exists():
            raise FileNotFoundError(path.name)
        return path.read_bytes(), "media"
    path = run_dir / f"artifact{_artifact_ext(task.type)}"
    if not path.exists():
        raise FileNotFoundError(path.name)
    return path.read_text(encoding="utf-8", errors="replace"), "text"


def _replay(runner: Runner, task: TaskSpec, artifact: Any, kind: str) -> tuple[bool, dict[str, Any]]:
    if kind == "zip":
        if task.type in ("code", "bugfix"):
            return runner._validate_code(task, read_zip(artifact))
        return runner._validate_multi(task, artifact)
    if kind == "media":
        if task.type == "image":
            return runner._validate_image(task, artifact)
        return runner._validate_video(task, artifact)
    if task.type in _CANDIDATE_TASKS:
        validate_name = _CANDIDATE_TASKS[task.type][2]
        return getattr(runner, validate_name)(task, artifact)
    return runner._validate(task, artifact)


def revalidate_runs(
    store: Any,
    *,
    run_group: str | None = None,
    task_id: str | None = None,
    orchestrator: str | None = None,
    worker: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    tasks_dir: Path | str = "tasks",
    sandbox: str = "local",
    sandbox_image: str | None = None,
) -> dict[str, Any]:
    """Re-run mechanical validators on finished runs' stored artifacts.

    Repairs ``score``/``passes`` on report.json and the index where the
    replayed verdict differs from the stored one; stamps
    ``report["revalidated"]`` with the old values for auditability.
    """
    metas = [
        m for m in store.list_runs(
            orchestrator=orchestrator, worker=worker,
            task_id=task_id, run_group=run_group,
        )
        if m.status == "finished"
    ]
    if limit is not None:
        metas = metas[:limit]
    runner = Runner(
        dry_run=False,
        store=store,
        sandbox=sandbox,
        sandbox_image=sandbox_image,
    )
    spec_cache: dict[str, TaskSpec] = {}

    def _spec(tid: str) -> TaskSpec:
        if tid not in spec_cache:
            path = find_task(tid, tasks_dir)
            if path is None:
                raise FileNotFoundError(f"task spec not found: {tid}")
            spec_cache[tid] = load_task(path)
        return spec_cache[tid]

    results: list[dict[str, Any]] = []
    repaired = unchanged = skipped = 0
    for meta in metas:
        run_dir = Path(meta.run_dir)
        report_path = run_dir / "report.json"
        if not report_path.exists():
            skipped += 1
            results.append({"run_id": meta.run_id, "skipped": "no report.json"})
            continue
        try:
            task = _spec(meta.task_id)
        except Exception as exc:
            skipped += 1
            results.append({"run_id": meta.run_id, "skipped": f"spec: {exc}"})
            continue
        try:
            artifact, kind = _load_artifact(run_dir, task)
        except Exception as exc:
            skipped += 1
            results.append({"run_id": meta.run_id, "skipped": f"artifact: {exc}"})
            continue
        try:
            passes, vreport = _replay(runner, task, artifact, kind)
        except Exception as exc:
            skipped += 1
            results.append({"run_id": meta.run_id, "skipped": f"validator: {exc}"})
            continue

        report = json.loads(report_path.read_text())
        new_score = vreport.get("score")
        new_checks = vreport.get("checks")
        new_errors = vreport.get("errors")
        old_score = report.get("score")
        old_passes = report.get("passes")
        old_checks = report.get("checks")
        old_errors = report.get("errors")
        if (old_score == new_score and old_passes == passes
                and old_checks == new_checks and old_errors == new_errors
                and meta.score == new_score and meta.passes == passes):
            unchanged += 1
            results.append({"run_id": meta.run_id, "task_id": meta.task_id,
                            "unchanged": True, "score": new_score, "passes": passes})
            continue

        entry = {
            "run_id": meta.run_id,
            "task_id": meta.task_id,
            "score": new_score, "score_was": old_score,
            "passes": passes, "passes_was": old_passes,
            "checks_changed": new_checks != old_checks,
            "meta_score_was": meta.score, "meta_passes_was": meta.passes,
        }
        if dry_run:
            entry["dry_run"] = True
            repaired += 1
            results.append(entry)
            continue

        # the verdict quartet moves together — a repaired report must be
        # self-consistent (`passes: false` beside `checks: {all true}` lies
        # to the reader); originals live in the stamp
        report["score"] = new_score
        report["passes"] = passes
        if new_checks is not None:
            report["checks"] = new_checks
        if new_errors is not None:
            report["errors"] = new_errors
        # merge into any earlier stamp — the FIRST repair's *_was values are
        # the run-time originals; a later pass must not record the already-
        # repaired values as history (checks_was is the one field an older
        # stamp schema may not carry, and old_checks is still original then)
        prior = report.get("revalidated") or {}
        report["revalidated"] = {
            "at": datetime.now(UTC).isoformat(),
            "score_was": prior.get("score_was", old_score),
            "passes_was": prior.get("passes_was", old_passes),
            "checks_was": prior.get("checks_was", old_checks),
            "errors_was": prior.get("errors_was", old_errors),
            "meta_score_was": prior.get("meta_score_was", meta.score),
            "meta_passes_was": prior.get("meta_passes_was", meta.passes),
        }
        report_path.write_text(json.dumps(report, indent=2, default=str))
        meta.score = new_score
        meta.passes = passes
        if passes is False:
            meta.failure_reason = "validation"
        elif meta.failure_reason == "validation":
            meta.failure_reason = None
        store.update_meta(meta)
        logger = EventLogger(run_dir, store=store, run_id=meta.run_id)
        try:
            logger.log(
                phase="validate", step=-1, event_type="revalidation.repaired",
                model="", role="harness",
                input_data={"task": meta.task_id},
                output_data={"score": new_score, "passes": passes},
                reasoning=(
                    f"Replayed mechanical validation: score {old_score} -> {new_score}, "
                    f"passes {old_passes} -> {passes}."
                ),
            )
        finally:
            logger.close()
        repaired += 1
        results.append(entry)

    return {
        "runs": len(metas), "repaired": repaired,
        "unchanged": unchanged, "skipped": skipped,
        "dry_run": dry_run, "results": results,
    }
