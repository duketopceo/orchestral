"""Tests for prompt-variant loading and plumbing."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from orchestral.planners import (
    ORCHESTRATOR_DEFAULT_PROMPT,
    PROMPTS_DIR,
    available_prompt_variants,
    load_prompt_variant,
)

# Only a ```json fence counts as a literal; an unlabeled fence is some other
# language and is not ours to parse.
_JSON_FENCE = re.compile(r"^```json\s*$", re.IGNORECASE)
_COUNT_GUIDANCE = re.compile(r"count small \((\d+)-(\d+)\)", re.IGNORECASE)


def _json_literals(text: str) -> list[tuple[int, str]]:
    """Every whole-document JSON literal in a prompt, with its 1-based line.

    A literal is either a ```json fence body or a bare line that opens with
    ``{`` or ``[`` and closes with ``}`` or ``]``. Prose that merely mentions
    JSON is skipped, so nothing here is a false positive.
    """
    literals: list[tuple[int, str]] = []
    fence: list[str] = []
    opened_at = 0
    inside = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _JSON_FENCE.match(line.strip()):
            if inside:
                literals.append((opened_at, "\n".join(fence)))
                fence, inside = [], False
            else:
                inside, opened_at = True, lineno
            continue
        if inside:
            fence.append(line)
            continue
        stripped = line.strip()
        # `"" in "{["` is True, so the emptiness check has to come first.
        if stripped and stripped[:1] in "{[" and stripped[-1:] in "}]":
            literals.append((lineno, stripped))
    return literals


class TestPromptExamplesAreValidJSON(unittest.TestCase):
    """A prompt example is copied verbatim by the model, so it must parse.

    ``prompts/orchestrator-explicit.md`` shipped a literal whose trailing
    ellipsis closed the array before the object. ``json.loads`` rejected it,
    and a model copying the example produced ``malformed_output``. These tests
    keep every prompt's JSON literal parseable and keep the explicit example
    consistent with the subtask-count guidance in the same file.
    """

    def test_every_prompt_json_literal_parses(self):
        checked = 0
        for path in sorted(Path(PROMPTS_DIR).glob("*.md")):
            for lineno, literal in _json_literals(path.read_text()):
                with self.subTest(prompt=path.name, line=lineno):
                    json.loads(literal)
                checked += 1
        self.assertGreater(checked, 0, "no prompt JSON literal found to check")

    def test_explicit_example_matches_stated_subtask_range(self):
        text = load_prompt_variant("explicit")
        literals = _json_literals(text)
        self.assertEqual(len(literals), 1, f"expected one example literal, got {[n for n, _ in literals]}")
        example = json.loads(literals[0][1])

        self.assertEqual(list(example), ["subtasks"])
        subtasks = example["subtasks"]
        self.assertIsInstance(subtasks, list)

        guidance = _COUNT_GUIDANCE.search(text)
        self.assertIsNotNone(guidance, "the stated subtask-count guidance line is gone")
        low, high = (int(g) for g in guidance.groups())
        self.assertGreaterEqual(len(subtasks), 2, "a single subtask cannot demonstrate a list")
        self.assertTrue(
            low <= len(subtasks) <= high,
            f"example shows {len(subtasks)} subtasks but the guidance says {low}-{high}",
        )

        for sub in subtasks:
            self.assertIsInstance(sub, dict)
            self.assertIsInstance(sub.get("id"), int, f"id is not an int: {sub!r}")
            self.assertIsInstance(sub.get("description"), str, f"description is not a str: {sub!r}")

    def test_explicit_variant_is_discoverable(self):
        self.assertIn("explicit", available_prompt_variants())


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
