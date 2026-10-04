"""The Score, U21: the launch video capture kit (demo/).

Static checks on the storyboard and shot list, the events.json schema, the
key refusal and, with playwright installed, a dry run of every captured beat
against the fixture corpus. The VHS tape is a media script verified by viewing
its frames, so it has no behavioral test beyond a copy lint. Zero paid calls.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_copy_rules import lint_text

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"

spec = importlib.util.spec_from_file_location("demo_observatory", DEMO / "capture" / "observatory.py")
assert spec and spec.loader
obs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(obs)

try:
    import playwright.sync_api  # noqa: F401
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.upper().endswith("_API_KEY")}


class TestShotList(unittest.TestCase):
    cfg = json.loads((DEMO / "fixtures" / "beats.json").read_text())

    def test_shot_list_is_valid(self):
        self.assertEqual(obs.validate_beats(self.cfg), [])
        self.assertEqual(obs.load_beats()["total_s"], 45)

    def test_beats_match_the_storyboard(self):
        story = (DEMO / "storyboard.md").read_text()
        expected = [("b0-hook", 0, 2), ("b1-score", 2, 8), ("b2-run", 8, 18), ("b3-axes", 18, 26),
                    ("b4-pairings", 26, 35), ("b5-card", 35, 41), ("b6-end", 41, 45)]
        got = [(b["id"], b["start_s"], b["end_s"]) for b in self.cfg["beats"]]
        self.assertEqual(got, expected)
        for bid, _, _ in expected:
            self.assertIn(f"`{bid}`", story)
        for bid, a, b in expected:
            self.assertRegex(story, rf"`{bid}` \| {a} to {b}s")

    def test_every_caption_is_in_the_storyboard_and_clean(self):
        story = (DEMO / "storyboard.md").read_text()
        for b in self.cfg["beats"]:
            self.assertEqual(lint_text(b["caption"]), [], b["id"])
            self.assertIn(b["caption"].split(" / ")[0].rstrip("."), story, b["id"])

    def test_capture_beats_plan_exactly_their_window(self):
        for b in self.cfg["beats"]:
            if b["kind"] == "capture":
                self.assertEqual(sum(obs.step_ms(s) for s in b["steps"]), round((b["end_s"] - b["start_s"]) * 1000), b["id"])

    def test_a_broken_shot_list_is_rejected(self):
        gap = copy.deepcopy(self.cfg)
        gap["beats"][2]["start_s"] = 9
        self.assertTrue(any("tile" in p for p in obs.validate_beats(gap)))
        short = copy.deepcopy(self.cfg)
        short["beats"][2]["steps"].append({"op": "hold", "ms": 1})
        self.assertTrue(any("steps take" in p for p in obs.validate_beats(short)))
        silent = copy.deepcopy(self.cfg)
        silent["beats"][0]["caption"] = ""
        self.assertTrue(any("caption" in p for p in obs.validate_beats(silent)))

    def test_demo_text_follows_the_copy_rules(self):
        for p in [*DEMO.rglob("*.md"), *DEMO.rglob("*.json"), DEMO / "capture" / "cli.tape"]:
            self.assertEqual(lint_text(p.read_text()), [], p.name)

    def test_readme_carries_the_qc_checklist(self):
        text = (DEMO / "README.md").read_text()
        for needle in ("-14 LUFS", "-1 dBTP", "H.264 High", "yuv420p", "60fps", "20 Mbps", "faststart",
                       "at least 2 seconds", "Muted playback", "within 3 seconds", "Remotion 4", "VHS"):
            self.assertIn(needle, text)

    def test_tape_emits_the_cli_scene_for_cli_sh(self):
        tape = (DEMO / "capture" / "cli.tape").read_text()
        self.assertIn("Output cli.mp4", tape)
        self.assertIn("report --leaderboard", tape)
        self.assertNotRegex(tape, r"(?i)api[_-]?key")


class TestEventsSchema(unittest.TestCase):
    def doc(self):
        return {
            "version": 1, "zoom": 1.3333333, "viewport": {"width": 1920, "height": 1080},
            "beats": [{"id": "b2-run", "kind": "capture", "start_ms": 8000, "end_ms": 18000}],
            "events": [{"t_ms": 800, "beat": "b2-run", "type": "target", "label": "lane timeline",
                        "rect": {"x": 120, "y": 140, "w": 600, "h": 200}}],
        }

    def test_valid_document_passes(self):
        self.assertEqual(obs.validate_events(self.doc()), [])

    def test_rect_outside_the_viewport_fails(self):
        for rect in ({"x": 1800, "y": 10, "w": 200, "h": 50}, {"x": -1, "y": 10, "w": 50, "h": 50},
                     {"x": 10, "y": 1000, "w": 50, "h": 100}):
            d = self.doc()
            d["events"][0]["rect"] = rect
            self.assertTrue(any("outside" in p for p in obs.validate_events(d)), rect)

    def test_malformed_events_fail(self):
        d = self.doc()
        d["events"][0]["t_ms"] = 99999
        self.assertTrue(any("t_ms" in p for p in obs.validate_events(d)))
        d = self.doc()
        d["events"][0]["beat"] = "nope"
        self.assertTrue(any("unknown beat" in p for p in obs.validate_events(d)))
        d = self.doc()
        d["events"][0]["rect"]["w"] = 0
        self.assertTrue(any("rect needs" in p for p in obs.validate_events(d)))
        d = self.doc()
        d["version"] = 2
        self.assertTrue(any("version" in p for p in obs.validate_events(d)))

    def test_clip_rect_keeps_only_the_visible_part(self):
        self.assertEqual(obs.clip_rect({"x": 1900, "y": 10, "w": 100, "h": 20}, 1920, 1080),
                         {"x": 1900.0, "y": 10.0, "w": 20.0, "h": 20.0})
        self.assertIsNone(obs.clip_rect({"x": 0, "y": 1200, "w": 50, "h": 50}, 1920, 1080))


class TestKeyRefusal(unittest.TestCase):
    def test_refuses_when_any_api_key_is_set(self):
        with self.assertRaises(SystemExit) as cm:
            obs.refuse_if_keys({"OPENROUTER_API_KEY": "sk-test", "PATH": "/usr/bin"})
        self.assertIn("OPENROUTER_API_KEY", str(cm.exception))
        with self.assertRaises(SystemExit):
            obs.refuse_if_keys({"anything_api_key": "x"})
        obs.refuse_if_keys({"PATH": "/usr/bin", "EMPTY_API_KEY": ""})  # unset in effect

    def test_script_exits_before_touching_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "capture"
            done = subprocess.run(
                [sys.executable, str(DEMO / "capture" / "observatory.py"), "--out", str(out), "--dry-run"],
                env={**CLEAN_ENV, "GROQ_API_KEY": "gsk-test"}, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("GROQ_API_KEY", done.stdout + done.stderr)
            self.assertFalse(out.exists())

    def test_cli_script_refuses_keys_and_the_repo_itself(self):
        script = str(DEMO / "capture" / "cli.sh")
        with tempfile.TemporaryDirectory() as tmp:
            keyed = subprocess.run(["bash", script, tmp], env={**CLEAN_ENV, "OPENROUTER_API_KEY": "sk-test"},
                                   capture_output=True, text=True, timeout=60)
            self.assertEqual(keyed.returncode, 1)
            self.assertIn("OPENROUTER_API_KEY", keyed.stderr)
        inside = subprocess.run(["bash", script, str(ROOT / "demo-out")], env=CLEAN_ENV,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(inside.returncode, 2)
        self.assertFalse((ROOT / "demo-out").exists())


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright not installed (pip install 'orchestral[shots]')")
class TestDryRun(unittest.TestCase):
    """Drives every captured beat against the fixture corpus, without video or waiting."""

    def test_dry_run_is_valid_deterministic_and_inside_the_viewport(self):
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.upper().endswith("_API_KEY")}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                first = obs.capture(Path(tmp) / "a", dry_run=True)
                second = obs.capture(Path(tmp) / "b", dry_run=True)
                self.assertEqual(obs.validate_events(first), [])
                self.assertEqual(first["events"], second["events"])
                self.assertEqual(sorted(p.name for p in (Path(tmp) / "a").iterdir()), ["events.json"])
                written = json.loads((Path(tmp) / "a" / "events.json").read_text())
                self.assertEqual(written["events"], first["events"])
        finally:
            os.environ.update(saved)
        vw, vh = first["viewport"]["width"], first["viewport"]["height"]
        labels = {e["label"] for e in first["events"]}
        for e in first["events"]:
            r = e["rect"]
            self.assertTrue(r["x"] >= 0 and r["y"] >= 0 and r["x"] + r["w"] <= vw and r["y"] + r["h"] <= vh, e)
        self.assertTrue({"lane timeline", "billed cost", "pass column", "judge column",
                         "leading pairing", "program note card"} <= labels, labels)
        captured = {e["beat"] for e in first["events"]}
        self.assertEqual(captured, {"b2-run", "b3-axes", "b4-pairings", "b5-card"})
        self.assertEqual(sum(1 for b in first["beats"] if b["video"]), 0)


if __name__ == "__main__":
    unittest.main()
