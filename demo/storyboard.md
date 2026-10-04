# The Score: 45 second launch video

The research note this comes from lives outside the repository, so the concept
is committed here in full. The video answers one question, "Which model pair is
actually worth it?", using only the real observatory and the real CLI over the
key-free fixture corpus. Nothing is generated: every frame is a deterministic
capture (see `demo/README.md`) or type and rules drawn from the design tokens.

## Rules for every beat

- Paper theme. Palette and type come from `ui/tokens.css`: Instrument Sans for
  captions, IBM Plex Mono for data. Color is evidence only (pass, fail, judge,
  live); nothing decorative.
- Captions are 48px or larger on a 1920x1080 frame, at most two lines, and sit
  in the top 70% of the frame. The bottom 15% stays clear for platform chrome.
- No emoji, no gradients, no stock footage, no music with vocals, no
  generated imagery. The copy rules of the product apply (DESIGN.md section 10):
  no dashes, no emoji, none of the banned words.
- Muted playback must tell the whole story, so every beat carries its caption.
- The real UI is on screen within the first 3 seconds of the first captured
  beat, and no frame holds still for more than 3 seconds before the end card.
- Cuts land on the beat of the score (see Audio); easing is the product's own,
  `cubic-bezier(.2, 0, 0, 1)` out and `cubic-bezier(.4, 0, 1, 1)` in.

## Beats

| Id | Time | Source | On-screen text | What happens |
|---|---|---|---|---|
| `b0-hook` | 0 to 2s | composed | Which model pair is actually worth it? | Two staff lines draw left to right. The baton stroke falls onto the top line and forms the Ictus mark. The question types in beside it, one line, ink on paper. |
| `b1-score` | 2 to 8s | composed | One planner. One worker. One bill. / Does the cheap pair hold up? | Kinetic type on staves: each phrase sits on a staff line, words land on the beat, the baton sweeps the line once as the phrase completes. The pair "orch-a to worker-cheap" is named here so the next beats can show it. |
| `b2-run` | 8 to 18s | capture | Every call has a lane. Every cent is counted. | Run detail of a judged, passing run: the lane timeline (orchestrator, worker, assemble, validate, judge) with bars at their real times, then the Cost and Tokens stats. Camera pushes in on the lanes (0 to 5s), then on the cost stat (5 to 10s). |
| `b3-axes` | 18 to 26s | capture | Mechanical pass and judge score are separate axes. | The Pairings ranking table. The camera frames the Pass column (green, mechanical) then the Judge column (blue), then both. They are never blended into one number. |
| `b4-pairings` | 26 to 35s | capture | Cost per pass, with n on every row. | The Pairings lane strip plot settles: pass rate with its 95% interval on the left, cost per pass on a log axis on the right. Camera holds the leading pair, then pulls out to the full strip. Low n rows stay hatched. |
| `b5-card` | 35 to 41s | capture | Then publish what holds up. | A real Program note card in the Publish editor: scope line, one claim, the primary number, its interval, the caveat, provenance. The camera fits the 1200x675 card. |
| `b6-end` | 41 to 45s | composed | orchestral / github.com/duketopceo/orchestral | End card: the mark, the wordmark, the repository. Held for at least 2 seconds. |

The CLI scene (`demo/capture/cli.tape`) is an optional insert for `b4-pairings`
or the 6-8 second README loop: `harness.py report --leaderboard` printing the
same ranking in the terminal, same glyphs and same numbers as the web.

## Timing sheet (seconds)

```
 0    2        8              18         26            35        41   45
 |b0--|b1------|b2-------------|b3--------|b4-----------|b5------|b6--|
```

Captured beats are recorded long enough to cover their window plus the page
load; `events.json` carries each beat's `video_offset_ms` so the composition
cuts to the right frame.

## Camera and cursor

Recordings have no cursor. The composition draws a synthetic cursor and a
camera move from `events.json`: every `target` event gives a rectangle in video
pixels (the capture viewport is the 1920x1080 frame itself, with the UI zoomed
1.333x so it reads as a 1440x810 layout drawn at full resolution) and a
timestamp in milliseconds from the beat start. Camera moves ease over 600 to
900ms and never exceed 1.5x zoom, so text stays crisp.

## Audio

A single quiet score in the register of the brand: sparse, dry, no melody
louder than the voice of the UI. Loudness target -14 LUFS integrated with a
-1 dBTP ceiling (see the QC checklist). Music license is an open question
tracked in the redesign plan; this repository ships no audio.

## Source of every number on screen

All figures come from the fixture corpus built by
`scripts/build-fixture-corpus.py`, which carries the label "fixture corpus" in
its model names and needs no key. If a take is rebuilt from scrubbed
`runs-pub/` data instead, the caption on `b4-pairings` must say so.
