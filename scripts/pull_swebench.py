#!/usr/bin/env python3
"""Pull a small sample from the public SWE-bench dataset.

Run:
    python3 scripts/pull_swebench.py

The sample is written to data/swe-bench-50.jsonl.
"""

from __future__ import annotations

import json
from pathlib import Path

def main() -> None:
    try:
        from datasets import load_dataset
    except ModuleNotFoundError:
        raise SystemExit(
            "The 'datasets' package is required. Install it with: pip install datasets"
        )

    out_dir = Path(__file__).resolve().parents[1] / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "swe-bench-50.jsonl"

    print("Streaming 50 rows from princeton-nlp/SWE-bench (test split)...")
    stream = load_dataset("princeton-nlp/SWE-bench", split="test", streaming=True)
    rows = []
    for i, row in enumerate(stream):
        rows.append(row)
        if i >= 49:
            break

    out_path.write_text("\n".join(json.dumps(row, default=str) for row in rows) + "\n")
    print(f"Wrote {len(rows)} rows to {out_path}")


if __name__ == "__main__":
    main()
