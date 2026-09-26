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
import urllib.parse
import urllib.request
import zipfile
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
            self.assertIn("verdict_line", gcard)
            self.assertIn("pass_ci", gcard)
            self.assertEqual(gcard["judged"], 0)
            self.assertIn("verdict_line", card)
            self.assertIn("judge_calibration", gcard)
            # pre-made eval-set description + comparable rows
            self.assertIn("1 tasks", gcard["description"])
            self.assertIn("unjudged", gcard["description"])
            self.assertEqual(len(gcard["pairing_rows"]), 1)
            self.assertEqual(gcard["pairing_rows"][0]["pass_rate"], 1.0)
            self.assertEqual(gcard["task_rows"][0]["task_id"], "t-task")
            self.assertIsNone(state.card_payload(store, "run", "ghost"))
            self.assertIsNone(state.card_payload(store, "group", "ghost"))

    def test_task_titles_and_group_labels_reach_payloads(self):
        """title/blurb on specs + groups.yaml entries surface through every
        payload — a stranger reads 'Expression parser', not a raw slug."""
        with tempfile.TemporaryDirectory() as tmp:
            rid = _seed_run(tmp, run_group="g-cards")
            store = RunStore(tmp)
            tasks = Path(tmp) / "tasks"
            tasks.mkdir()
            (tasks / "t-task.yaml").write_text(
                'id: t-task\ntype: html\nprompt: p\n'
                'title: "T Task"\nblurb: "one line"\n')
            gfile = Path(tmp) / "groups.yaml"
            gfile.write_text(
                'groups:\n  g-cards:\n    label: G Cards\n    description: desc\n')
            gcard = state.card_payload(
                store, "group", "g-cards",
                tasks_dir=tasks, groups_file=gfile)
            self.assertEqual(gcard["task_rows"][0]["title"], "T Task")
            self.assertEqual(gcard["task_rows"][0]["blurb"], "one line")
            self.assertEqual(gcard["group_label"], "G Cards")
            self.assertEqual(gcard["group_description"], "desc")
            rows = state.runs_payload(store, tasks_dir=tasks)
            self.assertEqual(rows[0]["task_title"], "T Task")
            detail = state.run_detail_payload(
                store, rid, tasks_dir=tasks, groups_file=gfile)
            self.assertEqual(detail["task_title"], "T Task")
            self.assertEqual(detail["task_blurb"], "one line")
            self.assertEqual(detail["group_label"], "G Cards")
            groups = state.groups_payload(store, gfile)
            self.assertEqual(groups[0]["label"], "G Cards")

    def test_unlabeled_specs_and_groups_fall_back_cleanly(self):
        """Specs without title/blurb and groups without a groups.yaml entry
        degrade to raw names — the explainability layer is additive."""
        with tempfile.TemporaryDirectory() as tmp:
            rid = _seed_run(tmp, run_group="g-bare")
            store = RunStore(tmp)
            gcard = state.card_payload(store, "group", "g-bare")
            self.assertEqual(gcard["task_rows"][0]["title"], "")
            self.assertEqual(gcard["group_label"], "")
            detail = state.run_detail_payload(store, rid)
            self.assertEqual(detail["task_title"], "")
            self.assertEqual(detail["group_label"], "")

    def test_judge_state_taxonomy(self):
        """The four judge states derive correctly — unjudged is never
        rendered as a bare dash again."""
        with tempfile.TemporaryDirectory() as tmp:
            rid = _seed_run(tmp)
            store = RunStore(tmp)
            meta = store.get_run(rid)
            run_dir = Path(meta.run_dir)

            # fresh seed (dry-run): artifact exists, judge never ran
            st, why = state.judge_state(meta)
            self.assertEqual(st, "not_judged")
            self.assertTrue(why)

            # inconclusive: judge block exists with no verdict
            report = json.loads((run_dir / "report.json").read_text() or "{}")
            report["judge"] = {"inconclusive": True, "model": "j/x",
                               "reasoning": "no usable verdict"}
            (run_dir / "report.json").write_text(json.dumps(report))
            self.assertEqual(state.judge_state(store.get_run(rid))[0],
                             "inconclusive")

            # conclusive judge result → judged
            report["judge"] = {"score": 0.9, "passed": True, "model": "j/x"}
            (run_dir / "report.json").write_text(json.dumps(report))
            m = store.get_run(rid)
            m.judge_score, m.judge_passed = 0.9, True
            store.update_meta(m)
            self.assertEqual(state.judge_state(store.get_run(rid))[0], "judged")

    def test_judge_state_not_judgeable(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = _seed_run(tmp)
            store = RunStore(tmp)
            run_dir = Path(store.get_run(rid).run_dir)
            for a in run_dir.glob("artifact.*"):
                a.unlink()
            st, why = state.judge_state(store.get_run(rid))
            self.assertEqual(st, "not_judgeable")
            self.assertIn("no artifact", why)


    def test_load_groups_validates_shape(self):
        from orchestral.config import ConfigError, load_groups
        with tempfile.TemporaryDirectory() as tmp:
            missing = load_groups(Path(tmp) / "nope.yaml")
            self.assertEqual(missing, {})
            bad = Path(tmp) / "groups.yaml"
            bad.write_text("groups:\n  g: not-a-mapping\n")
            with self.assertRaises(ConfigError):
                load_groups(bad)
            ok = Path(tmp) / "ok.yaml"
            ok.write_text("groups:\n  g:\n    label: L\n    extra: dropped\n")
            self.assertEqual(load_groups(ok), {"g": {"label": "L"}})

    def test_task_matrix_payload(self):
        """The tasks × pairings heatmap carries pass rate, n, judge mean,
        and task metadata; cells are absent (not zero) when unattempted."""
        with tempfile.TemporaryDirectory() as tmp:
            _seed_run(tmp, run_group="g1")
            store = RunStore(tmp)
            m = state.task_matrix_payload(store)
            self.assertEqual(len(m["tasks"]), 1)
            self.assertEqual(len(m["pairings"]), 1)
            t = m["tasks"][0]
            self.assertEqual(t["task_id"], "t-task")
            pair = next(iter(t["cells"].values()))
            self.assertEqual(pair["n"], 1)
            self.assertEqual(pair["pass_rate"], 1.0)
            self.assertIsNone(pair["judge_mean"])

    def test_pairing_aggregate_p90_and_judged_count(self):
        from orchestral.stats import pairing_leaderboard
        with tempfile.TemporaryDirectory() as tmp:
            _seed_run(tmp, run_group="g1")
            store = RunStore(tmp)
            rid = store.list_runs()[0].run_id
            meta = store.get_run(rid)
            meta.judge_score = 0.7
            store.update_meta(meta)
            row = pairing_leaderboard(store.list_runs())[0]
            self.assertEqual(row.judged, 1)
            self.assertIn("duration_p90_ms", row.to_dict())

    def test_card_payload_pairing_description_and_type_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_run(tmp, run_group="g1")
            store = RunStore(tmp)
            card = state.card_payload(store, "pairing", "o/model|w/model")
            self.assertIsNotNone(card)
            self.assertIn("plans", card["description"])
            self.assertIn("mechanical pass", card["description"])
            self.assertEqual(len(card["type_rows"]), 1)
            self.assertEqual(card["type_rows"][0]["pass_rate"], 1.0)

    def test_card_payload_judge_calibration(self):
        """A judged run's card carries the judge's persisted calibration
        state — uncalibrated when no labels exist yet."""
        with tempfile.TemporaryDirectory() as tmp:
            rid = _seed_run(tmp)
            store = RunStore(tmp)
            run_dir = Path(store.get_run(rid).run_dir)
            report = json.loads((run_dir / "report.json").read_text())
            report["judge"] = {"score": 0.9, "passed": True,
                               "model": "j/judge", "engine": "chat"}
            (run_dir / "report.json").write_text(json.dumps(report))
            card = state.card_payload(store, "run", rid, reports_dir=tmp)
            self.assertEqual(
                card["judge_calibration"],
                {"j/judge": {"calibrated": False, "kappa": None,
                             "verdict_pairs": 0}})

    def test_wilson_interval_and_verdict_lines(self):
        # known binomial: 1/4 pass → wide honest interval
        lo, hi = state._wilson(1, 4)
        self.assertLess(lo, 0.25)
        self.assertGreater(hi, 0.25)
        self.assertIsNone(state._wilson(0, 0))
        v = state._verdict_line
        self.assertEqual(v(True, True, "finished"), "passes both axes — structure and semantics")
        self.assertEqual(v(True, False, "finished"), "well-formed but semantically rejected")
        self.assertEqual(v(False, True, "finished"), "mechanical reject, semantic rescue — inspect")
        self.assertEqual(v(False, False, "finished"), "rejected on both axes")
        self.assertEqual(v(True, None, "finished"), "mechanical pass — unjudged")
        self.assertIn("no verdict", v(None, None, "running"))

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

    def _post(self, path: str, data: str,
              headers: dict[str, str] | None = None) -> tuple[int, str, str]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     **(headers or {})},
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

    def test_card_catalog_story_and_evidence_routes(self):
        code, body = self._get("/api/cards?scope=pairing&lens=overall")
        self.assertEqual(code, 200)
        catalog = json.loads(body)
        self.assertTrue(catalog["cards"])
        self.assertIn("story", catalog["cards"][0])
        self.assertIn("lenses", catalog)

        code, body = self._get(f"/api/card?kind=run&target={self.rid}&lens=divergence")
        self.assertEqual(code, 200)
        card = json.loads(body)
        self.assertEqual(card["lens"]["id"], "divergence")
        self.assertIn("claim", card["story"])

        code, body = self._get(f"/api/run/{self.rid}/evidence?max_bytes=300&max_lines=8")
        self.assertEqual(code, 200)
        evidence = json.loads(body)
        self.assertIn(evidence["status"], {"available", "partial", "unavailable"})
        if evidence["transcript"]:
            self.assertLessEqual(len(evidence["transcript"]["text"].encode()), 300)

    def test_nested_archive_member_url_is_readable(self):
        store = RunStore(self.tmp)
        run_id, run_dir = store.new_run(
            "o/model", "t-task", "w/model", {"dry_run": True},
        )
        with zipfile.ZipFile(Path(run_dir) / "artifact.zip", "w") as archive:
            archive.writestr("src/worker.py", "print('nested')\n")

        code, body = self._get(f"/api/run/{run_id}/artifact/src%2Fworker.py")
        self.assertEqual(code, 200)
        self.assertEqual(body, "print('nested')\n")

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

    def test_post_run_cannot_opt_into_executor(self):
        # allow_agent_exec is a server-start flag — a POST field would be
        # CSRF-triggerable from any web page the user visits
        for path in ("/run", "/api/run"):
            code, _, body = self._post(
                path,
                "task=t-task&orchestrator=o/model&worker=w/model"
                "&replicates=1&dry_run=1&allow_agent_exec=1",
            )
            self.assertEqual(code, 400, path)
            self.assertIn("unknown fields", body)

    def test_post_rejects_foreign_origin_and_host(self):
        # a cross-site form post carries the attacker's Origin — reject it
        code, _, _ = self._post(
            "/api/flag", "kind=run&target=x&flag=interesting",
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(code, 403)
        # sandboxed/null origins can't prove same-origin either
        code, _, _ = self._post(
            "/api/flag", "kind=run&target=x&flag=interesting",
            headers={"Origin": "null"},
        )
        self.assertEqual(code, 403)
        # a foreign Host smells like DNS rebinding — reject it
        code, _, _ = self._post(
            "/api/flag", "kind=run&target=x&flag=interesting",
            headers={"Host": "evil.example"},
        )
        self.assertEqual(code, 403)
        code, _, _ = self._post(
            "/api/flag", "kind=run&target=x&flag=interesting",
            headers={"Host": "evil.example:8787",
                     "Origin": "http://evil.example:8787"},
        )
        self.assertEqual(code, 403)

    def test_post_accepts_same_origin_and_no_origin(self):
        # matching Origin + Host → fine (a real same-site form post)
        code, _, _ = self._post(
            "/api/flag", "kind=run&target=x&flag=interesting",
            headers={"Origin": f"http://127.0.0.1:{self.port}"},
        )
        self.assertEqual(code, 200)
        # no Origin header → non-browser client, not a CSRF vector
        code, _, _ = self._post(
            "/api/flag", "kind=run&target=x&flag=not",
        )
        self.assertEqual(code, 200)

    def test_models_api_exposes_executor_and_default_judge(self):
        code, body = self._get("/api/models?role=worker")
        self.assertEqual(code, 200)
        rows = {r["slug"]: r for r in json.loads(body)}
        self.assertIn("w/model", rows)
        for row in rows.values():
            self.assertIn("executor", row)
            self.assertIn("capabilities", row)
            self.assertIn("modalities", row)
        # the default decisions judge is offered though no model file declares it
        code, body = self._get("/api/models")
        self.assertEqual(code, 200)
        rows = {r["slug"]: r for r in json.loads(body)}
        self.assertIn("~typesafe/jev-latest", rows)
        self.assertTrue(rows["~typesafe/jev-latest"]["default"])
        # role-filtered surfaces don't get the synthetic judge entry
        code, body = self._get("/api/models?role=worker")
        self.assertNotIn("~typesafe/jev-latest",
                         {r["slug"] for r in json.loads(body)})

    def test_shot_png_rejects_bad_routes(self):
        # protocol-relative and relative routes could steer the headless
        # browser off-origin — only app paths are allowed
        for route in ("//evil.example/x", "not-a-path"):
            code, _ = self._get(f"/api/shot.png?route={urllib.parse.quote(route)}")
            self.assertEqual(code, 400, route)

    def test_shot_png_503_when_capture_unavailable(self):
        from orchestral.shots import ScreenshotUnavailable
        with unittest.mock.patch("orchestral.shots.capture_page",
                                 side_effect=ScreenshotUnavailable("nope")):
            code, body = self._get("/api/shot.png?route=/leaderboard")
        self.assertEqual(code, 503)
        self.assertIn("nope", body)

    def test_shot_png_returns_png_and_picks_xcard_for_cards(self):
        from urllib.parse import quote

        with unittest.mock.patch(
                "orchestral.shots.capture_page",
                return_value=b"\x89PNG-fake") as cap:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/shot.png"
                f"?route={quote('/card?kind=pairing&target=o/m|w/m')}")
            with urllib.request.urlopen(req) as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(r.headers.get("Content-Type"), "image/png")
                self.assertIn("filename=", r.headers.get("Content-Disposition", ""))
                self.assertEqual(r.read(), b"\x89PNG-fake")
        self.assertEqual(cap.call_args.kwargs["element"], ".xcard")
        # non-card routes capture the settled view, not a card node
        with unittest.mock.patch(
                "orchestral.shots.capture_page",
                return_value=b"\x89PNG-fake") as cap, urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/shot.png?route=/leaderboard") as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), b"\x89PNG-fake")
        self.assertIsNone(cap.call_args.kwargs["element"])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _finished_run(runs_dir: str, *, group: str, task_id: str,
                  report_body: str | None, orchestrator: str = "o/m",
                  worker: str = "w/m", artifact: bool = True,
                  dry_run: bool = False) -> str:
    """A finished run whose report.json is written verbatim (or not at all).

    `report_body=None` writes no report at all, which is how a run killed
    before its report lands looks on disk.
    """
    store = RunStore(runs_dir)
    run_id, run_dir = store.new_run(
        orchestrator, task_id, worker, {"dry_run": dry_run}, run_group=group)
    meta = store.get_run(run_id)
    assert meta is not None
    meta.status = "finished"
    meta.passes = True
    meta.score = 1.0
    meta.dry_run = dry_run
    meta.started_at = "2026-09-23T00:00:00+00:00"
    meta.finished_at = meta.started_at
    if report_body is not None:
        (run_dir / "report.json").write_text(report_body, encoding="utf-8")
    if artifact:
        (run_dir / "artifact.py").write_text("x = 1\n", encoding="utf-8")
    store.update_meta(meta)
    return run_id


