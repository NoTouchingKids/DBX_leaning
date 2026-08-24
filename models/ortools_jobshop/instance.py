"""Turning bakehouse retail sales into a job-shop instance.

A bakery is a genuinely natural job shop, which is why the instance comes from
here rather than from the taxi trips the other models read: an order for a
product is a **job**, making it is an ordered run of **operations** on shared
equipment, and each piece of equipment does one thing at a time. That is the
problem `AddNoOverlap` over interval variables exists for.

`samples.bakehouse.sales_transactions` is the source, and it is chosen for a
second reason as well: `bakehouse` is the one schema in the sample catalog
whose columns are actually *verified* (`docs/sample-data-inventory.md`), so
this loader is written against a column listing rather than a guess.

## What is real and what is invented

Retail sales are not production orders, so the derivation has to say plainly
which half is data:

**Real** — read from the table, nothing added:

* the **grouping**: a job is one franchise's sales of one product on one day,
  aggregated with `SUM(quantity)`. The day, the franchise and the product are
  columns; so is the summed quantity.
* **how big a job is**: `units` is the real summed `quantity`. It is the one
  number that drives durations, and larger orders really do take longer.
* which products and franchises are busy — the candidate list is ordered by
  `units` descending, so the jobs scheduled are the batches that mattered.

**Invented** — plausible, and not derivable from anything in the table:

* that a day's *sales* stand in for that morning's *production run*. Nothing
  in the data says when anything was baked.
* the **stage recipes** (`RECIPES`). The table has no bill of materials, so
  which stages a product needs, and in what order, is assigned from a stable
  hash of the product name — deterministic, so one product always gets one
  recipe, but a modelling choice rather than a fact.
* every **rate** in `_stage_minutes`: minutes per unit, the fixed setup, the
  proving time, the oven's batch size. Calibrated to be plausible for a
  bakery; not measured.

Deliberately **not used**: `unitPrice`, `totalPrice`. They are `LONG`, so the
money is either integer minor units or rounded, and which of the two is not
verified anywhere. Nothing here depends on them, which is the cheapest way to
be right about them.

## Guards

Every column in the table is nullable, and `float(None)` on a real NULL is a
defect this repo has shipped once already. So: the SQL filters nulls, the
loaded rows are `dropna`'d again (the fallback and any injected dataset do not
go through the SQL), and per-row coercion skips anything that still will not
become a positive number. Quantities are clamped to a sanity range and
durations to another, and both clamps are *counted* so a run can say how much
of its instance was fabricated by the guard rather than by the data.

Size is capped by `max_jobs` because a job task has an hour. It is not capped
by anything to do with the solver: CP-SAT has no variable or constraint limit
and no licence file, which is this model's reason to exist next to the two
Gurobi ones (2000 variables / 2000 constraints, and an expiry date).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

from models._data import Dataset, load

__all__ = [
    "Operation",
    "Job",
    "Instance",
    "build_instance",
    "bakery_batches",
    "STAGES",
    "RECIPES",
    "MAX_JOBS",
    "SALES_TABLE",
    "operation_ceiling_for",
]

#: Verified columns as of 2026-08-24 — docs/sample-data-inventory.md.
SALES_TABLE = "samples.bakehouse.sales_transactions"

#: The shop floor. One machine per stage, and the index into this tuple *is*
#: the machine id on the result rows.
#:
#: One machine each keeps this a classic job shop. Two ovens would make it a
#: *flexible* job shop, which needs an optional interval per (operation,
#: machine) plus an exactly-one constraint over their presence literals — a
#: real modelling step, not a config value, and deliberately out of scope.
STAGES = ("mix", "rest", "bake", "decorate", "pack")

#: Which stages a product needs, in order. Invented (see the module docstring)
#: but not arbitrary: the four are the shapes a small bakery actually runs, and
#: — the part that matters for the model — they visit the machines in
#: *different orders*. `rest` before `bake` in one recipe and after it in
#: another is what keeps this a job shop rather than a flow shop, where every
#: job walks the machines in the same sequence and the problem collapses to a
#: permutation.
RECIPES: dict[str, tuple[str, ...]] = {
    # Cookies and cakes: mixed, baked, iced, boxed.
    "classic": ("mix", "bake", "decorate", "pack"),
    # Laminated dough: rests in the chiller before it ever sees the oven.
    "laminated": ("mix", "rest", "bake", "pack"),
    # Anything iced: has to cool on the rack *after* baking or the icing runs.
    "iced": ("mix", "bake", "rest", "decorate", "pack"),
    # Plain loaves: no decoration at all.
    "plain": ("mix", "bake", "pack"),
}
RECIPE_NAMES: tuple[str, ...] = tuple(sorted(RECIPES))

#: The longest recipe. Used to bound the operation count before an instance is
#: built, so a size guard does not have to construct the thing it is guarding.
MAX_OPERATIONS_PER_JOB = max(len(stages) for stages in RECIPES.values())

#: A wall-clock guard, not a solver limit: a Databricks job task has an hour,
#: and the app has five of them account-wide. 400 jobs is ~1700 operations and
#: ~3400 integer variables before the no-overlap constraints expand — already
#: well past what the bundled Gurobi licence would accept, which is the
#: comparison worth making.
MAX_JOBS = 400

#: Absurdity guards on the aggregated quantity. `quantity` is a LONG, so it
#: could be anything; the low end stops a zero/negative batch becoming a
#: zero-length operation, and the high end stops one outlier franchise-day
#: from turning the schedule into a single enormous bar.
MIN_UNITS = 1
MAX_UNITS = 240

#: Sanity floor and ceiling on every derived duration, in minutes. CP-SAT needs
#: integer durations, and a zero-length operation is not a thing a machine does.
MIN_OPERATION_MINUTES = 1
MAX_OPERATION_MINUTES = 120

#: How many units go in the oven at once, and how long a cycle takes. Bake time
#: is the one stage that is *not* linear in batch size: a tray is a tray.
OVEN_BATCH_UNITS = 24
BAKE_CYCLE_MINUTES = 12.0

#: Proving/cooling. Fixed on purpose — dough rests at its own pace, and a
#: bigger batch does not rest longer.
REST_MINUTES = 25.0


def operation_ceiling_for(job_count: int) -> int:
    """Upper bound on operations for this many jobs, without building them.

    An upper bound rather than a count: the recipe mix is not known until the
    products are, so the guard uses the longest recipe.
    """
    return job_count * MAX_OPERATIONS_PER_JOB


# --- the shape of a problem -------------------------------------------------


@dataclass(frozen=True)
class Operation:
    """One pass through one machine. ``minutes`` is an int because CP-SAT's
    interval sizes are integers, and rounding here rather than at model-build
    time keeps the results table and the solver agreeing on the number."""

    stage: str
    machine_id: int
    minutes: int


@dataclass(frozen=True)
class Job:
    label: str
    product: str
    franchise_id: int | None
    production_day: str | None
    units: int
    recipe: str
    operations: tuple[Operation, ...]

    @property
    def total_minutes(self) -> int:
        return sum(op.minutes for op in self.operations)


@dataclass(frozen=True)
class Instance:
    """A job-shop instance: jobs of ordered operations over shared machines.

    ``deadline_minutes`` is the one thing here that can make a run
    **INFEASIBLE** — a pure job shop with an open horizon always has a
    schedule, since the jobs can simply be run one after another. A hard
    "everything out of the door by opening time" cap is a real bakery
    constraint and the honest way to reach that terminal status.
    """

    jobs: tuple[Job, ...]
    machines: tuple[str, ...]
    seed: int
    deadline_minutes: int | None = None
    #: One line for a log message at the ``input`` phase.
    provenance: str = "generated batches (no dataset read)"
    #: ``Dataset.describe()`` fields, carried onto every result row.
    data_meta: dict[str, Any] = field(default_factory=dict)
    #: How the sales rows became a shop floor, for the record.
    instance_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def job_count(self) -> int:
        return len(self.jobs)

    @property
    def machine_count(self) -> int:
        return len(self.machines)

    @property
    def operation_count(self) -> int:
        return sum(len(job.operations) for job in self.jobs)

    @property
    def total_minutes(self) -> int:
        return sum(job.total_minutes for job in self.jobs)

    def machine_load(self, machine_id: int) -> int:
        """Minutes this machine must spend working, whatever the schedule."""
        return sum(
            op.minutes for job in self.jobs for op in job.operations if op.machine_id == machine_id
        )

    @property
    def makespan_lower_bound(self) -> int:
        """The trivial bound: no schedule beats the longest job, and none beats
        the busiest machine. Worth having for a log line, for a plausibility
        check on a returned schedule, and for choosing a deadline that is
        provably infeasible without guessing.
        """
        if not self.jobs:
            return 0
        return max(
            max(job.total_minutes for job in self.jobs),
            max(self.machine_load(m) for m in range(len(self.machines))),
        )

    @property
    def horizon(self) -> int:
        """Every start and end variable's upper bound.

        Running every job end to end is always feasible, so the sum of all
        durations is a valid horizon. A deadline shrinks it — and if the
        deadline is below what a single job needs, the domains are empty and
        CP-SAT says INFEASIBLE, which is the correct answer rather than a bug.
        """
        total = self.total_minutes
        if self.deadline_minutes is None:
            return total
        return min(total, self.deadline_minutes)


# --- reading the sales table ------------------------------------------------


def bakery_batches(*, limit: int = 400, seed: int = 20260824) -> Dataset:
    """Busiest (day, franchise, product) batches from bakehouse sales.

    Not in ``models/_data/datasets.py`` because that module is one function per
    *dataset* shared between models, and this shape has exactly one consumer so
    far. ``load()`` was never samples-specific — it takes arbitrary SQL and a
    source label — so nothing about living here makes it a lesser loader.

    ``production_day`` is formatted to a **string** in SQL rather than left as
    a DATE. Spark hands back a ``datetime.date`` for a date column while the
    fallback would hand back whatever the generator makes, and a model that
    only meets the difference on a workspace is the exact trap
    ``models/_data`` documents.
    """
    sql = f"""
        SELECT
            date_format(dateTime, 'yyyy-MM-dd')  AS production_day,
            franchiseID                          AS franchise_id,
            product                              AS product,
            SUM(quantity)                        AS units,
            COUNT(*)                             AS orders
        FROM {SALES_TABLE}
        WHERE dateTime IS NOT NULL
          AND product IS NOT NULL
          AND franchiseID IS NOT NULL
          AND quantity IS NOT NULL
          AND quantity > 0
        GROUP BY 1, 2, 3
        HAVING SUM(quantity) > 0
        -- Busiest first, then the keys: LIMIT without a total order returns a
        -- different instance every run, and an instance that changes under you
        -- cannot be compared against the Gurobi models or against itself.
        ORDER BY units DESC, production_day, franchise_id, product
        LIMIT {int(limit)}
    """
    return load(
        sql,
        source=SALES_TABLE,
        fallback=lambda: _synthetic_batches(limit, seed=seed),
        fallback_name="synthetic:bakery-batches",
        minimum_rows=8,
    )


def _synthetic_batches(n: int, *, seed: int) -> list[dict[str, Any]]:
    """The offline shape of the same query: same columns, same broad
    statistics, deterministic for a seed. This is what makes the model runnable
    with no Databricks, which is how its tests run."""
    import random

    rng = random.Random(seed)
    products = (
        "Golden Gate Ginger",
        "Outback Oatmeal",
        "Austin Almond Biscotti",
        "Tokyo Tidbits",
        "Orchard Oasis",
        "Pearly Pies",
        "Reykjavik Rye",
        "Carnival Chocolate",
    )
    rows = []
    for index in range(n):
        # A skewed batch size, like a real sales aggregate: many small, a few
        # large. Sorted descending below, matching the query's ORDER BY.
        units = max(1, min(MAX_UNITS, int(rng.lognormvariate(2.6, 0.8))))
        rows.append(
            {
                "production_day": f"2026-0{1 + index % 9}-{1 + index % 28:02d}",
                "franchise_id": 3000 + index % 12,
                "product": products[index % len(products)],
                "units": units,
                "orders": max(1, units // 3),
            }
        )
    rows.sort(key=lambda r: (-r["units"], r["production_day"], r["franchise_id"], r["product"]))
    return rows


# --- sales rows into operations ---------------------------------------------


def recipe_for(product: str) -> str:
    """Which stage sequence a product goes through.

    A stable hash, not ``hash()``: Python randomises string hashing per
    process, so ``hash()`` would give the same product a different recipe on
    every run and make the instance irreproducible for no reason at all.
    """
    digest = hashlib.blake2b(product.encode("utf-8"), digest_size=8).digest()
    return RECIPE_NAMES[int.from_bytes(digest, "big") % len(RECIPE_NAMES)]


def _stage_minutes(stage: str, units: int) -> float:
    """Minutes for one stage of a batch of ``units``.

    Every constant below is invented. What is *derived* is the shape: three
    stages scale with the batch (a bigger order really does take longer to mix,
    ice and box), one is a fixed rest, and baking steps with the oven's
    capacity rather than with the count — which is the behaviour that makes the
    oven the interesting bottleneck rather than just the slowest machine.
    """
    if stage == "mix":
        return 6.0 + 0.35 * units  # scale up, weigh out, run the mixer
    if stage == "rest":
        return REST_MINUTES  # proving or cooling: fixed, by definition
    if stage == "bake":
        return math.ceil(units / OVEN_BATCH_UNITS) * BAKE_CYCLE_MINUTES
    if stage == "decorate":
        return 3.0 + 0.4 * units  # hand work, so linear in the count
    if stage == "pack":
        return 2.0 + 0.15 * units
    raise ValueError(f"unknown stage {stage!r}; expected one of {', '.join(STAGES)}")


def _positive_int(value: Any) -> int | None:
    """A column value as a positive int, or None if it cannot honestly be one.

    Every bakehouse column is nullable and `quantity` is a LONG that nothing
    guarantees is small or positive. Returning None rather than raising lets
    the caller drop the row and *count* it, which is the difference between a
    thin instance you can see and a crash on a workspace only.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(number)


