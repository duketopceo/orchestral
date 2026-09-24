"""U4 launch-surface parity: CLI, web, and TUI resolve models, judges, and
executor opt-ins identically.

The launch field sets across the three surfaces are enumerated and compared
— divergence fails here, not in production. The executor opt-in is a
launch-context decision (CLI flag / TUI flag / server-start flag), never a
per-request field, so it must NOT appear in the shared field set.
"""

from __future__ import annotations

import argparse
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import harness
from orchestral.agentexec import (
    ADAPTERS,
    AgentAdapter,
    ExecutorPreflightError,
    launch_gate,
)
from orchestral.config import ModelConfig, TaskSpec, resolve_judge
from orchestral.judge import DEFAULT_JUDGE, is_decisions_model
from orchestral.storage import RunStore
from orchestral.tui.state import LAUNCH_SPEC_FIELDS, JobStatus
from orchestral.web import state


def _model(slug: str, role: str = "worker", metadata: dict | None = None) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.03, output_price_per_mtok=0.10,
        metadata=metadata or {},
    )


def _write_specs(root: Path) -> tuple[Path, Path]:
    tasks = root / "tasks"
    models = root / "models"
    tasks.mkdir(exist_ok=True)
    models.mkdir(exist_ok=True)
    (tasks / "t-task.yaml").write_text("id: t-task\ntype: html\nprompt: p\n")
    (tasks / "t-exec.yaml").write_text(
        "id: t-exec\ntype: code\nprompt: p\n"
        "metadata:\n  requires_executor: true\n"
    )
    (models / "m.yaml").write_text(
        "models:\n"
        "  - slug: o/model\n    name: o\n    role: orchestrator\n"
        "    input_price_per_mtok: 0.03\n    output_price_per_mtok: 0.10\n"
        "  - slug: w/model\n    name: w\n    role: worker\n"
        "    input_price_per_mtok: 0.03\n    output_price_per_mtok: 0.10\n"
        "  - slug: agent-opencode+x\n    name: a\n    role: worker\n"
        "    input_price_per_mtok: 0.0\n    output_price_per_mtok: 0.0\n"
        "    metadata:\n      executor: stub-cli\n      capabilities: [code]\n"
    )
    return tasks, models


def _wait(pred, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


class TestFieldSetParity(unittest.TestCase):
    def test_web_and_tui_launch_fields_are_identical(self):
        self.assertEqual(state.LAUNCH_FIELDS, LAUNCH_SPEC_FIELDS)

    def test_executor_optin_is_not_a_launch_field(self):
        # POST /api/run is an unauthenticated urlencoded endpoint — a
        # per-request allow_agent_exec field would be CSRF-triggerable.
        for fields in (state.LAUNCH_FIELDS, LAUNCH_SPEC_FIELDS):
            self.assertNotIn("allow_agent_exec", fields)
            self.assertNotIn("allow-agent-exec", fields)

    def test_cli_run_parser_covers_shared_fields(self):
        parser = harness._build_parser()
        subs = next(
            a for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)
        )
        run_dests = {a.dest for a in subs.choices["run"]._actions}
        self.assertLessEqual(state.LAUNCH_FIELDS, run_dests)

    def test_executor_optin_flag_on_every_cli_surface(self):
        parser = harness._build_parser()
        subs = next(
            a for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)
        )
        for cmd in ("run", "grid", "batch", "ablate", "tui", "serve"):
            dests = {a.dest for a in subs.choices[cmd]._actions}
            self.assertIn("allow_agent_exec", dests, cmd)


class TestJudgeSlugResolution(unittest.TestCase):
    """`~typesafe/jev-latest` never appears in load_models() output — the
    `~` prefix marks disabled entries there. Every launch path must still
    resolve it via the shared resolver's ad-hoc fallback."""

    def test_shared_resolver_resolves_tilde_slug(self):
        cfg = resolve_judge("~typesafe/jev-latest")
        self.assertEqual(cfg.slug, "~typesafe/jev-latest")
        self.assertEqual(cfg.role, "judge")
        self.assertTrue(is_decisions_model(cfg))

    def test_cli_judge_path_resolves_tilde_slug(self):
        args = argparse.Namespace(judge="~typesafe/jev-latest", models_dir="models")
        judge = harness._judge_from_arg(args)
        self.assertIsNotNone(judge)
        self.assertEqual(judge.slug, "~typesafe/jev-latest")
        self.assertEqual(judge.role, "judge")

    def test_web_launch_resolves_tilde_judge(self):
        """A web-launched run with judge `~typesafe/jev-latest` must reach
        the runner with a real ModelConfig — the old `models.get(slug)`
        lookup silently produced an unjudged run."""
        with tempfile.TemporaryDirectory() as tmp:
            tasks, models = _write_specs(Path(tmp))
            store = RunStore(Path(tmp) / "runs")
            reg = state.JobRegistry(Path(tmp) / "runs", tasks, models, store)
            job = reg.launch({
                "task": "t-task", "orchestrator": "o/model", "worker": "w/model",
                "judge": "~typesafe/jev-latest", "replicates": 1, "dry_run": True,
            })
            self.assertTrue(
                _wait(lambda: job.status == JobStatus.SUCCEEDED),
                f"job ended as {job.status}: {job.detail}",
            )
            report = state.read_json(Path(store.get_run(job.run_ids[0]).run_dir) / "report.json")
            self.assertEqual(report["judge"]["model"], "~typesafe/jev-latest")

    def test_tui_judge_choices_offer_default_judge(self):
        from orchestral.judge import judge_choices

        choices = judge_choices(["a/b", "c/d"])
        self.assertEqual(choices[0], DEFAULT_JUDGE)
        self.assertIn("a/b", choices)
        # a configured model with the same slug never duplicates it
        self.assertEqual(len(judge_choices([DEFAULT_JUDGE])), 1)


