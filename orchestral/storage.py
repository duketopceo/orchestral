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
import threading
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUNS_DIR = Path("runs")
DB_NAME = "index.db"

# Judge-cache payload version — bump when the result shape or the input
# contract changes so stale records go cold on read instead of being
# trusted. v1 was the bare result dict (pre-inconclusive rule).
JUDGE_CACHE_SCHEMA = 2


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
            for name, decl in _CALL_COLUMNS_V2:
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
                    run_group, replicate, dry_run, judge_score, judge_passed
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
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
            conn.execute(
                f"INSERT INTO calls ({', '.join(names)}) "
                f"VALUES ({', '.join('?' for _ in names)})",
                values,
            )

    def calls_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM calls WHERE run_id = ? ORDER BY call_id", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

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

    def set_annotation(
        self, kind: str, target: str, flag: str, note: str = ""
    ) -> dict[str, Any]:
        """Upsert a user annotation — the observatory's stateful layer.

        ``kind`` is ``run`` or ``group``; ``flag`` is ``interesting``,
        ``not``, or ``''`` (clears the flag but keeps the row for the note)."""
        if kind not in ("run", "group"):
            raise ValueError(f"annotation kind must be run|group, got {kind!r}")
        if flag not in ("interesting", "not", ""):
            raise ValueError(f"flag must be interesting|not|'', got {flag!r}")
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
        return {"kind": kind, "target": target, "flag": flag, "note": note}

    def annotations(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT kind, target, flag, note, updated_at FROM annotations"
            ).fetchall()
        return [dict(r) for r in rows]

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
        return _row_to_meta(row)

    def update_meta(self, meta: RunMeta) -> None:
        self._write_meta_file(Path(meta.run_dir), meta)
        self.index_meta(meta)

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
        return [_row_to_meta(row) for row in rows]

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
        """Recorded cost of all runs started today (UTC) — the spend-guard meter."""
        today = datetime.now(UTC).date().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(total_cost_usd), 0) FROM runs "
                "WHERE started_at >= ? AND COALESCE(dry_run, 0) = 0",
                (today,),
            ).fetchone()
        return float(row[0] or 0.0)

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
)

_CALL_COLUMNS_V2 = (
    ("worker_id", "TEXT"),
    ("sequence", "INTEGER"),
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
    )
