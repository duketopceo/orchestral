# Launch video capture kit

Everything needed to make "The Score", the 45 second launch video, reproducibly
and for $0. This directory holds the storyboard, the shot list and the capture
scripts. It does not hold a rendered video: composing and rendering is a
separate step (see "Pipeline").

| File | What it is |
|---|---|
| `storyboard.md` | The concept in full: seven beats, on-screen text, timing, rules |
| `fixtures/beats.json` | The shot list as data: routes, selectors, holds, scrolls, captions |
| `capture/observatory.py` | Playwright driver: one WebM per captured beat plus `events.json` |
| `capture/cli.tape` and `capture/cli.sh` | VHS scene for the terminal, extracted to a PNG sequence |

## Rules

- All footage is a deterministic capture of the real product over the key-free
  fixture corpus (`scripts/build-fixture-corpus.py`), or type and rules drawn
  from `ui/tokens.css`. No generated imagery, no stock.
- Captures run with no provider key. `observatory.py` and `cli.sh` refuse to
  start when any `*_API_KEY` variable is set, hand Chromium and VHS an empty
  environment, and make zero network calls beyond the loopback server.
- Output goes outside the repository. Nothing here writes into the tree.
- Copy follows the product rules (DESIGN.md section 10): no dashes, no emoji, none of the banned words.

## Pipeline

1. Capture (this directory). Needs the `[shots]` extra and Chromium, plus
   `vhs`, `ttyd` and `ffmpeg` for the terminal scene.
2. Compose with Remotion 4, which is verified to run on this aarch64 machine.
   The composition reads `events.json` for the synthetic cursor and the camera
   moves and draws the three composed beats (`b0-hook`, `b1-score`, `b6-end`)
   from the design tokens and `ui/brand/` marks. Where the composition lives is
   an open question in the redesign plan (U22).
3. Finish with ffmpeg: 1920x1080 hero, 1080x1080 feed and a 6 to 8 second
   README loop. Loudness and encode settings are in the QC checklist.

## Capture

```bash
python -m pip install -e '.[shots]' && python -m playwright install chromium

# UI beats: one WebM per beat plus events.json
env -u OPENROUTER_API_KEY python demo/capture/observatory.py --out /tmp/launch-capture
env ... python demo/capture/observatory.py --out /tmp/launch-capture --beat b4-pairings
env ... python demo/capture/observatory.py --out /tmp/x --dry-run   # no video, no waiting

# CLI beat: PNG sequence in <out>/frames/
demo/capture/cli.sh /tmp/launch-cli
```

How the recordings are made, and what they cost you:

- Playwright records video at the viewport's pixel size and ignores device
  scale. The viewport is therefore the 1920x1080 frame itself, with the UI zoomed
  1.333x so it lays out as 1440x810. The picture is drawn at full resolution,
  not upscaled. Playwright records at 25fps; the composition re-times to 60fps.
- Chromium draws no cursor into a recording. The composition adds one.
- `events.json` has the beat list (with `video_offset_ms`, how long the
  recording ran before the beat's t=0) and one event per `target`, `column` or
  `scroll` step: `t_ms` from the beat start, a `label`, and a `rect` in video
  pixels. Event times come from the planned clock, so they and the rectangles
  are identical on every run. Every rectangle lies inside the viewport.
- The run shown in `b2-run` is the first judged, passing, finished run by id, so
  it is the same run every time.
- VHS writes an MP4 at 25fps whatever `Framerate` says and no PNG frames of its
  own, so `cli.sh` extracts the PNG sequence with ffmpeg. The tape uses
  Liberation Mono; install IBM Plex Mono system-wide and edit `FontFamily` for
  the final take.

## Pre-posting QC checklist

Run on the final 1920x1080 and 1080x1080 files before anything is posted.

- [ ] Muted playback is complete: every beat's caption carries the story.
- [ ] The real UI is on screen within 3 seconds of the first captured beat.
- [ ] No static hold longer than 3 seconds before the end card.
- [ ] Captions are 48px or larger, the bottom 15% of the frame is clear, and
      there is no emoji, gradient or stock footage.
- [ ] Loudness is -14 LUFS integrated with a true peak of -1 dBTP or lower.
- [ ] Encode is H.264 High, yuv420p, 60fps, at most 20 Mbps, with `faststart`.
- [ ] The end card (mark, wordmark, repository) is held for at least 2 seconds.
- [ ] The caption of the Pairings beat says "fixture corpus" if the data is the
      fixture corpus, and names the source otherwise.