class TestExecutorLaunchGate(unittest.TestCase):
    """The shared launch gate — one implementation consumed by CLI env
    checks, the web registry, and the TUI launch modal."""

    def _exec_task(self) -> TaskSpec:
        return TaskSpec(id="t", type="code", prompt="p",
                        metadata={"requires_executor": True})

    def _exec_worker(self, adapter: str = "stub-cli") -> ModelConfig:
        return _model("agent-stub+x", metadata={"executor": adapter})

    def test_chat_worker_plain_task_returns_none(self):
        self.assertIsNone(
            launch_gate(_model("w/m"), TaskSpec(id="t", type="html", prompt="p"),
                        allow_agent_exec=False))

    def test_chat_worker_on_executor_task_fails(self):
        with self.assertRaises(ExecutorPreflightError) as ctx:
            launch_gate(_model("w/m"), self._exec_task(), allow_agent_exec=True)
        self.assertIn("requires_executor", str(ctx.exception))

    def test_executor_worker_on_plain_task_fails(self):
        with self.assertRaises(ExecutorPreflightError) as ctx:
            launch_gate(self._exec_worker(), TaskSpec(id="t", type="html", prompt="p"),
                        allow_agent_exec=True)
        self.assertIn("requires_executor", str(ctx.exception))

    def test_executor_without_optin_fails_naming_flag(self):
        with self.assertRaises(ExecutorPreflightError) as ctx:
            launch_gate(self._exec_worker(), self._exec_task(), allow_agent_exec=False)
        self.assertIn("--allow-agent-exec", str(ctx.exception))

    def test_executor_unknown_adapter_fails(self):
        with self.assertRaises(ExecutorPreflightError) as ctx:
            launch_gate(self._exec_worker("no-such-cli"), self._exec_task(),
                        allow_agent_exec=True)
        self.assertIn("no-such-cli", str(ctx.exception))

    def test_executor_missing_binary_fails_naming_it(self):
        stub = AgentAdapter(name="stub-cli", binary="definitely-missing-cli")
        with patch.dict(ADAPTERS, {"stub-cli": stub}), self.assertRaises(ExecutorPreflightError) as ctx:
            launch_gate(self._exec_worker(), self._exec_task(),
                        allow_agent_exec=True)
        self.assertIn("definitely-missing-cli", str(ctx.exception))

    def test_executor_ready_returns_adapter(self):
        stub = AgentAdapter(name="stub-cli", binary="python3")
        with patch.dict(ADAPTERS, {"stub-cli": stub}):
            adapter = launch_gate(self._exec_worker(), self._exec_task(),
                                  allow_agent_exec=True)
        self.assertIs(adapter, stub)

    def test_probe_skippable_for_dry_run_surfaces(self):
        stub = AgentAdapter(name="stub-cli", binary="definitely-missing-cli")
        with patch.dict(ADAPTERS, {"stub-cli": stub}):
            adapter = launch_gate(self._exec_worker(), self._exec_task(),
                                  allow_agent_exec=True, probe=False)
        self.assertIs(adapter, stub)


