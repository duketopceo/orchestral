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
            conn.execute("PRAGMA busy_timeout=10000")
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
                    config TEXT
                )
                """
            )
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

    def new_run(
        self,
        orchestrator: str,
        task_id: str,
        worker: str,
        config: dict[str, Any] | None = None,
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
                INSERT OR REPLACE INTO runs
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )

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
        if order_by not in _SORTABLE_COLUMNS:
            order_by = "started_at"
        query += f" ORDER BY {order_by} {'DESC' if descending else 'ASC'}"
        if limit:
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
        return json.loads(row[0]) if row else None

    def put_judge_result(self, task_id: str, judge_slug: str, artifact_sha256: str, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO judge_cache VALUES (?, ?, ?, ?, ?)",
                (task_id, judge_slug, artifact_sha256, json.dumps(result, default=str), datetime.now(UTC).isoformat()),
            )

    def summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            cost = conn.execute("SELECT SUM(total_cost_usd) FROM runs").fetchone()[0] or 0.0
            tokens = conn.execute("SELECT SUM(total_input_tokens + total_output_tokens) FROM runs").fetchone()[0] or 0
            orchestrators = conn.execute("SELECT orchestrator, COUNT(*) FROM runs GROUP BY orchestrator").fetchall()
            workers = conn.execute("SELECT worker, COUNT(*) FROM runs GROUP BY worker").fetchall()
        return {
            "runs": total,
            "total_cost_usd": cost,
            "total_tokens": tokens,
            "orchestrator_counts": dict(orchestrators),
            "worker_counts": dict(workers),
        }


_SORTABLE_COLUMNS = {"run_id", "started_at", "finished_at", "status", "orchestrator", "worker", "task_id", "total_cost_usd", "score"}


def _safe_name(name: str) -> str:
    """Make a slug safe as a single path component (no separators or dot segments)."""
    safe = re.sub(r"[^a-zA-Z0-9._-]", "-", name).lstrip(".")
    if not safe or set(safe) <= {"."}:
        return "_"
    return safe


def _row_to_meta(row: sqlite3.Row) -> RunMeta:
    config = json.loads(row[13]) if row[13] else {}
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
    )
