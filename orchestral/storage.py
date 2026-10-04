"""Filesystem + SQLite storage for orchestral runs.

Each run lives in its own directory:

    runs/{orchestrator_slug}/{task_id}/{worker_slug}/{run_id}/
        run.json       run metadata (status, cost, tokens, score, paths)
        events.jsonl   every agent action, reasoning, tool call, cost, latency
        plan.json      the orchestrator's decomposition
        artifact.*     the assembled final artifact
        cost.json      per-call and total cost breakdown
        report.json    validation/judge results

A global SQLite index at runs/index.db makes sorting/filtering fast for the
CLI and the web UI without walking the filesystem every time.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import threading
import uuid
from collections.abc import Iterable
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUNS_DIR = Path("runs")
DB_NAME = "index.db"

# Judge-cache payload version — bump when the result shape or the input
# contract changes so stale records go cold on read instead of being
# trusted. v1 was the bare result dict (pre-inconclusive rule); v2 added
# the inconclusive rule; v3 is judge_contract v2 — per-criterion
# {satisfied, supported, evidence} triples (wandr pattern).
JUDGE_CACHE_SCHEMA = 3

# Byte cap on a call's prompt/completion body when a caller wants a *preview*
# (the web observatory) rather than the ledger (dataset export, the TUI).
# `input_json` holds the whole `{"messages": [...]}` prompt, so an unbounded
# read hands every prompt ever sent to whoever can open the endpoint. The cap
# is applied in SQL by `call_previews`, not to the response afterwards, so the
# bytes never leave SQLite in the first place.
CALL_PREVIEW_MAX_BYTES = 2000

# A model needs this many priced non-dry-run calls before its own
# billed/rate-card ratio is trusted; below it the all-model ratio is used.
MIN_OWN_RATIO_CALLS = 20

# `RunMeta.cost_basis` values: every call priced by the provider, only some,
# or none (rate card scaled by a calibration ratio).
COST_BASES = ("billed", "mixed", "calibrated")


@dataclass(frozen=True)
class RatioChoice:
    """The billed/rate-card multiple chosen for one model, and why.

    ``source`` is ``own`` (the model's own priced calls), ``global`` (all
    priced calls, because the model has fewer than ``MIN_OWN_RATIO_CALLS``) or
    ``none`` (nothing was ever priced). ``n`` is the number of priced calls the
    ratio rests on.
    """

    ratio: float | None
    source: str
    n: int


@dataclass(frozen=True)
class Calibration:
    """Per-model billed/rate-card ratios derived from priced calls (KTD7).

    The ratio divides stored ``api_cost_usd`` by stored ``cost_usd``, and it is
    applied to a stored ``cost_usd``: both sides share the basis a call was
    recorded with, so editing a model's yaml rates later changes nothing here.
    """

    per_model: dict[str, tuple[int, float, float]]  # model -> (priced calls, api sum, cost sum)

    def _ratio(self, calls: int, api: float, cost: float) -> float | None:
        return api / cost if calls and cost > 0 else None

    def global_choice(self) -> RatioChoice:
        n = sum(v[0] for v in self.per_model.values())
        api = sum(v[1] for v in self.per_model.values())
        cost = sum(v[2] for v in self.per_model.values())
        ratio = self._ratio(n, api, cost)
        return RatioChoice(ratio, "global" if ratio is not None else "none", n)

    def ratio_for(self, model: str | None) -> RatioChoice:
        n, api, cost = self.per_model.get(model or "", (0, 0.0, 0.0))
        own = self._ratio(n, api, cost)
        if own is not None and n >= MIN_OWN_RATIO_CALLS:
            return RatioChoice(own, "own", n)
        return self.global_choice()


@dataclass(frozen=True)
class BilledEstimate:
    """Billed-cost estimate for one launch. ``per_run_usd`` is None when the
    index holds no billed history to estimate from (unknown, not zero)."""

    per_run_usd: float | None
    low_usd: float | None
    high_usd: float | None
    n: int
    basis: str  # task_pairing | pairing | unknown


def _like_escape(prefix: str) -> str:
    """Escape ``%``/``_``/``\\`` for a ``LIKE … ESCAPE '\\'`` prefix match —
    a matrix named ``jev_ab`` must not match ``jevxab`` groups."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _bounded_body(raw: Any, total: Any) -> tuple[str, int, bool]:
    """Decode a `substr(CAST(col AS BLOB), 1, cap)` slice, marked if it was cut.

    `raw` is the leading bytes SQLite already capped, `total` the true byte
    count. Returns `(text, total, truncated)`; a body at or under the cap comes
    back unchanged, so a healthy prompt is never mangled by the cap. The
    truncated flag has to come from the length comparison — the caller cannot
    infer it from `raw`, which is a string either way.
    """
    if raw is None:
        return "", int(total or 0), False
    text = (raw.decode("utf-8", errors="ignore")
            if isinstance(raw, bytes) else str(raw))
    size = int(total or 0)
    if size <= len(raw):
        return text, size, False
    return f"{text}…[truncated {size - len(raw)} of {size} bytes]", size, True


@dataclass
class RunMeta:
    run_id: str
    orchestrator: str
    task_id: str
    worker: str
    status: str
    started_at: str
    finished_at: str | None = None
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    score: float | None = None
    passes: bool | None = None
    run_dir: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    failure_reason: str | None = None
    env: dict[str, Any] = field(default_factory=dict)
    run_group: str | None = None
    replicate: int | None = None
    dry_run: bool = False
    judge_score: float | None = None
    judge_passed: bool | None = None
    delegated: bool | None = None
    # Read-side only, filled from `calls` by RunStore.list_runs/get_run. They
    # are not columns and never serialised, so run.json, exports and every
    # --json output keep their shape. None = not computed (in-memory metas).
    billed_cost_usd: float | None = field(default=None, repr=False, compare=False)
    cost_basis: str | None = field(default=None, repr=False, compare=False)

    @property
    def display_cost_usd(self) -> float:
        """Billed spend when known, else the recorded rate-card total."""
        return self.total_cost_usd if self.billed_cost_usd is None else self.billed_cost_usd

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("billed_cost_usd", None)
        d.pop("cost_basis", None)
        return d

    def to_public_dict(self) -> dict[str, Any]:
        """`to_dict()` minus `run_dir`.

        `run_dir` is the store's own filesystem path, so it is an absolute
        host path on any store created from an absolute root. It is an
        implementation detail the web observatory has no use for, and
        publishing it hands out local filesystem layout. `to_dict()` stays
        for the on-disk `run.json`, where the real path must survive.
        """
        d = self.to_dict()
        d.pop("run_dir", None)
        return d


