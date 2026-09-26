"""Run-time holdout arm — novel task instances that are never committed.

A holdout arm only measures contamination if its task text never reaches a place
a model could have read: not git, not ``tasks/``, not ``runs-pub/``. So the arm is
built here, from parameters, into a run-scoped directory at the moment it is
asked for. Nothing in this module writes inside the repository.

The families below are *novel instances of the shipped families*, not new
families. A generated ``sql`` task asks the same shape of question as
``tasks/sql-monthly-revenue.yaml`` over a schema, dataset, and vocabulary the
repository has never contained. Holding the family fixed keeps a
published-vs-holdout score gap readable as contamination instead of as a change
of subject; moving the data is what removes the memorised answer.

There is deliberately more than one family per task type. A generated arm built
from a single shape would be one problem counted many times — the same defect
``harness.py audit`` reports as ``near_duplicate_family`` for the shipped
``html-batch-*`` specs. Each family below exercises a different skill, so an arm
spanning them is a set of problems rather than one problem's worth of samples.

Every value is drawn from a ``random.Random`` stream keyed on ``(seed, index)``,
so a spec depends only on its own coordinates — not on how many specs were
requested or in which order they were produced. That is what makes ``--seed``
load-bearing: two seeds produce two different prompts for the same task id.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from orchestral.config import TaskSpec

# Marker recorded in every generated spec's metadata. `holdout` is the field the
# runner, the leaderboard, the scrubber, and the audit all key on; the rest is
# provenance so a run dir explains where its task text came from.
HOLDOUT_MARKER = "holdout"
GENERATED_BY = "orchestral.holdout"

# Task types this module can generate. Both are graded on content the model has
# to produce rather than on markup that merely has to exist, which is what makes
# a score difference between arms mean something.
HOLDOUT_TYPES = ("sql", "needle")

DEFAULT_ARM_SIZE = 8
DEFAULT_SEED = 20260926

# ---------------------------------------------------------------------------
# sql — three question shapes over a generated three-table fixture
# ---------------------------------------------------------------------------

# Each lexicon renames every table and column, so the model cannot pattern-match
# a schema it has seen. The question shape is held fixed per family; the schema,
# the rows, and the vocabulary are not.
_SQL_LEXICONS: tuple[dict[str, str], ...] = (
    {
        "catalog": "products", "catalog_name": "name",
        "orders": "orders", "orders_date": "placed_on", "orders_status": "status",
        "lines": "order_lines", "lines_order": "order_id", "lines_item": "product_id",
        "lines_qty": "qty", "lines_price": "unit_price",
    },
    {
        "catalog": "catalog_items", "catalog_name": "item_name",
        "orders": "purchases", "orders_date": "purchase_date", "orders_status": "state",
        "lines": "purchase_lines", "lines_order": "purchase_id", "lines_item": "item_id",
        "lines_qty": "quantity", "lines_price": "unit_cost",
    },
    {
        "catalog": "articles", "catalog_name": "article_name",
        "orders": "shipments", "orders_date": "shipped_day", "orders_status": "stage",
        "lines": "shipment_lines", "lines_order": "shipment_id", "lines_item": "article_id",
        "lines_qty": "units", "lines_price": "unit_amount",
    },
    {
        "catalog": "plans", "catalog_name": "plan_name",
        "orders": "invoices", "orders_date": "issued_day", "orders_status": "invoice_state",
        "lines": "invoice_lines", "lines_order": "invoice_id", "lines_item": "plan_id",
        "lines_qty": "seats", "lines_price": "seat_price",
    },
)

_ITEM_NOUNS = (
    "Kettle", "Grinder", "Dripper", "Press", "Filter", "Scale", "Burr", "Carafe",
    "Thermos", "Moka", "Aeropress", "Chemex", "Sifter", "Tamper", "Portafilter",
    "Knockbox", "Canister", "Funnel", "Tongs", "Steamer",
)
_ITEM_ADJECTIVES = ("Compact", "Rustic", "Featherweight", "Heavy-Duty", "Travel", "Barista")

# The qualifying status is the answer to "count these"; the others exist so a
# query that forgets the filter returns a different, wrong result.
_QUALIFYING_STATUSES = ("shipped", "settled", "delivered", "posted", "paid", "fulfilled")
_EXCLUDED_STATUSES = ("cancelled", "pending", "refunded", "on_hold", "draft", "reversed")

_SQL_TOP_PER_MONTH = "top_per_month"
_SQL_CUMULATIVE = "cumulative_by_item"
_SQL_MONTH_OVER_MONTH = "month_over_month"
_SQL_SHAPES = (_SQL_TOP_PER_MONTH, _SQL_CUMULATIVE, _SQL_MONTH_OVER_MONTH)


@dataclass
class _Fixture:
    """A generated three-table dataset plus the vocabulary that describes it."""

    lex: dict[str, str]
    months: list[str]
    item_names: list[str]
    qualifying: str
    excluded: list[str]
    orders: list[tuple[int, str, str]]
    lines: list[tuple[int, int, int, float]]


def _sql_fixture(rng: random.Random, shape: str) -> _Fixture:
    """Build a fixture guaranteed to give `shape`'s reference query a non-empty result.

    Every month carries at least one qualifying order with at least one line, and
    at least two months exist, so a per-month or per-month-delta question always
    has something to rank.
    """
    lex = dict(rng.choice(_SQL_LEXICONS))
    n_months = rng.randint(2, 4)
    n_items = rng.randint(3, 5)
    year = rng.randint(2023, 2026)
    first_month = rng.randint(1, 10)
    months = [f"{year}-{m:02d}" for m in range(first_month, first_month + n_months)]
    item_names = [
        f"{adj} {noun}"
        for adj, noun in zip(
            rng.sample(_ITEM_ADJECTIVES, n_items), rng.sample(_ITEM_NOUNS, n_items), strict=True
        )
    ]
    qualifying = rng.choice(_QUALIFYING_STATUSES)
    excluded = rng.sample([s for s in _EXCLUDED_STATUSES if s != qualifying], rng.randint(1, 3))

    orders: list[tuple[int, str, str]] = []
    lines: list[tuple[int, int, int, float]] = []
    order_id = 0
    line_rows: list[tuple[int, int, int, float]] = []

    def _emit(month: str, status: str, n_lines: int) -> None:
        nonlocal order_id
        order_id += 1
        day = rng.randint(1, 28)
        orders.append((order_id, f"{month}-{day:02d}", status))
        for _ in range(n_lines):
            line_rows.append((
                order_id,
                rng.randint(1, n_items),
                rng.randint(1, 9),
                round(rng.uniform(3.0, 95.0), 2),
            ))

    for month in months:
        # the qualifying order that guarantees this month is rankable
        _emit(month, qualifying, rng.randint(1, 3))
        # decoy orders in the same month that a forgotten filter would count
        for _ in range(rng.randint(1, 2)):
            _emit(month, rng.choice(excluded), rng.randint(1, 3))
    # extra non-qualifying orders, so a query that forgets the status filter
    # over-reports and cannot pass by accident
    for _ in range(rng.randint(1, 2)):
        _emit(rng.choice(months), rng.choice(excluded), rng.randint(1, 3))

    # guarantee every item sells at least once, so no shape returns an empty arm
    for item_id in range(1, n_items + 1):
        lines.append((orders[item_id - 1][0], item_id, rng.randint(1, 5), round(rng.uniform(5.0, 60.0), 2)))
    lines.extend(line_rows)
    rng.shuffle(lines)

    return _Fixture(
        lex=lex,
        months=months,
        item_names=item_names,
        qualifying=qualifying,
        excluded=excluded,
        orders=orders,
        lines=lines,
    )


def _sql_schema(fx: _Fixture) -> list[str]:
    lex = fx.lex
    return [
        f"CREATE TABLE {lex['catalog']} (id INTEGER PRIMARY KEY, {lex['catalog_name']} TEXT NOT NULL)",
        f"CREATE TABLE {lex['orders']} (id INTEGER PRIMARY KEY, "
        f"{lex['orders_date']} DATE NOT NULL, {lex['orders_status']} TEXT NOT NULL)",
        f"CREATE TABLE {lex['lines']} ({lex['lines_order']} INTEGER NOT NULL REFERENCES "
        f"{lex['orders']}(id), {lex['lines_item']} INTEGER NOT NULL REFERENCES "
        f"{lex['catalog']}(id), {lex['lines_qty']} INTEGER NOT NULL, {lex['lines_price']} REAL NOT NULL)",
    ]


def _sql_seed(fx: _Fixture) -> list[str]:
    lex = fx.lex
    catalog_values = ", ".join(
        f"({i}, '{name}')" for i, name in enumerate(fx.item_names, start=1)
    )
    order_values = ", ".join(
        f"({oid}, '{day}', '{status}')" for oid, day, status in fx.orders
    )
    line_values = ", ".join(
        f"({oid}, {item}, {qty}, {price})" for oid, item, qty, price in fx.lines
    )
    return [
        f"INSERT INTO {lex['catalog']} VALUES {catalog_values}",
        f"INSERT INTO {lex['orders']} (id, {lex['orders_date']}, {lex['orders_status']}) "
        f"VALUES {order_values}",
        f"INSERT INTO {lex['lines']} VALUES {line_values}",
    ]


def _sql_prompt(fx: _Fixture, shape: str) -> str:
    lex = fx.lex
    tables = (
        f"The database has tables: {lex['catalog']}(id, {lex['catalog_name']}), "
        f"{lex['orders']}(id, {lex['orders_date']} DATE, {lex['orders_status']}), "
        f"{lex['lines']}({lex['lines_order']}, {lex['lines_item']}, {lex['lines_qty']}, {lex['lines_price']})."
    )
    counted = f"Only count {lex['lines']} whose {lex['orders']} {lex['orders_status']} is '{fx.qualifying}'."
    revenue = f"{lex['lines_qty']} * {lex['lines_price']}"
    name = lex["catalog_name"]
    # Naming the catalogue and the window the fixture covers is what makes two
    # seeds two different problems rather than one problem over two datasets.
    # It states what exists, never which rows win, so it gives nothing away.
    scope = (
        f"The {lex['catalog']} table holds {len(fx.item_names)} rows: "
        f"{', '.join(fx.item_names)}. The {lex['orders']} span {fx.months[0]} to {fx.months[-1]}."
    )

    if shape == _SQL_TOP_PER_MONTH:
        question = (
            f"which {name} generated the most revenue in each month? Return one row per month "
            f"with columns: month (YYYY-MM), {name}, revenue (total, rounded to 2 decimals), "
            f"ordered by month ascending."
        )
    elif shape == _SQL_CUMULATIVE:
        question = (
            f"for each {name}, what is its total revenue and the first day it sold? Return one "
            f"row per {name} with columns: {name}, revenue (total, rounded to 2 decimals), "
            f"first_sale ({lex['orders_date']}), ordered by revenue descending, then {name} ascending."
        )
    else:
        question = (
            "what was the total revenue in each month and how did it change from the previous "
            "month? Return one row per month with columns: month (YYYY-MM), revenue (total, "
            "rounded to 2 decimals), change_pct (percentage change from the previous month, "
            "rounded to 2 decimals, NULL for the first month), ordered by month ascending."
        )
    return (
        f"Write a single read-only SQL query (SQLite dialect) that answers: {question} "
        f"Revenue is {revenue}. {counted} {scope} {tables}"
    )


def _sql_reference(fx: _Fixture, shape: str) -> str:
    """The correct answer for `shape`, written against `fx`'s generated columns.

    Kept adjacent to the prompt so a change to either fails loudly in the
    dry-run self-check rather than producing a task whose key is wrong.
    """
    lex = fx.lex
    cat, orders, lines = lex["catalog"], lex["orders"], lex["lines"]
    name, date, status = lex["catalog_name"], lex["orders_date"], lex["orders_status"]
    l_order, l_item, l_qty, l_price = lex["lines_order"], lex["lines_item"], lex["lines_qty"], lex["lines_price"]
    revenue = f"{l_qty} * {l_price}"
    qualified = (
        f"FROM {lines} l JOIN {orders} o ON o.id = l.{l_order} "
        f"JOIN {cat} c ON c.id = l.{l_item} WHERE o.{status} = '{fx.qualifying}'"
    )

    if shape == _SQL_TOP_PER_MONTH:
        month = f"strftime('%Y-%m', o.{date})"
        # `best` must aggregate the *rounded* total. Comparing a rounded revenue
        # against an unrounded MAX drops every row whose sum is not exactly
        # representable, which would leave the answer empty.
        rounded = f"ROUND(SUM({revenue}), 2)"
        return (
            "SELECT month, name, revenue FROM (\n"
            f"  SELECT {month} AS month, c.{name} AS name,\n"
            f"         {rounded} AS revenue,\n"
            f"         MAX({rounded}) OVER (PARTITION BY {month}) AS best\n"
            f"  {qualified}\n"
            f"  GROUP BY month, c.{name}\n"
            ") WHERE revenue = best ORDER BY month ASC"
        )
    if shape == _SQL_CUMULATIVE:
        return (
            f"SELECT c.{name} AS name, ROUND(SUM({revenue}), 2) AS revenue,\n"
            f"       MIN(o.{date}) AS first_sale\n"
            f"{qualified}\n"
            f"GROUP BY c.{name} ORDER BY revenue DESC, name ASC"
        )
    month = f"strftime('%Y-%m', o.{date})"
    return (
        f"SELECT month, revenue, change_pct FROM (\n"
        f"  SELECT month, revenue,\n"
        f"         ROUND(100.0 * (revenue - prev) / NULLIF(prev, 0), 2) AS change_pct\n"
        f"  FROM (\n"
        f"    SELECT {month} AS month, ROUND(SUM({revenue}), 2) AS revenue,\n"
        f"           LAG(ROUND(SUM({revenue}), 2)) OVER (ORDER BY {month}) AS prev\n"
        f"    {qualified}\n"
        f"    GROUP BY month\n"
        f"  )\n"
        f") ORDER BY month ASC"
    )


# ---------------------------------------------------------------------------
# needle — a single true record hidden among drafted ones
# ---------------------------------------------------------------------------

# Two key shapes so an arm is not one retrieval problem repeated. The record
# grammar differs, and so does the discrimination the model has to perform.
_NEEDLE_DEPLOY = "deploy_token"
_NEEDLE_PAYMENT = "settlement_reference"
_NEEDLE_FAMILIES = (_NEEDLE_DEPLOY, _NEEDLE_PAYMENT)

_TOKEN_PREFIXES = ("FALCON", "DELTA", "OMEGA", "ATLAS", "ORION", "VEGA", "LYRA", "PERSEUS")
_TOKEN_PREFIXES_2 = ("SETTLE", "RECON", "CLEAR", "PAYOUT", "ESCROW", "VOUCHER")
_REGIONS = ("eu-west", "us-east", "ap-south", "sa-east", "eu-north", "us-west")
_SERVICES = ("auth", "billing", "gateway", "search", "worker-7", "scheduler")
_LOG_LEVELS = ("INFO", "WARN", "DEBUG")
_NEEDLE_UNITS = ("kg", "crate", "pallet", "carton", "case")
_NEEDLE_STAGES = ("dispatched", "in transit", "at the depot", "out for delivery")
_NEEDLE_LINES = 120
_EPOCH = date(2025, 1, 1)


def _timestamp_on(rng: random.Random, day: str) -> str:
    return (
        f"{day}T{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}Z"
    )


def _log_days(rng: random.Random) -> tuple[list[str], str]:
    """Days for one haystack plus the window they cover.

    The days are drawn from inside a bounded span rather than across the whole
    year, so `min`/`max` describe that span instead of collapsing onto the
    calendar's extremes. Without the bound every generated log covers essentially
    the same window, two seeds collide, and the arm stops being distinct problems.
    """
    start = _EPOCH + timedelta(days=rng.randint(0, 300))
    span = rng.randint(15, 150)
    days = [(start + timedelta(days=rng.randint(0, span))).isoformat() for _ in range(_NEEDLE_LINES)]
    return days, f"{min(days)} to {max(days)}"


def _distractors(rng: random.Random, prefixes: tuple[str, ...], span: tuple[int, int], true_value: str) -> list[str]:
    """2-3 distinct decoy keys, none of them equal to `true_value`.

    A decoy that collided with the answer would land in `forbidden` and make the
    task unanswerable, so distinctness and separation are enforced rather than
    assumed unlikely.
    """
    low, high = span
    want = rng.randint(2, 3)
    taken = {true_value}
    decoys: list[str] = []
    while len(decoys) < want:
        token = f"{rng.choice(prefixes)}-{rng.randint(low, high)}"
        if token in taken:
            continue
        taken.add(token)
        decoys.append(token)
    return decoys


def _needle_deploy(rng: random.Random) -> tuple[str, str, list[str], str]:
    """A deploy log with one completed release among drafted rollbacks.

    Returns (document, answer, decoys, prompt).
    """
    true_token = f"{rng.choice(_TOKEN_PREFIXES)}-{rng.randint(1000, 9999)}"
    decoys = _distractors(rng, _TOKEN_PREFIXES, (1000, 9999), true_token)
    region = rng.choice(_REGIONS)

    records: list[str] = []
    days, window = _log_days(rng)
    for day in days:
        records.append(
            f"{_timestamp_on(rng, day)} {rng.choice(_SERVICES)} {rng.choice(_LOG_LEVELS)} "
            f"request handled id={rng.randint(10000, 99999)} ms={rng.randint(60, 900)}"
        )
    for token in decoys:
        records.append(
            f"{_timestamp_on(rng, rng.choice(days))} "
            f"search WARN request handled rollback scheduled: "
            f"release_token={token} region={rng.choice([r for r in _REGIONS if r != region])} "
            "(draft, not applied)"
        )
    records.append(
        f"{_timestamp_on(rng, rng.choice(days))} "
        f"search INFO request handled deploy complete: "
        f"release_token={true_token} region={region}"
    )
    rng.shuffle(records)

    prompt = (
        f"The subtask payload contains `document`: a {len(records)}-line deploy log covering "
        f"{window}, with {len(decoys)} drafted rollback(s) alongside the applied "
        f"deploys. Find the release_token of the deploy that actually completed (status "
        f"'deploy complete', region {region}) — NOT the drafted rollbacks. Answer with only the "
        f"token, nothing else."
    )
    return "\n".join(records), true_token, decoys, prompt


def _needle_payment(rng: random.Random) -> tuple[str, str, list[str], str]:
    """A warehouse log with one settled consignment among cancelled ones.

    Returns (document, answer, decoys, prompt).
    """
    true_ref = f"{rng.choice(_TOKEN_PREFIXES_2)}-{rng.randint(10000, 99999)}"
    decoys = _distractors(rng, _TOKEN_PREFIXES_2, (10000, 99999), true_ref)
    unit = rng.choice(_NEEDLE_UNITS)

    records: list[str] = []
    days, window = _log_days(rng)
    for day in days:
        records.append(
            f"{_timestamp_on(rng, day)} depot-{rng.randint(1, 9)} {rng.choice(_LOG_LEVELS)} "
            f"movement recorded id={rng.randint(10000, 99999)} weight={rng.randint(1, 400)}"
        )
    for ref in decoys:
        records.append(
            f"{_timestamp_on(rng, rng.choice(days))} "
            f"depot-4 WARN movement recorded consignment cancelled: "
            f"settlement_ref={ref} (voided before dispatch)"
        )
    records.append(
        f"{_timestamp_on(rng, rng.choice(days))} "
        f"depot-2 INFO movement recorded consignment settled: "
        f"settlement_ref={true_ref} weight={rng.randint(400, 900)} {unit}"
    )
    rng.shuffle(records)

    prompt = (
        f"The subtask payload contains `document`: a {len(records)}-line warehouse movement log "
        f"covering {window}, with {len(decoys)} voided consignment(s) mixed in. Find the "
        f"settlement_ref of the consignment that actually settled (status 'consignment settled') "
        f"— NOT the cancelled ones. Answer with only the reference, nothing else."
    )
    return "\n".join(records), true_ref, decoys, prompt


_NEEDLE_BUILDERS: dict[str, Callable[[random.Random], tuple[str, str, list[str], str]]] = {
    _NEEDLE_DEPLOY: _needle_deploy,
    _NEEDLE_PAYMENT: _needle_payment,
}


# ---------------------------------------------------------------------------
# Arm construction
# ---------------------------------------------------------------------------


def spec_id(index: int) -> str:
    """The id every seed shares for a given slot, so a run can be compared across seeds.

    The id is deliberately seed-independent: two seeds must produce the same task
    id with different task text, otherwise they are different tasks and a
    published-vs-holdout comparison has nothing to line up.
    """
    return f"holdout-{index:03d}"


def _stream(seed: int, index: int) -> random.Random:
    return random.Random(f"{GENERATED_BY}:{seed}:{index}")


def _sql_spec(rng: random.Random, index: int, seed: int) -> TaskSpec:
    shape = _SQL_SHAPES[index % len(_SQL_SHAPES)]
    fx = _sql_fixture(rng, shape)
    return TaskSpec(
        id=spec_id(index),
        type="sql",
        prompt=_sql_prompt(fx, shape),
        validation=[],
        assets=[],
        metadata={
            HOLDOUT_MARKER: True,
            "generated_by": GENERATED_BY,
            "generated_shape": shape,
            "generated_seed": seed,
            "difficulty": "medium",
            "version": f"{shape}-s{seed}",
            "ordered": True,
            "schema": _sql_schema(fx),
            "seed": _sql_seed(fx),
            "reference_sql": _sql_reference(fx, shape),
        },
    )


def _needle_spec(rng: random.Random, index: int, seed: int) -> TaskSpec:
    family = _NEEDLE_FAMILIES[index % len(_NEEDLE_FAMILIES)]
    document, answer, decoys, prompt = _NEEDLE_BUILDERS[family](rng)
    return TaskSpec(
        id=spec_id(index),
        type="needle",
        prompt=prompt,
        validation=["non_empty", "has_required", "no_forbidden"],
        assets=[],
        metadata={
            HOLDOUT_MARKER: True,
            "generated_by": GENERATED_BY,
            "generated_shape": family,
            "generated_seed": seed,
            "difficulty": "medium",
            "version": f"{family}-s{seed}",
            "document": document,
            "required": [answer],
            "forbidden": decoys,
            "expected_answer": answer,
        },
    )


_BUILDERS: dict[str, Callable[[random.Random, int, int], TaskSpec]] = {
    "sql": _sql_spec,
    "needle": _needle_spec,
}


def generate_spec(index: int, *, seed: int = DEFAULT_SEED, task_type: str | None = None) -> TaskSpec:
    """Build one holdout spec.

    `index` picks the family (`index % len(families)`) and the task id;
    `seed` picks the data. Same index and seed always give the same spec, and the
    same index with a different seed gives the same id with a different prompt.
    """
    chosen = task_type or HOLDOUT_TYPES[index % len(HOLDOUT_TYPES)]
    if chosen not in _BUILDERS:
        raise ValueError(f"cannot generate a holdout spec of type {chosen!r}")
    return _BUILDERS[chosen](_stream(seed, index), index, seed)


def generate_arm(
    count: int = DEFAULT_ARM_SIZE, *, seed: int = DEFAULT_SEED
) -> list[TaskSpec]:
    """Build `count` holdout specs spanning every family."""
    if count < 1:
        raise ValueError("a holdout arm needs at least one spec")
    return [generate_spec(index, seed=seed) for index in range(count)]


def is_holdout(spec: TaskSpec) -> bool:
    """Is this spec in the unpublished arm?"""
    return bool(spec.metadata.get(HOLDOUT_MARKER))


def spec_seed(spec: TaskSpec) -> int | None:
    """The seed that determined this spec's task data, if it was generated.

    A generated spec records the seed it was built from, so a run over it can
    carry that seed instead of `None`. Without this the run's seed field is a
    label with nothing behind it on exactly the runs where the seed decided the
    problem, which is where a reader most wants to know it.
    """
    value = spec.metadata.get("generated_seed")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def holdout_secrets(spec: TaskSpec) -> list[str]:
    """The answer-key strings for a holdout spec — what must never be published.

    The task *prompt* is not a secret: the model is shown it. What must not
    escape is the key that grades it, because a published key turns every future
    holdout instance of that family into a published problem.
    """
    if not is_holdout(spec):
        return []
    meta = spec.metadata
    raw: list[Any] = [
        meta.get("reference_sql"),
        meta.get("expected_answer"),
        *(meta.get("required") or []),
        *(meta.get("forbidden") or []),
    ]
    return [value for value in raw if isinstance(value, str) and value.strip()]


def materialize(specs: list[TaskSpec], out_dir: Path | str) -> list[Path]:
    """Write specs as YAML under `out_dir` and return the written paths.

    `out_dir` is a run-scoped directory the caller owns. Nothing here refuses a
    path inside the repository — that is deliberate: the guarantee that holdout
    text stays out of git is the caller's choice of directory plus `.gitignore`,
    and a silent refusal would hide a misconfiguration rather than surface it.
    """
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for spec in specs:
        if not is_holdout(spec):
            raise ValueError(f"refusing to materialize non-holdout spec {spec.id!r} into an arm")
        path = root / f"{spec.id}.yaml"
        payload = {
            "id": spec.id,
            "type": spec.type,
            "prompt": spec.prompt,
            "validation": list(spec.validation),
            "assets": list(spec.assets),
            "metadata": spec.metadata,
        }
        path.write_text(yaml.safe_dump(payload, sort_keys=False, width=100), encoding="utf-8")
        written.append(path)
    return written
