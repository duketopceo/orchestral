"""Contamination canaries — the memorization tripwire.

A canary is an opaque token stamped into ``spec.metadata.canary``. It lives
in the spec file but never in ``spec.prompt``, so the model is never shown
it: the only way a run's artifact can echo a canary is if the model already
had the spec file's text — in its training data, not in its context.

Committed specs carry ``metadata.canary`` so that when this repository's
task text is scraped into a corpus, a model reciting the string is caught
flat-footed. Generated holdout specs get a deterministic canary per
(seed, index) so their echoes are checkable without the spec on disk —
the runner records ``config.task_canary`` on every run for that reason.

Two echo classes matter:

- ``own``: the artifact echoes the canary of the task it was asked to
  solve — the model knew the spec before we showed it anything.
- ``foreign``: the artifact echoes another spec's canary — the model
  memorized task text outside its assignment, which is what publishing
  the suite would leak into every leaderboard number.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from orchestral.config import TaskSpec
from orchestral.storage import RunMeta

CANARY_RE = re.compile(r"orc-canary-[0-9a-f]{16}")

# Artifact payloads worth scanning. Binary deliverables (zip, media) can't
# be grepped for a string and are withheld from publish anyway.
_SCAN_SUFFIXES = {".txt", ".json", ".jsonl", ".diff", ".patch", ".md",
                  ".sql", ".html", ".css", ".js", ".py", ".yaml", ".toml"}


def canary_token(key: str) -> str:
    """Deterministic canary for a namespace key (spec id, seed:index, ...)."""
    return f"orc-canary-{hashlib.sha256(key.encode()).hexdigest()[:16]}"


def spec_canary(spec: TaskSpec) -> str | None:
    """The spec's canary if it carries a well-formed one."""
    value = (spec.metadata or {}).get("canary")
    if isinstance(value, str) and CANARY_RE.fullmatch(value):
        return value
    return None


def canary_index(specs: list[TaskSpec]) -> dict[str, str]:
    """canary -> owning task id, over every spec that has one."""
    return {c: spec.id for spec in specs if (c := spec_canary(spec))}


@dataclass(frozen=True)
class CanaryHit:
    """One canary string found in one run's artifact."""
    run_id: str
    task_id: str
    canary: str
    owner_task_id: str | None   # None when the token is in no loaded spec
    artifact: str

    @property
    def kind(self) -> str:
        if self.owner_task_id is None:
            return "unknown"
        return "own" if self.owner_task_id == self.task_id else "foreign"

    def to_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id, "task_id": self.task_id,
            "canary": self.canary, "owner_task_id": self.owner_task_id or "",
            "kind": self.kind, "artifact": self.artifact,
        }


def _artifact_text(run_dir: Path) -> list[tuple[str, str]]:
    """(name, text) for each scannable artifact file in a run dir."""
    out: list[tuple[str, str]] = []
    for path in sorted(run_dir.glob("artifact.*")):
        if path.suffix not in _SCAN_SUFFIXES or not path.is_file():
            continue
        try:
            out.append((path.name, path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return out


def canary_echoes(runs: list[RunMeta], index: dict[str, str]) -> list[CanaryHit]:
    """Every canary echo inside run artifacts.

    `index` maps canary -> owner task id. A run's own ``config.task_canary``
    is folded in first, so holdout runs — whose specs live nowhere on disk —
    still get their own-canary classified instead of reported unknown.
    """
    for run in runs:
        own = (run.config or {}).get("task_canary")
        if isinstance(own, str) and CANARY_RE.fullmatch(own):
            index.setdefault(own, run.task_id)
    hits: list[CanaryHit] = []
    for run in runs:
        if run.status != "finished" or not run.run_dir:
            continue
        for name, text in _artifact_text(Path(run.run_dir)):
            for token in set(CANARY_RE.findall(text)):
                hits.append(CanaryHit(
                    run_id=run.run_id, task_id=run.task_id, canary=token,
                    owner_task_id=index.get(token), artifact=name))
    return hits
