#!/usr/bin/env python3
"""Generate a set of HTML landing-page task YAMLs for batch evals.

Run:
    python3 scripts/gen_html_tasks.py --count 100

Defaults to 100 tasks in tasks/batch-100/.
"""

from __future__ import annotations

import argparse
from itertools import product
from pathlib import Path

import yaml

PRODUCTS = [
    "coffee roaster",
    "electric bike",
    "personal CRM",
    "no-code automation tool",
    "dev-ops observability platform",
    "AI coding assistant",
    "remote team retreat planner",
    "carbon offset marketplace",
    "open-source fonts library",
    "smart home dashboard",
]

AUDIENCES = [
    "early adopters",
    "small business owners",
    "open-source developers",
    "enterprise security teams",
    "remote-first startups",
    "climate-conscious consumers",
    "design systems engineers",
    "indie hackers",
    "product managers",
    "data engineering teams",
]


def _task_for(i: int, product: str, audience: str, index_offset: int) -> dict[str, object]:
    return {
        "id": f"html-batch-{i + index_offset:03d}",
        "type": "html",
        "prompt": (
            f"Create a landing page for a {product} targeted at {audience}. "
            "Write a strong headline, a 2-3 sentence value proposition, three feature bullets, "
            "and a call-to-action button. Output a single self-contained HTML file with inline CSS."
        ),
        "validation": ["html"],
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Generate HTML batch task YAMLs")
    p.add_argument("--count", type=int, default=100, help="Number of tasks to generate")
    p.add_argument("--out", default="tasks/batch-100", help="Output directory")
    p.add_argument("--offset", type=int, default=0, help="Starting index for task IDs")
    args = p.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    combos = list(product(PRODUCTS, AUDIENCES))
    while len(combos) < args.count:
        combos.extend(combos)
    combos = combos[: args.count]

    for i, (prod, audience) in enumerate(combos):
        task = _task_for(i, prod, audience, args.offset)
        (out_dir / f"{task['id']}.yaml").write_text(
            yaml.safe_dump(task, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    print(f"Wrote {args.count} HTML task files to {out_dir}")


if __name__ == "__main__":
    main()