def _job_from_row(row: dict[str, Any], *, index: int) -> tuple[Job, bool] | None:
    """One aggregated sales row as a job. ``None`` if the row is unusable.

    The bool is "the quantity had to be clamped" — reported, not swallowed.
    """
    raw_units = _positive_int(row.get("units"))
    if raw_units is None:
        return None
    units = max(MIN_UNITS, min(MAX_UNITS, raw_units))

    product = str(row.get("product") or "").strip() or f"product-{index:03d}"
    franchise_id = _positive_int(row.get("franchise_id"))
    day = row.get("production_day")
    day_label = str(day) if day is not None else None

    recipe = recipe_for(product)
    operations = tuple(
        Operation(
            stage=stage,
            machine_id=STAGES.index(stage),
            minutes=int(
                max(
                    MIN_OPERATION_MINUTES,
                    min(MAX_OPERATION_MINUTES, round(_stage_minutes(stage, units))),
                )
            ),
        )
        for stage in RECIPES[recipe]
    )

    label = f"{product} x{units}"
    if franchise_id is not None:
        label = f"{label} @f{franchise_id}"
    if day_label:
        label = f"{label} {day_label}"

    return (
        Job(
            label=label,
            product=product,
            franchise_id=franchise_id,
            production_day=day_label,
            units=units,
            recipe=recipe,
            operations=operations,
        ),
        units != raw_units,
    )