_HEALTHY_REPORT = json.dumps({
    "passes": True, "score": 1.0, "checks": {"tests_pass": True},
    "judge": {"score": 0.9, "noul": 0.0, "passed": True, "model": "j/m"},
})
# what a kill -9 mid-write leaves behind: valid prefix, no closing brace
_TRUNCATED_REPORT = _HEALTHY_REPORT[: len(_HEALTHY_REPORT) // 2]


class TestUnreadableJudgeReport(unittest.TestCase):
    """An unreadable report.json means the judge axis is *unknown*, not absent.

    `read_json` collapses missing and unparseable into None, so a report
    truncated by a kill used to be announced as "judge wasn't run for this
    run" — a false claim about work that may well have been performed.
    """

    def test_truncated_report_is_not_reported_as_never_judged(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = _finished_run(tmp, group="g", task_id="t1",
                                report_body=_TRUNCATED_REPORT)
            st, why = state.judge_state(RunStore(tmp).get_run(rid))
            self.assertEqual(st, "unreadable")
            self.assertNotIn("judge wasn't run", why)
            self.assertIn("unreadable", why)

    def test_missing_report_is_unknown_not_never_ran(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = _finished_run(tmp, group="g", task_id="t1", report_body=None)
            st, why = state.judge_state(RunStore(tmp).get_run(rid))
            self.assertEqual(st, "unreadable")
            self.assertIn("missing", why)
            self.assertNotIn("judge wasn't run", why)

    def test_report_that_is_not_an_object_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            rid = _finished_run(tmp, group="g", task_id="t1", report_body="[1, 2]")
            self.assertEqual(state.judge_state(RunStore(tmp).get_run(rid))[0],
                             "unreadable")

    def test_healthy_report_still_reads_judged(self):
        """Control: the fix must not disturb a run whose report reads clean."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            rid = _finished_run(tmp, group="g", task_id="t1",
                                report_body=_HEALTHY_REPORT)
            self.assertEqual(state.judge_state(store.get_run(rid)), ("judged", ""))
            # readable report, no judge block at all → a real, statable absence
            rid2 = _finished_run(tmp, group="g", task_id="t2", report_body="{}")
            st, why = state.judge_state(store.get_run(rid2))
            self.assertEqual(st, "not_judged")
            self.assertEqual(why, "judge wasn't run for this run")

    def test_lifecycle_states_still_win_over_read_failures(self):
        """A run that never finished, or has nothing to judge, is described by
        that fact — not by a read failure on a report it never wrote."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            rid = _finished_run(tmp, group="g", task_id="t1", report_body=None,
                                dry_run=True)
            st, why = state.judge_state(store.get_run(rid))
            self.assertEqual(st, "not_judged")
            self.assertIn("dry run", why)

            rid2 = _finished_run(tmp, group="g", task_id="t2", report_body=None,
                                 artifact=False)
            st, why = state.judge_state(store.get_run(rid2))
            self.assertEqual(st, "not_judgeable")
            self.assertIn("no artifact", why)

    def test_card_does_not_announce_a_judged_cohort_as_never_judged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            for i in range(3):
                _finished_run(tmp, group="g", task_id=f"t{i}",
                              report_body=_TRUNCATED_REPORT, orchestrator=f"o/{i}")
            for kind, target in (("group", "g"), ("pairing", "o/0|w/m")):
                card = state.card_payload(store, kind, target)
                self.assertEqual(card["judged"], 0, kind)
                self.assertEqual(card["judge_reports_unreadable"],
                                 3 if kind == "group" else 1, kind)
                self.assertNotIn("nothing judged yet", card["verdict_line"])
                self.assertIn("could not be read", card["verdict_line"])
                claims = " ".join(s["claim"] for s in card["story"]["signals"])
                self.assertNotIn("no judge verdicts are available", claims)
                self.assertIn("could not be read", claims)

    def test_card_keeps_the_judge_axis_but_flags_it_incomplete(self):
        """A cohort where some verdicts read and some don't must not present
        the readable subset's rate as the whole cohort's rate."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _finished_run(tmp, group="g", task_id="t1", report_body=_HEALTHY_REPORT)
            _finished_run(tmp, group="g", task_id="t2", report_body=_HEALTHY_REPORT)
            _finished_run(tmp, group="g", task_id="t3",
                          report_body=_TRUNCATED_REPORT)
            card = state.card_payload(store, "group", "g")
            self.assertEqual(card["judged"], 2)
            self.assertEqual(card["judge_reports_unreadable"], 1)
            self.assertIn("unreadable", card["verdict_line"])
            self.assertIn("semantic axis incomplete", card["verdict_line"])

    def test_run_card_carries_the_unknown_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            rid = _finished_run(tmp, group="g", task_id="t1",
                                report_body=_TRUNCATED_REPORT)
            card = state.card_payload(store, "run", rid)
            self.assertEqual(card["judge_state"], "unreadable")
            self.assertIn("unreadable", card["judge_state_reason"])
            self.assertIn("unknown",
                          " ".join(s["claim"] for s in card["story"]["signals"]))


class TestThreadRefusesUnreadableCard(unittest.TestCase):
    """POST /api/thread must not publish a card whose judge numbers are
    missing because report.json could not be read."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        _finished_run(cls.tmp, group="g-lost", task_id="t1",
                      report_body=_TRUNCATED_REPORT)
        _finished_run(cls.tmp, group="g-ok", task_id="t1",
                      report_body=_HEALTHY_REPORT)
        cls.tasks, cls.models = _write_specs(Path(cls.tmp))
        cls.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(Observatory(Path(cls.tmp), cls.tasks, cls.models)))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _post_thread(self, kind: str, target: str) -> tuple[int, str]:
        data = urllib.parse.urlencode({"kind": kind, "target": target, "n": "3"})
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/thread", data=data.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_group_card_with_unreadable_reports_is_not_publishable(self):
        code, body = self._post_thread("group", "g-lost")
        self.assertEqual(code, 409)
        self.assertIn("could not be read", body)
        self.assertIn("recoverable", body)
        self.assertNotIn("posts", body)

    def test_healthy_card_still_drafts(self):
        """Control: the guard must not block a card whose judge axis is real."""
        code, body = self._post_thread("group", "g-ok")
        self.assertEqual(code, 200)
        self.assertIn("posts", body)


if __name__ == "__main__":
    unittest.main()
