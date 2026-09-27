"""Regression tests: the spec topic anchors must be able to tell specs apart.

`harness.py audit` counts a spec as anchored when its grader contains
`has_required` or `matches_pattern`. Counting is not the same as working, and
the failure mode is silent: a decorative anchor turns 100 specs back into one
interchangeable template while the audit reports 0 `structural_only`. These
tests are the counterweight.

The artifacts here are written by hand from each spec's *prompt* — a headline, a
value proposition, three bullets, a button, in the voice a model would use. They
are deliberately **not** assembled from the anchor tables in
`scripts/gen_html_tasks.py`: an artifact built out of the very tokens the grader
looks for would pass for reasons that prove nothing, and the sibling rejections
below would be circular. If a hand-written honest page fails its own spec, the
anchor is too strict and the test says so.

Run: `python -m unittest tests.test_spec_anchors`
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from orchestral.audit import audit_spec
from orchestral.config import load_task
from orchestral.fileset import required_content
from orchestral.runner import Runner

REPO = Path(__file__).resolve().parent.parent
BATCH = REPO / "tasks" / "batch-100"
ROUTER = REPO / "tasks" / "router-eval"

# --- hand-written honest artifacts, one per product ----------------------------
# Written from the prompt ("a landing page for a <product>") without consulting
# the anchor tables, so a miss here is real evidence the anchor is too strict.
PRODUCT_PAGE: dict[str, str] = {
    "coffee roaster": (
        "Small-batch coffee, roasted the week it ships.\n"
        "Every bag is roasted to order and dispatched within 48 hours, so what you brew is "
        "never more than a fortnight off roast. Taste notes are printed on the bag, not hidden "
        "on a website, and the same recipe is available as a filter or an espresso blend.\n"
        "<ul><li>Roasted to order, dispatched in 48 hours</li>"
        "<li>Taste notes printed on every bag</li>"
        "<li>Filter and espresso recipes included</li></ul>"
        "<a href='#shop' class='cta'>Shop the roast</a>"
    ),
    "electric bike": (
        "A commuter e-bike you can take up the stairs.\n"
        "The frame folds to fit a car boot or a flat, and the battery is removable so you can "
        "charge it at your desk rather than hunt for a socket on the street. Range is quoted "
        "for your weight and hills, not for a rider of no particular shape.\n"
        "<ul><li>20 kg folding frame</li>"
        "<li>Removable battery, 70 km quoted range</li>"
        "<li>Step-through frame, 8-speed hub</li></ul>"
        "<a href='#ride' class='cta'>Book a test ride</a>"
    ),
    "personal CRM": (
        "Your relationships, in one place you actually look.\n"
        "Log a coffee, a favour owed, a birthday, and the next step, then let the follow-up "
        "queue surface it. It is your own database — export every row as CSV whenever you want, "
        "because your notes are yours.\n"
        "<ul><li>One timeline per person, not per deal</li>"
        "<li>Follow-up queue that sorts by due date</li>"
        "<li>Full CSV export, no seat lock-in</li></ul>"
        "<a href='#start' class='cta'>Start free</a>"
    ),
    "no-code automation tool": (
        "Wire your tools together without writing a service.\n"
        "Drag a trigger, add a step, and the flow runs. Every step logs what it sent and what "
        "came back, so when a run goes sideways you read the log instead of guessing. Nothing "
        "needs a deploy.\n"
        "<ul><li>Triggers and steps configured in the browser</li>"
        "<li>Per-step run log with payloads</li>"
        "<li>Roll back a flow to its last good version</li></ul>"
        "<a href='#flows' class='cta'>Build a flow</a>"
    ),
    "dev-ops observability platform": (
        "Traces, logs and metrics in one place, priced per host.\n"
        "Point an agent at a host and you get latency percentiles, error rates and a trace view "
        "that links a slow request to the log line that caused it. Retention is set per team, so "
        "a noisy service does not quietly consume the whole budget.\n"
        "<ul><li>OpenTelemetry ingest, no sidecar rewrite</li>"
        "<li>Trace-to-log jump on any span</li>"
        "<li>Per-team retention and cost caps</li></ul>"
        "<a href='#demo' class='cta'>Read the docs</a>"
    ),
    "AI coding assistant": (
        "An AI pair that reads the file you are in.\n"
        "It takes the whole open tab, not just the cursor line, and proposes a change as a diff "
        "you accept or reject. It runs the test suite before it claims anything is fixed, and it "
        "tells you when it does not know.\n"
        "<ul><li>Whole-file context, not cursor-line context</li>"
        "<li>Proposals arrive as reviewable diffs</li>"
        "<li>Runs your tests before claiming success</li></ul>"
        "<a href='#try' class='cta'>Try it in your repo</a>"
    ),
    "remote team retreat planner": (
        "Offsite planning that does not live in a spreadsheet.\n"
        "Pick a city, a window and a budget, and it shortlists places, drafts a run of show, and "
        "collects RSVPs with dietary needs attached. Everyone sees the same itinerary and the "
        "same per-head cost.\n"
        "<ul><li>Venue shortlist against a real per-head budget</li>"
        "<li>Auto-generated run of show</li>"
        "<li>RSVPs with dietary requirements attached</li></ul>"
        "<a href='#plan' class='cta'>Plan a retreat</a>"
    ),
    "carbon offset marketplace": (
        "Buy verified removals, priced by the tonne.\n"
        "Every listing names the project, the methodology and the vintage, and the registry "
        "serial is public so you can check it yourself. Retirements are batched monthly and the "
        "receipt shows exactly which tonnes were cancelled.\n"
        "<ul><li>Project, methodology and vintage named per listing</li>"
        "<li>Public registry serial on every retirement</li>"
        "<li>Monthly retirement batch with a receipt</li></ul>"
        "<a href='#offset' class='cta'>Offset a tonne</a>"
    ),
    "open-source fonts library": (
        "A typeface library you can self-host.\n"
        "Every family ships as a variable woff2 alongside the static cuts, licensed OFL, and the "
        "repository holds the design space and the interpolation tests. No build step: drop the "
        "folder in and it works.\n"
        "<ul><li>Variable woff2 beside the static cuts</li>"
        "<li>OFL licensed, self-host with no build step</li>"
        "<li>Design space and interpolation tests in the repo</li></ul>"
        "<a href='#fonts' class='cta'>Browse the families</a>"
    ),
    "smart home dashboard": (
        "Every light, lock and thermostat on one board.\n"
        "The board shows room, device and state at a glance, groups devices by floor, and takes "
        "scenes so one tap does what four taps used to. Runs locally on a Pi, so your house does "
        "not stop working when the internet does.\n"
        "<ul><li>Local-first, keeps running offline</li>"
        "<li>Rooms and floors, with one-tap scenes</li>"
        "<li>Device state history you can actually read</li></ul>"
        "<a href='#board' class='cta'>See the dashboard</a>"
    ),
}

# --- hand-written honest artifacts, one per audience ---------------------------
AUDIENCE_PARAGRAPH: dict[str, str] = {
    "early adopters": (
        "You have been waiting for someone to do this properly. Join the early access list and "
        "you get the build before the public release, at the founding-customer price, with a "
        "line into what gets built next while the decisions are still open."
    ),
    "small business owners": (
        "You run the shop, you are the one doing the work, and you do not have a platform team. "
        "This is priced for a small business: set up in an afternoon, no per-seat maths, and the "
        "monthly bill stays a line you can predict. Your staff learn it without a manual."
    ),
    "open-source developers": (
        "If you self-host, this belongs in your stack. Apache licensed, no vendor lock-in, a "
        "documented REST API and a CLI that does the same thing for scripting. Read the repo, "
        "open an issue, or fork it — the contributor guide is in the readme."
    ),
    "enterprise security teams": (
        "Built for the review, not just the demo. SOC 2 Type II, encryption at rest and in "
        "transit, SSO with SCIM, and a full audit log of every access. Bring your own policy: "
        "retention, least privilege and data residency are configuration, not a sales call."
    ),
    "remote-first startups": (
        "Your team is distributed across four time zones and half of them have never met in a "
        "room. This is built for async: every action is written down, nothing needs to be "
        "discussed live, and a distributed team can pick up the thread on Monday wherever it "
        "stopped on Friday."
    ),
    "climate-conscious consumers": (
        "Because the footprint matters, not just the convenience. Lower your impact with less "
        "packaging and a plan that is genuinely renewable, and the emissions of every delivery "
        " are published rather than netted off against an offset. Environmentally, it is the "
        "boring choice on purpose."
    ),
    "design systems engineers": (
        "Built on a single source of design tokens, so a colour change ships from one place "
        "instead of forty. WCAG 2.2 contrast is checked in CI, the type scale is a documented "
        "primitive set, and every component carries a figma link next to its usage example."
    ),
    "indie hackers": (
        "Made by people who ship. No sales team, no seat minimums, no call to qualify — start on "
        "the free tier, and if it works for you, pay. What ships next is decided in the open "
        "and built in a weekend, by a small team of one who answers the issue tracker."
    ),
    "product managers": (
        "Because the roadmap is the argument. Every feature request lands where the user "
        "research says it should, sprints and backlog are visible, and the voice of the customer "
        "is attached to the item instead of a screenshot in a deck. Ship the right thing and "
        "know what you traded for it."
    ),
    "data engineering teams": (
        "Built for the platform team, not for a report tab. Streaming ingest, a warehouse-native "
        "layout, dbt models you can version, and lineage from a pipeline all the way to the "
        "table it writes. Ingestion is the product here, so it gets the reliability treatment "
        "the rest of the stack takes for granted."
    ),
}

_PAGE = (
    "<!doctype html>\n<html lang='en'><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>{title}</title>\n<style>body{{font-family:system-ui,sans-serif}}\n"
    ".hero{{padding:2rem}}\n</style></head>\n<body>\n"
    "<section class='hero'><h1>{headline}</h1><p>{value}</p></section>\n"
    "{body}\n<footer><small>&copy; 2026</small></footer>\n</body></html>"
)


def _product_page(product: str) -> str:
    return _PAGE.format(
        title=product,
        headline=product,
        value="A page about " + product + ".",
        body=PRODUCT_PAGE[product],
    )


def _honest_artifact(spec) -> str:
    """A landing page a competent model would write for `spec`'s own prompt."""
    body = spec.prompt
    product = body.removeprefix("Create a landing page for a ").partition(" targeted at ")[0]
    audience = body.partition(" targeted at ")[2].split(".")[0]
    return _PAGE.format(
        title=f"{product} for {audience}",
        headline=f"{product} for {audience}",
        value=AUDIENCE_PARAGRAPH[audience],
        body=PRODUCT_PAGE[product],
    )


