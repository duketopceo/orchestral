"""Tests for the multi-file file-set contract (parse, sanitize, merge, zip)."""

from __future__ import annotations

import io
import unittest
import zipfile
from unittest.mock import patch

from orchestral.fileset import (
    MAX_FILE_CONTENT_BYTES,
    MAX_FILES_PER_SET,
    FilesetError,
    build_zip,
    check_response_size,
    expected_paths,
    manifest_listing,
    merge_filesets,
    parse_fileset,
    sanitize_path,
    summarize_fileset,
    validate_fileset,
)


class TestParseFileset(unittest.TestCase):
    def test_documented_shape(self):
        files = parse_fileset({"files": [{"path": "index.html", "content": "<h1>x</h1>"}]})
        self.assertEqual(files, {"index.html": "<h1>x</h1>"})

    def test_fenced_json_and_bare_map(self):
        files = parse_fileset({"index.html": "<h1>a</h1>", "notes": "ignore me"})
        self.assertEqual(files, {"index.html": "<h1>a</h1>"})

    def test_files_map_shape(self):
        files = parse_fileset({"files": {"style.css": "body{}"}})
        self.assertEqual(files, {"style.css": "body{}"})

    def test_prose_without_json_yields_nothing(self):
        self.assertEqual(parse_fileset(None), {})
        self.assertEqual(parse_fileset({"notes": "no files"}), {})

    def test_non_string_content_is_coerced(self):
        files = parse_fileset({"files": [{"path": "data.json", "content": {"a": 1}}]})
        self.assertEqual(files["data.json"], "{'a': 1}")

    def test_paths_are_canonicalized(self):
        files = parse_fileset({"files": [{"path": "Assets/Site.CSS", "content": "x"}]})
        self.assertEqual(list(files), ["assets/site.css"])

    def test_oversized_response_rejected_before_parse(self):
        with self.assertRaises(FilesetError):
            check_response_size("x" * 1_000_001)

    def test_per_file_cap(self):
        big = "x" * (MAX_FILE_CONTENT_BYTES + 1)
        with self.assertRaises(FilesetError):
            parse_fileset({"files": [{"path": "big.txt", "content": big}]})

    def test_file_count_cap(self):
        files = {f"f{i}.txt": "x" for i in range(MAX_FILES_PER_SET + 1)}
        with self.assertRaises(FilesetError):
            parse_fileset(files)


class TestSanitizePath(unittest.TestCase):
    def test_accepts_ordinary_paths(self):
        self.assertEqual(sanitize_path("index.html"), "index.html")
        self.assertEqual(sanitize_path("assets/site.css"), "assets/site.css")
        self.assertEqual(sanitize_path("./index.html"), "index.html")

    def test_rejects_unsafe_paths(self):
        for bad in (
            "/etc/passwd",
            "../../etc/passwd",
            "..\\..\\evil.txt",
            "C:\\evil.txt",
            "C:relative.txt",
            "\\\\server\\share\\x",
            "a/../b",
            "a/./b",
            "",
            "   ",
            "CON",
            "nul.txt",
            "com1.js",
            "a:b.txt",
            "a<b.txt",
            "a|b.txt",
            "a\x00b.txt",
            "a\x1fb.txt",
            "index.html.",
            ".hidden",
            "caf\u00e9.html",
            "x" * 300,
            # encoded separators/traversal must not survive into a member name
            "a%2f%2e%2e%2fb",
            "%2e%2e%2f",
            "a%5c..%5cb",
            "%252e%252e%252f",
            "50%.html",
        ):
            with self.assertRaises(FilesetError, msg=bad):
                sanitize_path(bad)

    def test_collapses_repeated_separators(self):
        self.assertEqual(sanitize_path("a//b.txt"), "a/b.txt")

    def test_trims_surrounding_whitespace_on_the_whole_path(self):
        self.assertEqual(sanitize_path("  index.html  "), "index.html")

    def test_rejects_trailing_dot_segment(self):
        with self.assertRaises(FilesetError):
            sanitize_path("index.html.")


class TestValidateFileset(unittest.TestCase):
    def test_rejects_case_collision(self):
        with self.assertRaises(FilesetError):
            parse_fileset({"files": [
                {"path": "Index.HTML", "content": "a"},
                {"path": "index.html", "content": "b"},
            ]})

    def test_rejects_backslash_collision(self):
        with self.assertRaises(FilesetError):
            parse_fileset({"files": [
                {"path": "a/b.txt", "content": "a"},
                {"path": "a\\b.txt", "content": "b"},
            ]})

    def test_rejects_prefix_conflict(self):
        with self.assertRaises(FilesetError):
            validate_fileset({"foo": "a", "foo/bar": "b"})

    def test_rejects_directory_entry(self):
        with self.assertRaises(FilesetError):
            validate_fileset({"dir/": ""})

    def test_accepts_disjoint_set(self):
        validate_fileset({"index.html": "a", "style.css": "b"})


