"""The Score, U20: motion polish (DESIGN.md 6.7).

Static lint on the shipped CSS and JS plus a Chromium suite for the runtime
behavior: the baton, number tick, row insert, phase fill, route crossfade and
the row-height skeleton, each with and without reduced motion. Zero paid
calls; the corpus is built locally. Skipped without playwright
(BROWSER=1 scripts/bootstrap-venv.sh).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestral.web.server import Observatory, make_handler

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"
FIXTURES = ROOT / "tests" / "fixtures" / "observatory"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sb = _load("serve_browser_helpers", "tests/test_serve_browser.py")
corpus = _load("build_fixture_corpus", "scripts/build-fixture-corpus.py")
HAS_PLAYWRIGHT = sb.HAS_PLAYWRIGHT

ALLOWED_ANIMATED = {"transform", "opacity"}


class TestMotionLint(unittest.TestCase):
    css = "\n".join(p.read_text() for p in sorted(UI.glob("*.css")))

    def test_no_transition_all(self):
        for p in [*UI.glob("*.css"), *UI.rglob("*.js"), *UI.rglob("*.html")]:
            text = p.read_text()
            self.assertNotRegex(text, r"transition(-property)?\s*:\s*[^;{}]*\ball\b", p.name)

    def test_keyframes_animate_only_transform_and_opacity(self):
        blocks = re.findall(r"@keyframes\s+([\w-]+)\s*\{((?:[^{}]*\{[^{}]*\})*)\s*\}", self.css)
        self.assertGreaterEqual(len(blocks), 4, [b[0] for b in blocks])
        for name, body in blocks:
            props = set(re.findall(r"([\w-]+)\s*:", " ".join(re.findall(r"\{([^{}]*)\}", body))))
            self.assertLessEqual(props, ALLOWED_ANIMATED, f"@keyframes {name} animates {props}")

    def test_transitions_name_only_transform_or_opacity(self):
        for m in re.finditer(r"transition\s*:\s*([^;{}]+)", self.css):
            decl = m.group(1).strip()
            if decl in {"none", "0s"}:
                continue
            self.assertRegex(decl, r"^(transform|opacity)\b", decl)

    def test_motion_module_uses_no_timers(self):
        src = (UI / "js" / "motion.js").read_text()
        for banned in ("setTimeout", "setInterval"):
            self.assertNotIn(banned, src)

    def test_import_map_lists_motion(self):
        self.assertIn('"/static/js/motion.js": "/static/js/motion.js?v=__V__"', (UI / "app.html").read_text())

    def test_reduced_motion_block_zeroes_durations(self):
        m = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\n\}", self.css, re.S)
        self.assertIsNotNone(m)
        block = m.group(1)
        self.assertRegex(block, r"animation-duration:\s*0(\.0+)?s\s*!important")
        self.assertRegex(block, r"transition-duration:\s*0(\.0+)?s\s*!important")


JOB = {"run_id": "motion-fixture-run", "label": "corpus/orch-a to corpus/worker-cheap", "state": "running",
       "phase": None, "elapsed": "1m", "idle_s": 2, "owned": True, "spend_usd": 0.0042,
       "cancellable": False, "abandonable": False}


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class TestMotionBrowser(sb._Browser):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = Path(tempfile.mkdtemp())
        with patch.dict(os.environ, {}, clear=True):
            cls.manifest = corpus.build_corpus(cls.tmp / "runs", "full")
        obs = Observatory(cls.tmp / "runs", FIXTURES / "tasks", FIXTURES / "models")
        cls.httpd, port = sb._serve(make_handler(obs))
        cls.base = f"http://127.0.0.1:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def _now_with_live_job(self, **kw):
        pg = self.page(viewport={"width": 1440, "height": 900}, **kw)

        def overview(route):
            resp = route.fetch()
            body = json.loads(resp.text())
            body["jobs"] = [JOB]
            route.fulfill(response=resp, body=json.dumps(body))

        pg.route("**/api/overview", overview)
        pg.goto(f"{self.base}/#/")
        pg.wait_for_selector("#view[data-ready='ok'] .live-lane .live-ring")
        return pg

    def _module(self, pg, expr: str):
        return pg.evaluate(f"(async () => {{ const m = await import('/static/js/motion.js'); return ({expr}); }})()")

    # -- baton and reduced motion -------------------------------------------

    def test_baton_sweeps_and_stops_under_reduced_motion(self):
        pg = self._now_with_live_job()
        style = pg.evaluate("(() => { const s = getComputedStyle(document.querySelector('.live-lane .live-ring')); "
                            "return [s.animationName, s.animationDuration, s.animationIterationCount]; })()")
        self.assertEqual(style, ["baton", "2.4s", "infinite"])
        self.assertEqual(self.errors, [])

        pg = self._now_with_live_job(reduced_motion="reduce")
        ring = pg.evaluate("(() => { const el = document.querySelector('.live-lane .live-ring'); "
                           "const s = getComputedStyle(el); const a = getComputedStyle(el, '::after'); "
                           "return {dur: s.animationDuration, after: a.display, "
                           "meta: document.querySelector('.live-lane .ll-meta').textContent, "
                           "dot: !!el.querySelector('svg use')}; })()")
        self.assertEqual(ring["dur"], "0s")
        self.assertEqual(ring["after"], "none")  # the baton stroke is gone, the static dot stays
        self.assertTrue(ring["dot"])
        self.assertIn("live", ring["meta"])

    def test_reduced_motion_zeroes_every_duration_on_every_route(self):
        pg = self.page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        run = self.manifest["failed_run_id"]
        for route in ("/", "/runs", f"/run/{run}", "/leaderboard", "/cards", "/models"):
            pg.goto(f"{self.base}/#{route}")
            pg.wait_for_selector("#view[data-ready]", timeout=20000)
            bad = pg.evaluate("""() => {
              const out = [];
              for (const el of document.querySelectorAll('*')) {
                for (const pe of [null, '::before', '::after']) {
                  const s = getComputedStyle(el, pe);
                  const d = [...s.animationDuration.split(','), ...s.transitionDuration.split(',')];
                  if (d.some(v => parseFloat(v) > 0)) out.push((el.id || el.className || el.tagName) + (pe || '') + ' ' + d.join('|'));
                }
              }
              return out;
            }""")
            self.assertEqual(bad, [], route)
            live = pg.evaluate("document.getAnimations().filter(a => a.effect.getTiming().duration > 0).length")
            self.assertEqual(live, 0, route)
        self.assertEqual(self.errors, [])

    # -- route change -------------------------------------------------------

    def test_route_change_crossfades_view_in_120ms(self):
        pg = self.page(viewport={"width": 1440, "height": 900})
        pg.add_init_script("""
          window.__fades = [];
          const orig = Element.prototype.animate;
          Element.prototype.animate = function (kf, opts) {
            if (this.id === 'view') window.__fades.push({kf: JSON.stringify(kf), d: typeof opts === 'number' ? opts : opts.duration});
            return orig.call(this, kf, opts);
          };""")
        pg.goto(f"{self.base}/#/runs")
        pg.wait_for_selector("#view[data-ready='ok']")
        pg.evaluate("location.hash = '#/models'")
        pg.wait_for_function("document.title.startsWith('Model catalog')")
        fades = pg.evaluate("window.__fades")
        self.assertTrue(fades, "no crossfade on route change")
        self.assertEqual({f["d"] for f in fades}, {120})
        for f in fades:
            props = set(re.findall(r'"(\w+)":', f["kf"])) - {"offset", "easing"}
            self.assertLessEqual(props, ALLOWED_ANIMATED)
        self.assertEqual(self.errors, [])

    def test_reduced_motion_route_change_does_not_animate_view(self):
        pg = self.page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
        pg.add_init_script("""
          window.__fades = 0;
          const orig = Element.prototype.animate;
          Element.prototype.animate = function () { window.__fades += 1; return orig.apply(this, arguments); };""")
        pg.goto(f"{self.base}/#/runs")
        pg.wait_for_selector("#view[data-ready='ok']")
        pg.evaluate("location.hash = '#/models'")
        pg.wait_for_function("document.title.startsWith('Model catalog')")
        self.assertEqual(pg.evaluate("window.__fades"), 0)

    def test_skeleton_row_height_equals_final_row_height(self):
        pg = self.page(viewport={"width": 1440, "height": 900})
        held: list = []
        pg.route("**/api/runs*", lambda route: held.append(route))
        pg.goto(f"{self.base}/#/runs")
        pg.wait_for_selector("#view .skeleton .skel-row", state="attached")
        self.assertIsNone(pg.get_attribute("#view", "data-ready"))
        skel = pg.evaluate("[...document.querySelectorAll('#view .skel-row')].map(r => r.getBoundingClientRect().height)")
        self.assertGreaterEqual(len(skel), 6)
        self.assertEqual(len(set(skel)), 1, skel)
        self.assertEqual(pg.get_attribute("#view .skeleton", "aria-hidden"), "true")
        for route in held:
            route.continue_()
        pg.unroute("**/api/runs*")
        pg.wait_for_selector("#view[data-ready='ok'] #runs-body tr")
        final = pg.evaluate("[...document.querySelectorAll('#runs-body tr')].slice(0, 8).map(r => r.getBoundingClientRect().height)")
        self.assertEqual(set(final), {skel[0]}, (skel, final))
        self.assertEqual(pg.locator("#view .skeleton").count(), 0)
        self.assertEqual(self.errors, [])

    # -- number tick, row insert, phase fill ---------------------------------

    def test_number_tick_animates_only_the_changed_digits(self):
        pg = self.page(viewport={"width": 1440, "height": 900})
        pg.goto(f"{self.base}/#/about")
        pg.wait_for_selector("#view[data-ready='ok']")
        out = pg.evaluate("""async () => {
          const m = await import('/static/js/motion.js');
          const el = document.createElement('span'); el.id = 'tk-test'; document.body.append(el);
          el.textContent = '$0.0418';
          m.tick(el, '$0.0431');
          const changed = el.querySelector('.tk');
          const anims = changed ? changed.getAnimations() : [];
          return {text: el.textContent, stable: el.firstChild.textContent, changed: changed && changed.textContent,
                  count: anims.length, dur: anims[0] && anims[0].effect.getTiming().duration,
                  props: anims[0] && anims[0].effect.getKeyframes().map(k => Object.keys(k).filter(x => !['offset','easing','composite','computedOffset'].includes(x)).sort().join())};
        }""")
        self.assertEqual(out["text"], "$0.0431")
        self.assertEqual(out["stable"], "$0.04")
        self.assertEqual(out["changed"], "31")
        self.assertEqual((out["count"], out["dur"]), (1, 120))
        self.assertEqual(set(out["props"]), {"opacity,transform"})
        same = pg.evaluate("""async () => {
          const m = await import('/static/js/motion.js'); const el = document.getElementById('tk-test');
          m.tick(el, '$0.0431'); return el.querySelectorAll('.tk').length + ':' + el.getAnimations({subtree: true}).length;
        }""")
        self.assertEqual(same, "1:1")  # an unchanged value starts nothing new

    def test_number_tick_is_instant_under_reduced_motion(self):
        pg = self.page(reduced_motion="reduce")
        pg.goto(f"{self.base}/#/about")
        pg.wait_for_selector("#view[data-ready='ok']")
        out = pg.evaluate("""async () => {
          const m = await import('/static/js/motion.js');
          const el = document.createElement('span'); document.body.append(el);
          el.textContent = '1,204'; m.tick(el, '1,310');
          return [el.textContent, el.getAnimations({subtree: true}).length];
        }""")
        self.assertEqual(out, ["1,310", 0])

    def test_row_insert_and_phase_fill_use_css_animation(self):
        pg = self.page(viewport={"width": 1440, "height": 900})
        pg.goto(f"{self.base}/#/about")
        pg.wait_for_selector("#view[data-ready='ok']")
        out = pg.evaluate("""async () => {
          const m = await import('/static/js/motion.js');
          document.body.insertAdjacentHTML('beforeend',
            '<div id="rows"><div class="ev-item" id="r1">row</div><div class="ln-bar" id="b1"></div></div>');
          m.enterRows([document.getElementById('r1')]);
          m.fillBars([document.getElementById('b1')]);
          const name = id => getComputedStyle(document.getElementById(id)).animationName;
          const wash = getComputedStyle(document.getElementById('r1'), '::after');
          return [name('r1'), name('b1'), wash.animationName, wash.animationDuration];
        }""")
        self.assertEqual(out, ["row-in", "ph-fill", "row-wash", "1.2s"])

    def test_live_header_ticks_cost_and_tokens_on_a_running_run(self):
        run = self.manifest["failed_run_id"]
        pg = self.page(viewport={"width": 1440, "height": 900})
        pg.goto(f"{self.base}/#/run/{run}")
        pg.wait_for_selector("#view[data-ready='ok'] [data-tick='cost']")
        self.assertEqual(pg.locator("[data-tick='tokens']").count(), 1)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