# The template every one of these specs asks for, with the subject left out. It
# satisfies html_parses, non_empty, has_title, has_cta and has_form — the whole
# of the pre-anchor grader. If it still scores anywhere, the anchors are not
# doing the work they were added for.
GENERIC_TEMPLATE = (
    "<!doctype html>\n<html lang='en'><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>Landing page</title></head>\n<body>\n"
    "<section class='hero'><h1>Build better, faster</h1>"
    "<p>Everything your team needs in one place. Sign up and get started today.</p></section>\n"
    "<ul><li>Fast</li><li>Simple</li><li>Secure</li></ul>\n"
    "<a href='#signup' class='cta'>Get started</a>\n"
    "<form id='signup'><input type='email' name='email'>"
    "<button type='submit'>Sign up</button></form>\n</body></html>"
)


def _family_specs(directory: Path) -> list:
    return [load_task(p) for p in sorted(directory.glob("*.yaml"))]


def _grader_for(spec, runner: Runner):
    """The validator the runner would use for `spec`'s task type.

    `html-batch` grades through `_validate` and `router-eval` through
    `_validate_extract`, so a test that called one for both would be measuring
    the wrong grader for half the family.
    """
    return runner._validate_extract if spec.type == "extract" else runner._validate


def _router_artifact(choice: str) -> str:
    """The JSON an assistant decision router is asked to return."""
    return f'{{"choice": "{choice}"}}'


