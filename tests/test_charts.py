"""U13: the hand-built SVG chart kit (ui/js/charts/*), exercised under node.

No browser: the kit is pure string builders, so output is deterministic and can
be asserted directly. Covers the plan's chart scenarios: role/title/desc, the
table fallback, log-axis ticks, row order, hatch for low n, tokens only.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHARTS = ROOT / "ui" / "js" / "charts"

ROWS = [
    {"key": "o1|w1", "orchestrator": "corpus/orch-a", "worker": "corpus/worker-cheap",
     "finished": 281, "passed": 207, "pass_rate": 0.737, "pass_ci": [0.682, 0.785],
     "cost_per_pass": 0.0104, "cost_total": 2.5, "href": "#/card?kind=pairing&target=o1|w1"},
    {"key": "o2|w2", "orchestrator": "corpus/orch-b", "worker": "corpus/worker-hot",
     "finished": 221, "passed": 119, "pass_rate": 0.538, "pass_ci": [0.473, 0.603],
     "cost_per_pass": 0.0442, "cost_total": 5.0, "href": "#/card?kind=pairing&target=o2|w2"},
    {"key": "o3|w3", "orchestrator": "corpus/orch-a", "worker": "corpus/worker-fresh",
     "finished": 2, "passed": 2, "pass_rate": 1.0, "pass_ci": [0.342, 1.0],
     "cost_per_pass": 0.131, "cost_total": 0.26, "href": "#/card?kind=pairing&target=o3|w3"},
    {"key": "o4|w4", "orchestrator": "corpus/orch-a", "worker": "corpus/worker-free",
     "finished": 12, "passed": 6, "pass_rate": 0.5, "pass_ci": [0.25, 0.75],
     "cost_per_pass": None, "cost_total": 0, "href": "#/card?kind=pairing&target=o4|w4"},
    {"key": "o5|w5", "orchestrator": "corpus/orch-b", "worker": "corpus/worker-bad",
     "finished": 12, "passed": 0, "pass_rate": 0.0, "pass_ci": [0.0, 0.24],
     "cost_per_pass": None, "cost_total": 0.4, "href": "#/card?kind=pairing&target=o5|w5"},
]


def run_node(body: str):
    script = (
        f"import * as C from {(CHARTS / 'index.js').as_uri()!r};"
        f"const ROWS = {json.dumps(ROWS)};"
        f"{body}"
    )
    proc = subprocess.run(["node", "--input-type=module", "-e", script],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestChartKit(unittest.TestCase):
    def test_kit_files_exist_and_use_no_library_or_cdn(self):
        files = sorted(CHARTS.glob("*.js"))
        self.assertGreaterEqual(len(files), 4)
        for p in files:
            src = p.read_text(encoding="utf-8")
            self.assertNotRegex(src, r"https?://(?!www\.w3\.org)", p.name)
            self.assertNotRegex(src, r"\bd3\b|chart\.js|from ['\"][^.]", p.name)
            self.assertNotIn("setTimeout", src, p.name)

    def test_no_colour_literals_in_the_kit(self):
        for p in CHARTS.glob("*.js"):
            self.assertNotRegex(p.read_text(encoding="utf-8"), r"#[0-9a-fA-F]{3,8}\b", p.name)

    def test_log_ticks_cover_the_data_decades(self):
        got = run_node("console.log(JSON.stringify([C.logTicks(0.0104, 0.131), C.logTicks(0.0007, 0.9),"
                       " C.logTicks(0.02, 0.03)]));")
        self.assertEqual(got[0], [0.01, 0.1, 1])
        self.assertEqual(got[1], [0.0001, 0.001, 0.01, 0.1, 1])
        self.assertEqual(got[2], [0.01, 0.1])

    def test_ramp_has_six_steps_and_never_diverges(self):
        got = run_node("console.log(JSON.stringify([0,0.1,0.35,0.5,0.8,1].map(v => C.rampStep(v))));")
        self.assertEqual(got, [0, 0, 2, 3, 4, 5])
        got = run_node("console.log(JSON.stringify([C.rampStep(0.5, 0.5), C.rampStep(null), C.rampStep(0.05,0.1)]));")
        self.assertEqual(got, [5, None, 3])

    def test_strip_plot_is_an_accessible_image_with_title_desc_and_table(self):
        html = run_node("console.log(JSON.stringify(C.laneStrip({id:'s1', rows: ROWS, width: 960,"
                        " lensLabel: 'Best overall'})));")
        self.assertEqual(len(re.findall(r'<svg[^>]*role="img"', html)), 1)
        self.assertIn('aria-labelledby="s1-title s1-desc"', html)
        self.assertRegex(html, r'<title id="s1-title">[^<]+</title>')
        desc = re.search(r'<desc id="s1-desc">([^<]+)</desc>', html).group(1)
        self.assertIn("74%", desc)  # leading pairing's pass rate, from the shared formatter
        self.assertIn("$0.010", desc)
        self.assertIn("5 pairings", desc)
        self.assertIn("<summary>View as table</summary>", html)
        self.assertEqual(len(re.findall(r"<tr data-key=", html)), len(ROWS))

    def test_strip_rows_keep_the_given_order_and_are_focusable_links(self):
        html = run_node("console.log(JSON.stringify(C.laneStrip({id:'s2', rows: ROWS, width: 960})));")
        keys = re.findall(r'<a class="ch-mark"[^>]*data-key="([^"]+)"', html)
        self.assertEqual(keys, [r["key"] for r in ROWS])
        anchors = re.findall(r'<a class="ch-mark"[^>]*>', html)
        self.assertTrue(all('tabindex="0"' in a and "href=" in a and "aria-label=" in a for a in anchors))

    def test_cost_axis_labels_ticks_across_the_span(self):
        html = run_node("console.log(JSON.stringify(C.laneStrip({id:'s3', rows: ROWS, width: 960})));")
        for tick in ("$0.01", "$0.1", "$1"):
            self.assertRegex(html, rf'<text class="ch-tick"[^>]*>{re.escape(tick)}</text>')

    def test_unmetered_and_zero_pass_rows_are_labelled_not_plotted(self):
        html = run_node("console.log(JSON.stringify(C.laneStrip({id:'s4', rows: ROWS, width: 960})));")
        self.assertIn("unmetered", html)
        self.assertIn("no passes", html)
        self.assertEqual(html.count('class="ch-cost-pt"'), 3)

    def test_low_n_uses_the_hatch_pattern_and_a_text_marker(self):
        html = run_node("console.log(JSON.stringify(C.laneStrip({id:'s5', rows: ROWS, width: 960})));")
        # n<10 (LOW_N_BEST): the 2-run pairing only
        self.assertEqual(html.count('fill="url(#hatch)"'), 1)
        self.assertIn("low n", html)

    def test_zero_metered_rows_drop_the_cost_panel(self):
        html = run_node("console.log(JSON.stringify(C.laneStrip({id:'s6', rows: ROWS.map(r => ({...r,"
                        " cost_per_pass: null, cost_total: 0})), width: 960})));")
        self.assertNotIn('class="ch-cost-pt"', html)
        self.assertIn("No pairing is metered", html)

    def test_narrow_width_stacks_pass_above_cost_with_labels_above_rows(self):
        wide = run_node("console.log(JSON.stringify(C.laneStrip({id:'w', rows: ROWS, width: 960})));")
        narrow = run_node("console.log(JSON.stringify(C.laneStrip({id:'n', rows: ROWS, width: 350})));")
        self.assertIn('data-layout="wide"', wide)
        self.assertIn('data-layout="stacked"', narrow)
        titles = re.findall(r'<text class="ch-panel"[^>]*y="([\d.]+)"[^>]*>([^<]+)</text>', narrow)
        self.assertEqual([t[1] for t in titles][:2], ["Pass rate, 95% interval", "Cost per pass, log scale"])
        self.assertLess(float(titles[0][0]), float(titles[1][0]))
        w = int(re.search(r'<svg[^>]*width="(\d+)"', narrow).group(1))
        self.assertLessEqual(w, 350)

    def test_output_is_deterministic(self):
        body = ("console.log(JSON.stringify([C.laneStrip({id:'d', rows: ROWS, width: 800}),"
                " C.laneStrip({id:'d', rows: ROWS, width: 800})]));")
        a, b = run_node(body)
        self.assertEqual(a, b)

    def test_chart_text_alternatives_are_escaped(self):
        html = run_node("console.log(JSON.stringify(C.laneStrip({id:'e', rows: [{...ROWS[0], orchestrator:"
                        " '<b>x</b>'}], width: 800})));")
        self.assertNotIn("<b>x</b>", html)

    def test_heatmap_cells_are_links_with_ramp_hatch_and_empty_states(self):
        body = ("console.log(JSON.stringify(C.heatmap({id:'h', caption:'Scrolls sideways', corner:'Orchestrator',"
                " rowHeads:[{key:'a',label:'a'}], colHeads:[{key:'x',label:'x'},{key:'y',label:'y'},{key:'z',label:'z'}],"
                " cells:[{row:'a',col:'x',value:0.9,n:20,href:'#/c1',title:'t'},"
                " {row:'a',col:'y',value:0.4,n:2,href:'#/c2',title:'t2'}]})));")
        html = run_node(body)
        self.assertIn('class="chart-scroll"', html)
        self.assertIn('tabindex="0"', html)
        self.assertIn("Scrolls sideways", html)
        self.assertEqual(len(re.findall(r'<a class="hm-cell[^"]*"', html)), 2)
        self.assertIn("ramp-5", html)
        self.assertIn("hm-thin", html)
        self.assertIn("hm-none", html)

    def test_dumbbell_carries_both_intervals_and_a_text_alternative(self):
        body = ("console.log(JSON.stringify(C.dumbbell({id:'d1', a:{passed:8, finished:10, ci:[0.49,0.94]},"
                " b:{passed:5, finished:10, ci:[0.24,0.76]}, width: 220})));")
        html = run_node(body)
        self.assertIn('role="img"', html)
        self.assertRegex(html, r"<desc[^>]*>[^<]*8 of 10 \(80%\)[^<]*5 of 10 \(50%\)")
        self.assertEqual(html.count('class="ch-whisker'), 2)
        self.assertEqual(html.count('class="ch-pt'), 2)

    def test_dumbbell_hatches_low_n_whiskers(self):
        body = ("console.log(JSON.stringify(C.dumbbell({id:'d2', a:{passed:1, finished:2, ci:[0.1,0.9]},"
                " b:{passed:5, finished:10, ci:[0.24,0.76]}, width: 220})));")
        self.assertEqual(run_node(body).count('fill="url(#hatch)"'), 1)


if __name__ == "__main__":
    unittest.main()
