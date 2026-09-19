# Migration handoff — 2026-09-18

Machine `omarchy-macbook-m1` -> `omarchy-max`. All work committed + pushed on
`feat/review-terminal-tasks` before wipe. Clone via `gh repo clone
duketopceo/orchestral` on the new machine.

## State at handoff

- **Suite: 564 tests green** (incl. new `test_agentexec`, `test_agentic_runner`,
  `test_launch_parity`, `test_runner_cancel`, `test_taxonomy`).
- **U3 shipped** (`feat(agentexec)` + `feat(runner)` commits): agent-executor
  subsystem — adapter registry, workspace seed/harvest, tripwire milestones,
  `delegate_agentic`, `launch_gate`, preflight containment, executor failure
  categories, unmetered pricing handling in leaderboard.
- **U4 shipped** (`feat(launch)`): launch-surface parity — shared
  `resolve_model`/`resolve_judge`, `DEFAULT_JUDGE = ~typesafe/jev-latest` on
  every surface incl. dry-run, `--allow-agent-exec` / `ORCHESTRAL_ALLOW_AGENT_EXEC`
  opt-in (server-start only, rejected as POST field), Origin/Host CSRF on web
  POSTs, `/api/models` executor metadata, pairing-scoped spend estimates with
  per-cell recheck.
- **Working tree clean** at handoff. Nothing stashed.

## Next units (per `docs/plans/2026-09-18-001-feat-agentic-workers-judge-axis-plan.md`)

- **U5+**: judge-axis integration, calibration badges, `MIN_LEADERBOARD_SAMPLES`
  threshold work. Open gaps noted: `pipeline` task type has no dedicated
  validator; run-level `passes` still conflates mechanical and judge axes
  (KTD14 target); calibration `agreement_metrics` not yet consumed into badges.

## Verify on new machine

```bash
python3 -m venv .venv && .venv/bin/pip install -e .[tui]
.venv/bin/python -m unittest discover -s tests
python3 -m compileall orchestral harness.py
python3 harness.py init && python3 harness.py validate
python3 harness.py run --task landing-page-coffee \
  --orchestrator deepseek/deepseek-v4-flash-0731 \
  --worker z-ai/glm-5.3-flash --dry-run
python3 harness.py report --html
```

Note: system python3 lacks httpx — use `.venv/bin/python` for tests.
