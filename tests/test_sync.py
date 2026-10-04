"""Hosted-sync boundary tests: dirty journal, D1 projection allowlists,
hosted payload sanitization, and the canary-prompt egress check.

Every test runs against a real RunStore + real run dirs — the guarantees
here are publication boundaries, not logic that can be faked.
"""

import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import httpx

from orchestral import cf
from orchestral.config import ModelConfig, TaskSpec
from orchestral.runner import Runner
from orchestral.storage import RunStore

CANARY = "CANARY-PROMPT-b5f2e1c8-never-leaves-the-machine"


def _make_run(store: RunStore, *, holdout: bool = False,
              with_calls: bool = True) -> tuple[str, Path]:
    run_id, run_dir = store.new_run("org/orch", "task-x", "org/work")
    (run_dir / "manifest.json").write_text(json.dumps({"holdout": holdout}))
    (run_dir / "report.json").write_text(json.dumps({
        "execution": {"output_tail": "worker did the thing"},
        "score": 9.0,
    }))
    (run_dir / "events.jsonl").write_text(
        json.dumps({"type": "llm_call", "role": "worker", "step": 1,
                    "input": {"messages": [{"role": "user",
                                            "content": CANARY}]},
                    "output": {"content": "ok"}})
        + "\n"
    )
    (run_dir / "artifact.html").write_text("<html>hi</html>")
    meta = store.get_run(run_id)
    meta.status = "finished"
    store.update_meta(meta)
    if with_calls:
        store.record_call(
            run_id=run_id, phase="delegate", step=1, role="worker",
            model="org/work", input_tokens=10, output_tokens=5,
            cost_usd=0.001,
            input_json=json.dumps({"messages": [{"content": CANARY}]}),
            output_json=json.dumps({"content": "ok"}),
        )
    return run_id, run_dir


def _find_in_obj(obj, needle: str) -> bool:
    return needle in json.dumps(obj, default=str)