class RunStore:
    """Create run directories and keep an SQLite index."""

    def __init__(self, root: str | Path = RUNS_DIR):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / DB_NAME
        self._judge_locks: dict[tuple[str, str, str], threading.Lock] = {}
        self._judge_locks_mu = threading.Lock()
        self._init_db()

    @contextmanager
    def _connect(self):
        """Yield a connection that commits on success and always closes."""
        with closing(sqlite3.connect(self.db, timeout=30.0)) as conn, conn:
            yield conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            # WAL so parallel runners can write the index concurrently
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    orchestrator TEXT,
                    task_id TEXT,
                    worker TEXT,
                    status TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    total_cost_usd REAL,
                    total_input_tokens INTEGER,
                    total_output_tokens INTEGER,
                    score REAL,
                    passes INTEGER,
                    run_dir TEXT,
                    config TEXT,
                    latency_ms REAL,
                    failure_reason TEXT,
                    env TEXT,
                    run_group TEXT,
                    replicate INTEGER,
                    dry_run INTEGER
                )
                """
            )
            # Migrate pre-v2 indexes: columns are appended at the END so
            # positional reads in _row_to_meta stay valid for both schemas.
            # The check-then-ALTER can race a second process doing the same
            # upgrade, so a duplicate-column error is treated as already-done.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
            for name, decl in _RUN_COLUMNS_V2:
                if name not in cols:
                    try:
                        conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {decl}")
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    phase TEXT,
                    step INTEGER,
                    role TEXT,
                    model TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cost_usd REAL,
                    api_cost_usd REAL,
                    pricing_source TEXT,
                    latency_ms REAL,
                    attempt INTEGER,
                    error_category TEXT,
                    error TEXT,
                    dry_run INTEGER,
                    created_at TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_run ON calls(run_id)")
            # calls v2: worker_id + sequence so live views can order calls and
            # group them per worker without re-parsing events.jsonl
            call_cols = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
            for name, decl in _CALL_COLUMNS_V2 + _CALL_COLUMNS_V3:
                if name not in call_cols:
                    try:
                        conn.execute(f"ALTER TABLE calls ADD COLUMN {name} {decl}")
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise
            for column in ("orchestrator", "worker", "task_id", "status", "total_cost_usd", "score"):
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{column} ON runs({column})"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS judge_cache (
                    task_id TEXT NOT NULL,
                    judge_slug TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, judge_slug, artifact_sha256)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS annotations (
                    kind TEXT NOT NULL,
                    target TEXT NOT NULL,
                    flag TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (kind, target)
                )
                """
            )
            # Sync journal: finished_at alone misses post-finish mutation
            # (judge backfill, revalidate, calls backfill, annotations), so
            # every mutation funnel marks the run dirty for the next
            # `harness.py sync` push. Rows clear only on a clean push.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_dirty (
                    run_id TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    dirty_at TEXT NOT NULL
                )
                """
            )

    def new_run(
        self,
        orchestrator: str,
        task_id: str,
        worker: str,
        config: dict[str, Any] | None = None,
        *,
        run_group: str | None = None,
        replicate: int | None = None,
        env: dict[str, Any] | None = None,
    ) -> tuple[str, Path]:
        """Create a new run directory and index entry."""
        run_id = uuid.uuid4().hex[:12]
        now = datetime.now(UTC).isoformat()
        # slugify every component so ids can't escape the runs root
        safe_orch = _safe_name(orchestrator)
        safe_worker = _safe_name(worker)
        safe_task = _safe_name(task_id)
        run_dir = self.root / safe_orch / safe_task / safe_worker / run_id
        if not run_dir.resolve().is_relative_to(self.root.resolve()):
            raise ValueError(f"Unsafe run path derived from task/model ids: {run_dir}")
        run_dir.mkdir(parents=True, exist_ok=True)

        meta = RunMeta(
            run_id=run_id,
            orchestrator=orchestrator,
            task_id=task_id,
            worker=worker,
            status="running",
            started_at=now,
            run_dir=str(run_dir),
            config=config or {},
            run_group=run_group,
            replicate=replicate,
            env=env or {},
            # indexed so spend meters can exclude dry runs without parsing
            # the config blob on every query
            dry_run=bool((config or {}).get("dry_run")),
        )
        self._write_meta_file(run_dir, meta)
        self.index_meta(meta)
        return run_id, run_dir

    def _write_meta_file(self, run_dir: Path, meta: RunMeta) -> None:
        (run_dir / "run.json").write_text(json.dumps(meta.to_dict(), indent=2, default=str))

    def index_meta(self, meta: RunMeta) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO runs (
                    run_id, orchestrator, task_id, worker, status,
                    started_at, finished_at, total_cost_usd,
                    total_input_tokens, total_output_tokens, score, passes,
                    run_dir, config, latency_ms, failure_reason, env,
                    run_group, replicate, dry_run, judge_score, judge_passed,
                    delegated
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    meta.run_id,
                    meta.orchestrator,
                    meta.task_id,
                    meta.worker,
                    meta.status,
                    meta.started_at,
                    meta.finished_at,
                    meta.total_cost_usd,
                    meta.total_input_tokens,
                    meta.total_output_tokens,
                    meta.score,
                    int(meta.passes) if meta.passes is not None else None,
                    meta.run_dir,
                    json.dumps(meta.config, default=str),
                    meta.latency_ms,
                    meta.failure_reason,
                    json.dumps(meta.env, default=str),
                    meta.run_group,
                    meta.replicate,
                    int(meta.dry_run),
                    meta.judge_score,
                    int(meta.judge_passed) if meta.judge_passed is not None else None,
                    int(meta.delegated) if meta.delegated is not None else None,
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO sync_dirty (run_id, reason, dirty_at)"
                " VALUES (?, 'meta', ?)",
                (meta.run_id, datetime.now(UTC).isoformat()),
            )

    def record_call(
        self,
        *,
        run_id: str,
        phase: str | None,
        step: int | None,
        role: str | None,
        model: str | None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        api_cost_usd: float | None = None,
        pricing_source: str | None = None,
        latency_ms: float = 0.0,
        attempt: int | None = None,
        error_category: str | None = None,
        error: str | None = None,
        dry_run: bool = False,
        worker_id: str | None = None,
        sequence: int | None = None,
        input_json: str | None = None,
        output_json: str | None = None,
        finish_reason: str | None = None,
    ) -> None:
        """Index one call-level event (llm_call or worker_error)."""
        with self._connect() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
            names = [
                "run_id", "phase", "step", "role", "model", "input_tokens",
                "output_tokens", "cost_usd", "api_cost_usd", "pricing_source",
                "latency_ms", "attempt", "error_category", "error", "dry_run",
                "created_at",
            ]
            values: list[Any] = [
                run_id, phase, step, role, model, input_tokens,
                output_tokens, cost_usd, api_cost_usd, pricing_source,
                latency_ms, attempt, error_category,
                (error or "")[:500] or None, int(dry_run),
                datetime.now(UTC).isoformat(),
            ]
            if "worker_id" in cols:
                names.append("worker_id")
                values.append(worker_id)
            if "sequence" in cols:
                names.append("sequence")
                values.append(sequence)
            if "input_json" in cols:
                names.append("input_json")
                values.append(input_json)
            if "output_json" in cols:
                names.append("output_json")
                values.append(output_json)
            if "finish_reason" in cols:
                names.append("finish_reason")
                values.append(finish_reason)
            conn.execute(
                f"INSERT INTO calls ({', '.join(names)}) "
                f"VALUES ({', '.join('?' for _ in names)})",
                values,
            )

    def calls_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """The full ledger, prompt and completion bodies included.

        Deliberately unbounded: `dataset.py` exports these bodies and the TUI
        shows them. Anything serving a caller that is not the local operator
        wants `call_previews` instead.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM calls WHERE run_id = ? ORDER BY call_id", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def call_previews(
        self, run_id: str, *, max_bytes: int = CALL_PREVIEW_MAX_BYTES
    ) -> list[dict[str, Any]]:
        """`calls_for_run` with each body cut to `max_bytes` of leading bytes,
        plus a truncation marker, for callers that render a run rather than
        export it.

        Two differences from `calls_for_run`, both load-bearing:

        - Explicit column list. `SELECT *` silently widens the payload when a
          migration appends a column, which is how a prompt column shipped
          once already.
        - The cut happens in SQL, on the byte-cast blob, so the body never
          crosses the process boundary whole. Capping the returned value
          instead would leave the disclosure intact for the next caller and
          make the guarantee a claim rather than a bound. `substr` on the BLOB
          cast is also a byte bound: `length()` on TEXT counts characters, so
          a CJK prompt would slip through at three times the budget.

        Adds `input_bytes`/`output_bytes` (true size) and
        `input_truncated`/`output_truncated` so a reader can tell a short
        prompt from a cut one, and a marker carrying both numbers. A body at
        or under the cap is returned byte-for-byte.

        `error` rides through uncapped here because it is already bounded at
        the write — `record_call` and `backfill_calls` both store
        `(error or "")[:500]`, under the cap. If that write-side bound is ever
        lifted, this projection needs one too.

        The cut keeps the *leading* bytes, so this bounds size, it does not
        redact: whatever sits in the first `max_bytes` of a body still ships.
        Callers that need redaction have to omit the body, not shrink the cap.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT call_id, run_id, phase, step, role, model,
                       input_tokens, output_tokens, cost_usd, api_cost_usd,
                       pricing_source, latency_ms, attempt, error_category,
                       error, dry_run, created_at, worker_id, sequence,
                       finish_reason,
                       substr(CAST(input_json AS BLOB), 1, ?) AS input_json,
                       length(CAST(input_json AS BLOB)) AS input_bytes,
                       substr(CAST(output_json AS BLOB), 1, ?) AS output_json,
                       length(CAST(output_json AS BLOB)) AS output_bytes
                FROM calls WHERE run_id = ? ORDER BY call_id
                """,
                (max_bytes, max_bytes, run_id),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            for body, total in (("input_json", "input_bytes"),
                                ("output_json", "output_bytes")):
                text, size, truncated = _bounded_body(d.get(body), d[total])
                d[body] = text
                d[f"{body.rsplit('_', 1)[0]}_truncated"] = truncated
                d[total] = size
            out.append(d)
        return out

    def model_role_usage(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Per-(model, role) usage: call count, distinct runs, spend.

        Two sources, unioned on run_id: the ``calls`` ledger, which is the
        only place judge calls appear but which only exists once a call is
        recorded, and the ``runs`` row's orchestrator/worker columns, which
        cover runs that died before their first call. Cost comes from calls
        alone — the runs table has no per-role split. Dry runs are excluded:
        a stubbed call is not evidence the model ran.
        """
        usage: dict[str, dict[str, dict[str, Any]]] = {}

        def entry(model: Any, role: Any) -> dict[str, Any]:
            return usage.setdefault(str(model), {}).setdefault(
                str(role or "unknown"),
                {"calls": 0, "runs": 0, "cost_usd": 0.0, "errors": 0},
            )

        with self._connect() as conn:
            for model, role, n_calls, n_err, cost in conn.execute(
                "SELECT model, role, COUNT(*), "
                "SUM(CASE WHEN error IS NOT NULL AND error != '' THEN 1 "
                "ELSE 0 END), COALESCE(SUM(cost_usd), 0) "
                "FROM calls WHERE COALESCE(dry_run, 0) = 0 "
                "AND model IS NOT NULL AND model != '' "
                "GROUP BY model, role"
            ):
                e = entry(model, role)
                e["calls"] += n_calls
                e["errors"] += int(n_err or 0)
                e["cost_usd"] += float(cost or 0.0)
            # UNION dedups run_ids across the calls ledger and the
            # run-level orch/worker columns in one pass.
            for model, role, n_runs in conn.execute(
                "SELECT model, role, COUNT(DISTINCT run_id) FROM ("
                "  SELECT model, role, run_id FROM calls"
                "  WHERE COALESCE(dry_run, 0) = 0"
                "  AND model IS NOT NULL AND model != ''"
                "  UNION"
                "  SELECT orchestrator, 'orchestrator', run_id FROM runs"
                "  WHERE COALESCE(dry_run, 0) = 0 AND orchestrator IS NOT NULL"
                "  UNION"
                "  SELECT worker, 'worker', run_id FROM runs"
                "  WHERE COALESCE(dry_run, 0) = 0 AND worker IS NOT NULL"
                ") GROUP BY model, role"
            ):
                entry(model, role)["runs"] = n_runs
        return usage

    def backfill_calls(self, meta: RunMeta) -> int:
        """Rebuild a run's `calls` rows from its events.jsonl, payloads and all.

        Call rows written before the payload columns existed (or runs indexed
        before the calls table existed at all) carry no prompt/completion —
        events.jsonl is authoritative, so this replays it. Rows for the run
        are deleted and reinserted in event order; safe to re-run. Returns the
        number of call events indexed.
        """
        events_path = Path(meta.run_dir) / "events.jsonl"
        if not events_path.exists():
            # archived corpus (e.g. runs/ moved to runs-v1/): rebase the
            # recorded orch/task/worker/run_id tail under this store's root
            tail = Path(meta.run_dir).parts[-4:]
            rebased = self.root.joinpath(*tail) / "events.jsonl"
            if not rebased.exists():
                return 0
            events_path = rebased
        call_types = {"llm_call", "worker_error"}
        events: list[dict[str, Any]] = []
        with events_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue  # truncated tail of a killed run — skip, not fatal
                if ev.get("type") in call_types:
                    events.append(ev)
        rows = []
        for ev in events:
            cost = ev.get("cost") or {}
            out = ev.get("output") or {}
            rows.append((
                meta.run_id, ev.get("phase"), ev.get("step"), ev.get("role"),
                ev.get("model"), cost.get("input_tokens") or 0,
                cost.get("output_tokens") or 0, cost.get("usd") or 0.0,
                cost.get("api_cost_usd"), cost.get("pricing_source"),
                ev.get("latency_ms") or 0.0, out.get("attempt"),
                (ev.get("metadata") or {}).get("error_category"),
                (ev.get("error") or "")[:500] or None, int(meta.dry_run),
                datetime.now(UTC).isoformat(), ev.get("worker_id"),
                ev.get("sequence"),
                json.dumps(ev.get("input") or {}, default=str),
                json.dumps(out, default=str),
                out.get("finish_reason"),
            ))
        with self._connect() as conn:
            conn.execute("DELETE FROM calls WHERE run_id = ?", (meta.run_id,))
            conn.executemany(
                """
                INSERT INTO calls (
                    run_id, phase, step, role, model, input_tokens,
                    output_tokens, cost_usd, api_cost_usd, pricing_source,
                    latency_ms, attempt, error_category, error, dry_run,
                    created_at, worker_id, sequence, input_json, output_json,
                    finish_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.execute(
                "INSERT OR REPLACE INTO sync_dirty (run_id, reason, dirty_at)"
                " VALUES (?, 'calls', ?)",
                (meta.run_id, datetime.now(UTC).isoformat()),
            )
        return len(rows)

    def calls_pricing_summary(self) -> list[dict[str, Any]]:
        """Per-(model, pricing_source) aggregates for pricing-drift analysis.

        Dry-run rows are excluded — fake usage would pollute the comparison.
        Token and cost sums are enough to recompute configured-rate estimates
        because rates are per-model constants.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT model, pricing_source, COUNT(*) AS calls,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(cost_usd) AS cost_usd,
                       SUM(api_cost_usd) AS api_cost_usd
                FROM calls
                WHERE dry_run = 0 AND model IS NOT NULL
                GROUP BY model, pricing_source
                ORDER BY model
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def unmetered_workers(self) -> set[str]:
        """Model slugs whose calls are declared `pricing_source="unmetered"`.

        Leaderboards must not read a $0 total as a free `cost_per_pass` —
        unmetered is detected here, never inferred from `cost_total == 0`
        (a legitimately cheap run is not unmetered). Dry-run rows excluded.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT model FROM calls "
                "WHERE pricing_source = 'unmetered' AND dry_run = 0 AND model IS NOT NULL"
            ).fetchall()
        return {r[0] for r in rows}

    def mean_cell_cost(
        self, task_id: str, orchestrator: str, worker: str, *, arm: str = "baseline"
    ) -> float | None:
        """Mean *rate-card* cost per finished run for one experiment cell arm.

        This is ``runs.total_cost_usd``, the harness's configured-rate estimate,
        not billed spend (billed spend is ``billed_estimate`` and
        ``RunMeta.billed_cost_usd``). Replicate planning keeps this basis on
        purpose so the targets of a matrix in flight do not shift.

        Task-scoped (``mean_run_cost`` is pairing-scoped — a task's cost
        profile dominates a pairing's). ``arm`` selects on the run's
        recorded ``config.jev_assist``; the baseline arm prices the pair's
        cheap side. Returns None when no finished runs match.
        """
        jev = 1 if arm == "jev" else 0
        with self._connect() as conn:
            row = conn.execute(
                "SELECT AVG(total_cost_usd) FROM runs "
                "WHERE task_id = ? AND orchestrator = ? AND worker = ? "
                "AND status = 'finished' AND COALESCE(dry_run, 0) = 0 "
                "AND COALESCE(json_extract(config, '$.jev_assist'), 0) = ?",
                (task_id, orchestrator, worker, jev),
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    # -- billed cost (KTD7) -------------------------------------------------

    def cost_calibration(self) -> Calibration:
        """Per-model billed/rate-card ratios over priced non-dry-run calls."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT COALESCE(model, ''), COUNT(*), SUM(api_cost_usd), SUM(cost_usd) "
                "FROM calls WHERE COALESCE(dry_run, 0) = 0 AND api_cost_usd IS NOT NULL "
                "GROUP BY COALESCE(model, '')"
            ).fetchall()
        return Calibration({m: (int(n), float(api or 0.0), float(cost or 0.0))
                            for m, n, api, cost in rows})

    def _billed_map(self, run_ids: list[str] | None = None) -> dict[str, tuple[float, str]]:
        """run_id -> (billed cost, basis) for the given runs, or every run.

        Billed cost is ``SUM(COALESCE(api_cost_usd, cost_usd * ratio(model)))``
        over a run's non-dry-run calls, failed runs included (``total_cost_usd``
        is left at 0 for them). A model with no usable ratio contributes its
        rate-card ``cost_usd`` unscaled, which is a floor. A dry run bills
        nothing. A run with no call rows at all falls back to its recorded
        ``total_cost_usd`` and is labelled ``calibrated`` (rate card).
        """
        cal = self.cost_calibration()
        marks = ",".join("?" * len(run_ids)) if run_ids is not None else ""
        call_scope = f" AND c.run_id IN ({marks})" if run_ids is not None else ""
        run_scope = f" WHERE run_id IN ({marks})" if run_ids is not None else ""
        params = list(run_ids or [])
        with self._connect() as conn:
            call_rows = conn.execute(
                "SELECT c.run_id, COALESCE(c.model, ''), COUNT(*), "
                "COALESCE(SUM(c.api_cost_usd IS NOT NULL), 0), COALESCE(SUM(c.api_cost_usd), 0.0), "
                "COALESCE(SUM(CASE WHEN c.api_cost_usd IS NULL THEN c.cost_usd END), 0.0) "
                "FROM calls c WHERE COALESCE(c.dry_run, 0) = 0" + call_scope +
                " GROUP BY c.run_id, COALESCE(c.model, '')", params
            ).fetchall()
            run_rows = conn.execute(
                "SELECT run_id, COALESCE(total_cost_usd, 0.0), COALESCE(dry_run, 0) FROM runs"
                + run_scope, params
            ).fetchall()
        acc: dict[str, list[Any]] = {}
        for run_id, model, n, priced, api_sum, unpriced_cost in call_rows:
            a = acc.setdefault(run_id, [0.0, False, False])  # billed, has_priced, has_unpriced
            a[0] += float(api_sum)
            a[1] = a[1] or priced > 0
            if priced < n:
                ratio = cal.ratio_for(model).ratio
                a[0] += float(unpriced_cost) * (ratio if ratio is not None else 1.0)
                a[2] = True
        out: dict[str, tuple[float, str]] = {}
        for run_id, total, dry in run_rows:
            if dry:
                out[run_id] = (0.0, "billed")
            elif run_id in acc:
                billed, has_priced, has_unpriced = acc[run_id]
                basis = ("billed" if not has_unpriced
                         else "mixed" if has_priced else "calibrated")
                out[run_id] = (round(billed, 10), basis)
            else:
                out[run_id] = (float(total), "calibrated")
        return out

    def billed_costs(self, run_ids: Iterable[str] | None = None) -> dict[str, tuple[float, str]]:
        return self._billed_map(None if run_ids is None else list(run_ids))

    def _attach_billed(self, metas: list[RunMeta]) -> None:
        if not metas:
            return
        ids = [m.run_id for m in metas]
        billed = self._billed_map(ids if len(ids) <= 500 else None)
        for m in metas:
            if m.run_id in billed:
                m.billed_cost_usd, m.cost_basis = billed[m.run_id]

    def _billed_spend(self, where: str, params: tuple[Any, ...]) -> float:
        with self._connect() as conn:
            ids = [r[0] for r in conn.execute(
                f"SELECT run_id FROM runs WHERE COALESCE(dry_run, 0) = 0 AND {where}", params)]
        if not ids:
            return 0.0
        billed = self._billed_map(ids if len(ids) <= 500 else None)
        return float(sum(billed[i][0] for i in ids if i in billed))

    def billed_estimate(
        self, task_id: str, orchestrator: str, worker: str, *,
        exclude_task_id: str | None = None,
    ) -> BilledEstimate:
        """Billed cost of one more run of this pairing, from past billed runs.

        Prefers the same task + pairing, then the pairing on any task. Finished
        and failed runs count (a failed run still spent money); dry runs,
        running and cancelled runs do not. The range is the 10th to 90th
        percentile of per-run billed cost, clamped to within 2x of the mean so
        ``high / low`` never exceeds 4. ``exclude_task_id`` drops that task's
        runs entirely, which is how the held-out backtest scores an estimate
        without the cell it predicts.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id, task_id FROM runs WHERE orchestrator = ? AND worker = ? "
                "AND status IN ('finished', 'failed') AND COALESCE(dry_run, 0) = 0",
                (orchestrator, worker)).fetchall()
        if exclude_task_id is not None:
            rows = [r for r in rows if r[1] != exclude_task_id]
        same_task = [r[0] for r in rows if r[1] == task_id]
        basis, ids = "unknown", []
        if same_task and exclude_task_id is None:
            basis, ids = "task_pairing", same_task
        elif rows:
            basis, ids = "pairing", [r[0] for r in rows]
        if not ids:
            return BilledEstimate(None, None, None, 0, "unknown")
        billed = self._billed_map(ids if len(ids) <= 500 else None)
        xs = sorted(billed[i][0] for i in ids if i in billed)
        mean = sum(xs) / len(xs)
        if len(xs) >= 5:
            q = statistics.quantiles(xs, n=10, method="inclusive")
            low, high = q[0], q[8]
        else:
            low, high = xs[0], xs[-1]
        low, high = min(low, mean), max(high, mean)
        low, high = max(low, mean / 2), min(high, mean * 2)
        return BilledEstimate(mean, low, high, len(xs), basis)

    def billed_total_usd(self) -> float:
        """Billed spend over every non-dry run in the index."""
        return self._billed_spend("1 = 1", ())

    def month_to_date_billed_usd(self, now: datetime | None = None) -> float:
        """Billed spend of non-dry runs started this calendar month (UTC)."""
        now = now or datetime.now(UTC)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return self._billed_spend("started_at >= ?", (start.isoformat(),))

    def group_spend(self, group_prefix: str) -> float:
        """Live billed spend meter for one experiment: every non-dry call of
        runs under ``group_prefix%``, at ``api_cost_usd`` where the provider
        priced it and at ``cost_usd`` scaled by the model's calibration ratio
        where it did not.

        Runs meter ``total_cost_usd`` at $0 until they finish and index;
        ``calls`` rows land per call during the run, so this sees in-flight
        spend that ``spend_today`` is blind to. Dry-run rows excluded.
        ``%``/``_`` in the prefix are escaped — a matrix named ``jev_ab``
        must not meter ``jevxab`` groups.
        """
        esc = _like_escape(group_prefix)
        cal = self.cost_calibration()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT COALESCE(c.model, ''), COALESCE(SUM(c.api_cost_usd), 0.0), "
                "COALESCE(SUM(CASE WHEN c.api_cost_usd IS NULL THEN c.cost_usd END), 0.0) "
                "FROM calls c JOIN runs r ON c.run_id = r.run_id "
                "WHERE r.run_group LIKE ? ESCAPE '\\' "
                "AND COALESCE(r.dry_run, 0) = 0 GROUP BY COALESCE(c.model, '')",
                (f"{esc}%",),
            ).fetchall()
        total = 0.0
        for model, api_sum, unpriced in rows:
            ratio = cal.ratio_for(model).ratio
            total += float(api_sum) + float(unpriced) * (ratio if ratio is not None else 1.0)
        return total

    def repair_orphan_costs(self, run_id: str) -> bool:
        """Recompute a run's cost/token totals from its ``calls`` rows.

        A run killed mid-flight meters ``total_cost_usd = 0`` forever —
        finalize never ran, so ``coverage_rows``, ``spend_today`` and
        ``mean_cell_cost`` all undercount it while its billed calls sit
        in the ledger. Recompute from the ledger; returns True when the
        row actually changed.
        """
        with self._connect() as conn:
            sums = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0), "
                "COALESCE(SUM(input_tokens), 0), "
                "COALESCE(SUM(output_tokens), 0) "
                "FROM calls WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            n = conn.execute(
                "UPDATE runs SET total_cost_usd = ?, total_input_tokens = ?, "
                "total_output_tokens = ? WHERE run_id = ? "
                "AND (total_cost_usd IS NULL OR total_cost_usd != ? "
                "OR total_input_tokens IS NULL OR total_input_tokens != ? "
                "OR total_output_tokens IS NULL OR total_output_tokens != ?)",
                (sums[0], sums[1], sums[2], run_id, sums[0], sums[1], sums[2]),
            ).rowcount
        if n:
            meta = self.get_run(run_id)
            # run_dir can be empty on rows that predate the column —
            # update_meta would write run.json into the caller's cwd
            if meta is not None and meta.run_dir:
                self.update_meta(meta)
        return bool(n)

    def missing_cost_count(
        self, *, run_id: str | None = None, group_prefix: str | None = None
    ) -> int:
        """Calls with no provider-reported cost.

        Keyed on ``api_cost_usd IS NULL`` against the pricing vocabulary:
        ``flat_estimate``/``configured_estimate`` are estimates by
        definition, ``cli_reported``/``api`` count when the provider
        returned no usage cost; ``unmetered``/``none`` are legitimately
        $0 and excluded. Dry-run rows excluded.
        """
        where = (
            "api_cost_usd IS NULL "
            "AND COALESCE(pricing_source, '') NOT IN ('unmetered', 'none') "
            "AND COALESCE(dry_run, 0) = 0"
        )
        params: tuple = ()
        if run_id is not None:
            where += " AND run_id = ?"
            params = (run_id,)
        elif group_prefix is not None:
            esc = _like_escape(group_prefix)
            where += (
                " AND run_id IN (SELECT run_id FROM runs "
                "WHERE run_group LIKE ? ESCAPE '\\')"
            )
            params = (f"{esc}%",)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM calls WHERE {where}", params
            ).fetchone()
        return int(row[0])

    def missing_cost_counts_by_group(self, group_prefix: str) -> dict[str, int]:
        """``missing_cost_count`` grouped by ``run_group`` — one pass for
        a whole matrix instead of a per-cell LIKE scan."""
        esc = _like_escape(group_prefix)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.run_group, COUNT(*) FROM calls c "
                "JOIN runs r ON c.run_id = r.run_id "
                "WHERE r.run_group LIKE ? ESCAPE '\\' "
                "AND c.api_cost_usd IS NULL "
                "AND COALESCE(c.pricing_source, '') NOT IN ('unmetered', 'none') "
                "AND COALESCE(c.dry_run, 0) = 0 "
                "GROUP BY r.run_group",
                (f"{esc}%",),
            ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    def set_annotation(
        self, kind: str, target: str, flag: str, note: str = ""
    ) -> dict[str, Any]:
        """Upsert a user annotation — the observatory's stateful layer.

        ``kind`` is ``run``, ``group``, ``pairing``, ``post`` (publication
        marks — latest wins, PK already ``(kind, target)``), or
        ``cell-state`` (driver-persisted experiment states such as
        ``aborted``). ``flag`` is ``interesting``, ``not``, ``posted``,
        ``aborted``, or ``''`` (clears the flag but keeps the note)."""
        if kind not in ("run", "group", "pairing", "post", "cell-state"):
            raise ValueError(
                "annotation kind must be run|group|pairing|post|cell-state, "
                f"got {kind!r}"
            )
        if flag not in ("interesting", "not", "posted", "aborted", ""):
            raise ValueError(
                f"flag must be interesting|not|posted|aborted|'', got {flag!r}"
            )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO annotations (kind, target, flag, note, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (kind, target) DO UPDATE SET
                    flag = excluded.flag,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (kind, target, flag, note, datetime.now(UTC).isoformat()),
            )
            if kind == "run":
                # run annotations change the hosted run detail payload;
                # other kinds only feed always-pushed global payloads.
                conn.execute(
                    "INSERT OR REPLACE INTO sync_dirty (run_id, reason, dirty_at)"
                    " VALUES (?, 'annotation', ?)",
                    (target, datetime.now(UTC).isoformat()),
                )
        return {"kind": kind, "target": target, "flag": flag, "note": note}

    def annotations(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT kind, target, flag, note, updated_at FROM annotations"
            ).fetchall()
        return [dict(r) for r in rows]

    def claim_aborted(self, run_id: str, note: str) -> bool:
        """Atomically mark a run ``aborted`` unless it already is —
        returns True only for the caller that won the claim.

        The aborted flag is the cross-process claim on an orphaned
        ``running`` row's replicate slot. set_annotation is a blind
        upsert, so two relaunchers can both observe "not aborted" and
        both write it; this form returns False for the loser instead of
        silently succeeding."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO annotations (kind, target, flag, note, updated_at)
                VALUES ('run', ?, 'aborted', ?, ?)
                ON CONFLICT (kind, target) DO UPDATE SET
                    flag = 'aborted',
                    note = excluded.note,
                    updated_at = excluded.updated_at
                WHERE annotations.flag != 'aborted'
                """,
                (run_id, note, datetime.now(UTC).isoformat()),
            )
            won = cur.rowcount == 1
            if won:
                conn.execute(
                    "INSERT OR REPLACE INTO sync_dirty (run_id, reason, dirty_at)"
                    " VALUES (?, 'annotation', ?)",
                    (run_id, datetime.now(UTC).isoformat()),
                )
        return won

    def debug_log(self, component: str, message: str, **fields: Any) -> None:
        """Append to the root-level runs/debug.jsonl for events that happen
        before a run directory exists (e.g. provider resolution failures)."""
        path = self.root / "debug.jsonl"
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "component": component,
            "message": message,
            "fields": fields,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def get_run(self, run_id: str) -> RunMeta | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        meta = _row_to_meta(row)
        meta.run_dir = self._anchor_run_dir(meta.run_dir)
        self._attach_billed([meta])
        return meta

    def _anchor_run_dir(self, run_dir: str) -> str:
        """Resolve a recorded run_dir that is relative to the launch cwd.

        The index stores `run_dir` as it was when the run started — relative
        (e.g. `runs/<orch>/<task>/<worker>/<id>`) for runs launched with a
        relative runs path. Read from another cwd that points nowhere, so the
        `<orch>/<task>/<worker>/<id>` tail is rebased under this store's root
        (the same rule `backfill_calls` and `web.state.resolve_run_dir` use).
        Existing and absolute paths pass through untouched.
        """
        if not run_dir:
            return run_dir
        p = Path(run_dir)
        if p.is_absolute() or p.exists():
            return run_dir
        cand = self.root.joinpath(*p.parts[-4:])
        return str(cand) if cand.exists() else run_dir

    def update_meta(self, meta: RunMeta) -> None:
        self._write_meta_file(Path(meta.run_dir), meta)
        self.index_meta(meta)

    def dirty_runs(self) -> set[str]:
        """Run ids mutated since the last clean hosted sync."""
        with self._connect() as conn:
            rows = conn.execute("SELECT run_id FROM sync_dirty").fetchall()
        return {r[0] for r in rows}

    def clear_dirty(
        self, run_ids: Iterable[str], *, before: str | None = None
    ) -> None:
        """Drop journal entries after a clean push; failures stay dirty.

        ``before`` is the sync's start watermark — a run re-dirtied while
        the push was in flight has a newer dirty_at and must NOT clear, or
        the mutation would never sync.
        """
        ids = list(run_ids)
        if not ids:
            return
        with self._connect() as conn:
            if before is None:
                conn.executemany(
                    "DELETE FROM sync_dirty WHERE run_id = ?",
                    [(r,) for r in ids],
                )
            else:
                conn.executemany(
                    "DELETE FROM sync_dirty WHERE run_id = ? AND dirty_at <= ?",
                    [(r, before) for r in ids],
                )

    def list_runs(
        self,
        orchestrator: str | None = None,
        worker: str | None = None,
        task_id: str | None = None,
        run_group: str | None = None,
        order_by: str = "started_at",
        descending: bool = True,
        limit: int | None = None,
    ) -> list[RunMeta]:
        query = "SELECT * FROM runs WHERE 1=1"
        params: list[Any] = []
        if orchestrator:
            query += " AND orchestrator = ?"
            params.append(orchestrator)
        if worker:
            query += " AND worker = ?"
            params.append(worker)
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if run_group is not None:
            query += " AND run_group = ?"
            params.append(run_group)
        if order_by not in _SORTABLE_COLUMNS:
            order_by = "started_at"
        query += f" ORDER BY {order_by} {'DESC' if descending else 'ASC'}"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        metas = [_row_to_meta(row) for row in rows]
        for m in metas:
            m.run_dir = self._anchor_run_dir(m.run_dir)
        self._attach_billed(metas)
        return metas

    def judge_lock(self, key: tuple[str, str, str]) -> threading.Lock:
        """Per-(task, judge, artifact) lock so parallel runners don't duplicate judge calls."""
        with self._judge_locks_mu:
            return self._judge_locks.setdefault(key, threading.Lock())

    def get_judge_result(self, task_id: str, judge_slug: str, artifact_sha256: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT result_json FROM judge_cache WHERE task_id = ? AND judge_slug = ? AND artifact_sha256 = ?",
                (task_id, judge_slug, artifact_sha256),
            ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        # pre-schema rows carry a bare result dict — a rubric/format change
        # must replay the call, not trust the old payload (e.g. records
        # written when parse failures were coerced into passed=false)
        if not isinstance(data, dict) or data.get("schema") != JUDGE_CACHE_SCHEMA:
            return None
        result = data.get("result")
        return result if isinstance(result, dict) else None

    def judge_slugs(self, task_ids: set[str]) -> list[str]:
        """Distinct judge models seen in the cache for these tasks — provenance
        fallback for runs judged before report.json recorded judge.model."""
        if not task_ids:
            return []
        marks = ",".join("?" for _ in task_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT judge_slug FROM judge_cache WHERE task_id IN ({marks})",
                sorted(task_ids),
            ).fetchall()
        return [r[0] for r in rows]

    def put_judge_result(self, task_id: str, judge_slug: str, artifact_sha256: str, result: dict[str, Any]) -> None:
        payload = {"schema": JUDGE_CACHE_SCHEMA, "result": result}
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO judge_cache VALUES (?, ?, ?, ?, ?)",
                (task_id, judge_slug, artifact_sha256, json.dumps(payload, default=str), datetime.now(UTC).isoformat()),
            )

    def summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            cost = conn.execute("SELECT SUM(total_cost_usd) FROM runs").fetchone()[0] or 0.0
            tokens = conn.execute("SELECT SUM(total_input_tokens + total_output_tokens) FROM runs").fetchone()[0] or 0
            orchestrators = conn.execute("SELECT orchestrator, COUNT(*) FROM runs GROUP BY orchestrator ORDER BY orchestrator").fetchall()
            workers = conn.execute("SELECT worker, COUNT(*) FROM runs GROUP BY worker ORDER BY worker").fetchall()
        return {
            "runs": total,
            "total_cost_usd": cost,
            "total_tokens": tokens,
            "orchestrator_counts": dict(orchestrators),
            "worker_counts": dict(workers),
        }

    def spend_today(self) -> float:
        """Billed cost of all runs started today (UTC) — the spend-guard meter."""
        today = datetime.now(UTC).date().isoformat()
        return self._billed_spend("started_at >= ?", (today,))

    def mean_run_cost(self, *, orchestrator: str | None = None,
                      worker: str | None = None) -> float | None:
        """Mean cost per finished run, optionally scoped to a pairing.

        Used to estimate grid cost before launching. Returns None when no
        finished runs match (caller falls back to the global mean or a
        conservative default)."""
        where = "status = 'finished' AND COALESCE(dry_run, 0) = 0"
        params: list[Any] = []
        if orchestrator:
            where += " AND orchestrator = ?"
            params.append(orchestrator)
        if worker:
            where += " AND worker = ?"
            params.append(worker)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT AVG(total_cost_usd) FROM runs WHERE {where}", params,
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None


