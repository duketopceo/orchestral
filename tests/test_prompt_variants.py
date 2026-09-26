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

# A ```json fence opens a candidate whose body must parse. A closing fence is
# a bare ```, so it needs its own pattern. An unlabeled fence is another
# language, so its body is not emitted as a fence candidate — but a body line
# that is itself whole-line JSON is still caught by the bare-line path below.
_FENCE_OPEN = re.compile(r"^```json\s*$", re.IGNORECASE)
_FENCE_CLOSE = re.compile(r"^```\s*$")
_COUNT_GUIDANCE = re.compile(r"count small \((\d+)-(\d+)\)", re.IGNORECASE)


def _json_literals(text: str) -> list[tuple[int, str]]:
    """Every whole-document JSON literal in a prompt, with its 1-based line.

    A literal is either the body of a ```json fence, which may span lines, or
    a bare line that opens with ``{`` or ``[`` and closes with ``}`` or ``]``.
    Prose that merely mentions JSON is skipped.

    The reported line is always the first line of the literal's own text, never
    the fence tag, so a body-relative ``JSONDecodeError.lineno`` maps to the
    prompt with one arithmetic step.

    A fenced body is emitted verbatim and an unterminated fence is emitted at
    EOF, so a body carrying prose, a nested fence tag, or a missing close
    fails ``json.loads`` rather than passing unread. A guard that goes loud on
    a malformed prompt is the point; it must not go quiet.

    Known limit: multi-line JSON that is neither fenced nor a single line is
    not detected. No prompt in ``prompts/`` uses that shape.
    """
    literals: list[tuple[int, str]] = []
    fence: list[str] = []
    body_at = 0
    inside = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if inside:
            if _FENCE_CLOSE.match(stripped):
                literals.append((body_at, "\n".join(fence)))
                fence, inside = [], False
            else:
                fence.append(line)
            continue
        if _FENCE_OPEN.match(stripped):
            inside, body_at = True, lineno + 1
            continue
        # `"" in "{["` is True, so the emptiness check has to come first.
        if stripped and stripped[:1] in "{[" and stripped[-1:] in "}]":
            literals.append((lineno, stripped))
    if inside:
        # No close ever arrived. Emitting the partial body fails closed; before
        # this, `inside` stayed True to EOF and every later literal went unseen.
        literals.append((body_at, "\n".join(fence)))
    return literals


def _assert_json_parses(prompt: str, lineno: int, literal: str) -> None:
    """``json.loads``, but a multi-line failure names the prompt line it is on.

    ``JSONDecodeError.lineno`` is relative to the literal, so a fenced body
    reports "line 2" for something that is line 4 of the prompt. Translate it,
    or the next person debugs the wrong line.
    """
    try:
        json.loads(literal)
    except json.JSONDecodeError as exc:
        where = lineno + exc.lineno - 1
        raise AssertionError(f"{prompt}:{where}:{exc.colno}: {exc.msg}") from exc


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
                    _assert_json_parses(path.name, lineno, literal)
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
        # A deleted line and a changed dash fail this identically, so name both
        # causes: only ASCII hyphen-minus matches, not an en dash or a spaced
        # hyphen, and that is deliberate.
        self.assertIsNotNone(
            guidance,
            "no 'count small (2-5)' line matched — either the guidance is gone, "
            "or its dash changed (only ASCII hyphen-minus is matched)",
        )
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


class TestJsonLiteralDetection(unittest.TestCase):
    """_json_literals must actually see every shape it claims to see.

    The ```json fence path shipped dead: a well-formed fence closes with a
    bare ``` that the open-tag pattern did not match, so `inside` never reset
    and the literal was never emitted. Nothing tested the helper directly, so
    no test noticed. These cases pin the detection itself.
    """

    def test_bare_line_is_detected(self):
        self.assertEqual(_json_literals('{"a": [1, 2]}\n'), [(1, '{"a": [1, 2]}')])

    def test_prose_and_blank_lines_are_ignored(self):
        self.assertEqual(_json_literals("Return JSON.\n\nRules:\n- no fences\n"), [])

    def test_json_fence_body_is_detected_even_when_multiline(self):
        text = '```json\n{\n  "a": [1, 2]\n}\n```\n'
        self.assertEqual(_json_literals(text), [(2, '{\n  "a": [1, 2]\n}')])

    def test_sibling_fences_do_not_contaminate_each_other(self):
        text = '```json\n{"x": [1, 2]}\n```\nprose\n```json\n{"y": [3, 4]}\n```\n'
        # A fenced literal is reported at its first body line, not its fence.
        self.assertEqual(
            _json_literals(text),
            [(2, '{"x": [1, 2]}'), (6, '{"y": [3, 4]}')],
        )

    def test_unlabeled_fence_body_is_not_a_candidate(self):
        """A ```python block is not JSON, so it must not reach json.loads."""
        text = '```python\nd = {"a": 1}\nprint(d)\n```\n'
        self.assertEqual(_json_literals(text), [])

    def test_unlabeled_fence_whole_line_json_is_caught_by_the_bare_path(self):
        """The documented exception: a body line that is itself whole-line JSON.

        An unlabeled fence opens nothing, so its body falls through to the
        bare-line rule. That is the only reason a ```python block holding a
        lone ``{"a": 1}`` is still checked.
        """
        text = '```python\n{"a": 1}\nprint(d)\n```\n'
        self.assertEqual(_json_literals(text), [(2, '{"a": 1}')])

    def test_nested_fence_tag_in_a_body_is_kept_and_fails_closed(self):
        """A ```json line inside an open fence is body text, not a new fence.

        Per GFM only a bare ``` closes a block, so a nested tag belongs to the
        body. Emitting it verbatim keeps the guard fail-closed: that body
        really is not parseable JSON. Dropping the line would hide content and
        is the opposite of what this guard is for.
        """
        text = '```json\n{"a": [1, 2}\n```json\n```\n'
        self.assertEqual(_json_literals(text), [(2, '{"a": [1, 2}\n```json')])
        with self.assertRaises(json.JSONDecodeError):
            json.loads(_json_literals(text)[0][1])

    def test_failure_reports_the_prompt_line_not_the_body_line(self):
        """A fenced failure must name the prompt line, not the body line.

        ``json`` calls the bad token body-line 2, which in this file is the
        ``{``. Only the translation puts a reader on the real line 4.
        """
        text = 'prose\n```json\n{\n  "a": nope\n}\n```\n'
        lineno, body = _json_literals(text)[0]
        with self.assertRaises(AssertionError) as ctx:
            _assert_json_parses("orchestrator-explicit.md", lineno, body)
        self.assertIn("orchestrator-explicit.md:4:", str(ctx.exception))
        self.assertEqual(text.splitlines()[3], '  "a": nope')

    def test_unterminated_fence_still_emits_its_body(self):
        """A stray ```json must not swallow the rest of the file silently."""
        text = 'prose\n```json\n{"a": [1, 2}\n'
        self.assertEqual(_json_literals(text), [(3, '{"a": [1, 2}')])

    def test_stray_fence_does_not_hide_a_later_bare_literal(self):
        """The cascade case: no close arrives, so a later literal is inside."""
        text = '```json\n\n{"a": [1, 2}\n\n{"b": [3, 4]}\n'
        literals = _json_literals(text)
        self.assertEqual(literals, [(2, '\n{"a": [1, 2}\n\n{"b": [3, 4]}')])
        with self.assertRaises(json.JSONDecodeError):
            json.loads(literals[0][1])


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
