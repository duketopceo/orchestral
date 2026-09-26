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


def _trigger_names(workflow: dict[str, Any]) -> set[str]:
    triggers = workflow.get("on", {})
    if isinstance(triggers, str):
        return {triggers}
    if isinstance(triggers, list):
        return {str(name) for name in triggers}
    if isinstance(triggers, dict):
        return {str(name) for name in triggers}
    return set()


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

    def test_manual_eval_uses_default_branch_without_persisting_token(self) -> None:
        checkout = next(
            step for step in self.eval_job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        self.assertEqual(
            checkout["with"]["ref"],
            "${{ github.event.repository.default_branch }}",
        )
        self.assertEqual(checkout["with"]["persist-credentials"], "false")

    def test_all_workflow_actions_are_pinned_to_full_shas(self) -> None:
        for path in sorted(WORKFLOW_DIR.glob("*.yml")):
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
        for path in sorted(WORKFLOW_DIR.glob("*.yml")):
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


if __name__ == "__main__":
    unittest.main()
