"""The model-catalog payload — ``#/models`` view data.

A separate module from ``state.py`` because the cluster is self-contained:
four source merges (configured / decisions / provider / ledger) with no
dependency on the observatory's other payload machinery — only the
``RunStore`` usage read and the model/config loaders.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestral import remotecatalog
from orchestral.config import load_models, load_yaml
from orchestral.judge import DEFAULT_JUDGE
from orchestral.storage import RunStore

_CATALOG_ROLES = ("orchestrator", "worker", "judge", "reference")
# Deliberately NOT _CATALOG_ROLES order: the table columns show judge
# before reference, but the row sort tails declared-judge rows below
# declared-reference ones — a model that only judges is less interesting
# than one that generates references.
_DECLARED_ROLE_SORT = {"orchestrator": 0, "worker": 1, "reference": 2, "judge": 3}
CATALOG_SOURCES = ("configured", "decisions", "provider", "ledger")
CATALOG_SOURCE_LABELS = {
    "configured": "models/*.yaml",
    "decisions": "disabled / decisions engines",
    "provider": "provider",
    "ledger": "ran, unconfigured",
}


def models_catalog_payload(store: RunStore, models_dir: Path) -> dict[str, Any]:
    """The model roster joined against what the ledger has actually seen.

    Each row answers two different questions, and the view keeps them
    visually separate:

    - **Qualified** — which roles the model may fill. Declared role plus
      ``judge`` (any model can judge, same rule as the launch form) plus
      any role it has already demonstrably run. `vision` widens judging to
      image artifacts; `modalities` bound which task types it can produce
      as a worker.
    - **Coverage** — runs/calls/spend per role from the calls ledger and
      run rows. A role with zero runs shows "not run" — that is the
      done/not-done axis, not a judgment about the model.

    Four sources, merged in priority order and marked by ``source``:
    ``configured`` (models/*.yaml), ``decisions`` (``~``-prefixed yaml
    entries — engines like jev that ``load_models`` drops by contract),
    ``provider`` (the synced provider list — ``harness.py models sync``),
    and ``ledger`` (slugs that ran but appear nowhere else). Rows never
    duplicate: first source wins the row, later sources only add usage.
    """
    try:
        models = list(load_models(models_dir))
    except Exception:
        models = []
    usage = store.model_role_usage()
    try:
        telemetry = store.model_telemetry()
    except Exception:
        telemetry = {}

    # Demonstrated roles count toward "qualified" only when they name a
    # real catalog role — a NULL-role call buckets as 'unknown' and a
    # stray role string is not a qualification claim.
    def _demonstrated(u: dict[str, Any]) -> set[str]:
        return set(u) & set(_CATALOG_ROLES)

    def _row(slug: str, source: str, u: dict[str, Any],
             **kw: Any) -> dict[str, Any]:
        # sparse usage — the view defaults missing roles to "not run"
        row: dict[str, Any] = {
            "slug": slug, "name": slug, "source": source,
            "declared_role": None, "qualified": [],
            "vision": False, "modalities": [], "executor": False,
            "structured": None, "free": False, "expires": None,
            "context": 0, "input_price_per_mtok": 0.0,
            "output_price_per_mtok": 0.0,
            "usage": u, "default": False,
            "telemetry": telemetry.get(slug) or {},
        }
        row.update(kw)
        return row

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in models:
        seen.add(m.slug)
        u = usage.get(m.slug, {})
        rows.append(_row(
            m.slug, "configured", u,
            name=m.name,
            declared_role=m.role,
            qualified=sorted({m.role, "judge"} | _demonstrated(u)),
            vision=bool(m.metadata.get("vision")),
            modalities=sorted(m.metadata.get("modalities") or []),
            executor=bool(m.metadata.get("executor")),
            context=m.context,
            input_price_per_mtok=m.input_price_per_mtok,
            output_price_per_mtok=m.output_price_per_mtok,
        ))

    # `~`-prefixed entries in models/*.yaml are skipped by load_models
    # (the prefix marks a disabled entry), but for the catalog they are
    # the disabled roster + decisions engines — read them raw so parked
    # models like the jev engines and gated watchlist entries stay
    # visible, plus the configured default if unlisted.
    engine_slugs = _tilde_entries(models_dir)
    engine_slugs.setdefault(DEFAULT_JUDGE, {})
    for slug in sorted(set(engine_slugs) - seen):
        meta = engine_slugs[slug]
        u = usage.get(slug, {})
        rows.append(_row(
            slug, "decisions", u,
            name=meta.get("name") or slug,
            declared_role=meta.get("role") or "judge",
            qualified=sorted({meta.get("role") or "judge", "judge"} | _demonstrated(u)),
            modalities=["text"],
            default=slug == DEFAULT_JUDGE,
        ))
        seen.add(slug)

    # The provider's own catalog — what *could* run, synced by
    # `harness.py models sync`. Capability-derived qualification only:
    # text output → can work/judge; structured params → plausible
    # orchestrator. Declared yaml roles stay the stronger signal.
    remote = remotecatalog.load_catalog(models_dir)
    remote_models = remote.get("models") if remote else []
    for rm in remote_models or []:
        # a hand-edited or corrupted snapshot must not 500 the endpoint —
        # validate each persisted row, not just the top-level list
        if not isinstance(rm, dict):
            continue
        mslug = rm.get("slug")
        if not isinstance(mslug, str) or not mslug or mslug in seen:
            continue
        seen.add(mslug)
        u = usage.get(mslug, {})
        modalities = rm.get("output_modalities")
        modalities = modalities if isinstance(modalities, list) else []
        qualified = _demonstrated(u)
        if "text" in modalities:
            qualified |= {"worker", "judge"}
        if rm.get("structured"):
            qualified.add("orchestrator")
        rows.append(_row(
            mslug, "provider", u,
            name=rm.get("name") or mslug,
            qualified=sorted(qualified),
            vision=bool(rm.get("vision")),
            modalities=modalities,
            structured=bool(rm.get("structured")),
            free=bool(rm.get("free")),
            expires=rm.get("expires"),
            context=rm.get("context") or 0,
            input_price_per_mtok=rm.get("input_price_per_mtok") or 0.0,
            output_price_per_mtok=rm.get("output_price_per_mtok") or 0.0,
        ))

    for slug in sorted(set(usage) - seen):
        u = usage[slug]
        rows.append(_row(
            slug, "ledger", u,
            qualified=sorted(_demonstrated(u) | {"judge"}),
        ))

    order = {s: i for i, s in enumerate(CATALOG_SOURCES)}
    rows.sort(key=lambda r: (
        order.get(r["source"], 4),
        _DECLARED_ROLE_SORT.get(r["declared_role"] or "", 4),
        str(r["slug"])))
    return {
        "roles": list(_CATALOG_ROLES),
        "sources": list(CATALOG_SOURCES),
        "source_labels": CATALOG_SOURCE_LABELS,
        "models": rows,
        "provider_synced_at": (remote or {}).get("fetched_at"),
        "provider_source": (remote or {}).get("source"),
        "provider_sync": {
            "state": "synced" if remote else "never",
            "synced_at": (remote or {}).get("fetched_at"),
            "source": (remote or {}).get("source"),
        },
    }


def _tilde_entries(models_dir: Path) -> dict[str, dict[str, Any]]:
    """``~``-prefixed entries inside models/*.yaml — disabled entries and
    decisions-engine declarations that ``load_models`` drops by contract.
    Returns slug → raw entry so the catalog keeps their declared role/name."""
    out: dict[str, dict[str, Any]] = {}
    try:
        files = list(Path(models_dir).glob("*.yaml"))
    except OSError:
        return out
    for f in files:
        try:
            data = load_yaml(f) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for item in data.get("models", []) or []:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or "")
            if slug.startswith("~"):
                out[slug] = item
    return out
