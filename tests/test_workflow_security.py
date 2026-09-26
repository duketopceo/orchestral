from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
PAID_WORKFLOW = WORKFLOW_DIR / "orchestral.yml"
ACTION_REF = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _load_workflow(path: Path) -> dict[str, Any]:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _workflow_files() -> list[Path]:
    # Actions runs both extensions. A check that globs one of them leaves a
    # workflow free to reach a secret by renaming its file.
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def _trigger_names(workflow: dict[str, Any]) -> set[str]:
    triggers = workflow.get("on", {})
    if isinstance(triggers, str):
        return {triggers}
    if isinstance(triggers, list):
        return {str(name) for name in triggers}
    if isinstance(triggers, dict):
        return {str(name) for name in triggers}
    return set()


def _steps(workflow: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (job_name, step)
        for job_name, job in workflow["jobs"].items()
        for step in job["steps"]
    ]


class WorkflowSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow_text = PAID_WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = _load_workflow(PAID_WORKFLOW)
        cls.eval_job = cls.workflow["jobs"]["eval"]

    def test_paid_eval_is_manual_and_environment_gated(self) -> None:
        self.assertEqual(set(self.workflow["on"]), {"workflow_dispatch"})
        self.assertNotIn("pull_request", self.workflow_text)
        self.assertEqual(self.eval_job["environment"], "paid-eval")
        self.assertEqual(self.eval_job["permissions"], {"contents": "read"})

    def test_paid_eval_job_is_closed_by_default(self) -> None:
        guard = str(self.eval_job.get("if", ""))
        self.assertIn("PAID_EVAL_ENABLED", guard)
        self.assertIn("'true'", guard)
        self.assertIn("PAID_EVAL_ENABLED", self.workflow_text)

    def test_provider_secret_is_scoped_to_eval_step(self) -> None:
        job_env = self.eval_job.get("env", {})
        self.assertNotIn("OPENROUTER_API_KEY", job_env)
        secret_steps = [
            step
            for step in self.eval_job["steps"]
            if "OPENROUTER_API_KEY" in step.get("env", {})
        ]
        self.assertEqual(len(secret_steps), 1)
        self.assertEqual(secret_steps[0]["name"], "Run eval")
        self.assertEqual(
            secret_steps[0]["env"]["OPENROUTER_API_KEY"],
            "${{ secrets.OPENROUTER_API_KEY }}",
        )
        self.assertEqual(self.workflow_text.count("secrets.OPENROUTER_API_KEY"), 1)

    def test_dispatch_defaults_to_the_default_branch(self) -> None:
        ref_input = self.workflow["on"]["workflow_dispatch"]["inputs"]["ref"]
        self.assertEqual(ref_input["required"], "false")
        self.assertEqual(ref_input["default"], "")
        checkout = next(
            step for step in self.eval_job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        self.assertEqual(
            checkout["with"]["ref"],
            "${{ inputs.ref || github.event.repository.default_branch }}",
        )
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        self.assertIn("default_branch", self.workflow_text)

    def test_all_workflow_actions_are_pinned_to_full_shas(self) -> None:
        for path in _workflow_files():
            workflow = _load_workflow(path)
            for job in workflow["jobs"].values():
                for step in job["steps"]:
                    uses = step.get("uses")
                    if uses is not None:
                        self.assertRegex(
                            uses,
                            ACTION_REF,
                            msg=f"{path.name} must pin actions to a full commit SHA",
                        )

    def test_no_pull_request_triggered_workflow_references_secrets(self) -> None:
        for path in _workflow_files():
            if "pull_request" not in _trigger_names(_load_workflow(path)):
                continue
            self.assertNotIn(
                "secrets.",
                path.read_text(encoding="utf-8"),
                msg=(
                    f"{path.name} runs on pull_request and must not reach a secret: "
                    "PR code is attacker-controlled in any repository where a "
                    "same-repo branch can open a pull request"
                ),
            )

    def test_run_blocks_do_not_interpolate_workflow_inputs(self) -> None:
        for path in _workflow_files():
            for job_name, step in _steps(_load_workflow(path)):
                run = str(step.get("run", ""))
                for context in ("${{ inputs.", "${{ github.event.inputs."):
                    self.assertNotIn(
                        context,
                        run,
                        msg=(
                            f"{path.name}:{job_name} inlines {context} into a run line. "
                            "Pass the value through env: instead, or it is shell-injected "
                            "before any reviewer sees it"
                        ),
                    )

    def test_run_blocks_do_not_escape_variable_references(self) -> None:
        for path in _workflow_files():
            for job_name, step in _steps(_load_workflow(path)):
                run = str(step.get("run", ""))
                self.assertNotIn(
                    "\\${",
                    run,
                    msg=(
                        f"{path.name}:{job_name} escapes a variable reference. The shell "
                        "prints the literal text instead of the value, so the step "
                        "reports success without reporting the number it checked"
                    ),
                )


if __name__ == "__main__":
    unittest.main()
