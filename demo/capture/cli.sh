#!/usr/bin/env bash
# Render the CLI scene of the launch video as a PNG sequence.
#
#   demo/capture/cli.sh /path/outside/the/repo
#
# Builds the key-free fixture corpus, runs the VHS tape with an empty
# environment (only HOME, PATH and the two paths the tape reads) and extracts
# <out>/frames/00001.png onward from the MP4 VHS writes. Needs vhs, ttyd and
# ffmpeg on PATH. Writes only under the output directory.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <output-dir outside the repository>" >&2
  exit 2
fi
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$(realpath -m "$1")"
case "$OUT" in "$REPO"|"$REPO"/*) echo "refusing to write inside the repository: $OUT" >&2; exit 2;; esac

keys="$(env | sed -n 's/^\([A-Za-z0-9_]*_API_KEY\)=.*/\1/p' | tr '\n' ' ')"
if [ -n "$keys" ]; then
  echo "refusing to start with provider keys in the environment: $keys" >&2
  exit 1
fi
for tool in vhs ttyd ffmpeg; do
  command -v "$tool" >/dev/null || { echo "missing $tool on PATH" >&2; exit 2; }
done

mkdir -p "$OUT"
PY="${PYTHON:-python3}"
rm -rf "$OUT/corpus-runs" "$OUT/frames" "$OUT/cli.mp4"
"$PY" "$REPO/scripts/build-fixture-corpus.py" --out "$OUT/corpus-runs" >/dev/null
mkdir -p "$OUT/frames"
( cd "$OUT" && env -i HOME="$HOME" PATH="$PATH" ORCH_REPO="$REPO" ORCH_DEMO_RUNS="$OUT/corpus-runs" \
    vhs --quiet "$REPO/demo/capture/cli.tape" )
ffmpeg -v error -y -i "$OUT/cli.mp4" "$OUT/frames/%05d.png"
echo "wrote $(ls "$OUT/frames" | wc -l) frames to $OUT/frames"
