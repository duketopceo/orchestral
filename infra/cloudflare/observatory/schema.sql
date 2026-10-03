-- D1 schema for the orchestral reconciliation ledger.
-- Apply once:  wrangler d1 execute orchestral-runs --file schema.sql
--
-- This is NOT the observatory read store — the SPA reads R2 snapshots.
-- D1 exists so gateway-log reconciliation can query per-call cost/token
-- rows without pulling objects. Columns mirror orchestral/cf.py
-- RUN_COLUMNS / CALL_COLUMNS / ANNOTATION_COLUMNS exactly; input_json,
-- output_json, and error are absent by design.

CREATE TABLE IF NOT EXISTS runs (
  run_id              TEXT PRIMARY KEY,
  orchestrator        TEXT,
  task_id             TEXT,
  worker              TEXT,
  status              TEXT,
  started_at          TEXT,
  finished_at         TEXT,
  total_cost_usd      REAL,
  total_input_tokens  INTEGER,
  total_output_tokens INTEGER,
  score               REAL,
  passes              INTEGER,
  judge_score         REAL,
  judge_passed        INTEGER,
  latency_ms          REAL,
  failure_reason      TEXT,
  run_group           TEXT,
  replicate           INTEGER,
  dry_run             INTEGER,
  delegated           INTEGER,
  holdout             INTEGER NOT NULL DEFAULT 0
);

-- call_id is the local sqlite row id — only unique within (machine, run).
-- The PK is (run_id, call_id): run_id is globally unique, so rebuilt local
-- indexes or a second machine can't collide into each other's rows.
CREATE TABLE IF NOT EXISTS calls (
  run_id          TEXT NOT NULL REFERENCES runs(run_id),
  call_id         TEXT NOT NULL,
  phase           TEXT,
  step            INTEGER,
  role            TEXT,
  model           TEXT,
  input_tokens    INTEGER,
  output_tokens   INTEGER,
  cost_usd        REAL,
  api_cost_usd    REAL,
  pricing_source  TEXT,
  latency_ms      REAL,
  attempt         INTEGER,
  error_category  TEXT,
  sequence        INTEGER,
  worker_id       TEXT,
  dry_run         INTEGER,
  created_at      TEXT,
  finish_reason   TEXT,
  PRIMARY KEY (run_id, call_id)
);

CREATE TABLE IF NOT EXISTS annotations (
  kind       TEXT NOT NULL,
  target     TEXT NOT NULL,
  flag       TEXT,
  note       TEXT,
  updated_at TEXT,
  PRIMARY KEY (kind, target)
);