def build_instance(
    *,
    max_jobs: int = 60,
    seed: int = 20260824,
    use_sample_data: bool = True,
    sales_data: Dataset | None = None,
    deadline_minutes: int | None = None,
    candidate_limit: int = 400,
) -> Instance:
    """A fixed instance for a given seed and size. Same inputs, same problem.

    Batches come from bakehouse sales unless ``use_sample_data`` is False,
    which reads nothing at all and generates them. Pass ``sales_data`` to
    supply the rows directly — which is how the tests state exactly what the
    derivation is supposed to be looking at.

    Raises ``ValueError`` above ``MAX_JOBS``: that ceiling is about the job
    task's hour, not about the solver, and clipping it silently would hide a
    run that was never going to finish.
    """
    if max_jobs < 1:
        raise ValueError(f"max_jobs must be at least 1, got {max_jobs}")
    if max_jobs > MAX_JOBS:
        raise ValueError(
            f"max_jobs={max_jobs} would build up to {operation_ceiling_for(max_jobs)} "
            f"operations, which will not solve inside a job task's hour. "
            f"Maximum is {MAX_JOBS}. Note this is a wall-clock guard: CP-SAT itself "
            f"has no variable or constraint cap."
        )
    if deadline_minutes is not None and deadline_minutes < 1:
        raise ValueError(f"deadline_minutes must be at least 1, got {deadline_minutes}")

    data: Dataset | None = sales_data
    if data is None and use_sample_data:
        data = bakery_batches(limit=max(candidate_limit, max_jobs), seed=seed)

    rows: list[dict[str, Any]] = []
    data_meta: dict[str, Any] = {}
    provenance = ""
    if data is not None:
        # dropna again, even though the SQL filters: the fallback rows and any
        # injected dataset never went through that WHERE clause.
        usable = data.dropna("product", "units")
        data_meta = dict(usable.describe())
        provenance = usable.provenance
        rows = usable.rows

    if not rows:
        why = (
            "sample data not requested"
            if not use_sample_data and sales_data is None
            else "no usable sales rows"
        )
        data = Dataset(
            rows=_synthetic_batches(max(candidate_limit, max_jobs), seed=seed),
            source="synthetic:bakery-batches",
            synthetic=True,
            reason=why,
        )
        rows = data.rows
        data_meta = dict(data.describe())
        provenance = data.provenance

    candidates = len(rows)
    jobs: list[Job] = []
    clamped = 0
    skipped = 0
    for index, row in enumerate(rows):
        if len(jobs) >= max_jobs:
            break
        made = _job_from_row(row, index=index)
        if made is None:
            skipped += 1
            continue
        job, was_clamped = made
        jobs.append(job)
        clamped += int(was_clamped)

    instance_meta: dict[str, Any] = {
        "batches_offered": candidates,
        "jobs_built": len(jobs),
        # What the cap actually threw away, so a run can say so out loud rather
        # than quietly scheduling a tenth of the day.
        "batches_capped": max(0, candidates - len(jobs) - skipped),
        "rows_skipped_unusable": skipped,
        "quantities_clamped": clamped,
        "units_clamp": [MIN_UNITS, MAX_UNITS],
        "duration_clamp_minutes": [MIN_OPERATION_MINUTES, MAX_OPERATION_MINUTES],
        "recipes": {recipe: sum(1 for job in jobs if job.recipe == recipe) for recipe in RECIPES},
        "deadline_minutes": deadline_minutes,
    }
    # The results table has a fixed schema: every provenance key present on
    # every row, null rather than absent.
    data_meta.setdefault("data_fallback_reason", None)

    return Instance(
        jobs=tuple(jobs),
        machines=STAGES,
        seed=seed,
        deadline_minutes=deadline_minutes,
        provenance=f"job-shop batches from {provenance}" if provenance else "generated batches",
        data_meta=data_meta,
        instance_meta=instance_meta,
    )