class TestRegistryExecutorGate(unittest.TestCase):
    def test_executor_launch_rejected_without_server_optin(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks, models = _write_specs(Path(tmp))
            reg = state.JobRegistry(Path(tmp) / "runs", tasks, models,
                                    RunStore(Path(tmp) / "runs"),
                                    allow_agent_exec=False)
            with self.assertRaises(ValueError) as ctx:
                reg.launch({
                    "task": "t-exec", "orchestrator": "o/model",
                    "worker": "agent-opencode+x", "replicates": 1,
                    "dry_run": True,
                })
            self.assertIn("--allow-agent-exec", str(ctx.exception))

    def test_executor_launch_rejected_missing_binary(self):
        stub = AgentAdapter(name="stub-cli", binary="definitely-missing-cli")
        with tempfile.TemporaryDirectory() as tmp:
            tasks, models = _write_specs(Path(tmp))
            reg = state.JobRegistry(Path(tmp) / "runs", tasks, models,
                                    RunStore(Path(tmp) / "runs"),
                                    allow_agent_exec=True)
            with patch.dict(ADAPTERS, {"stub-cli": stub}), self.assertRaises(ValueError) as ctx:
                reg.launch({
                    "task": "t-exec", "orchestrator": "o/model",
                    "worker": "agent-opencode+x", "replicates": 1,
                })
            self.assertIn("definitely-missing-cli", str(ctx.exception))

    def test_executor_pairing_errors_rejected_at_launch(self):
        # executor worker on an undeclared task — a 400, not a failed run
        stub = AgentAdapter(name="stub-cli", binary="python3")
        with tempfile.TemporaryDirectory() as tmp:
            tasks, models = _write_specs(Path(tmp))
            reg = state.JobRegistry(Path(tmp) / "runs", tasks, models,
                                    RunStore(Path(tmp) / "runs"),
                                    allow_agent_exec=True)
            with patch.dict(ADAPTERS, {"stub-cli": stub}), self.assertRaises(ValueError) as ctx:
                reg.launch({
                    "task": "t-task", "orchestrator": "o/model",
                    "worker": "agent-opencode+x", "replicates": 1,
                })
            self.assertIn("requires_executor", str(ctx.exception))

    def test_executor_dry_run_skips_binary_probe_but_keeps_optin(self):
        stub = AgentAdapter(name="stub-cli", binary="definitely-missing-cli")
        with tempfile.TemporaryDirectory() as tmp:
            tasks, models = _write_specs(Path(tmp))
            reg = state.JobRegistry(Path(tmp) / "runs", tasks, models,
                                    RunStore(Path(tmp) / "runs"),
                                    allow_agent_exec=True)
            with patch.dict(ADAPTERS, {"stub-cli": stub}):
                # dry_run + opt-in + declared task → gate passes (the runner
                # resolves the rest; a missing binary only matters when a
                # process would actually spawn)
                job = reg.launch({
                    "task": "t-exec", "orchestrator": "o/model",
                    "worker": "agent-opencode+x", "replicates": 1,
                    "dry_run": True,
                })
            self.assertTrue(_wait(lambda: not job.active))


class TestEligibleWorkersCapabilities(unittest.TestCase):
    def test_executor_workers_filtered_by_capability_and_declaration(self):
        pool = [
            _model("w/chat"),
            _model("agent/x", metadata={"executor": "stub-cli",
                                       "capabilities": ["code", "swe-patch"]}),
        ]
        plain = TaskSpec(id="t", type="code", prompt="p")
        self.assertEqual(
            [m.slug for m in harness._eligible_workers(pool, plain)], ["w/chat"])
        exec_task = TaskSpec(id="t", type="code", prompt="p",
                             metadata={"requires_executor": True})
        self.assertEqual(
            [m.slug for m in harness._eligible_workers(pool, exec_task)],
            ["agent/x"])
        # capability mismatch — an executor task type the worker can't produce
        other = TaskSpec(id="t", type="terminal", prompt="p",
                         metadata={"requires_executor": True})
        self.assertEqual(harness._eligible_workers(pool, other), [])

    def test_undeclared_capabilities_never_eligible(self):
        # executor worker with no capabilities list is never auto-selected
        pool = [_model("agent/x", metadata={"executor": "stub-cli"})]
        task = TaskSpec(id="t", type="code", prompt="p",
                        metadata={"requires_executor": True})
        self.assertEqual(harness._eligible_workers(pool, task), [])


class TestCliEnvCheckExecutor(unittest.TestCase):
    def test_missing_binary_fails_fast_naming_it(self):
        stub = AgentAdapter(name="stub-cli", binary="definitely-missing-cli")
        args = argparse.Namespace(dry_run=False, allow_agent_exec=True)
        worker = _model("agent/x", metadata={"executor": "stub-cli"})
        with patch.dict(ADAPTERS, {"stub-cli": stub}), self.assertRaises(SystemExit):
            harness._check_provider_envs(args, worker)

    def test_missing_optin_fails_fast(self):
        args = argparse.Namespace(dry_run=False, allow_agent_exec=False)
        worker = _model("agent/x", metadata={"executor": "stub-cli"})
        with self.assertRaises(SystemExit):
            harness._check_provider_envs(args, worker)

    def test_dry_run_skips_checks(self):
        args = argparse.Namespace(dry_run=True, allow_agent_exec=False)
        worker = _model("agent/x", metadata={"executor": "stub-cli"})
        harness._check_provider_envs(args, worker)  # no raise


if __name__ == "__main__":
    unittest.main()