_SORTABLE_COLUMNS = {"run_id", "started_at", "finished_at", "status", "orchestrator", "worker", "task_id", "total_cost_usd", "score", "latency_ms", "failure_reason", "run_group", "replicate"}

# (name, SQL decl) — appended at the end of `runs` for both fresh CREATEs and
# ALTER migrations so positional row reads stay valid.
_RUN_COLUMNS_V2 = (
    ("latency_ms", "REAL"),
    ("failure_reason", "TEXT"),
    ("env", "TEXT"),
    ("run_group", "TEXT"),
    ("replicate", "INTEGER"),
    ("dry_run", "INTEGER"),
    ("judge_score", "REAL"),
    ("judge_passed", "INTEGER"),
    ("delegated", "INTEGER"),
)

_CALL_COLUMNS_V2 = (
    ("worker_id", "TEXT"),
    ("sequence", "INTEGER"),
)

# calls v3: full step payloads so the index is a self-contained RL/telemetry
# store — prompt messages, raw completion, and provider finish_reason per call
_CALL_COLUMNS_V3 = (
    ("input_json", "TEXT"),
    ("output_json", "TEXT"),
    ("finish_reason", "TEXT"),
)


def _safe_name(name: str) -> str:
    """Make a slug safe as a single path component (no separators or dot segments)."""
    safe = re.sub(r"[^a-zA-Z0-9._-]", "-", name).lstrip(".")
    if not safe or set(safe) <= {"."}:
        return "_"
    return safe


