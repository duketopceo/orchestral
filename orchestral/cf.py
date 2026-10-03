"""Push the observatory's rendered payloads and scrubbed artifacts to the
hosted mirror (obs.shippedit.dev → Cloudflare Worker → R2/D1).

The sync unit is the *rendered payload*, not the row: the same
``state.*_payload`` functions the local server calls produce the JSON
snapshots pushed here, so hosted parity comes from sharing the code path,
not re-deriving it.

Two egress channels share the scrub boundary:

- **Artifacts** — only the output tree of ``privacy.scrub_run`` is ever
  uploaded. ``HoldoutRunError`` means push nothing for that run.
- **Rows** — the D1 projection is an explicit column allowlist. Call
  bodies (``input_json``/``output_json``), ``run_dir``, and free-text
  error payloads never leave the machine. Retained free text goes through
  ``privacy.scrub_dict``.

Auth env vars (resolved by the caller, never stored in the repo):

- ``ORCHESTRAL_OBS_URL`` — mirror base URL (default obs.shippedit.dev)
- ``ORCHESTRAL_CF_ID`` / ``ORCHESTRAL_CF_SECRET`` — Access service-token
  pair for ``/ingest`` (omaseal ``cloudflare/obs-ingest``,
  ``client_id:client_secret``)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx

from orchestral import privacy
from orchestral.privacy import HoldoutRunError
from orchestral.storage import RunStore
from orchestral.web import catalog, state

OBS_URL = os.environ.get("ORCHESTRAL_OBS_URL", "https://obs.shippedit.dev").rstrip("/")

# ---------------------------------------------------------------------------
# Publication projections — explicit allowlists, nothing wider ever syncs.
# ---------------------------------------------------------------------------

# D1 `runs` projection: ledger fields only. run_dir/config/env stay local —
# run_dir is a host path, config/env are free text that can embed provider
# endpoints and local layout.
RUN_COLUMNS = (
    "run_id", "orchestrator", "task_id", "worker", "status",
    "started_at", "finished_at", "total_cost_usd",
    "total_input_tokens", "total_output_tokens", "score", "passes",
    "judge_score", "judge_passed", "latency_ms", "failure_reason",
    "run_group", "replicate", "dry_run", "delegated",
)

# D1 `calls` projection: size/cost ledger. input_json/output_json/error are
# deliberately absent — bodies and provider-controlled text never sync.
CALL_COLUMNS = (
    "call_id", "run_id", "phase", "step", "role", "model",
    "input_tokens", "output_tokens", "cost_usd", "api_cost_usd",
    "pricing_source", "latency_ms", "attempt", "error_category",
    "sequence", "worker_id", "dry_run", "created_at", "finish_reason",
)

ANNOTATION_COLUMNS = ("kind", "target", "flag", "note", "updated_at")


@dataclass
class PushResult:
    pushed: list[str] = field(default_factory=list)
    skipped_holdout: list[str] = field(default_factory=list)
    skipped_missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class SyncError(RuntimeError):
    """A non-fatal push failure — the driver warns and retries next run."""


# ---------------------------------------------------------------------------
# Payload rendering — same functions the local server calls.
# ---------------------------------------------------------------------------

def global_payloads(store: RunStore, tasks_dir: Path, models_dir: Path,
                    groups_file: Path) -> dict[str, Any]:
    """The api/*.json snapshot tree minus per-run payloads."""
    registry = state.JobRegistry(Path(store.root), tasks_dir, models_dir, store)
    experiments_dir = Path(tasks_dir).parent / "experiments"
    out: dict[str, Any] = {
        "api/overview.json": state.overview_payload(
            store, registry, tasks_dir=tasks_dir, groups_file=groups_file),
        "api/runs.json": state.runs_payload(store, tasks_dir=tasks_dir),
        "api/groups.json": state.groups_payload(store, groups_file),
        "api/matrix.json": state.task_matrix_payload(store, tasks_dir),
        "api/leaderboard.json": state.leaderboard_rows(store),
        "api/pairings.json": state.pairings_payload(store, tasks_dir=tasks_dir),
        "api/flags.json": store.annotations(),
        "api/cards.json": state.card_catalog_payload(
            store, tasks_dir=tasks_dir, groups_file=groups_file),
        "api/models-catalog.json": catalog.models_catalog_payload(store, models_dir),
        "api/tasks.json": state.task_choices(tasks_dir),
        "api/models.json": state.model_choices(models_dir, None),
    }
    if experiments_dir.is_dir():
        for spec in sorted(experiments_dir.glob("*.yaml")):
            payload = state.experiment_payload(store, spec, tasks_dir=tasks_dir)
            if payload is not None:
                out[f"api/experiment.{spec.stem}.json"] = payload
    return out


def _ledger_calls(store: RunStore, run_id: str) -> list[dict[str, Any]]:
    """calls_for_run projected to the body-free ledger shape."""
    return [
        {k: row.get(k) for k in CALL_COLUMNS}
        for row in store.calls_for_run(run_id)
    ]


class _ScrubbedStore:
    """Store shim that answers get_run with the scrubbed run dir.

    ``run_detail_payload``/``run_evidence_payload`` read report.json,
    events.jsonl (timeline + transcript), and artifact bytes straight out
    of ``meta.run_dir``. Pointed at the raw dir they would serialize
    prompt/completion text the scrubber withholds; pointed at the scrubbed
    tree they emit exactly what the scrubber already approved for
    publication — same code path, sanitized input.
    """

    def __init__(self, store: RunStore, run_id: str, scrubbed_dir: Path):
        self._store = store
        self._run_id = run_id
        self._dir = str(scrubbed_dir)

    def get_run(self, run_id: str):
        meta = self._store.get_run(run_id)
        if meta is not None and run_id == self._run_id:
            from dataclasses import replace
            meta = replace(meta, run_dir=self._dir)
        return meta

    def __getattr__(self, name: str):
        return getattr(self._store, name)


def _hosted_detail(store: RunStore, run_id: str, scrubbed: Path, tasks_dir: Path,
                   groups_file: Path) -> dict[str, Any] | None:
    """run_detail_payload over the scrubbed tree + body-free calls ledger."""
    shim = cast(RunStore, _ScrubbedStore(store, run_id, scrubbed))
    payload = state.run_detail_payload(
        shim, run_id, tasks_dir=tasks_dir, groups_file=groups_file)
    if payload is None:
        return None
    payload["calls"] = _ledger_calls(store, run_id)
    return payload


# ---------------------------------------------------------------------------
# Scrub + artifact collection
# ---------------------------------------------------------------------------

def scrub_to_dir(run_dir: Path, dst_root: Path) -> Path:
    """Run privacy.scrub_run into dst_root; returns the scrubbed run dir."""
    return privacy.scrub_run(run_dir, dst_root)


def _collect_files(scrubbed: Path) -> dict[str, str]:
    """{relative posix path: base64} for every file in the scrubbed tree."""
    files: dict[str, str] = {}
    for f in sorted(scrubbed.rglob("*")):
        if f.is_file():
            files[f.relative_to(scrubbed).as_posix()] = base64.b64encode(
                f.read_bytes()).decode()
    return files


def manifest_hash(run_dir: Path) -> str:
    """Content hash over the scrub-publishable file set.

    report.json/events.jsonl legitimately change post-finish (judge
    backfill, revalidation) — hashing the allowlisted sources detects when
    the R2 artifact tree needs re-push independent of the sqlite watermark.
    """
    h = hashlib.sha256()
    for f in sorted(run_dir.iterdir(), key=lambda p: p.name):
        if not (f.is_file() and privacy._is_allowed(f.name)):
            continue
        h.update(f.name.encode())
        h.update(b"\0")
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# D1 projection
# ---------------------------------------------------------------------------

def d1_projection(store: RunStore, run_ids: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Allowlisted row projections for the reconciliation ledger."""
    runs: list[dict[str, Any]] = []
    metas = (
        [m for m in (store.get_run(r) for r in run_ids) if m is not None]
        if run_ids is not None else store.list_runs(limit=None)
    )
    for m in metas:
        if privacy.run_is_holdout(Path(m.run_dir)):
            continue  # holdout runs never leave the machine, even as rows
        row = m.to_public_dict()
        runs.append({k: row.get(k) for k in RUN_COLUMNS} | {"holdout": 0})
    calls: list[dict[str, Any]] = []
    for m in metas:
        if privacy.run_is_holdout(Path(m.run_dir)):
            continue
        calls.extend(_ledger_calls(store, m.run_id))
    annotations = [
        {k: privacy.scrub_dict(v) if k == "note" else v
         for k, v in row.items() if k in ANNOTATION_COLUMNS}
        for row in store.annotations()
    ]
    return {"runs": runs, "calls": calls, "annotations": annotations}


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------

def _ingest_headers() -> dict[str, str]:
    """Access service-token pair for /ingest."""
    pair = os.environ.get("ORCHESTRAL_OBS_TOKEN", "")
    if ":" in pair:
        client_id, secret = pair.split(":", 1)
    else:
        client_id = os.environ.get("ORCHESTRAL_CF_ID", "")
        secret = os.environ.get("ORCHESTRAL_CF_SECRET", "")
    if not client_id or not secret:
        raise SyncError(
            "ingest credentials unset — export ORCHESTRAL_OBS_TOKEN "
            "($client_id:$client_secret) or ORCHESTRAL_CF_ID/ORCHESTRAL_CF_SECRET"
        )
    return {
        "CF-Access-Client-Id": client_id,
        "CF-Access-Client-Secret": secret,
        "User-Agent": "orchestral-sync/1.0",
    }


def ingest_client() -> httpx.Client:
    """Shared httpx client for /ingest pushes (post-run hooks, sync)."""
    return httpx.Client(headers=_ingest_headers())


def _post(client: httpx.Client, path: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        r = client.post(f"{OBS_URL}{path}", json=body, timeout=120)
    except httpx.HTTPError as exc:
        raise SyncError(f"{path}: transport failed: {exc}") from exc
    if r.status_code != 200:
        raise SyncError(f"{path}: HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except json.JSONDecodeError:
        return {}


def push_run(client: httpx.Client, store: RunStore, meta: Any,
             tasks_dir: Path, groups_file: Path) -> dict[str, Any]:
    """Scrub + payload + ledger for one run → POST /ingest/run."""
    run_dir = Path(meta.run_dir)
    with tempfile.TemporaryDirectory(prefix="orch-scrub-") as tmp:
        scrubbed = scrub_to_dir(run_dir, Path(tmp))
        files = _collect_files(scrubbed)
        detail = _hosted_detail(store, meta.run_id, scrubbed, tasks_dir, groups_file)
        evidence = state.run_evidence_payload(
            cast(RunStore, _ScrubbedStore(store, meta.run_id, scrubbed)),
            meta.run_id)
    body: dict[str, Any] = {
        "run_id": meta.run_id,
        "payloads": {},
        "d1": d1_projection(store, [meta.run_id]),
        "files": {f"runs/{meta.run_id}/{name}": b64
                  for name, b64 in files.items()},
        "manifest_hash": manifest_hash(run_dir),
    }
    if detail is not None:
        body["payloads"][f"api/run/{meta.run_id}.json"] = detail
    if evidence is not None:
        body["payloads"][f"api/run/{meta.run_id}/evidence.json"] = evidence
    return _post(client, "/ingest/run", body)


def push_state(client: httpx.Client, store: RunStore, tasks_dir: Path,
               models_dir: Path, groups_file: Path) -> dict[str, Any]:
    """Global payload snapshot + full ledger projection → POST /ingest/state."""
    return _post(client, "/ingest/state", {
        "payloads": global_payloads(store, tasks_dir, models_dir, groups_file),
        "d1": d1_projection(store),
    })


def push_run_events_only(client: httpx.Client, store: RunStore, meta: Any) -> dict[str, Any]:
    """Lightweight post-run hook: one run's row + calls ledger, no artifacts.

    Keeps hosted row state fresh after every finish/failure; the heavy
    scrub+artifact push happens on `harness.py sync`.
    """
    return _post(client, "/ingest/run", {
        "run_id": meta.run_id,
        "payloads": {},
        "d1": d1_projection(store, [meta.run_id]),
        "files": {},
        "manifest_hash": None,
    })


# ---------------------------------------------------------------------------
# Dirty-set orchestration
# ---------------------------------------------------------------------------

def sync(store: RunStore, tasks_dir: Path, models_dir: Path, groups_file: Path,
         *, push: bool = False, all_runs: bool = False) -> PushResult:
    """Render + push the dirty set (or everything with --all).

    Dry run (push=False) renders nothing remote — it reports what would
    move so the operator can see the dirty set without credentials.
    """
    result = PushResult()
    dirty = {r.run_id for r in store.list_runs(limit=None)} if all_runs else store.dirty_runs()
    metas = [m for rid in dirty if (m := store.get_run(rid)) is not None]
    with httpx.Client(headers=_ingest_headers() if push else {}) as client:
        for meta in metas:
            try:
                if privacy.run_is_holdout(Path(meta.run_dir)):
                    result.skipped_holdout.append(meta.run_id)
                    continue
                if not Path(meta.run_dir).is_dir():
                    result.skipped_missing.append(meta.run_id)
                    continue
                if push:
                    push_run(client, store, meta, tasks_dir, groups_file)
                result.pushed.append(meta.run_id)
            except HoldoutRunError:
                result.skipped_holdout.append(meta.run_id)
            except (SyncError, OSError) as exc:
                result.errors.append(f"{meta.run_id}: {exc}")
        if push:
            try:
                push_state(client, store, tasks_dir, models_dir, groups_file)
            except SyncError as exc:
                result.errors.append(f"state: {exc}")
        # Journal entries clear only on a clean push — errors stay dirty so
        # the next sync retries them. Holdout/missing clear too: they are
        # terminal, not transient.
        if push and not result.errors:
            store.clear_dirty(dirty)
    return result


def verify(store: RunStore) -> dict[str, Any]:
    """Diff local counts/sums against the hosted api/runs.json snapshot."""
    remote = httpx.get(f"{OBS_URL}/api/runs.json", timeout=30,
                       headers={"User-Agent": "orchestral-sync/1.0"})
    remote.raise_for_status()
    body = remote.json()
    remote_runs = body if isinstance(body, list) else body.get("runs", [])
    local = store.list_runs(limit=None)
    hosted_ids = {r["run_id"] for r in remote_runs}
    local_ids = {r.run_id for r in local}
    return {
        "remote_runs": len(remote_runs),
        "local_runs": len(local),
        "missing_remote": sorted(local_ids - hosted_ids),
        "remote_only": sorted(hosted_ids - local_ids),
        "local_total_cost": sum(r.total_cost_usd for r in local),
        "remote_total_cost": sum(r.get("total_cost_usd") or 0 for r in remote_runs),
    }
