"""U18: CLI tables. One helper, shared vocabulary, clean degradation.

Layers: unit goldens for ``orchestral/cli_table.py`` (Unicode and ASCII, width
80, NO_COLOR), command-level checks over the U23 fixture corpus, and a JSON
golden proving ``--json`` and exports are byte-identical to before U18
(display only: data unchanged, KTD3).
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import pty
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orchestral.cli_table import (
    Column,
    Note,
    Style,
    detect_style,
    fmt_duration_compact,
    format_error,
    n_cell,
    render_table,
    verdict_cell,
)

ROOT = Path(__file__).resolve().parent.parent
ANSI = re.compile(r"\x1b\[[0-9;]*m")
BOX = re.compile("[─-╿]")
SPEC = importlib.util.spec_from_file_location(
    "build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
assert SPEC and SPEC.loader
corpus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(corpus)

UNI = Style(unicode=True, color=False, width=80)
ASC = Style(unicode=False, color=False, width=80)


class _Stream(io.StringIO):
    def __init__(self, tty: bool, encoding: str = "utf-8"):
        super().__init__()
        self._tty = tty
        self._enc = encoding

    def isatty(self) -> bool:
        return self._tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._enc


class TestDetectStyle(unittest.TestCase):
    def test_tty_utf8_is_unicode_and_color(self):
        s = detect_style(_Stream(True), {"TERM": "xterm-256color"})
        self.assertTrue(s.unicode)
        self.assertTrue(s.color)

    def test_pipe_is_ascii_and_plain(self):
        s = detect_style(_Stream(False), {"TERM": "xterm"})
        self.assertFalse(s.unicode)
        self.assertFalse(s.color)

    def test_no_color_on_tty_keeps_glyphs(self):
        s = detect_style(_Stream(True), {"TERM": "xterm", "NO_COLOR": "1"})
        self.assertTrue(s.unicode)
        self.assertFalse(s.color)

    def test_term_dumb_is_ascii_and_plain(self):
        s = detect_style(_Stream(True), {"TERM": "dumb"})
        self.assertFalse(s.unicode)
        self.assertFalse(s.color)

    def test_non_utf8_encoding_is_ascii(self):
        s = detect_style(_Stream(True, "latin-1"), {"TERM": "xterm"})
        self.assertFalse(s.unicode)
        self.assertTrue(s.color)

    def test_orch_ascii_forces_ascii(self):
        s = detect_style(_Stream(True), {"TERM": "xterm", "ORCH_ASCII": "1"})
        self.assertFalse(s.unicode)

    def test_width_sources(self):
        self.assertEqual(detect_style(_Stream(False), {"COLUMNS": "100"}).width, 100)
        self.assertIsNone(detect_style(_Stream(False), {}).width)


class TestDuration(unittest.TestCase):
    def test_compact_forms(self):
        self.assertEqual(fmt_duration_compact(4_008_538), "66.8m")
        self.assertEqual(fmt_duration_compact(444_000), "7.4m")
        self.assertEqual(fmt_duration_compact(612), "612ms")
        self.assertEqual(fmt_duration_compact(12_340), "12.3s")
        self.assertEqual(fmt_duration_compact(None), "-")
        self.assertNotIn(" ", fmt_duration_compact(125_000))


class TestCells(unittest.TestCase):
    def test_verdict_glyph_and_word(self):
        self.assertEqual(verdict_cell(True, UNI)[0], "■ pass")
        self.assertEqual(verdict_cell(False, UNI)[0], "□ fail")
        self.assertEqual(verdict_cell(True, ASC)[0], "[+] pass")
        self.assertEqual(verdict_cell(False, ASC)[0], "[x] fail")
        self.assertEqual(verdict_cell(None, UNI)[0], "-")

    def test_low_n_marker(self):
        self.assertEqual(n_cell(2, True, UNI)[0], "2 ░ low n")
        self.assertEqual(n_cell(2, True, ASC)[0], "2 [.] low n")
        self.assertEqual(n_cell(292, False, UNI)[0], "292")


COLS = [
    Column("orchestrator", priority=0, max_width=24),
    Column("worker", priority=0, max_width=24),
    Column("n", "r", priority=0),
    Column("pass", "r", priority=1),
    Column("cost", "r", priority=0),
    Column("latency", "r", priority=9),
    Column("tokens", "r", priority=8),
]
ROWS = [
    ["corpus-orch-a", "corpus-worker-cheap", "292", "74%", "$0.0079", "66.8m", "12.3k"],
    ["corpus-orch-b", "corpus-worker-hot", "4", "50%", "$0.0192", "612ms", "999"],
]


class TestRender(unittest.TestCase):
    def test_golden_unicode_wide(self):
        out = render_table(COLS, ROWS, Style(True, False, 120))
        self.assertEqual(out, "\n".join([
            "orchestrator   worker                 n  pass     cost  latency  tokens",
            "─" * 71,
            "corpus-orch-a  corpus-worker-cheap  292   74%  $0.0079    66.8m   12.3k",
            "corpus-orch-b  corpus-worker-hot      4   50%  $0.0192    612ms     999",
        ]))

    def test_golden_ascii_wide(self):
        out = render_table(COLS, ROWS, Style(False, False, 120))
        self.assertEqual(out.splitlines()[1], "-" * 71)
        self.assertFalse(BOX.search(out))

    def test_drops_highest_priority_number_first_at_80(self):
        wide = [Column("orchestrator", max_width=40), Column("worker", max_width=40),
                Column("n", "r"), Column("pass", "r", priority=1),
                Column("cost", "r"), Column("latency", "r", priority=9),
                Column("tokens", "r", priority=8)]
        rows = [["o" * 30, "w" * 30, "9", "74%", "$0.0079", "66.8m", "12.3k"]]
        out = render_table(wide, rows, Style(True, False, 80))
        head = out.splitlines()[0]
        self.assertNotIn("latency", head)
        self.assertNotIn("tokens", head)
        for keep in ("orchestrator", "worker", "n", "pass", "cost"):
            self.assertIn(keep, head)
        for line in out.splitlines():
            self.assertLessEqual(len(line), 80)

    def test_long_cell_truncates_with_marker(self):
        cols = [Column("slug", max_width=10), Column("n", "r")]
        uni = render_table(cols, [["x" * 40, "1"]], UNI)
        asc = render_table(cols, [["x" * 40, "1"]], ASC)
        self.assertIn("xxxxxxxxx…", uni)
        self.assertIn("xxxxxxx...", asc)

    def test_unbounded_width_drops_nothing(self):
        out = render_table(COLS, ROWS, Style(False, False, None))
        self.assertIn("latency", out)

    def test_note_row_spans(self):
        out = render_table(COLS[:3], [ROWS[0][:3], Note("unranked: fewer than 3 runs"), ROWS[1][:3]], ASC)
        self.assertIn("unranked: fewer than 3 runs", out)

    def test_color_only_when_asked(self):
        plain = render_table(COLS[:3], [["a", "b", ("1", "fail")]], UNI)
        colored = render_table(COLS[:3], [["a", "b", ("1", "fail")]], Style(True, True, 80))
        self.assertFalse(ANSI.search(plain))
        self.assertTrue(ANSI.search(colored))
        self.assertEqual(ANSI.sub("", colored), plain)

    def test_no_trailing_whitespace(self):
        for line in render_table(COLS, ROWS, UNI).splitlines():
            self.assertEqual(line, line.rstrip())

    def test_null_rows_render_null_glyph_not_blank(self):
        out = render_table(COLS[:3], [["a", "b", None]], UNI)
        self.assertIn("-", out.splitlines()[-1])


class TestErrorFormat(unittest.TestCase):
    def test_summary_cause_fix_on_own_lines(self):
        msg = format_error("no runs found", "the runs directory is empty",
                           "python harness.py run --task t --orchestrator o --worker w")
        lines = msg.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("error: no runs found"))
        self.assertIn("empty", lines[1])
        self.assertTrue(lines[2].startswith("fix: python harness.py"))


# ---- command level over the U23 corpus ---------------------------------------

_TMP: tempfile.TemporaryDirectory | None = None
_CORPUS: Path


def setUpModule():
    global _TMP, _CORPUS
    _TMP = tempfile.TemporaryDirectory()
    _CORPUS = Path(_TMP.name) / "corpus"
    corpus.build_corpus(_CORPUS, "full")


def tearDownModule():
    if _TMP:
        _TMP.cleanup()


def _run(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "NO_COLOR", "COLUMNS", "ORCH_ASCII", "TERM")}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(ROOT / "harness.py"), "--runs-dir", str(_CORPUS),
         "--models-dir", str(ROOT / "tests" / "fixtures" / "observatory" / "models"), *args],
        capture_output=True, text=True, env=env, cwd=ROOT)


class TestCommandTables(unittest.TestCase):
    def test_piped_output_has_no_ansi_and_no_box_drawing(self):
        for args in (["report", "--limit", "5"], ["report", "--leaderboard"], ["report", "--pairings"],
                     ["report", "--groups"], ["history"], ["prices"],
                     ["report", "--compare", "corpus-dry,corpus-failed"]):
            r = _run(*args)
            self.assertEqual(r.returncode, 0, (args, r.stderr))
            self.assertFalse(ANSI.search(r.stdout), args)
            self.assertFalse(BOX.search(r.stdout), args)
            self.assertTrue(r.stdout.isascii(), (args, [c for c in r.stdout if ord(c) > 127][:5]))

    def test_leaderboard_at_80_drops_latency_first_keeps_core(self):
        r = _run("report", "--leaderboard", env_extra={"COLUMNS": "80"})
        head = r.stdout.splitlines()[0]
        self.assertNotIn("latency", head)
        for keep in ("orchestrator", "worker", "n", "cost"):
            self.assertIn(keep, head)
        table = r.stdout.split("\n\n")[0]  # prose notes below the table are not columns
        for line in table.splitlines():
            self.assertLessEqual(len(line), 80, line)

    def test_leaderboard_full_width_has_compact_latency_not_raw_ms(self):
        r = _run("report", "--leaderboard", env_extra={"COLUMNS": "300"})
        self.assertIn("latency", r.stdout.splitlines()[0])
        self.assertNotRegex(r.stdout, r"\b2428\b")
        self.assertRegex(r.stdout, r"\b2\.4s\b")

    def test_money_uses_shared_tiers(self):
        r = _run("history")
        self.assertNotRegex(r.stdout, r"\$\s+\d+\.\d{6}")
        self.assertRegex(r.stdout, r"\$0\.0\d{2,3}\b")

    def test_report_list_has_verdict_words_and_compact_tokens(self):
        r = _run("report", "--limit", "8", env_extra={"COLUMNS": "300"})
        self.assertIn("[+] pass", r.stdout)
        self.assertIn("[x] fail", r.stdout)
        self.assertRegex(r.stdout, r"\d+\.\dk")

    def test_orch_ascii_unaffected_unicode_forced_never_on_pipe(self):
        r = _run("report", "--limit", "3", env_extra={"TERM": "xterm-256color"})
        self.assertTrue(r.stdout.isascii())

    def test_no_color_env_has_no_escapes(self):
        r = _run("report", "--leaderboard", env_extra={"NO_COLOR": "1"})
        self.assertFalse(ANSI.search(r.stdout))


def _run_pty(*args: str, env_extra: dict | None = None) -> str:
    """Run the CLI with a pseudo-terminal as stdout (a real TTY, UTF-8)."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "NO_COLOR", "COLUMNS", "ORCH_ASCII", "TERM")}
    env.update({"TERM": "xterm-256color", "PYTHONIOENCODING": "utf-8", "COLUMNS": "80"})
    env.update(env_extra or {})
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "harness.py"), "--runs-dir", str(_CORPUS),
         "--models-dir", str(ROOT / "tests" / "fixtures" / "observatory" / "models"), *args],
        stdout=slave, stderr=subprocess.PIPE, env=env, cwd=ROOT)
    os.close(slave)
    chunks = []
    while True:
        try:
            data = os.read(master, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    proc.wait()
    os.close(master)
    assert proc.returncode == 0, proc.stderr.read() if proc.stderr else ""
    return b"".join(chunks).decode("utf-8").replace("\r\n", "\n")


GOLDEN_DIR = ROOT / "tests" / "fixtures" / "cli_tables"
GOLDEN_CMDS = {
    "report": ["report", "--limit", "10"],
    "leaderboard": ["report", "--leaderboard"],
    "pairings": ["report", "--pairings"],
    "groups": ["report", "--groups", "--group", "corpus-holdout"],
    "delta": ["report", "--compare", "corpus-dry,corpus-failed"],
    "contamination": ["report", "--contamination"],
    "history": ["history"],
    "prices": ["prices"],
}


class TestGoldenRenders(unittest.TestCase):
    """Corpus renders at 80 columns, ASCII (piped) and Unicode (TTY). Regenerate
    with ``ORCH_UPDATE_GOLDEN=1`` and review the diff by eye."""

    def _check(self, name: str, mode: str, actual: str):
        path = GOLDEN_DIR / f"{name}.{mode}.txt"
        if os.environ.get("ORCH_UPDATE_GOLDEN"):
            GOLDEN_DIR.mkdir(exist_ok=True)
            path.write_text(actual, encoding="utf-8")
        self.assertEqual(actual, path.read_text(encoding="utf-8"), f"{name} {mode}")

    def test_ascii_goldens(self):
        for name, args in GOLDEN_CMDS.items():
            with self.subTest(name):
                self._check(name, "ascii", _run(*args, env_extra={"COLUMNS": "80"}).stdout)

    def test_unicode_goldens(self):
        for name, args in GOLDEN_CMDS.items():
            with self.subTest(name):
                out = _run_pty(*args, env_extra={"NO_COLOR": "1"})
                self.assertFalse(ANSI.search(out))
                self._check(name, "unicode", out)

    def test_tty_color_on_by_default_and_off_with_no_color(self):
        colored = _run_pty("report", "--limit", "4")
        self.assertTrue(ANSI.search(colored))
        self.assertIn("\u25a0 pass", ANSI.sub("", colored))
        plain = _run_pty("report", "--limit", "4", env_extra={"NO_COLOR": "1"})
        self.assertFalse(ANSI.search(plain))
        self.assertIn("\u25a0 pass", plain)
        self.assertEqual(ANSI.sub("", colored), plain)

    def test_term_dumb_on_tty_is_ascii_and_plain(self):
        out = _run_pty("report", "--limit", "4", env_extra={"TERM": "dumb"})
        self.assertFalse(ANSI.search(out))
        self.assertTrue(out.isascii())
        self.assertIn("[+] pass", out)


class TestJsonUnchanged(unittest.TestCase):
    """Hashes recorded from the pre-U18 code on this corpus (run_dir normalised)."""

    def test_json_and_exports_byte_identical(self):
        golden = json.loads((ROOT / "tests" / "fixtures" / "cli_json_golden.json").read_text())
        for name, args in golden["cmds"].items():
            with self.subTest(name):
                r = _run(*args)
                self.assertEqual(r.returncode, 0, r.stderr)
                text = r.stdout.replace(str(_CORPUS), "<ROOT>")
                self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), golden["sha256"][name])


if __name__ == "__main__":
    unittest.main()