def _answer(spec) -> object:
    """What a correct artifact for `spec` must contain.

    Two specs with the same answer cannot be told apart by any grader, so
    discrimination has to be measured against specs with a *different* answer.
    """
    if spec.type == "extract":
        return spec.metadata.get("expected")
    return (tuple(spec.metadata.get("required") or ()), spec.metadata.get("pattern"))


class TestAnchorsTellSpecsApart(unittest.TestCase):
    """A near-duplicate family must stop being N copies of one gradeable page.

    Before the anchors, all 100 `html-batch` specs ran the same two checks
    (`html_parses`, `non_empty`) and the family was 100 ways to score the same
    template. Each property below is one way that can regress.
    """

    def setUp(self) -> None:
        self.runner = Runner(runs_dir="/tmp/orchestral-anchor-tests", dry_run=True)
        self.batch = _family_specs(BATCH)
        self.router = _family_specs(ROUTER)

    def _accepted_by(self, spec, artifact: str) -> bool:
        return _grader_for(spec, self.runner)(spec, artifact)[0]

    def _honest_for(self, spec) -> str:
        if spec.type == "extract":
            return _router_artifact(spec.metadata["expected"]["choice"])
        return _honest_artifact(spec)

    def test_generic_template_scores_on_no_batch_spec(self) -> None:
        """The template that used to score 100 times now scores zero times.

        `GENERIC_TEMPLATE` satisfies the whole pre-anchor grader: it parses, it
        is non-empty, and it carries a title, a call to action and a form.
        """
        for spec in self.batch:
            with self.subTest(spec=spec.id):
                self.assertFalse(
                    self._accepted_by(spec, GENERIC_TEMPLATE),
                    f"{spec.id} still accepts an artifact with no subject in it",
                )

    def test_a_generic_guess_scores_on_no_router_eval_spec(self) -> None:
        """`router-eval` was never shape-only, but check the guessing path anyway.

        These are `extract` specs with a typed `enum` and an `expected` value,
        so they are self-anchored and out of scope for the `html` anchors. The
        property worth pinning is that a guess unrelated to the state is
        rejected — a spec whose enum collapsed to "any of these is fine" would
        otherwise be a free four-way guess repeated ten times.
        """
        guesses = ['{"choice": "browser"}', '{"choice": "terminal"}', '{"choice": "files"}',
                   '{"choice": "vscode"}', "{}", "not json at all"]
        for spec in self.router:
            right = spec.metadata["expected"]["choice"]
            for guess in guesses:
                with self.subTest(spec=spec.id, guess=guess):
                    if guess == _router_artifact(right):
                        continue
                    self.assertFalse(
                        self._accepted_by(spec, guess),
                        f"{spec.id} accepts {guess!r}, which is the wrong answer",
                    )

    def test_honest_artifact_passes_its_own_spec(self) -> None:
        """Anchors must not fail the work they are meant to grade.

        This is the false-failure guard. Every artifact is hand-written from the
        spec's prompt without consulting the anchor tables, so a failure here
        means the anchor is stricter than honest work — a capability drop, not
        a security win.
        """
        for spec in self.batch:
            with self.subTest(spec=spec.id):
                self.assertTrue(
                    self._accepted_by(spec, self._honest_for(spec)),
                    f"{spec.id} rejects a hand-written honest page",
                )

    def test_each_family_discriminates_a_spec_from_those_with_a_different_answer(self) -> None:
        """The regression the anchors exist for: the family is not one grade.

        Discrimination is only meaningful against a sibling that expects a
        *different* answer. For `html-batch` the answer is the (product stem,
        audience vocabulary) pair, so a sibling with a different pair is a
        different problem. For `router-eval` the answer is
        `metadata.expected.choice`, and ten specs carry only four distinct
        values — so an artifact that answers `re-001` correctly also answers
        `re-005` and `re-008` correctly, and that is arithmetic, not a
        decorative anchor. The test therefore asks, per family, that at least
        one spec is accepted by itself and by no spec expecting a different
        answer.
        """
        for name, specs in (("html-batch", self.batch), ("router-eval", self.router)):
            clean = [
                spec.id
                for spec in specs
                if self._accepted_by(spec, self._honest_for(spec))
                and not [
                    other
                    for other in specs
                    if _answer(other) != _answer(spec) and self._accepted_by(other, self._honest_for(spec))
                ]
            ]
            with self.subTest(family=name):
                self.assertTrue(
                    clean,
                    f"no spec in {name} is distinguished from the specs that expect a "
                    "different answer, so the grade is shared across the family",
                )

    def test_sibling_acceptance_is_exactly_the_three_unavoidable_overlaps(self) -> None:
        """Pin the measured leak set so a widened anchor fails here.

        97 of the 100 `html-batch` honest pages are accepted by exactly one spec.
        The remaining 27 cross-family passes all land on three siblings whose
        *product name is itself a sibling audience's vocabulary*:

        - `html-batch-064` (retreat x remote-first) — the product is a
          "remote team retreat planner", so every honest page for it says
          "remote team"
        - `html-batch-075` (carbon x climate) — the product is a "carbon offset
          marketplace", so every honest page for it says "carbon"
        - `html-batch-082` (fonts x open-source developers) — the product is an
          "open-source fonts library", so every honest page for it says
          "open source"

        Removing those would mean failing honest work, so they stay. They are
        the `near_duplicate_family` finding, and collapsing the batch is a
        separate decision. Any *other* sibling accepting a page is a regression.
        """
        expected = {"html-batch-064", "html-batch-075", "html-batch-082"}
        accepting: dict[str, list[str]] = {}
        for spec in self.batch:
            artifact = self._honest_for(spec)
            for other in self.batch:
                if other.id != spec.id and self._accepted_by(other, artifact):
                    accepting.setdefault(other.id, []).append(spec.id)
        self.assertEqual(
            sorted(accepting),
            sorted(expected),
            f"sibling acceptance moved off the three unavoidable overlaps: {accepting}",
        )
        for sibling, sources in accepting.items():
            with self.subTest(sibling=sibling):
                self.assertEqual(
                    len(sources), 9, f"{sibling} accepts {len(sources)} siblings, expected its 9 product peers"
                )

    def test_every_batch_spec_declares_a_topic_anchor(self) -> None:
        """Guards the count itself: an anchor that is not in `validation:` or
        `metadata` is not gating anything, and the audit would stop counting
        the spec as anchored for a reason unrelated to the grader."""
        for spec in self.batch:
            with self.subTest(spec=spec.id):
                self.assertNotIn("structural_only", {f.rule for f in audit_spec(spec)})
                self.assertTrue(spec.metadata.get("required"), f"{spec.id} has no required token")
                self.assertTrue(spec.metadata.get("pattern"), f"{spec.id} has no audience pattern")

    def test_committed_specs_match_the_generator_that_writes_them(self) -> None:
        """The anchors live in `scripts/gen_html_tasks.py`; the YAML is its output.

        Without this, editing a spec by hand silently desynchronises it from the
        generator and the next `gen_html_tasks.py` run reverts the anchor — the
        spec still looks anchored in the audit while the grader stops gating on
        it. This compares every committed spec against the generator's own dict
        for the same (product, audience) cell, so a hand edit fails here rather
        than in a later run.
        """
        import importlib.util
        import itertools

        import yaml

        spec = importlib.util.spec_from_file_location(
            "gen_html_tasks", REPO / "scripts" / "gen_html_tasks.py"
        )
        assert spec and spec.loader
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)

        cells = list(itertools.product(generator.PRODUCTS, generator.AUDIENCES))[:100]
        self.assertEqual(len(cells), len(self.batch))
        for index, (product, audience) in enumerate(cells):
            task_id = f"html-batch-{index:03d}"
            expected = generator._task_for(index, product, audience, 0)
            with self.subTest(spec=task_id):
                self.assertEqual(
                    yaml.safe_load((BATCH / f"{task_id}.yaml").read_text()), expected
                )

    def test_every_generator_anchor_is_a_valid_regex(self) -> None:
        """A pattern that does not compile fails every artifact with an opaque
        error at grade time, and the audit has no rule that would catch it."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "gen_html_tasks_anchors", REPO / "scripts" / "gen_html_tasks.py"
        )
        assert spec and spec.loader
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        for audience, pattern in generator.AUDIENCE_ANCHORS.items():
            with self.subTest(audience=audience):
                re.compile(pattern)
                self.assertTrue(pattern.strip(), f"{audience} has an empty pattern")
        # The product anchor has to be *this* product's word, not a shared one:
        # `fonts` rather than `font`, because every stylesheet has a
        # `font-family`, and a stem that another product also uses cannot
        # discriminate the two.
        for product, stem in generator.PRODUCT_ANCHORS.items():
            with self.subTest(product=product):
                others = [p for p in generator.PRODUCTS if p != product]
                self.assertFalse(
                    any(stem in other.lower() for other in others),
                    f"{product} anchor {stem!r} also appears in a sibling product name",
                )
                self.assertIn(stem, product.lower(), f"{product} anchor {stem!r} is not in its own name")


class TestMultiFileReadsFileBodies(unittest.TestCase):
    """`has_paths` accepted a one-byte file per declared name.

    `has_content` is the companion that reads the body, so `multi-file-site`
    grades the site the prompt describes rather than the filenames it mentions.
    """

    def setUp(self) -> None:
        self.runner = Runner(runs_dir="/tmp/orchestral-anchor-tests", dry_run=True)
        self.spec = load_task(REPO / "tasks" / "multi-file-site.yaml")

    def _zip(self, files: dict[str, str]) -> bytes:
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for path, body in files.items():
                archive.writestr(path, body)
        return buffer.getvalue()

    def test_spec_reads_bodies_not_just_names(self) -> None:
        self.assertIn("has_content", self.spec.validation)
        self.assertEqual(
            required_content(self.spec.metadata),
            {
                "index.html": ["hero", "pricing", "email"],
                "style.css": ["pricing", "form"],
            },
        )

    def test_one_byte_file_per_name_fails(self) -> None:
        """The exact artifact that passed before: right names, one byte each."""
        passes, report = self.runner._validate_multi(
            self.spec, self._zip({"index.html": "x", "style.css": "x"})
        )
        self.assertFalse(passes)
        self.assertFalse(report["checks"]["has_content"])

    def test_empty_bodies_fail_even_though_they_exist(self) -> None:
        passes, report = self.runner._validate_multi(
            self.spec, self._zip({"index.html": "", "style.css": ""})
        )
        self.assertFalse(passes)
        self.assertFalse(report["checks"]["has_paths"])
        self.assertFalse(report["checks"]["has_content"])

    def test_faithful_site_passes(self) -> None:
        artifact = self._zip(
            {
                "index.html": (
                    "<section class='hero'><h1>Roasted weekly</h1></section>"
                    "<section id='pricing'><h2>Plans</h2></section>"
                    "<form><input type='email'><button>Join</button></form>"
                ),
                "style.css": ".hero{padding:2rem}\n.pricing{display:grid}\nform{margin:0}",
            }
        )
        passes, report = self.runner._validate_multi(self.spec, artifact)
        self.assertTrue(passes, report["errors"])
        self.assertTrue(report["checks"]["has_content"])

    def test_html_without_the_sections_fails(self) -> None:
        """A real page that is a real page, but not the page the prompt asked for."""
        artifact = self._zip(
            {
                "index.html": "<h1>About us</h1><p>We have been roasting since 1994.</p>",
                "style.css": "body{margin:0}\n.pricing{}\nform{}",
            }
        )
        passes, report = self.runner._validate_multi(self.spec, artifact)
        self.assertFalse(passes)
        self.assertIn("index.html:hero", " ".join(report["errors"]))

    def test_requested_without_metadata_fails_closed(self) -> None:
        """A check the spec asked for but did not configure must not pass open."""
        from orchestral.config import TaskSpec

        bare = TaskSpec(
            id="mf-bare",
            type="multi-file",
            prompt="Build a two-page microsite.",
            validation=["has_content"],
            metadata={"expected_paths": ["index.html"]},
        )
        passes, report = self.runner._validate_multi(bare, self._zip({"index.html": "x"}))
        self.assertFalse(passes)
        self.assertIn("metadata.required_content is empty", " ".join(report["errors"]))

    def test_declared_path_absent_from_the_archive_fails(self) -> None:
        artifact = self._zip({"index.html": "<h1>hero</h1><h2>pricing</h2><input type='email'>"})
        passes, report = self.runner._validate_multi(self.spec, artifact)
        self.assertFalse(passes)
        self.assertIn("No file body to read for: style.css", " ".join(report["errors"]))


if __name__ == "__main__":
    unittest.main()
