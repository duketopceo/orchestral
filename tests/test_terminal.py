"""Tests for the terminal task type — virtual shell replay validation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner
from orchestral.terminal import check_terminal, run_command

FS = {
    "app.ini": "[app]\ndebug = true\n",
    "scripts/start.sh": "#!/bin/sh\nexec python3 -m old_module\n",
}
EXPECT = {
    "files": {
        "app.ini": {"contains": "debug = false"},
        "scripts/start.sh": {"contains": "app_main"},
        "READY": {"contains": "deploy-ok"},
    }
}
COMMANDS = [
    {"run": "sed -i 's/debug = true/debug = false/' app.ini"},
    {"run": "sed -i 's/old_module/app_main/' scripts/start.sh"},
    {"run": "echo deploy-ok > READY"},
]


def _metadata(**overrides):
    import copy
    md = {"fs": dict(FS), "expect": copy.deepcopy(EXPECT), "commands": list(COMMANDS)}
    md.update(overrides)
    return md


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(
        slug=slug, name=slug, role=role,
        input_price_per_mtok=0.5, output_price_per_mtok=2.0, retry_limit=1,
    )


def _task(**kwargs) -> TaskSpec:
    base = {
        "id": "term-test",
        "type": "terminal",
        "prompt": "Fix the deploy with shell commands.",
        "metadata": _metadata(),
    }
    base.update(kwargs)
    return TaskSpec(**base)


class TestVirtualShell(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        for rel, body in FS.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)

    def run_cmd(self, raw):
        return run_command(self.root, self.root, raw)

    def test_sed_in_place(self):
        _, out = self.run_cmd("sed -i 's/debug = true/debug = false/' app.ini")
        self.assertEqual(out, "")
        self.assertIn("debug = false", (self.root / "app.ini").read_text())

    def test_echo_write_and_append(self):
        self.run_cmd("echo hello > f.txt")
        self.run_cmd("echo world >> f.txt")
        self.assertEqual((self.root / "f.txt").read_text(), "hello\nworld\n")

    def test_mkdir_mv_cp_rm(self):
        self.run_cmd("mkdir -p a/b")
        self.run_cmd("cp app.ini a/b/copy.ini")
        self.assertTrue((self.root / "a/b/copy.ini").is_file())
        self.run_cmd("mv a/b/copy.ini moved.ini")
        self.assertTrue((self.root / "moved.ini").is_file())
        _, out = self.run_cmd("rm a")
        self.assertIn("is a directory", out)
        self.run_cmd("rm -r a")
        self.assertFalse((self.root / "a").exists())

    def test_grep_and_ls_and_pwd(self):
        _, out = self.run_cmd("grep debug app.ini")
        self.assertIn("debug = true", out)
        _, out = self.run_cmd("ls")
        self.assertIn("app.ini", out)
        _, out = self.run_cmd("pwd")
        self.assertIn("/", out)

    def test_sandbox_escape_rejected(self):
        _, out = self.run_cmd("cat ../../etc/passwd")
        self.assertIn("error", out)
        _, out = self.run_cmd("cat /etc/passwd")
        # absolute path is remapped inside the sandbox, not the real FS
        self.assertIn("error", out)

    def test_unsupported_command(self):
        _, out = self.run_cmd("curl https://evil.example")
        self.assertIn("unsupported", out)


class TestCheckTerminal(unittest.TestCase):
    def test_correct_plan_passes(self):
        report = check_terminal(_metadata(), json.dumps(COMMANDS))
        self.assertTrue(report["parsed"])
        self.assertTrue(report["passes"])
        self.assertEqual(report["score"], 1.0)
        self.assertEqual(len(report["transcript"]), 3)

    def test_partial_credit(self):
        report = check_terminal(_metadata(), json.dumps(COMMANDS[:2]))
        self.assertFalse(report["passes"])
        self.assertAlmostEqual(report["score"], 2 / 3)
        self.assertIn("READY", report["missing"])

    def test_command_error_fails(self):
        cmds = [{"run": "rm nonexistent.txt"}, *COMMANDS]
        report = check_terminal(_metadata(), json.dumps(cmds))
        self.assertFalse(report["passes"])
        self.assertTrue(report["command_errors"])

    def test_absent_rule(self):
        md = _metadata()
        md["expect"] = {"files": {"app.ini": {"absent": True}}}
        report = check_terminal(md, json.dumps([{"run": "rm app.ini"}]))
        self.assertTrue(report["passes"])

    def test_over_command_budget(self):
        md = _metadata()
        md["expect"]["max_commands"] = 2
        report = check_terminal(md, json.dumps(COMMANDS))
        self.assertFalse(report["passes"])
        self.assertTrue(report["over_command_budget"])

    def test_unparseable_plan(self):
        report = check_terminal(_metadata(), "just do it")
        self.assertFalse(report["parsed"])

    def test_missing_expect(self):
        report = check_terminal({"fs": FS}, "[]")
        self.assertTrue(any("expect.files" in e for e in report["command_errors"]))


class _FakeClient:
    def __init__(self, plans, pick=0):
        self.plans = plans
        self.pick = pick
        self.worker_calls = 0

    def chat(self, model, messages, max_tokens=4096, temperature=0.4):
        try:
            data = json.loads(messages[-1]["content"])
        except json.JSONDecodeError:
            data = {}
        if "subtask" in data:
            body = self.plans[min(self.worker_calls, len(self.plans) - 1)]
            self.worker_calls += 1
        elif "candidates" in data:
            body = json.dumps({"subtask_id": self.pick})
        elif "prompt" in data:
            body = json.dumps({"subtasks": [{"id": 0, "description": "fix it"}]})
        else:
            body = json.dumps({"score": 0.5, "passed": True, "reasoning": "ok"})
        return {"content": body, "usage": {"prompt_tokens": 20, "completion_tokens": 10}, "latency_ms": 1, "id": "fake"}

    def close(self):
        pass


class TestTerminalRunner(unittest.TestCase):
    def test_dry_run_replays_reference_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Runner(runs_dir=tmp, planner="raw", dry_run=True).run(
                _task(), _model("org/x", "orchestrator"), _model("wrk/x", "worker"),
            )
            self.assertTrue(meta.passes)
            self.assertEqual(meta.score, 1.0)

    def test_live_correct_plan_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(plans=[json.dumps(COMMANDS)])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/x", "worker"))
            self.assertTrue(meta.passes)
            self.assertEqual(meta.score, 1.0)

    def test_live_partial_plan_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _FakeClient(plans=[json.dumps(COMMANDS[:1])])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(_task(), _model("org/x", "orchestrator"), _model("wrk/x", "worker"))
            self.assertFalse(meta.passes)
            self.assertAlmostEqual(meta.score, 1 / 3)


class TestBugfix(unittest.TestCase):
    """bugfix = code-type validation over a provided broken repo."""

    def _bugfix_task(self) -> TaskSpec:
        return TaskSpec(
            id="bf-test", type="bugfix",
            prompt="Fix the bug in solution.py.",
            metadata={
                "module": "solution.py",
                "expected_paths": ["solution.py"],
                "files": {"solution.py": "def add(a, b):\n    return a - b  # BUG\n"},
                "tests": "import unittest\nfrom solution import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
            },
        )

    def test_dry_run_bugfix_compiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Runner(runs_dir=tmp, planner="raw", dry_run=True).run(
                self._bugfix_task(), _model("org/x", "orchestrator"), _model("wrk/x", "worker"),
            )
            self.assertIn(meta.status, ("finished", "failed"))
            report = json.loads((Path(meta.run_dir) / "report.json").read_text())
            self.assertIn("expected_paths", report["checks"])

    def test_live_fixed_file_passes(self):
        files_json = json.dumps({"files": [{"path": "solution.py", "content": "def add(a, b):\n    return a + b\n"}]})

        class C(_FakeClient):
            def chat(self, model, messages, max_tokens=4096, temperature=0.4):
                try:
                    data = json.loads(messages[-1]["content"])
                except json.JSONDecodeError:
                    data = {}
                if "subtask" in data:
                    self.worker_calls += 1
                    # bugfix subtask must carry the broken files
                    assert data["subtask"].get("broken_files"), "broken_files missing"
                    return {"content": files_json, "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "latency_ms": 1}
                return super().chat(model, messages, max_tokens, temperature)

        with tempfile.TemporaryDirectory() as tmp:
            client = C(plans=["x"])
            meta = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": client, "worker": client},
            ).run(self._bugfix_task(), _model("org/x", "orchestrator"), _model("wrk/x", "worker"))
            self.assertTrue(meta.passes)
            self.assertEqual(meta.score, 1.0)


if __name__ == "__main__":
    unittest.main()