class TestDirtyJournal(unittest.TestCase):
    def test_index_meta_marks_run_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id, _ = _make_run(store)
            self.assertIn(run_id, store.dirty_runs())

    def test_clear_dirty_empties_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id, _ = _make_run(store)
            store.clear_dirty([run_id])
            self.assertNotIn(run_id, store.dirty_runs())

    def test_run_annotation_marks_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id, _ = _make_run(store)
            store.clear_dirty([run_id])
            store.set_annotation("run", run_id, "interesting")
            self.assertIn(run_id, store.dirty_runs())

    def test_group_annotation_does_not_dirty_a_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id, _ = _make_run(store)
            store.clear_dirty([run_id])
            store.set_annotation("group", "g1", "interesting")
            self.assertNotIn(run_id, store.dirty_runs())
            self.assertNotIn("g1", store.dirty_runs())

    def test_post_finish_update_remarks_dirty(self):
        """The failure the plan exists to catch: judge backfill mutates a
        finished run and a finished_at watermark would never see it."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id, _ = _make_run(store)
            store.clear_dirty([run_id])
            meta = store.get_run(run_id)
            meta.judge_score = 8.0
            store.update_meta(meta)
            self.assertIn(run_id, store.dirty_runs())


class TestD1Projection(unittest.TestCase):
    def test_run_rows_are_column_allowlisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _make_run(store)
            proj = cf.d1_projection(store)
            self.assertEqual(len(proj["runs"]), 1)
            row = proj["runs"][0]
            self.assertEqual(set(row), set(cf.RUN_COLUMNS) | {"holdout"})
            for leaked in ("run_dir", "config", "env"):
                self.assertNotIn(leaked, row)

    def test_call_rows_carry_no_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _make_run(store)
            proj = cf.d1_projection(store)
            self.assertEqual(len(proj["calls"]), 1)
            row = proj["calls"][0]
            self.assertEqual(set(row), set(cf.CALL_COLUMNS))
            for leaked in ("input_json", "output_json", "error"):
                self.assertNotIn(leaked, row)
            self.assertFalse(_find_in_obj(proj, CANARY))

    def test_holdout_run_produces_no_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _make_run(store, holdout=True)
            proj = cf.d1_projection(store)
            self.assertEqual(proj["runs"], [])
            self.assertEqual(proj["calls"], [])


class TestHostedDetail(unittest.TestCase):
    def test_calls_are_ledger_not_previews(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs")
            run_id, run_dir = _make_run(store)
            scrubbed = cf.scrub_to_dir(run_dir, Path(tmp) / "pub")
            payload = cf._hosted_detail(store, run_id, scrubbed,
                                        Path(tmp) / "tasks", Path(tmp) / "groups.yaml")
            self.assertIsNotNone(payload)
            for call in payload["calls"]:
                self.assertNotIn("input_json", call)
                self.assertNotIn("output_json", call)
                self.assertNotIn("error", call)

    def test_canary_prompt_never_reaches_hosted_payload(self):
        """The sentinel in events.jsonl input + calls bodies must appear in
        no hosted payload — detail, evidence, or D1 projection."""
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs")
            run_id, run_dir = _make_run(store)
            scrubbed = cf.scrub_to_dir(run_dir, Path(tmp) / "pub")
            detail = cf._hosted_detail(store, run_id, scrubbed,
                                       Path(tmp) / "tasks", Path(tmp) / "groups.yaml")
            from orchestral.web import state
            evidence = state.run_evidence_payload(
                cf._ScrubbedStore(store, run_id, scrubbed), run_id)
            proj = cf.d1_projection(store, [run_id])
            for name, payload in (("detail", detail), ("evidence", evidence),
                                  ("d1", proj)):
                self.assertFalse(_find_in_obj(payload, CANARY),
                                 f"canary leaked into hosted {name} payload")

    def test_holdout_push_produces_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs")
            _, run_dir = _make_run(store, holdout=True)
            with self.assertRaises(cf.HoldoutRunError):
                cf.scrub_to_dir(run_dir, Path(tmp) / "pub")


class TestManifestHash(unittest.TestCase):
    def test_hash_changes_when_publishable_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _, run_dir = _make_run(store, with_calls=False)
            before = cf.manifest_hash(run_dir)
            (run_dir / "report.json").write_text(json.dumps({"score": 1.0}))
            self.assertNotEqual(before, cf.manifest_hash(run_dir))

    def test_hash_ignores_non_publishable_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _, run_dir = _make_run(store, with_calls=False)
            before = cf.manifest_hash(run_dir)
            (run_dir / "debug.jsonl").write_text("debug noise\n")
            self.assertEqual(before, cf.manifest_hash(run_dir))


class TestSyncOrchestration(unittest.TestCase):
    def test_dry_run_reports_dirty_without_pushing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id, _ = _make_run(store)
            result = cf.sync(store, Path(tmp) / "tasks", Path(tmp) / "models",
                             Path(tmp) / "groups.yaml", push=False)
            self.assertIn(run_id, result.pushed)
            self.assertEqual(result.errors, [])

    def test_all_covers_every_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            ids = [_make_run(store, with_calls=False)[0] for _ in range(3)]
            store.clear_dirty(ids)
            result = cf.sync(store, Path(tmp) / "tasks", Path(tmp) / "models",
                             Path(tmp) / "groups.yaml", push=False, all_runs=True)
            self.assertEqual(set(result.pushed), set(ids))


class TestFinishHook(unittest.TestCase):
    """Runner.on_run_finished must fire on every terminal path — the journal
    alone can't keep hosted state fresh if a failed run re-raises past the
    normal launch return."""

    def _task(self) -> TaskSpec:
        return TaskSpec(
            id="api-hook", type="api", prompt="Plan the API calls.",
            metadata={"stub": [
                {"method": "GET", "path": "/health", "status": 200,
                 "json": {"ok": True}},
            ], "calls": [{"method": "GET", "path": "/health"}],
                      "strict": True},
        )

    def _model(self, slug: str, role: str) -> ModelConfig:
        return ModelConfig(
            slug=slug, name=slug, role=role,
            input_price_per_mtok=0.5, output_price_per_mtok=2.0,
            retry_limit=1,
        )

    def test_hook_fires_on_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = []
            meta = Runner(
                runs_dir=tmp, planner="raw", dry_run=True,
                on_run_finished=seen.append,
            ).run(self._task(), self._model("org/x", "orchestrator"),
                  self._model("wrk/y", "worker"))
            self.assertEqual([m.run_id for m in seen], [meta.run_id])
            self.assertEqual(seen[0].status, "finished")

    def test_hook_fires_on_failure(self):
        class _BadClient:
            def chat(self, *a, **kw):
                raise RuntimeError("provider exploded")

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            seen = []
            runner = Runner(
                runs_dir=tmp, planner="raw",
                clients={"orchestrator": _BadClient(),
                         "worker": _BadClient()},
                on_run_finished=seen.append,
            )
            with self.assertRaises(RuntimeError):
                runner.run(self._task(), self._model("org/x", "orchestrator"),
                           self._model("wrk/y", "worker"))
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0].status, "failed")

    def test_hook_disabled_without_env(self):
        import harness
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            os.environ.pop("ORCH_CF_SYNC", None)
            self.assertIsNone(harness._cf_sync_hook(store))

    def test_hook_skips_holdout_runs(self):
        import harness
        from orchestral import privacy
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id, _ = _make_run(store, holdout=True)
            meta = store.get_run(run_id)
            pushed = []
            try:
                with unittest.mock.patch.dict(
                        os.environ, {"ORCH_CF_SYNC": "1",
                                     "ORCHESTRAL_OBS_TOKEN": "id:secret"}), \
                     unittest.mock.patch(
                         "orchestral.cf.push_run_events_only",
                         side_effect=lambda c, s, m: pushed.append(m.run_id)):
                    hook = harness._cf_sync_hook(store)
                    self.assertIsNotNone(hook)
                    self.assertTrue(privacy.run_is_holdout(Path(meta.run_dir)))
                    hook(meta)
                self.assertEqual(pushed, [])
            finally:
                if harness._CF_HOOK_CLIENT is not None:
                    harness._CF_HOOK_CLIENT.close()
                    harness._CF_HOOK_CLIENT = None


if __name__ == "__main__":
    unittest.main()


def _store_with_relative_run_dirs(tmp: str) -> tuple[RunStore, list[str]]:
    """A store whose index records run_dir relative to the repo, as a run
    launched with `--runs-dir runs` does."""
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        store = RunStore("runs")
        ids = [_make_run(store, with_calls=False)[0] for _ in range(2)]
    finally:
        os.chdir(cwd)
    return store, ids


def _creds_env():
    return unittest.mock.patch.dict(
        os.environ, {"ORCHESTRAL_OBS_TOKEN": "id:secret"})


class TestSyncCwdIndependence(unittest.TestCase):
    def test_relative_run_dir_resolves_against_store_root(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            _, ids = _store_with_relative_run_dirs(tmp)
            store = RunStore(Path(tmp) / "runs")  # absolute root, index still relative
            cwd = os.getcwd()
            os.chdir(other)
            try:
                result = cf.sync(store, Path(tmp) / "tasks", Path(tmp) / "models",
                                 Path(tmp) / "groups.yaml", push=False)
            finally:
                os.chdir(cwd)
            self.assertEqual(result.skipped_missing, [])
            self.assertEqual(set(result.pushed), set(ids))
            for r in store.list_runs():
                self.assertTrue(Path(r.run_dir).is_dir())


class TestPartialFailureAndRetry(unittest.TestCase):
    def _sync(self, tmp, store, handler, **kw):
        transport = httpx.MockTransport(handler)
        with (_creds_env(),
              unittest.mock.patch.object(cf, "_sleep", lambda s: None),
              httpx.Client(transport=transport) as client):
            return cf.sync(store, Path(tmp) / "tasks", Path(tmp) / "models",
                           Path(tmp) / "groups.yaml", push=True,
                           client=client, **kw)

    def test_failed_runs_stay_dirty_successes_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            good, bad = (_make_run(store, with_calls=False)[0] for _ in range(2))

            def handler(req: httpx.Request) -> httpx.Response:
                body = json.loads(req.content)
                if req.url.path == "/ingest/run" and body["run_id"] == bad:
                    return httpx.Response(503, text="<html>" + "x" * 500 + "</html>")
                return httpx.Response(200, json={})

            result = self._sync(tmp, store, handler)
            self.assertEqual(result.pushed, [good])
            self.assertEqual(len(result.errors), 1)
            self.assertLessEqual(len(result.errors[0]), 250)  # no HTML dump
            self.assertEqual(store.dirty_runs(), {bad})

            again = self._sync(tmp, store, lambda r: httpx.Response(200, json={}))
            self.assertEqual(again.pushed, [bad])
            self.assertEqual(store.dirty_runs(), set())

    def test_retries_503_then_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id = _make_run(store, with_calls=False)[0]
            attempts: dict[str, int] = {}

            def handler(req: httpx.Request) -> httpx.Response:
                n = attempts[req.url.path] = attempts.get(req.url.path, 0) + 1
                if req.url.path == "/ingest/run" and n < 3:
                    return httpx.Response(503, text="busy")
                return httpx.Response(200, json={})

            result = self._sync(tmp, store, handler)
            self.assertEqual(attempts["/ingest/run"], 3)
            self.assertEqual(result.pushed, [run_id])
            self.assertEqual(result.errors, [])

    def test_gives_up_after_three_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            run_id = _make_run(store, with_calls=False)[0]
            calls = []

            def handler(req: httpx.Request) -> httpx.Response:
                if req.url.path == "/ingest/run":
                    calls.append(1)
                    return httpx.Response(429, text="slow down")
                return httpx.Response(200, json={})

            result = self._sync(tmp, store, handler)
            self.assertEqual(len(calls), 3)
            self.assertEqual(store.dirty_runs(), {run_id})
            self.assertIn("429", result.errors[0])

    def test_no_retry_on_4xx(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            _make_run(store, with_calls=False)
            calls = []

            def handler(req: httpx.Request) -> httpx.Response:
                if req.url.path == "/ingest/run":
                    calls.append(1)
                    return httpx.Response(403, text="forbidden")
                return httpx.Response(200, json={})

            result = self._sync(tmp, store, handler)
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(result.errors), 1)
