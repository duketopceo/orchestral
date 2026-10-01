"""Tests that docs/publishing.md describes the policy the scrubber enforces.

`docs/publishing.md` is the only thing a publisher reads before copying
`runs-pub/` somewhere public, so a policy change that is not reflected there is
a defect in its own right: the document told a reader the opposite of what the
code does. These tests read the document and compare every enumerated policy
claim against the constant or code path that implements it.

Scope: these tests check agreement, not correctness. They cannot tell you the
policy is the right policy, only that the prose and the code describe the same
one.

Known limit: a container signature added to the code and never mentioned in the
document is invisible here, because the table below is written in prose terms
(named containers) while the code holds raw bytes. Closing that gap needs the
code to name its own signatures.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from orchestral.privacy import (
    _ARCHIVE_SIGNATURES,
    _BZIP2_SIGNATURE,
    ALLOWED_NAMES,
    ALLOWED_PREFIXES,
    ARCHIVE_EXTS,
    BINARY_EXTS,
    DATABASE_EXTS,
    _publication_block_reason,
    _read_prefix,
)

DOC = Path(__file__).resolve().parents[1] / "docs" / "publishing.md"

# Leading bytes that identify each container the document claims is detected by
# content rather than by extension. Byte-accurate against the production table;
# test_fixtures_match_production_signature_bytes keeps it that way.
CONTENT_SIGNATURES: dict[str, bytes] = {
    "ZIP": b"PK\x03\x04",
    "gzip": b"\x1f\x8b",
    "bzip2": b"BZh9" + b"1AY&SY",
    "xz": b"\xfd7zXZ\x00",
    "7z": b"7z\xbc\xaf\x27\x1c",
    "RAR": b"Rar!\x1a\x07",
    "POSIX tar": b" " * 257 + b"ustar",
}

# Filler with no NUL byte and no control characters, so a fixture is withheld
# because of its magic and nothing else. See test_filler_alone_is_published.
PADDING = b"P" * 32

# A neutral, allowlisted name. No archive suffix, so only a content check can
# withhold a file that carries it.
NEUTRAL_NAME = "report.json"

# Substring of the reason the archive rules return. Asserting the reason rather
# than merely "was blocked" is what keeps the fixtures honest.
ARCHIVE_REASON = "archive contents cannot be redacted"

# The window docs/publishing.md states as "8 KiB".
DOCUMENTED_WINDOW = 8192

EXTENSION = re.compile(r"`(\.[A-Za-z0-9]+)`")
NAME_TOKEN = re.compile(r"`([A-Za-z0-9_.\-*]+)`")


def _bullets(document: str) -> list[tuple[int, str]]:
    """Every markdown bullet as (indent, text), with wrapped lines folded in.

    Fenced code blocks are skipped: the release-verification commands in this
    document contain lines that would otherwise parse as bullets.
    """
    bullets: list[list] = []
    in_fence = False
    for raw in document.splitlines():
        stripped = raw.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        indent = len(raw) - len(stripped)
        if stripped.startswith(("- ", "* ")):
            bullets.append([indent, stripped[2:].strip()])
        elif bullets and indent > bullets[-1][0]:
            bullets[-1][1] += " " + stripped
    return [(indent, text) for indent, text in bullets]


def _bullet_starting(document: str, marker: str) -> str:
    """The single bullet whose text begins with `marker`, or fail loudly."""
    matches = [text for _, text in _bullets(document) if text.startswith(marker)]
    if len(matches) != 1:
        raise AssertionError(f"expected one bullet starting {marker!r}, found {len(matches)}")
    return matches[0]


def _extensions(bullet: str) -> set[str]:
    return set(EXTENSION.findall(bullet))


def _allowlist_names(bullet: str) -> set[str]:
    return {t for t in NAME_TOKEN.findall(bullet) if "." in t and not t.endswith("*")}


def _allowlist_prefixes(bullet: str) -> set[str]:
    return {t[:-1] for t in NAME_TOKEN.findall(bullet) if t.endswith("*")}


def _named_containers(bullet: str) -> set[str]:
    """The container names enumerated in an "archives by content" bullet."""
    _, separator, tail = bullet.partition("—")
    if not separator:
        raise AssertionError("archives-by-content bullet enumerates nothing after an em dash")
    names = set()
    for part in tail.split(","):
        name = part.strip().rstrip(".;: ")
        if name.lower().startswith("and "):
            name = name[4:]
        name = re.sub(r"\s+headers?$", "", name).strip()
        if name:
            names.add(name)
    return names


class TestPublishingDocMatchesPolicy(unittest.TestCase):
    """The publication policy documented in docs/publishing.md is the policy enforced."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.document = DOC.read_text(encoding="utf-8")

    def test_approved_media_and_font_extensions_are_documented(self) -> None:
        bullet = _bullet_starting(self.document, "**Copies approved media and fonts verbatim**")
        self.assertEqual(_extensions(bullet), BINARY_EXTS)

    def test_archive_extensions_are_documented(self) -> None:
        bullet = _bullet_starting(self.document, "archives by extension")
        self.assertEqual(_extensions(bullet), ARCHIVE_EXTS)

    def test_database_extensions_are_documented(self) -> None:
        bullet = _bullet_starting(self.document, "databases —")
        self.assertEqual(_extensions(bullet), DATABASE_EXTS)

    def test_allowlisted_filenames_are_documented(self) -> None:
        bullet = _bullet_starting(self.document, "**Copies only allowlisted names**")
        self.assertEqual(_allowlist_names(bullet), ALLOWED_NAMES)

    def test_allowlisted_prefixes_are_documented(self) -> None:
        bullet = _bullet_starting(self.document, "**Copies only allowlisted names**")
        self.assertEqual(_allowlist_prefixes(bullet), set(ALLOWED_PREFIXES))

    def test_documented_content_signatures_are_exactly_the_detected_ones(self) -> None:
        """The "a renamed archive is still withheld" claim must be exhaustive.

        Two directions, because either alone is a hole: a container the code
        detects but the document does not name is an undocumented withholding,
        and a container the document names that the code does not detect is an
        over-claim about the fail-closed posture.
        """
        bullet = _bullet_starting(self.document, "archives by content")
        self.assertEqual(_named_containers(bullet), set(CONTENT_SIGNATURES))

    def test_fixtures_match_production_signature_bytes(self) -> None:
        """Each fixture must begin with bytes the code actually looks for.

        Without this, a transcription slip in the table above would leave the
        suite testing the filler rather than the signature.
        """
        for name, prefix in CONTENT_SIGNATURES.items():
            with self.subTest(container=name):
                if name == "POSIX tar":
                    self.assertEqual(prefix[257:262], b"ustar")
                elif name == "bzip2":
                    self.assertIsNotNone(_BZIP2_SIGNATURE.match(prefix))
                else:
                    self.assertTrue(
                        prefix.startswith(_ARCHIVE_SIGNATURES),
                        f"{name} fixture does not begin with any production archive signature",
                    )

    def test_filler_alone_is_published(self) -> None:
        """The control: the filler used by every fixture must pass the policy.

        If the filler were itself withheld, a fixture could be blocked by the
        filler's NUL bytes or its encoding while the archive check sat untested,
        and deleting an archive check would leave this suite green.
        """
        self.assertNotIn(b"\0", PADDING)
        self.assertIsNone(
            _publication_block_reason(Path(NEUTRAL_NAME), PADDING),
            "fixture filler must be publishable, or the signature assertions prove nothing",
        )

    def test_every_documented_content_signature_is_actually_withheld(self) -> None:
        """Execute the document's claim: a renamed container is still withheld.

        The reason is asserted, not just the block, because "withheld" and
        "withheld as an archive" are different claims.
        """
        # Raises if the document dropped the claim this test exists to execute.
        self.assertTrue(_bullet_starting(self.document, "archives by content"))
        self.assertNotIn(Path(NEUTRAL_NAME).suffix.lower(), ARCHIVE_EXTS)
        for name, prefix in CONTENT_SIGNATURES.items():
            with self.subTest(container=name):
                reason = _publication_block_reason(Path(NEUTRAL_NAME), prefix + PADDING)
                self.assertIsNotNone(reason, f"{NEUTRAL_NAME} carrying {name} bytes would be published")
                self.assertIn(ARCHIVE_REASON, reason)

    def test_documented_inspection_window_matches_what_is_read(self) -> None:
        """The document states the inspection window as 8 KiB; pin it to the code."""
        self.assertIn("8 KiB", self.document)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "probe.txt"
            source.write_bytes(b"a" * (DOCUMENTED_WINDOW * 2) + b"\0")
            self.assertEqual(len(_read_prefix(source)), DOCUMENTED_WINDOW)

        # A NUL inside the inspected window is withheld; the same file without
        # one is not. This is the boundary the document describes.
        self.assertIsNotNone(
            _publication_block_reason(Path(NEUTRAL_NAME), b"a" * (DOCUMENTED_WINDOW - 1) + b"\0")
        )
        self.assertIsNone(_publication_block_reason(Path(NEUTRAL_NAME), b"a" * DOCUMENTED_WINDOW))


if __name__ == "__main__":
    unittest.main()