class TestMergeFilesets(unittest.TestCase):
    def test_union_and_later_wins(self):
        merged, conflicts = merge_filesets([
            (0, {"index.html": "first", "style.css": "a"}),
            (1, {"index.html": "second", "app.js": "b"}),
        ])
        self.assertEqual(merged["index.html"], "second")
        self.assertEqual(merged["app.js"], "b")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["path"], "index.html")
        self.assertEqual(conflicts[0]["winner_subtask"], 1)
        self.assertEqual(conflicts[0]["loser_subtask"], 0)

    def test_conflicts_carry_no_content(self):
        _, conflicts = merge_filesets([
            (0, {"a.txt": "SECRET"}),
            (1, {"a.txt": "ALSO_SECRET"}),
        ])
        self.assertNotIn("SECRET", str(conflicts))


class TestBuildZip(unittest.TestCase):
    def test_deterministic_bytes_regardless_of_insertion_order(self):
        one = build_zip({"a.txt": "alpha", "b.txt": "beta"})
        two = build_zip({"b.txt": "beta", "a.txt": "alpha"})
        self.assertEqual(one, two)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(one)))

    def test_entries_sorted_and_regular_files(self):
        data = build_zip({"z.txt": "z", "a/b.txt": "b", "index.html": "h"})
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
        self.assertEqual([i.filename for i in infos], ["a/b.txt", "index.html", "z.txt"])
        for info in infos:
            self.assertFalse(info.filename.endswith("/"))
            self.assertEqual(info.external_attr >> 16, 0o100644)
            self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))

    def test_contents_round_trip(self):
        data = build_zip({"index.html": "<h1>hi</h1>"})
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(archive.read("index.html"), b"<h1>hi</h1>")

    def test_output_cap(self):
        with patch("orchestral.fileset.MAX_ZIP_OUTPUT_BYTES", 10), self.assertRaises(FilesetError):
            build_zip({"a.txt": "x" * 5000})

    def test_rejects_non_canonical_path(self):
        # build_zip is the last boundary before bytes exist — an unsanitized
        # path must not be zippable even by a direct caller
        for bad in ("Index.HTML", "../escape.txt", "a//b.txt", "a\\b.txt"):
            with self.assertRaises(FilesetError, msg=bad):
                build_zip({bad: "x"})

    def test_rejects_non_string_values(self):
        with self.assertRaises(FilesetError):
            build_zip({"a.txt": 123})  # type: ignore[dict-item]


class TestCapsAreByteBudgets(unittest.TestCase):
    def test_multibyte_content_counts_as_bytes(self):
        # 300k CJK characters are ~900KB of UTF-8, over the 500KB per-file cap
        body = "\u4e2d" * 300_000
        with self.assertRaises(FilesetError):
            parse_fileset({"files": [{"path": "big.txt", "content": body}]})

    def test_multibyte_response_counts_as_bytes(self):
        with self.assertRaises(FilesetError):
            check_response_size("\u4e2d" * 400_000)


class TestSummaries(unittest.TestCase):
    def test_summarize_has_no_content(self):
        summary = summarize_fileset({"index.html": "SECRET_BODY"})
        self.assertNotIn("SECRET_BODY", str(summary))
        self.assertEqual(summary["paths"], ["index.html"])
        self.assertEqual(summary["sizes"]["index.html"], len("SECRET_BODY"))
        self.assertEqual(len(summary["sha256"]["index.html"]), 64)

    def test_manifest_listing_is_paths_and_sizes(self):
        listing = manifest_listing({"index.html": "SECRET_BODY"})
        self.assertIn("index.html", listing)
        self.assertNotIn("SECRET_BODY", listing)

    def test_expected_paths_sanitizes_and_tolerates_garbage(self):
        self.assertEqual(
            expected_paths({"expected_paths": ["Index.HTML", "assets/site.css"]}),
            ["index.html", "assets/site.css"],
        )
        self.assertEqual(expected_paths({}), [])
        self.assertEqual(expected_paths({"expected_paths": "nope"}), [])


if __name__ == "__main__":
    unittest.main()
