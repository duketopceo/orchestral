"""Tests for the visual comparison gallery in the reporter."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestral.reporter import generate_gallery
from orchestral.storage import RunMeta


def _run(
    runs_dir: Path,
    run_id: str,
    *,
    passes: bool = True,
    shot: bool = False,
    png_artifact: bool = False,
    mp4_artifact: bool = False,
) -> RunMeta:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if mp4_artifact:
        (run_dir / "artifact.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
    elif png_artifact:
        (run_dir / "artifact.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    else:
        (run_dir / "artifact.html").write_text("<html><body><h1>demo</h1></body></html>")
    if shot:
        (run_dir / "screenshot.png").write_bytes(b"pngbytes")
    (run_dir / "report.json").write_text(json.dumps({"task_id": "t1", "checks": {}, "errors": []}))
    return RunMeta(
        run_id=run_id, task_id="t1", orchestrator="org/a", worker="wrk/b",
        run_dir=str(run_dir), status="finished", passes=passes,
        score=0.8, total_cost_usd=0.0123,
        started_at="2026-09-11T00:00:00+00:00",
    )


class TestGallery(unittest.TestCase):
    def test_card_per_finished_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            reports = base / "reports"
            reports.mkdir()
            runs = [_run(base, "r1"), _run(base, "r2", passes=False)]
            page = generate_gallery(runs, reports)
            self.assertEqual(page.count("class='card'"), 2)
            self.assertIn("org/a", page)
            self.assertIn("wrk/b", page)
            self.assertIn("$0.0123", page)

    def test_iframe_fallback_without_screenshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            reports = base / "reports"
            reports.mkdir()
            page = generate_gallery([_run(base, "r1")], reports)
            self.assertIn("<iframe class='thumb' src='shots/r1.html'", page)
            self.assertTrue((reports / "shots" / "r1.html").exists())

    def test_screenshot_img_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            reports = base / "reports"
            reports.mkdir()
            page = generate_gallery([_run(base, "r1", shot=True)], reports)
            self.assertIn("shots/r1.png", page)
            self.assertTrue((reports / "shots" / "r1.png").exists())

    def test_png_artifact_shown_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            reports = base / "reports"
            reports.mkdir()
            page = generate_gallery([_run(base, "r1", png_artifact=True)], reports)
            self.assertIn("shots/r1-artifact.png", page)

    def test_mp4_artifact_shown_as_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            reports = base / "reports"
            reports.mkdir()
            page = generate_gallery([_run(base, "r1", mp4_artifact=True)], reports)
            self.assertIn("<video class='thumb' src='shots/r1-artifact.mp4'", page)
            self.assertTrue((reports / "shots" / "r1-artifact.mp4").exists())

    def test_empty_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            reports = base / "reports"
            reports.mkdir()
            page = generate_gallery([], reports)
            self.assertIn("No finished runs yet.", page)


if __name__ == "__main__":
    unittest.main()
