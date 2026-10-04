"""U14 payloads: the New run task picker and the Models provider-sync stamp.

Key-free: temp specs and a temp run index, no provider is ever contacted."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from orchestral import remotecatalog
from orchestral.storage import RunMeta, RunStore
from orchestral.web import state
from orchestral.web.catalog import models_catalog_payload
from orchestral.web.server import Observatory, make_handler

TASKS = {
    "code-slugify.yaml": "id: code-slugify\ntype: code\ntitle: Slugify\nprompt: p\nmetadata: {difficulty: easy}\n",
    "bugfix-lru.yaml": "id: bugfix-lru\ntype: code\nprompt: p\nmetadata: {difficulty: hard, archetype: bugfix}\n",
    "html-hero.yaml": "id: html-hero\ntype: html\nprompt: p\n",
    "batch/sql-top.yaml": "id: sql-top\ntype: sql\nprompt: p\nmetadata: {difficulty: medium}\n",
}


def _meta(rid: str, task: str, cost: float, **kw) -> RunMeta:
    base = {"run_id": rid, "orchestrator": "o/m", "task_id": task, "worker": "w/m", "status": "finished",
            "started_at": "2026-10-01T00:00:00+00:00", "finished_at": "2026-10-01T00:01:00+00:00",
            "total_cost_usd": cost, "passes": True, "run_group": "g", "config": {}, "run_dir": ""}
    base.update(kw)
    return RunMeta(**base)


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        for rel, text in TASKS.items():
            p = self.tasks / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.models = root / "models"
        self.models.mkdir()
        (self.models / "m.yaml").write_text(
            "models:\n  - slug: o/m\n    name: o\n    role: orchestrator\n"
            "    input_price_per_mtok: 1\n    output_price_per_mtok: 2\n")
        self.store = RunStore(root / "runs")

    def tearDown(self):
        self.tmp.cleanup()


class TestTaskPicker(_Fixture):
    def test_rows_carry_type_family_difficulty_and_history(self):
        for i, c in enumerate((0.01, 0.03, 0.02)):
            self.store.index_meta(_meta(f"r{i}", "code-slugify", c))
        self.store.index_meta(_meta("dry", "code-slugify", 0.0, dry_run=True))
        d = state.task_picker_payload(self.store, self.tasks)
        rows = {r["id"]: r for r in d["tasks"]}
        self.assertEqual(set(rows), {"code-slugify", "bugfix-lru", "html-hero", "sql-top"})
        slug = rows["code-slugify"]
        self.assertEqual((slug["type"], slug["difficulty"], slug["title"]), ("code", "easy", "Slugify"))
        self.assertEqual(slug["runs"], 3)  # dry runs are not history
        self.assertAlmostEqual(slug["expected_cost_usd"], 0.02)  # median of billed runs
        self.assertIsNone(rows["html-hero"]["expected_cost_usd"])  # unknown, never $0
        self.assertEqual(rows["html-hero"]["runs"], 0)

    def test_family_is_archetype_then_folder_then_empty(self):
        rows = {r["id"]: r for r in state.task_picker_payload(self.store, self.tasks)["tasks"]}
        self.assertEqual(rows["bugfix-lru"]["family"], "bugfix")
        self.assertEqual(rows["sql-top"]["family"], "batch")
        self.assertEqual(rows["html-hero"]["family"], "")

    def test_rows_sort_by_type_then_id_and_list_the_types(self):
        d = state.task_picker_payload(self.store, self.tasks)
        self.assertEqual([r["id"] for r in d["tasks"]], ["bugfix-lru", "code-slugify", "html-hero", "sql-top"])
        self.assertEqual(d["types"], ["code", "html", "sql"])

    def test_served_at_api_task_picker_and_tasks_list_is_unchanged(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Observatory(
            Path(self.tmp.name) / "runs", self.tasks, self.models)))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with urllib.request.urlopen(base + "/api/task-picker") as r:
            self.assertEqual(len(json.loads(r.read())["tasks"]), 4)
        with urllib.request.urlopen(base + "/api/tasks") as r:
            self.assertTrue(all(isinstance(x, str) for x in json.loads(r.read())))


class TestProviderSyncStamp(_Fixture):
    def test_never_synced_when_the_catalog_file_is_absent(self):
        d = models_catalog_payload(self.store, self.models)
        self.assertEqual(d["provider_sync"], {"state": "never", "synced_at": None, "source": None})

    def test_synced_carries_time_and_source(self):
        remotecatalog.catalog_path(self.models).write_text(json.dumps({
            "fetched_at": "2026-10-01T12:00:00", "source": "openrouter", "models": []}))
        d = models_catalog_payload(self.store, self.models)
        self.assertEqual(d["provider_sync"],
                         {"state": "synced", "synced_at": "2026-10-01T12:00:00", "source": "openrouter"})
        self.assertEqual(d["provider_synced_at"], "2026-10-01T12:00:00")


if __name__ == "__main__":
    unittest.main()
