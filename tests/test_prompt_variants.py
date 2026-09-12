"""Tests for prompt-variant loading and plumbing."""

from __future__ import annotations

import unittest

from orchestral.planners import (
    ORCHESTRATOR_DEFAULT_PROMPT,
    available_prompt_variants,
    load_prompt_variant,
)


class TestPromptVariants(unittest.TestCase):
    def test_default_returns_builtin(self):
        self.assertEqual(load_prompt_variant("default"), ORCHESTRATOR_DEFAULT_PROMPT)

    def test_shipped_variants_load(self):
        self.assertIn("terse", available_prompt_variants())
        self.assertIn("detailed", available_prompt_variants())
        self.assertIn("minimal plan", load_prompt_variant("terse"))

    def test_unknown_variant_lists_available(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            load_prompt_variant("nope")
        self.assertIn("terse", str(ctx.exception))

    def test_variant_flows_into_orchestrator_system_message(self):
        """_build_messages must use the loaded variant for the orchestrator role."""
        from orchestral.planners import _build_messages

        terse = load_prompt_variant("terse")
        msgs = _build_messages("orchestrator", {"prompt": "x"}, True, system_override=terse)
        self.assertTrue(msgs[0]["content"].startswith(terse))
        # worker role ignores the override
        msgs = _build_messages("worker", {"subtask": {}}, True, system_override=terse)
        self.assertIn("You are a worker", msgs[0]["content"])
        # no override keeps today's exact prompt
        msgs = _build_messages("orchestrator", {"prompt": "x"}, True)
        self.assertTrue(msgs[0]["content"].startswith(ORCHESTRATOR_DEFAULT_PROMPT))


if __name__ == "__main__":
    unittest.main()
