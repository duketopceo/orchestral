"""Hosted deploy parity: the sync path, the Worker's key grammar and the staged assets.

Everything is local: no network, no Cloudflare, no provider keys."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestral import cf, privacy
from orchestral.storage import RunStore
from orchestral.web import snapshot

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "observatory"
WORKER = ROOT / "infra" / "cloudflare" / "observatory"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


corpus = _load("build_fixture_corpus", ROOT / "scripts" / "build-fixture-corpus.py")
SECRET = "CANARY-HOSTED-SYNC-5b0e9d"


class TestSyncPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = corpus.build_corpus(cls.tmp / "runs", "full")
        cls.store = RunStore(cls.tmp / "runs")
        runs = cls.store.list_runs(limit=None)
        cls.held = {r.run_id for r in runs if (r.config or {}).get("holdout")}
        cls.open = [r.run_id for r in runs if r.run_id not in cls.held]
        for rid in cls.held:
            run_dir = Path(cls.store.get_run(rid).run_dir)  # type: ignore[union-attr]
            (run_dir / "artifact.html").write_text(f"<p>{SECRET}</p>")
            (run_dir / "plan.md").write_text(SECRET)
            cls.store.set_annotation("run", rid, "interesting", SECRET)
        cls.tasks, cls.models, cls.groups = (
            FIXTURES / "tasks", FIXTURES / "models", FIXTURES / "groups.yaml")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _push(self) -> list[tuple[str, dict]]:
        cached = getattr(type(self), "_sent", None)  # the push renders the whole corpus: do it once
        if cached is not None:
            return cached
        sent: list[tuple[str, dict]] = []

        def fake_post(_client, path, body):
            sent.append((path, body))
            return {"ok": True, "written": len(body.get("payloads", {}))}

        env = {"ORCHESTRAL_CF_ID": "id", "ORCHESTRAL_CF_SECRET": "secret"}
        with patch.object(cf, "_post", fake_post), patch.dict(os.environ, env):
            result = cf.sync(self.store, self.tasks, self.models, self.groups,
                             push=True, all_runs=True)
        self.assertEqual(result.errors, [])
        type(self)._sent = sent
        return sent

    def test_global_payloads_are_the_snapshot_tree(self):
        got = cf.global_payloads(self.store, self.tasks, self.models, self.groups)
        snap = snapshot.build_snapshot(
            self.store, self.tasks, self.models, self.groups,
            run_ids=snapshot.holdout_run_ids(self.store))
        self.assertEqual(set(got), {f"api/{k}" for k in snap})
        self.assertNotIn("api/tasks.json", got)  # the read-only SPA never asks for it
        meta = got["api/meta.json"]
        self.assertEqual(meta["mode"], "hosted")
        self.assertTrue(meta["synced_at"])
        self.assertIn("source_commit", meta)
        self.assertTrue(meta["capabilities"])
        self.assertFalse(any(meta["capabilities"].values()))
        # no legacy key shapes survive
        self.assertFalse([k for k in got if ".vs." in k or k.startswith("api/card.")])

    def test_every_pushed_key_is_a_shape_the_worker_accepts(self):
        sent = self._push()
        keys = [k for _, body in sent for k in body.get("payloads", {})]
        self.assertTrue(keys)
        self._assert_worker_accepts(keys)

    def _assert_worker_accepts(self, keys: list[str]) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        script = (
            f"import {{ingestKeyKind}} from {json.dumps((WORKER / 'keys.js').as_uri())};"
            "import {readFileSync} from 'node:fs';"
            "const ks=JSON.parse(readFileSync(0,'utf8'));"
            "console.log(JSON.stringify(ks.filter(k=>ingestKeyKind(k)!=='payload')))"
        )
        out = subprocess.run([node, "--input-type=module", "-e", script],
                             input=json.dumps(keys), capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [], "keys the Worker would reject")

    def test_state_is_chunked_with_meta_last_and_d1_once(self):
        sent = [(p, b) for p, b in self._push() if p == "/ingest/state"]
        self.assertGreater(len(sent), 1)
        self.assertEqual(list(sent[-1][1]["payloads"]), ["api/meta.json"])
        self.assertTrue(sent[0][1]["d1"]["runs"])
        self.assertTrue(all(not b["d1"] for _, b in sent[1:]))

    def test_holdout_never_reaches_any_push_body(self):
        sent = self._push()
        blob = json.dumps(sent, default=str)
        self.assertNotIn(SECRET, blob)
        pushed_runs = {b["run_id"] for p, b in sent if p == "/ingest/run"}
        self.assertEqual(pushed_runs & self.held, set())
        self.assertEqual(pushed_runs, set(self.open))
        for path, body in sent:
            for row in body.get("d1", {}).get("runs", []):
                self.assertNotIn(row["run_id"], self.held)
            for row in body.get("d1", {}).get("calls", []):
                self.assertNotIn(row["run_id"], self.held)
            for row in body.get("d1", {}).get("annotations", []):
                self.assertNotIn(row["target"], self.held)
            for key in body.get("files", {}):
                self.assertFalse(any(h in key for h in self.held), key)
            if path == "/ingest/run":
                continue
            for h in self.held:  # only the withheld stub, never live or evidence
                keys = [k for k in body["payloads"] if k.startswith(f"api/run/{h}")]
                self.assertTrue(set(keys) <= {f"api/run/{h}.json"}, keys)

    def test_run_push_keys_follow_the_snapshot_shapes(self):
        sent = [b for p, b in self._push() if p == "/ingest/run"]
        for body in sent:
            rid = snapshot.enc(body["run_id"])
            for key in body["payloads"]:
                self.assertRegex(key, rf"^api/run/{re.escape(rid)}(\.json|/(live|evidence)\.json)$")
            self.assertTrue(all(k.startswith(f"runs/{body['run_id']}/") for k in body["files"]))

    def test_holdout_run_push_is_refused(self):
        held = self.store.get_run(sorted(self.held)[0])
        with self.assertRaises(cf.HoldoutRunError):
            cf.push_run(None, self.store, held, self.tasks, self.groups)  # type: ignore[arg-type]

    def test_d1_annotations_on_a_holdout_run_are_withheld(self):
        proj = cf.d1_projection(self.store)
        self.assertFalse([a for a in proj["annotations"] if a["target"] in self.held])
        self.assertNotIn(SECRET, json.dumps(proj))
        self.assertTrue(any(a["target"] in self.held for a in self.store.annotations()))
        self.assertFalse(privacy.run_is_holdout(Path(self.store.get_run(self.open[0]).run_dir)))  # type: ignore[union-attr]


class TestStagedAssets(unittest.TestCase):
    def test_version_token_is_resolved_and_every_url_exists(self):
        build = _load("build_hosted_assets", ROOT / "scripts" / "build-hosted-assets.py")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ui"
            version = build.build(ROOT / "ui", out)
            html = (out / "app.html").read_text(encoding="utf-8")
            self.assertNotIn("__V__", html)
            self.assertRegex(version, r"^[0-9a-f]{10}$")
            urls = re.findall(r'["\'](/static/[^"\'?]+)\?v=([0-9a-f]+)["\']', html)
            self.assertGreater(len(urls), 40)
            for path, v in urls:
                self.assertEqual(v, version, path)
                self.assertTrue((out / path.removeprefix("/static/")).is_file(), path)
            self.assertTrue((out / "icons.svg").is_file())  # shell.js fetches /static/icons.svg?v=
            # the source tree keeps its token for the local server
            self.assertIn("__V__", (ROOT / "ui" / "app.html").read_text(encoding="utf-8"))

    def test_version_changes_with_content(self):
        build = _load("build_hosted_assets", ROOT / "scripts" / "build-hosted-assets.py")
        with tempfile.TemporaryDirectory() as tmp:
            ui = Path(tmp) / "ui"
            shutil.copytree(ROOT / "ui", ui)
            before = build.content_version(ui)
            (ui / "theme.js").write_text((ui / "theme.js").read_text() + "\n//x\n")
            self.assertNotEqual(before, build.content_version(ui))

    def test_wrangler_serves_the_staged_copy(self):
        toml = (WORKER / "wrangler.toml").read_text()
        self.assertIn('directory = ".build/ui"', toml)
        self.assertIn("build-hosted-assets.py", toml)


if __name__ == "__main__":
    unittest.main()
