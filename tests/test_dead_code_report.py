"""Tests for scripts/dead_code_report.py — the tiered candidate join."""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dead_code_report as dcr


def _fixture_db(path: Path) -> None:
    """Mini graph.db: one dead fn, one called fn, one cmd_* root, helpers."""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE projects (name TEXT PRIMARY KEY, indexed_at TEXT NOT NULL,
                               root_path TEXT NOT NULL);
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL, label TEXT NOT NULL, name TEXT NOT NULL,
            qualified_name TEXT NOT NULL, file_path TEXT DEFAULT '',
            start_line INTEGER DEFAULT 0, end_line INTEGER DEFAULT 0,
            properties TEXT DEFAULT '{}');
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL, source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL, type TEXT NOT NULL,
            properties TEXT DEFAULT '{}');
        INSERT INTO projects VALUES ('orchestral', 'now', '/repo');
        """
    )
    nodes = [
        # called helper — a CALLS edge points at it, not a candidate
        ("Function", "alive", "orchestral.orchestral.foo.alive",
         "orchestral/foo.py", 5, 9),
        # dead helper — the fixture's one true candidate
        ("Function", "dead", "orchestral.orchestral.foo.dead",
         "orchestral/foo.py", 12, 20),
        # registered CLI root — zero inbound but registry-listed
        ("Function", "cmd_serve", "orchestral.harness.cmd_serve",
         "harness.py", 30, 40),
        # caller for `alive`
        ("Function", "caller", "orchestral.orchestral.foo.caller",
         "orchestral/foo.py", 22, 28),
        # dunder — pattern-excluded even at zero inbound
        ("Function", "__repr__", "orchestral.orchestral.foo.W.__repr__",
         "orchestral/foo.py", 42, 44),
        # test discovery — pattern-excluded
        ("Function", "test_it", "orchestral.tests.test_foo.test_it",
         "tests/test_foo.py", 3, 8),
        # a zero-inbound JS symbol — tier C
        ("Function", "renderThing", "orchestral.ui.js.app.renderThing",
         "ui/js/app.js", 10, 15),
        # __getattr__ module's dead helper — capped at B
        ("Function", "wrapped", "orchestral.orchestral.proxy.wrapped",
         "orchestral/proxy.py", 10, 14),
        # callback absorbed by USAGE edge — not a candidate
        ("Function", "callback", "orchestral.orchestral.foo.callback",
         "orchestral/foo.py", 46, 50),
    ]
    for label, name, qn, fp, start, end in nodes:
        db.execute(
            "INSERT INTO nodes (project,label,name,qualified_name,file_path,"
            "start_line,end_line) VALUES ('orchestral',?,?,?,?,?,?)",
            (label, name, qn, fp, start, end),
        )
    # caller -> alive (CALLS), caller -> callback (USAGE)
    db.execute(
        "INSERT INTO edges (project,source_id,target_id,type) "
        "VALUES ('orchestral',4,1,'CALLS')"
    )
    db.execute(
        "INSERT INTO edges (project,source_id,target_id,type) "
        "VALUES ('orchestral',4,9,'USAGE')"
    )
    db.commit()
    db.close()


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "graph.db"
        _fixture_db(self.db_path)
        self.artifact = root / "artifact.json"
        self.repo = root / "repo"
        (self.repo / "orchestral").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        (self.repo / "harness.py").write_text("def main():\n    pass\n")
        (self.repo / "orchestral" / "foo.py").write_text("x = 1\n")
        (self.repo / "orchestral" / "proxy.py").write_text(
            "def __getattr__(name):\n    return None\n"
        )
        self.registry = root / "entrypoints.yaml"
        self.registry.write_text(
            "schema: 1\npatterns: []\nentry_points:\n"
            "- name: harness.cmd_serve\n  kind: cli_command\n"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _report(self, **kw) -> dict:
        self.artifact.write_text(
            json.dumps({"commit": kw.pop("commit", "head-sha")})
        )
        return dcr.build_report(
            db_path=self.db_path,
            artifact_path=self.artifact,
            registry_path=self.registry,
            repo_root=self.repo,
            head="head-sha",
            **kw,
        )


class TestPoolAndFilters(_Fixture):
    def test_yields_only_the_dead_function(self) -> None:
        rows = self._report()["rows"]
        symbols = {r["symbol"] for r in rows}
        self.assertEqual(
            symbols,
            {"orchestral.foo.dead", "orchestral.foo.caller",
             "orchestral.proxy.wrapped", "ui.js.app.renderThing"},
        )

    def test_registry_symbol_never_a_candidate(self) -> None:
        rows = self._report()["rows"]
        self.assertNotIn("harness.cmd_serve", {r["symbol"] for r in rows})

    def test_getattr_module_caps_at_b_with_note(self) -> None:
        rows = self._report()["rows"]
        wrapped = next(r for r in rows if r["symbol"].endswith("wrapped"))
        self.assertEqual(wrapped["tier"], "B")
        self.assertIn("__getattr__", wrapped["note"])

    def test_js_always_tier_c(self) -> None:
        rows = self._report()["rows"]
        js = next(r for r in rows if r["file"].startswith("ui/js/"))
        self.assertEqual(js["tier"], "C")
        self.assertEqual(js["surface"], "js")

    def test_freshness_gate(self) -> None:
        with self.assertRaises(SystemExit):
            self._report(commit="old-sha")
        report = self._report(commit="old-sha", allow_stale=True)
        self.assertFalse(report["index_fresh"])
        self.assertEqual(report["index_commit"], "old-sha")


class TestEvidenceJoin(_Fixture):
    def _vulture(self, text: str) -> Path:
        p = Path(self.tmp.name) / "vulture.txt"
        p.write_text(text)
        return p

    def _coverage(self, executed: dict) -> Path:
        p = Path(self.tmp.name) / "cov.json"
        p.write_text(json.dumps({"files": executed}))
        return p

    def test_two_sources_make_tier_a(self) -> None:
        v = self._vulture(
            "orchestral/foo.py:14: unused function 'dead' (60% confidence)\n"
        )
        c = self._coverage({"orchestral/foo.py": {"executed_lines": [5, 6, 7]}})
        rows = self._report(vulture_path=v, coverage_path=c)["rows"]
        dead = next(r for r in rows if r["symbol"].endswith(".dead"))
        self.assertEqual(dead["tier"], "A")
        self.assertEqual(len(dead["evidence"]), 3)

    def test_graph_only_is_tier_b(self) -> None:
        rows = self._report()["rows"]
        dead = next(r for r in rows if r["symbol"].endswith(".dead"))
        self.assertEqual(dead["tier"], "B")
        self.assertEqual(len(dead["evidence"]), 1)

    def test_coverage_executed_drops_candidate(self) -> None:
        c = self._coverage(
            {"orchestral/foo.py": {"executed_lines": [15]}}
        )
        rows = self._report(coverage_path=c)["rows"]
        self.assertNotIn("orchestral.foo.dead", {r["symbol"] for r in rows})

    def test_no_optional_sources_means_no_tier_a(self) -> None:
        report = self._report()
        self.assertEqual(report["counts"]["A"], 0)
        self.assertFalse(report["sources"]["vulture"])

    def test_deterministic_output(self) -> None:
        v = self._vulture(
            "orchestral/foo.py:14: unused function 'dead' (60% confidence)\n"
        )
        a = json.dumps(self._report(vulture_path=v), sort_keys=True)
        b = json.dumps(self._report(vulture_path=v), sort_keys=True)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