def _row_to_meta(row: sqlite3.Row) -> RunMeta:
    config = json.loads(row[13]) if row[13] else {}
    # rows 14+ exist on v2 indexes; tolerate narrower rows from unmigrated DBs
    env = json.loads(row[16]) if len(row) > 16 and row[16] else {}
    return RunMeta(
        run_id=row[0],
        orchestrator=row[1],
        task_id=row[2],
        worker=row[3],
        status=row[4],
        started_at=row[5],
        finished_at=row[6],
        total_cost_usd=row[7],
        total_input_tokens=row[8],
        total_output_tokens=row[9],
        score=row[10],
        passes=bool(row[11]) if row[11] is not None else None,
        run_dir=row[12],
        config=config,
        latency_ms=row[14] if len(row) > 14 and row[14] is not None else 0.0,
        failure_reason=row[15] if len(row) > 15 else None,
        env=env,
        run_group=row[17] if len(row) > 17 else None,
        replicate=row[18] if len(row) > 18 else None,
        dry_run=bool(row[19]) if len(row) > 19 else False,
        judge_score=row[20] if len(row) > 20 else None,
        judge_passed=bool(row[21]) if len(row) > 21 and row[21] is not None else None,
        delegated=bool(row[22]) if len(row) > 22 and row[22] is not None else None,
    )
