"""Web observatory: pure layer, HTTP routes, launch/cancel."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner
from orchestral.storage import RunStore
from orchestral.tui.state import JobStatus
from orchestral.web import render, state
from orchestral.web.server import Observatory, make_handler


def _model(slug: str, role: str) -> ModelConfig:
    return ModelConfig(slug=slug, name=slug, role=role,
                       input_price_per_mtok=0.03, output_price_per_mtok=0.10)


def _seed_run(runs_dir: str, **kw) -> str:
    meta = Runner(dry_run=True, runs_dir=runs_dir, store=RunStore(runs_dir), **kw).run(
        TaskSpec(id="t-task", type="html", prompt="p"),
        _model("o/model", "orchestrator"), _model("w/model", "worker"),
    )
    return meta.run_id


def _write_specs(root: Path) -> tuple[Path, Path]:
    tasks = root / "tasks"
    models = root / "models"
    tasks.mkdir()
    models.mkdir()
    (tasks / "t-task.yaml").write_text("id: t-task\ntype: html\nprompt: p\n")
    (models / "m.yaml").write_text(
        "models:\n"
        "  - slug: o/model\n    name: o\n    role: orchestrator\n"
        "    input_price_per_mtok: 0.03\n    output_price_per_mtok: 0.10\n"
        "  - slug: w/model\n    name: w\n    role: worker\n"
        "    input_price_per_mtok: 0.03\n    output_price_per_mtok: 0.10\n"
    )
    return tasks, models


def _wait(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


class TestImportHygiene(unittest.TestCase):
    def test_web_package_never_imports_textual(self):
        code = (
            "import orchestral.web, orchestral.web.server, orchestral.web.state, "
            "orchestral.web.render, sys; "
            "sys.exit(1 if any(m.startswith('textual') for m in sys.modules) else 0)"
        )
        r = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent.parent)
        self.assertEqual(r.returncode, 0)


class TestPureLayer(unittest.TestCase):
    def test_live_payload_cursor_slices_and_derives(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = _seed_run(tmp)
            run_dir = Path(RunStore(tmp).get_run(rid).run_dir)
            p0 = state.live_payload(run_dir, 0)
            self.assertGreater(p0["next"], 0)
            self.assertEqual(len(p0["rows"]), p0["next"])
            self.assertIn(p0["phase"], ("finished", "evaluating", "accounting"))
            p1 = state.live_payload(run_dir, p0["next"])
            self.assertEqual(p1["rows"], [])
            self.assertEqual(p1["next"], p0["next"])
            # cursor past EOF (rewritten file) → resync, not an empty diff
            p2 = state.live_payload(run_dir, p0["next"] + 999)
            self.assertEqual(len(p2["rows"]), p0["next"])

    def test_run_sections_degrade_on_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "r-missing"
            run_dir.mkdir()
            sections = state.run_sections(run_dir)
            self.assertEqual(sections["events"], [])
            self.assertIsNone(sections["report"])
            self.assertIsNone(sections["manifest"])

    def test_run_sections_malformed_events_dont_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "r-bad"
            run_dir.mkdir()
            (run_dir / "events.jsonl").write_text('{"type":"x"}\nnot-json\n{"type":"y"}\n')
            sections = state.run_sections(run_dir)
            self.assertEqual(len(sections["events"]), 3)

    def test_leaderboard_and_history_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_run(tmp)
            store = RunStore(tmp)
            rows = state.leaderboard_rows(store)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["orchestrator"], "o/model")
            hist = state.history_rows(store)
            self.assertEqual(len(hist), 1)
            self.assertEqual(state.history_rows(store, "nomatch"), [])

    def test_annotation_roundtrip_upsert_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            store.set_annotation("run", "r1", "interesting", "worth a post")
            store.set_annotation("group", "g1", "not")
            anns = {(a["kind"], a["target"]): a for a in store.annotations()}
            self.assertEqual(anns[("run", "r1")]["flag"], "interesting")
            self.assertEqual(anns[("run", "r1")]["note"], "worth a post")
            self.assertEqual(anns[("group", "g1")]["flag"], "not")
            # upsert on the same (kind, target) updates rather than duplicating
            store.set_annotation("run", "r1", "not", "revised")
            anns = store.annotations()
            self.assertEqual(len(anns), 2)
            self.assertEqual(anns[0]["flag"] if anns[0]["target"] == "r1" else anns[1]["flag"], "not")
            # clearing with '' keeps the row (note survives) but drops the flag
            store.set_annotation("run", "r1", "", "still noted")
            anns = {(a["kind"], a["target"]): a for a in store.annotations()}
            self.assertEqual(anns[("run", "r1")]["flag"], "")
            self.assertEqual(anns[("run", "r1")]["note"], "still noted")
            with self.assertRaises(ValueError):
                store.set_annotation("bogus", "x", "interesting")
            with self.assertRaises(ValueError):
                store.set_annotation("run", "x", "bogus")

    def test_card_payload_run_group_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = _seed_run(tmp, run_group="g-cards")
            store = RunStore(tmp)
            store.set_annotation("run", rid, "interesting")
            card = state.card_payload(store, "run", rid)
            self.assertIsNotNone(card)
            self.assertEqual(card["kind"], "run")
            self.assertEqual(card["suite"], "v3")
            self.assertEqual(card["task_id"], "t-task")
            self.assertEqual(card["flag"], "interesting")
            gcard = state.card_payload(store, "group", "g-cards")
            self.assertIsNotNone(gcard)
            self.assertEqual(gcard["suite"], "v3")
            self.assertEqual(gcard["runs"], 1)
            self.assertEqual(len(gcard["pairings"]), 1)
            self.assertIsNone(state.card_payload(store, "run", "ghost"))
            self.assertIsNone(state.card_payload(store, "group", "ghost"))

    def test_escaping_in_error_pages(self):
        # model/path strings reach the browser through render.py's error
        # pages — they must escape, same contract the SPA's esc() upholds
        evil = "<script>alert(1)</script>"
        html = render.render_not_found(evil)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>alert", html)


class TestJobRegistry(unittest.TestCase):
    def test_launch_dry_run_completes_and_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks, models = _write_specs(Path(tmp))
            store = RunStore(Path(tmp) / "runs")
            reg = state.JobRegistry(Path(tmp) / "runs", tasks, models, store)
            job = reg.launch({
                "task": "t-task", "orchestrator": "o/model", "worker": "w/model",
                "replicates": 1, "dry_run": True,
            })
            self.assertTrue(_wait(lambda: job.status == JobStatus.SUCCEEDED))
            self.assertEqual(len(job.run_ids), 1)
            self.assertIsNotNone(store.get_run(job.run_ids[0]))

    def test_launch_rejects_unknown_and_missing_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks, models = _write_specs(Path(tmp))
            reg = state.JobRegistry(Path(tmp) / "runs", tasks, models,
                                    RunStore(Path(tmp) / "runs"))
            with self.assertRaises(ValueError):
                reg.launch({"task": "t", "orchestrator": "o", "worker": "w", "hack": "1"})
            with self.assertRaises(ValueError):
                reg.launch({"task": "t"})

    def test_cancel_propagates_to_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks, models = _write_specs(Path(tmp))
            reg = state.JobRegistry(Path(tmp) / "runs", tasks, models,
                                    RunStore(Path(tmp) / "runs"))

            class SlowRunner:
                def __init__(self, **kwargs):
                    self.kwargs = kwargs

                def run(self, task, orch, worker, judge):
                    rid = "r-cancel"
                    if self.kwargs.get("on_run_created"):
                        self.kwargs["on_run_created"](rid)
                    # wait until cancelled, then report like a cancelled run
                    self.kwargs["cancel_event"].wait(timeout=5)
                    return MagicMock(run_id=rid, status="cancelled")

            orig = state.Runner
            state.Runner = SlowRunner
            try:
                job = reg.launch({
                    "task": "t-task", "orchestrator": "o/model", "worker": "w/model",
                    "replicates": 1, "dry_run": True,
                })
                self.assertTrue(_wait(lambda: bool(job.run_ids)))
                self.assertTrue(reg.cancel_run(job.run_ids[0]))
                self.assertTrue(_wait(lambda: job.status == JobStatus.CANCELLED))
            finally:
                state.Runner = orig

    def test_cancel_unknown_run_is_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = state.JobRegistry(Path(tmp), Path(tmp), Path(tmp), RunStore(tmp))
            self.assertFalse(reg.cancel_run("nope"))


class TestHttpRoutes(unittest.TestCase):
    """Real ThreadingHTTPServer on an ephemeral port against a seeded runs dir."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.rid = _seed_run(cls.tmp)
        cls.tasks, cls.models = _write_specs(Path(cls.tmp))
        cls.obs = Observatory(Path(cls.tmp), cls.tasks, cls.models)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.obs))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def _post(self, path: str, data: str) -> tuple[int, str, str]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(req) as r:
                return r.status, r.headers.get("Location", ""), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location", ""), e.read().decode()

    def test_pages_200(self):
        for path in ("/", "/runs", "/leaderboard", "/new"):
            code, body = self._get(path)
            self.assertEqual(code, 200, path)
            self.assertIn("<", body)

    def test_run_detail_and_tabs(self):
        code, _ = self._get(f"/run/{self.rid}")
        self.assertEqual(code, 200)
        for tab in ("events", "calls", "metrics", "plan", "manifest", "report"):
            code, _ = self._get(f"/run/{self.rid}?tab={tab}")
            self.assertEqual(code, 200, tab)
        code, _ = self._get(f"/run/{self.rid}?tab=bogus")  # unknown tab → events
        self.assertEqual(code, 200)

    def test_live_page_and_poll(self):
        # legacy /live URL redirects to the SPA hash route; the polling
        # contract lives in app.js + /api/run/<id>/live
        code, _ = self._get(f"/run/{self.rid}/live")
        self.assertEqual(code, 200)
        code, body = self._get("/static/app.js")
        self.assertEqual(code, 200)
        self.assertIn("/api/run/", body)
        code, body = self._get(f"/api/run/{self.rid}/live?after=0")
        self.assertEqual(code, 200)
        payload = json.loads(body)
        self.assertGreater(payload["next"], 0)
        self.assertIn("phase", payload)

    def test_spa_shell_and_api_surface(self):
        code, body = self._get("/")
        self.assertEqual(code, 200)
        self.assertIn('src="/static/app.js"', body)
        for path in ("/api/overview", "/api/groups", "/api/tasks",
                     "/api/models", "/api/leaderboard", "/api/runs"):
            code, body = self._get(path)
            self.assertEqual(code, 200, path)
            json.loads(body)  # every API route returns parseable JSON
        code, body = self._get(f"/api/run/{self.rid}")
        self.assertEqual(code, 200)
        payload = json.loads(body)
        self.assertIn("timeline", payload)
        self.assertIn("artifact", payload)

    def test_flags_and_card_api(self):
        # set a flag through the API, read it back through /api/flags + /api/card
        code, _, body = self._post(
            "/api/flag",
            f"kind=run&target={self.rid}&flag=interesting&note=post+candidate",
        )
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["flag"], "interesting")
        code, body = self._get("/api/flags")
        self.assertEqual(code, 200)
        flags = {(a["kind"], a["target"]): a for a in json.loads(body)}
        self.assertEqual(flags[("run", self.rid)]["flag"], "interesting")
        code, body = self._get(f"/api/card?kind=run&target={self.rid}")
        self.assertEqual(code, 200)
        card = json.loads(body)
        self.assertEqual(card["suite"], "v3")
        self.assertEqual(card["flag"], "interesting")
        code, _ = self._get("/api/card?kind=run&target=ghost")
        self.assertEqual(code, 404)
        # invalid flag values are rejected, not silently stored
        code, _, body = self._post("/api/flag", "kind=run&target=x&flag=bogus")
        self.assertEqual(code, 400)
        self.assertIn("flag", json.loads(body)["error"])

    def test_unknown_routes_404(self):
        for path in ("/nope", "/run/nope", "/api/run/nope/live"):
            code, _ = self._get(path)
            self.assertEqual(code, 404, path)

    def test_post_run_dry_run_and_cancel_route(self):
        code, location, _ = self._post(
            "/run",
            "task=t-task&orchestrator=o/model&worker=w/model&replicates=1&dry_run=1",
        )
        self.assertEqual(code, 303)
        self.assertTrue(location.startswith("/#/run/"), location)
        run_id = location.split("/")[3]
        job = self.obs.registry.job_for_run(run_id)
        self.assertIsNotNone(job)
        self.assertTrue(_wait(lambda: job.status == JobStatus.SUCCEEDED))
        # finished run is no longer cancellable; route still 303s without error
        code, location, _ = self._post(f"/run/{run_id}/cancel", "")
        self.assertEqual(code, 303)
        # unknown run id → 303 back to its (nonexistent) live view; no 500
        code, _, _ = self._post("/run/ghost/cancel", "")
        self.assertEqual(code, 303)

    def test_post_run_rejects_unknown_field(self):
        code, _, body = self._post(
            "/run", "task=t-task&orchestrator=o&worker=w&evil=1"
        )
        self.assertEqual(code, 400)
        self.assertIn("unknown fields", body)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


if __name__ == "__main__":
    unittest.main()
