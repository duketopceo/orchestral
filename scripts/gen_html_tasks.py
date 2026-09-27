#!/usr/bin/env python3
"""Generate a set of HTML landing-page task YAMLs for batch evals.

Run:
    python3 scripts/gen_html_tasks.py --count 100

Defaults to 100 tasks in tasks/batch-100/.

Every generated spec carries a topic anchor, because `html` alone grades shape:
`html_parses` + `non_empty` accept any well-formed page, so 100 copies of one
template scored 100 without a model writing a word about coffee roasters. The
anchor is two-part, one leg per thing that makes a spec unique:

- `metadata.required` — one stem from the *product*, matched as a substring, so
  it also matches its inflections ("roast" covers roast/roasted/roastery).
- `metadata.pattern` — an alternation of the vocabulary an *audience*-targeted
  page actually uses, matched as a regex so "early adopter", "early access" and
  "be the first" all satisfy the same leg.

Both are derived from the spec's own subject. A generic word any template would
carry ("landing", "button", "headline") is not an anchor and does not appear.
`tests/test_spec_anchors.py` is the regression: it asserts a faithful artifact
for one spec is rejected by its siblings, so an anchor cannot go decorative.
"""

from __future__ import annotations

import argparse
import re
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

# One distinctive stem per product. Chosen so a page for a *different* product
# in this list does not contain it: `fonts` rather than `font` (every page has a
# `font-family` in its CSS), `carbon` rather than `marketplace`, and so on.
PRODUCT_ANCHORS: dict[str, str] = {
    "coffee roaster": "roast",
    "electric bike": "bike",
    "personal CRM": "crm",
    "no-code automation tool": "automat",
    "dev-ops observability platform": "observab",
    "AI coding assistant": "assistant",
    "remote team retreat planner": "retreat",
    "carbon offset marketplace": "carbon",
    "open-source fonts library": "fonts",
    "smart home dashboard": "dashboard",
}

# Vocabulary a page aimed at that audience really reaches for, as an alternation.
# Written as synonyms rather than the audience name alone so a page that says
# "bootstrapped founders" is not failed for omitting the word "indie hacker".
#
# Every alternative here has to be a word a landing page *about that audience*
# would choose, and no word that a generic template would carry for free.
# Measured against real artifacts and hand-written honest pages, and four were
# removed as over-generic: `\bspacing\b` (every stylesheet has it), bare `roadmap`
# (any product page mentions one) and bare `typeface` (any page about type
# mentions one). Every short alternative also needs its own `\b`: unanchored
# `git` matched "digit", and unanchored `etl` matched "budget". A token that any
# template, or any unrelated word in the artifact, contains is not an anchor.
#
# Some overlap is irreducible and is left in rather than papered over. Three
# product names are themselves the sibling audience's vocabulary — "open-source
# fonts library" contains `open-source`, "carbon offset marketplace" contains
# `carbon`, "remote team retreat planner" contains `remote team` — so an honest
# page for those specs also satisfies the matching audience leg. That is the
# `near_duplicate_family` finding, not an anchor defect, and the family is
# collapsed in a separate issue.
AUDIENCE_ANCHORS: dict[str, str] = {
    "early adopters": (
        r"early adopter|early access|early bird|early|first[ -]?look|be the first|"
        r"get early|first to try|first in line|waitlist|before everyone|"
        r"founding (?:member|customer)|locked in early|head start"
    ),
    "small business owners": (
        r"small business|small biz|\bsmb\b|local business|independent (?:business|shop|retailer)|"
        r"shop owner|business owner|sole proprietor|main street|mom[- ]and[- ]pop|"
        r"grow your business|run your business|boutique owner"
    ),
    "open-source developers": (
        r"open[ -]source|developer|dev team|contributor|codebase|self[ -]host|"
        r"\bcli\b|api[- ]first|github|git repository|repository|readme|"
        r"ship your own|for developers|by developers"
    ),
    "enterprise security teams": (
        r"security|compliance|soc ?2|zero trust|encrypt|audit|identity|"
        r"access control|\brisk\b|governance|pen test|least privilege|threat"
    ),
    "remote-first startups": (
        r"remote[ -]?first|distributed|async|remote team|work from anywhere|"
        r"hybrid|global team|timezone|time zone|hire anywhere"
    ),
    "climate-conscious consumers": (
        r"climate|carbon|sustainab|eco[ -]?friendly|emission|planet|renewable|"
        r"footprint|greenhouse|net[ -]zero|lower your impact|environmentally"
    ),
    "design systems engineers": (
        r"design system|design token|\btokens?\b|typograph|accessib|\bwcag\b|"
        r"ui kit|figma|a11y|visual language|component librar|spacing scale|color palette"
    ),
    "indie hackers": (
        r"indie|hacker|solo founder|bootstrapped|side project|side hustle|"
        r"one[ -]person|ship fast|build in public|\bdoer\b"
    ),
    "product managers": (
        r"product manager|product management|\bpm\b|roadmap review|sprint planning|"
        r"backlog grooming|stakeholder|user research|prioriti[sz]|feature request|"
        r"voice of the customer|product team|ship the right thing"
    ),
    "data engineering teams": (
        r"data engineer|data platform|data team|pipeline|warehouse|\betl\b|streaming|"
        r"\bdbt\b|analytics engineer|airflow|\bspark\b|lakehouse|data ingestion|"
        r"transformation"
    ),
}


def _task_for(i: int, product: str, audience: str, index_offset: int) -> dict[str, object]:
    required = PRODUCT_ANCHORS[product]
    pattern = AUDIENCE_ANCHORS[audience]
    # A spec whose pattern does not compile would fail every artifact at grade
    # time with an opaque error, so refuse to emit it.
    re.compile(pattern)
    return {
        "id": f"html-batch-{i + index_offset:03d}",
        "type": "html",
        "prompt": (
            f"Create a landing page for a {product} targeted at {audience}. "
            "Write a strong headline, a 2-3 sentence value proposition, three feature bullets, "
            "and a call-to-action button. Output a single self-contained HTML file with inline CSS."
        ),
        "validation": ["html", "has_required", "matches_pattern"],
        "metadata": {"required": [required], "pattern": pattern},
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Generate HTML batch task YAMLs")
    p.add_argument("--count", type=int, default=100, help="Number of tasks to generate")
    p.add_argument("--out", default="tasks/batch-100", help="Output directory")
    p.add_argument("--offset", type=int, default=0, help="Starting index for task IDs")
    args = p.parse_args()

    missing = [p_ for p_ in PRODUCTS if p_ not in PRODUCT_ANCHORS]
    if missing:
        raise SystemExit(f"no product anchor for {missing}")
    missing = [a for a in AUDIENCES if a not in AUDIENCE_ANCHORS]
    if missing:
        raise SystemExit(f"no audience anchor for {missing}")

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
