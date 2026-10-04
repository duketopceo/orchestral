"""The hosted observatory's key tree (U6, KTD5).

One writer renders every payload the hosted SPA reads, by calling the same
``state.py`` functions the local server calls, so hosted parity comes from
sharing the code path rather than porting it. ``build_snapshot`` returns
``{key: payload}``; ``write_snapshot`` lays the keys out under ``api/``.

Keys are what the hosted adapter in ``ui/js/data.js`` requests, minus the
``.json`` suffix the Worker adds (``/api/<name>`` maps to ``api/<name>.json``):

    meta.json  overview.json  runs.json  groups.json  matrix.json
    leaderboard.json  flags.json  models-catalog.json  experiments.json  experiment.<name>.json
    pairings.json  pairings.<group>.json
    cards.<lens>.json  card/<kind>/<target>.<lens>.json
    compare.<a>.<b>.json   (every ordered pair, only below MAX_COMPARE_GROUPS)
    run/<id>.json  run/<id>/live.json  run/<id>/evidence.json

Names inside a key are percent-encoded with ``quote(..., safe="")``; the SPA
applies the same encoding (``encodeURIComponent`` plus ``!'()*``). Filtering,
sorting and pagination are client-side, so query parameters never select a key.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

from orchestral import cf
from orchestral.privacy import HoldoutRunError, run_is_holdout
from orchestral.storage import RunStore
from orchestral.web import catalog, state

# Compare keys grow quadratically; past this many groups the hosted build omits
# them and the SPA says the pair is not part of the snapshot.
MAX_COMPARE_GROUPS = 30


def enc(name: str) -> str:
    """Key-safe form of a group, run or card target (matches ui/js/data.js)."""
    return quote(name, safe="")


def _plain(value: Any) -> Any:
    """JSON round-trip so every payload is exactly what the wire would carry."""
    return json.loads(json.dumps(value, default=str))


class _PublishedStore:
    """The store as the hosted mirror may see it: holdout runs are not listed.

    Every aggregate (groups, matrix, pairings, compare, cards, overview,
    experiments) reads runs through ``list_runs``, so hiding them here keeps a
    holdout run's outcome out of every rate and cell. Per-run keys and the runs
    list still read the real store: the withheld page has to stay reachable."""

    def __init__(self, store: RunStore) -> None:
        self._store = store

    def list_runs(self, *args: Any, limit: int | None = None, **kwargs: Any) -> list[Any]:
        rows = [m for m in self._store.list_runs(*args, **{**kwargs, "limit": None})
                if not _is_holdout_meta(m)]
        return rows if limit is None else rows[:limit]

    def annotations(self) -> list[dict[str, Any]]:
        held = {m.run_id for m in self._store.list_runs(limit=None) if _is_holdout_meta(m)}
        return [a for a in self._store.annotations()
                if not (a.get("kind") == "run" and a.get("target") in held)]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


def _is_holdout_meta(meta: Any) -> bool:
    return run_is_holdout(Path(meta.run_dir), meta.config) if meta.run_dir else bool(
        (meta.config or {}).get("holdout"))


# What a run row says about how it went. The withheld row keeps the identity the
# publication manifest also keeps (id, task id, models, group, cost).
_OUTCOME_FIELDS = ("passes", "score", "judge_score", "judge_passed", "failure_reason")


def _runs_rows(store: RunStore, tasks_dir: Path, groups_file: Path | None) -> list[dict[str, Any]]:
    held = {m.run_id for m in store.list_runs(limit=None) if _is_holdout_meta(m)}
    rows = state.runs_payload(store, tasks_dir=tasks_dir, groups_file=groups_file)
    for row in rows:
        if row["run_id"] in held:
            row.update(dict.fromkeys(_OUTCOME_FIELDS), holdout=True)
            row["judge_state"], row["judge_reason"] = "not_judged", "Withheld: holdout arm."
    return rows


def build_snapshot(
    store: RunStore,
    tasks_dir: Path,
    models_dir: Path,
    groups_file: Path | None = None,
    *,
    run_ids: list[str] | None = None,
    synced_at: str | None = None,
    source_commit: str | None = None,
) -> dict[str, Any]:
    """Render the key tree for ``store``. ``run_ids`` limits the per-run keys
    (None means every run in the index)."""
    tasks_dir, models_dir = Path(tasks_dir), Path(models_dir)
    full_store = store
    store = cast(RunStore, _PublishedStore(full_store))
    registry = state.JobRegistry(Path(store.root), tasks_dir, models_dir, store)
    out: dict[str, Any] = {
        "meta.json": state.meta_payload("hosted", synced_at, source_commit or ""),
        "overview.json": state.overview_payload(
            store, registry, tasks_dir=tasks_dir, groups_file=groups_file),
        "runs.json": _runs_rows(full_store, tasks_dir, groups_file),
        "groups.json": state.groups_payload(store, groups_file),
        "matrix.json": state.task_matrix_payload(store, tasks_dir),
        "leaderboard.json": state.leaderboard_rows(store),
        "flags.json": store.annotations(),
        "pairings.json": state.pairings_payload(store, tasks_dir=tasks_dir),
        "models-catalog.json": catalog.models_catalog_payload(store, models_dir),
    }
    experiments_dir = tasks_dir.parent / "experiments"
    out["experiments.json"] = state.experiments_list(store, experiments_dir, tasks_dir=tasks_dir)
    if experiments_dir.is_dir():
        for spec in sorted(experiments_dir.glob("*.yaml")):
            payload = state.experiment_payload(store, spec, tasks_dir=tasks_dir)
            if payload is not None:
                out[f"experiment.{enc(spec.stem)}.json"] = payload
    groups = [g["group"] for g in out["groups.json"]]
    for g in groups:
        out[f"pairings.{enc(g)}.json"] = state.pairings_payload(
            store, tasks_dir=tasks_dir, group=g)
    if len(groups) < MAX_COMPARE_GROUPS:
        for a in groups:
            for b in groups:
                if a != b:
                    out[f"compare.{enc(a)}.{enc(b)}.json"] = state.compare_payload(store, a, b)
    for lens in state.CARD_LENSES:
        lens_id = lens["id"]
        lens_catalog = state.card_catalog_payload(
            store, tasks_dir=tasks_dir, groups_file=groups_file, lens=lens_id)
        out[f"cards.{lens_id}.json"] = lens_catalog
        for card in lens_catalog["cards"]:
            kind, target = card.get("kind"), card.get("target")
            if kind and target:
                out[f"card/{kind}/{enc(target)}.{lens_id}.json"] = card
    ids = run_ids if run_ids is not None else [r.run_id for r in full_store.list_runs(limit=None)]
    for rid in ids:
        out.update(run_payloads(full_store, rid, tasks_dir, groups_file))
    return _plain(out)


def run_payloads(
    store: RunStore, run_id: str, tasks_dir: Path, groups_file: Path | None = None,
) -> dict[str, Any]:
    """Detail, live and evidence keys for one run, rendered over the scrubbed
    tree so prompt and completion text never reach a hosted payload. A holdout
    run, or an unknown id, yields no keys."""
    meta = store.get_run(run_id)
    if meta is None:
        return {}
    gf = groups_file or Path(tasks_dir).parent / "groups.yaml"
    try:
        if _is_holdout_meta(meta):  # the index row alone can mark it
            raise HoldoutRunError(run_id)
        with tempfile.TemporaryDirectory(prefix="orch-snap-") as tmp:
            scrubbed = cf.scrub_to_dir(Path(meta.run_dir), Path(tmp))
            detail = cf._hosted_detail(store, run_id, scrubbed, Path(tasks_dir), gf)
            if detail is None:
                return {}
            evidence = state.run_evidence_payload(
                cast(RunStore, cf._ScrubbedStore(store, run_id, scrubbed)), run_id)
            live = state.live_payload(scrubbed, 0, started_at=meta.started_at, meta=meta)
    except HoldoutRunError:
        # No file of a holdout run is published, but its page still has to say why
        # every tab is empty: a stub detail with each section withheld, built from
        # the index row alone.
        stub = state.run_detail_payload(store, run_id, tasks_dir, gf, hosted=True)
        return {f"run/{run_id}.json": stub} if stub else {}
    live["cancellable"] = False
    out: dict[str, Any] = {f"run/{run_id}.json": detail, f"run/{run_id}/live.json": live}
    if evidence is not None:
        out[f"run/{run_id}/evidence.json"] = evidence
    return out


def write_snapshot(snapshot: dict[str, Any], out_dir: Path) -> int:
    """Write ``snapshot`` as ``<out_dir>/api/<key>``; returns the file count."""
    root = (Path(out_dir) / "api").resolve()
    for key, payload in snapshot.items():
        dest = (root / key).resolve()
        if not dest.is_relative_to(root) or dest == root:
            raise ValueError(f"snapshot key escapes api/: {key!r}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return len(snapshot)
