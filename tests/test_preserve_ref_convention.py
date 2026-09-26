"""Guard: the `preserve/*` convention stays documented.

This is a **documentation** guard, not an enforcement mechanism, and the test
names say so on purpose. It cannot stop anyone opening a PR against a preserve
ref — that needs either a CI check over the GitHub API or a platform rule about
where worktrees and branches come from. What it does do is stop the rule from
quietly disappearing, which is how it was lost in the first place: the
convention existed, every agent who needed it acted on it, and no document in
the repository said so.

The real enforcement belongs in CI and is not in this repository's test suite
because it needs a token with `pull-requests: read` and a network call, and this
suite is deliberately hermetic (`tests/test_integration_mock.py` is the pattern:
mock providers, no live calls).

The defect this documents is DUK-115's cousin: a red PR (#70) whose base is
`preserve/duk115-sql-orderby-d32440c`, a frozen snapshot 11 commits behind
`main`. `update-branch` cannot fix it —

    422 There are no new commits on the base branch.

— because the base has nothing the head lacks. The content was fine; the base
was the wrong kind of ref.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

# Emphasis markers are presentation, not content. A guard that asserts on them
# breaks when someone reformats the prose, which trains people to ignore it.
# Code spans are protected first, because they carry meaning here: the `*` in
# `preserve/*` is part of a ref pattern, not a formatting marker.
_CODE_SPAN = re.compile(r"(`[^`]*`)")
_EMPHASIS = str.maketrans("", "", "*_")


def plain(markdown: str) -> str:
    """Drop Markdown emphasis outside code spans, so assertions test wording."""
    parts = _CODE_SPAN.split(markdown)
    return "".join(
        part if index % 2 else part.translate(_EMPHASIS)
        for index, part in enumerate(parts)
    )


class TestPreserveRefConventionIsDocumented(unittest.TestCase):
    def setUp(self) -> None:
        self.text = plain(CONTRIBUTING.read_text())

    def test_contributing_documents_the_preserve_ref_convention(self) -> None:
        self.assertIn(
            "`preserve/*` refs are recovery targets",
            self.text,
            "CONTRIBUTING.md no longer states the preserve-ref rule. Without it "
            "the rule survives only in an issue comment, which is exactly where "
            "the next agent will not look. See DUK-115.",
        )

    def test_documentation_states_all_four_rules(self) -> None:
        for rule, expected in (
            ("never merge into a preserve ref", "Never merge into one"),
            ("never base a PR on a preserve ref", "Never open a PR with one as the base"),
            ("retarget onto the real branch", "Retarget onto the branch the work actually lives on"),
            ("preserve a head with a new ref", "create a new `preserve/*` ref"),
        ):
            with self.subTest(rule=rule):
                self.assertIn(
                    expected,
                    self.text,
                    f"the documented rule '{rule}' is missing from CONTRIBUTING.md",
                )

    def test_documentation_names_the_mechanism_it_prevents(self) -> None:
        # The rule is only actionable if it says why update-branch cannot help.
        # An agent who does not know that will try it, get a 422, and burn a
        # heartbeat rediscovering this.
        for expected in ("refs/pull/N/merge", "422"):
            with self.subTest(expected=expected):
                self.assertIn(
                    expected,
                    self.text,
                    f"CONTRIBUTING.md does not mention {expected!r}, so the reason "
                    "update-branch cannot rescue a preserve-based PR is undocumented",
                )

    def test_documentation_covers_the_dangling_object_rules(self) -> None:
        # The preserve-ref rules are not sufficient on their own. A stash-shaped
        # object has no name at all, so it survives none of them, and an empty
        # `git stash list` is the *normal* appearance of work that was dropped.
        # That misreading is what nearly cost 17 uncommitted paths.
        for rule, expected in (
            ("stashed work must reach a named ref", "Never leave a run's uncommitted work only in a stash"),
            ("an empty stash list proves nothing", "not evidence that nothing was stashed"),
            ("recover by pinning", "Recover by pinning, not by looking"),
        ):
            with self.subTest(rule=rule):
                self.assertIn(
                    expected,
                    self.text,
                    f"the documented rule '{rule}' is missing from CONTRIBUTING.md",
                )

    def test_this_file_is_a_documentation_guard_not_enforcement(self) -> None:
        # If someone later wires real enforcement into this suite, this test
        # becomes wrong and should be deleted rather than left to mislead.
        doc = __doc__ or ""
        self.assertIn(
            "documentation",
            doc,
            "the module docstring must keep saying that this is a documentation "
            "guard, so nobody mistakes it for enforcement",
        )
        self.assertIn(
            "not an enforcement",
            doc,
            "the module docstring must keep saying what this file is not, so "
            "nobody trusts it to stop a preserve-based PR",
        )


if __name__ == "__main__":
    unittest.main()
