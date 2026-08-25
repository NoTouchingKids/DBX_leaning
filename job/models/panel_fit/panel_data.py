"""The country x year panel this model fits, and the generator that stands in
for it until somebody lands the real thing.

**There is no OWID table in Unity Catalog today.** Nobody has landed one. So
the synthetic generator below is not a test fixture that happens to also run
offline — it is the *primary* path for every run of this model right now, and
it is built to be worth fitting. See the module docstring of ``model.py`` for
what a user has to do to point this at real data instead.

Why a loader here rather than in ``job/models/_data/datasets.py``: that module's
rule is one function per *dataset*, and it currently holds the two NYC-taxi
shapes every other model reads. This is the only panel-shaped consumer on the
platform. If a second one appears, this function belongs beside those two —
it already goes through ``models._data.load()`` and would move unchanged.
"""

from __future__ import annotations

import math
import random
import re
from typing import Any

from .._data import Dataset, load

__all__ = [
    "DEFAULT_PANEL_TABLE",
    "PanelColumns",
    "load_panel",
    "synthetic_panel",
    "validate_table_name",
]

#: Where the model looks for real data. Deliberately named after what it wants
#: rather than after something that exists: this table is **not** in Unity
#: Catalog, so a run against the default configuration falls back to the
#: generator below and says so loudly in its provenance. Landing an OWID CSV
#: under this name is the whole change needed to make a run read real data.
DEFAULT_PANEL_TABLE = "main.dbx_leaning.owid_country_year"

#: A country x year panel is worthless below a few hundred rows, and a table
#: that exists but comes back nearly empty is the case `minimum_rows` in
#: `models._data.load` exists for — better to fall back visibly than to fit
#: eleven countries and call it a panel.
DEFAULT_MINIMUM_ROWS = 100

#: Only ever interpolated into SQL after passing this. The table and column
#: names arrive as a job parameter (`DBX_MODEL_CONFIG`), which is trusted but
#: not validated by anything upstream, and there is no bound-parameter form
#: for an identifier — so the check has to happen here or nowhere.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PanelColumns:
    """Which columns of the panel mean what.

    Named rather than positional because four string columns in a row is
    exactly the shape that gets silently transposed.
    """

    __slots__ = ("group", "label", "period", "response", "predictor")

    def __init__(
        self,
        *,
        group: str,
        label: str | None,
        period: str,
        response: str,
        predictor: str,
    ) -> None:
        self.group = _identifier(group, "group_column")
        # Optional: OWID's `Code` is null for aggregates like "World", so a
        # panel keyed on it would collapse every aggregate into one group.
        # The key is the entity; the code rides along as a label.
        self.label = _identifier(label, "label_column") if label else None
        self.period = _identifier(period, "period_column")
        self.response = _identifier(response, "response_column")
        self.predictor = _identifier(predictor, "predictor_column")

    @property
    def selected(self) -> list[str]:
        """The columns to read, de-duplicated, order preserved.

        `predictor` defaults to `period` — fitting a per-country time trend is
        the natural question on a panel — and then it is one column, not two.
        """
        out: list[str] = []
        for name in (self.group, self.label, self.period, self.response, self.predictor):
            if name and name not in out:
                out.append(name)
        return out


def _identifier(name: str, what: str) -> str:
    if not _IDENTIFIER.match(str(name)):
        raise ValueError(f"{what}={name!r} is not a bare SQL identifier")
    return str(name)


def validate_table_name(table: str) -> str:
    """A 1-, 2- or 3-part name of bare identifiers, or `ValueError`.

    Called from the model's constructor as well as from `load_panel`, so a
    malformed `table` in the job config fails immediately rather than at the
    first read — by which point the run has already claimed one of five
    account-wide task slots.
    """
    parts = str(table).split(".")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"table={table!r} is not a 1-, 2- or 3-part name")
    for part in parts:
        _identifier(part, "table")
    return str(table)


def load_panel(
    *,
    table: str = DEFAULT_PANEL_TABLE,
    columns: PanelColumns,
    limit: int = 20_000,
    seed: int = 24,
    minimum_rows: int = DEFAULT_MINIMUM_ROWS,
) -> Dataset:
    """Read the panel from Unity Catalog, or generate one shaped like it.

    No HTTP, no `pandas.read_csv` of a URL, nothing that reaches the internet:
    Free Edition restricts outbound traffic from job compute, so a fetch here
    would work on a laptop and hang on the job after taking one of five
    account-wide task slots (`docs/free-edition-constraints.md`).
    """
    table = validate_table_name(table)
    projection = ", ".join(columns.selected)
    sql = f"""
        SELECT {projection}
        FROM {table}
        WHERE {columns.group} IS NOT NULL
          AND {columns.period} IS NOT NULL
        ORDER BY {columns.group}, {columns.period}
        LIMIT {int(limit)}
    """
    return load(
        sql,
        source=table,
        fallback=lambda: synthetic_panel(columns=columns, seed=seed),
        fallback_name="synthetic:owid-panel",
        minimum_rows=minimum_rows,
    )


# --- the generator ---------------------------------------------------------
#
# Deterministic for a seed, and shaped like an OWID export: one row per entity
# per year, `Entity` / `Code` / `Year` plus value columns, nulls where a
# country did not report.
#
# **Varied group sizes are the design requirement, not a nicety.** This model
# exists to answer "what does a run that SUCCEEDED with 12 of 180 units FAILED
# look like on the wire". A generator that only ever produced fittable groups
# would make its own model untestable, so the plan below deliberately includes
# countries that cannot be fitted, and reaches each failure by a different
# route.

#: (entity, ISO3) pairs. Real countries, so a per-group view reads like
#: something rather than like `group_0017`.
_COUNTRIES: tuple[tuple[str, str | None], ...] = (
    ("Argentina", "ARG"),
    ("Australia", "AUS"),
    ("Austria", "AUT"),
    ("Bangladesh", "BGD"),
    ("Belgium", "BEL"),
    ("Brazil", "BRA"),
    ("Canada", "CAN"),
    ("Chile", "CHL"),
    ("China", "CHN"),
    ("Colombia", "COL"),
    ("Denmark", "DNK"),
    ("Egypt", "EGY"),
    ("Ethiopia", "ETH"),
    ("Finland", "FIN"),
    ("France", "FRA"),
    ("Germany", "DEU"),
    ("Ghana", "GHA"),
    ("Greece", "GRC"),
    ("India", "IND"),
    ("Indonesia", "IDN"),
    ("Ireland", "IRL"),
    ("Italy", "ITA"),
    ("Japan", "JPN"),
    ("Kenya", "KEN"),
    ("Malaysia", "MYS"),
    ("Mexico", "MEX"),
    ("Morocco", "MAR"),
    ("Netherlands", "NLD"),
    ("New Zealand", "NZL"),
    ("Nigeria", "NGA"),
    ("Norway", "NOR"),
    ("Pakistan", "PAK"),
    ("Peru", "PER"),
    ("Philippines", "PHL"),
    ("Poland", "POL"),
    ("Portugal", "PRT"),
    ("South Africa", "ZAF"),
    ("South Korea", "KOR"),
    ("Spain", "ESP"),
    ("Sweden", "SWE"),
    ("Thailand", "THA"),
    ("Turkey", "TUR"),
    ("Ukraine", "UKR"),
    ("United Kingdom", "GBR"),
    ("United States", "USA"),
    ("Vietnam", "VNM"),
    # Aggregates: present in every OWID export, and their `Code` is null.
    # They are the reason `group_label` is allowed to be null and the group
    # key is the entity name.
    ("World", None),
    ("Sub-Saharan Africa", None),
)

_FIRST_YEAR = 1960
_LAST_YEAR = 2023

#: How many years each *healthy* country reports, cycled through in order.
#: Spread over more than a decade of magnitudes on purpose — a panel where
#: every group is the same size hides every bug that depends on group size.
_HEALTHY_SIZES = (64, 58, 51, 44, 39, 33, 28, 24, 19, 15, 12, 9, 7, 5, 4, 3)

#: Group shapes that cannot be fitted, and the failure each one reaches at the
#: default configuration (degree 1, min_observations 3). Sizes here are
#: *rows*, not usable observations.
_TOO_FEW = (1, 2, 2, 1)  # -> too_few_observations, straight away
_ALL_NULL_RESPONSE = (9, 6, 7)  # -> too_few_observations, after the null drop
_SINGLE_YEAR = (5, 4)  # -> zero_predictor_variance
_TWO_YEARS = (8, 6, 6)  # -> fittable at degree 1, singular at degree >= 2


def synthetic_panel(*, columns: PanelColumns, seed: int = 24) -> list[dict[str, Any]]:
    """A deterministic OWID-shaped panel, warts included.

    Life expectancy per entity per year, over 48 entities whose group sizes
    run from 1 row to 64. Columns come out under whatever names the config
    asked for; when ``predictor_column`` names something other than the
    period, that column is generated as a plausible GDP-per-capita series so
    a "response against a covariate" configuration has real data too.
    """
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []

    plan = _plan()
    for (entity, code), (kind, size) in zip(_COUNTRIES, plan, strict=False):
        rows.extend(_country_rows(rng, entity, code, kind, size, columns))

    # Rows that cannot be placed at all: no entity, or no year. Real exports
    # have them (footnote rows, partial merges) and the model has to drop them
    # before grouping rather than manufacture a group called None.
    for _ in range(4):
        rows.append(_row(columns, None, "XXX", 1990, 60.0, 5_000.0))
    for _ in range(3):
        rows.append(_row(columns, "Nowhereland", None, None, 61.0, 5_100.0))

    # A real `SELECT` has no order guarantee, and a model that only works on
    # pre-sorted input is a model that works offline and misbehaves on a
    # workspace. Shuffled deterministically so this stays reproducible.
    rng.shuffle(rows)
    return rows


def _plan() -> list[tuple[str, int]]:
    """One (kind, size) per country, interleaved so the broken groups are not
    all clustered at one end — a run cancelled halfway must still have met
    some of both, and so must the first result chunk."""
    broken = (
        [("too_few", n) for n in _TOO_FEW]
        + [("all_null", n) for n in _ALL_NULL_RESPONSE]
        + [("single_year", n) for n in _SINGLE_YEAR]
        + [("two_years", n) for n in _TWO_YEARS]
    )
    healthy_count = len(_COUNTRIES) - len(broken)
    healthy = [("healthy", _HEALTHY_SIZES[i % len(_HEALTHY_SIZES)]) for i in range(healthy_count)]

    out: list[tuple[str, int]] = []
    broken_iter = iter(broken)
    healthy_iter = iter(healthy)
    for index in range(len(_COUNTRIES)):
        # Every third slot is a broken group until the broken ones run out.
        pick = broken_iter if index % 3 == 2 else healthy_iter
        nxt = next(pick, None) or next(healthy_iter, None) or next(broken_iter, None)
        if nxt is None:
            break
        out.append(nxt)
    return out


def _country_rows(
    rng: random.Random,
    entity: str,
    code: str | None,
    kind: str,
    size: int,
    columns: PanelColumns,
) -> list[dict[str, Any]]:
    # Per-country trend: poorer countries start lower and improve faster,
    # which is the actual shape of the real series and gives the per-group
    # slopes something to differ about.
    base = rng.uniform(35.0, 62.0)
    slope = rng.uniform(0.08, 0.42) * (1.0 + (62.0 - base) / 40.0)
    gdp0 = math.exp(rng.uniform(6.0, 10.4))
    growth = rng.uniform(0.005, 0.055)

    def value(year: int) -> tuple[float, float]:
        t = year - _FIRST_YEAR
        life = base + slope * t + rng.gauss(0.0, 0.55)
        # Life expectancy is bounded in reality; letting the trend run past 90
        # would put a shape in the data that no fit should be asked to match.
        life = max(20.0, min(90.0, life))
        gdp = gdp0 * math.exp(growth * t) * rng.uniform(0.93, 1.07)
        return round(life, 3), round(gdp, 2)

    if kind == "single_year":
        # One reporting year, repeated across export revisions. Every x is the
        # same, so there is no slope to estimate -> zero_predictor_variance.
        year = rng.randint(1995, 2015)
        years = [year] * size
    elif kind == "two_years":
        # Two reporting years only. Fits a line; cannot fit a parabola.
        first, second = sorted(rng.sample(range(1970, 2015), 2))
        years = [first if i % 2 == 0 else second for i in range(size)]
    else:
        span = min(size, _LAST_YEAR - _FIRST_YEAR + 1)
        start = rng.randint(_FIRST_YEAR, _LAST_YEAR - span + 1)
        years = list(range(start, start + span))

    rows = []
    for index, year in enumerate(years):
        life, gdp = value(year)
        if kind == "all_null":
            # The country is in the panel every year but reported the response
            # almost never. This is the route that matters: the group looks
            # big enough right up until the nulls are dropped, which is why
            # `job/models/README.md` says never to assume a column is non-null.
            life = life if index == 0 else None
        elif kind != "too_few" and rng.random() < 0.06:
            # Ordinary sparseness. Not enough to break a healthy group.
            life = None
        rows.append(_row(columns, entity, code, year, life, gdp))
    return rows


def _row(
    columns: PanelColumns,
    entity: str | None,
    code: str | None,
    year: int | None,
    life: float | None,
    gdp: float | None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        columns.group: entity,
        columns.period: year,
        columns.response: life,
    }
    if columns.label:
        row[columns.label] = code
    # `predictor` is `period` by default, in which case this is the same key
    # and setdefault leaves the year alone.
    row.setdefault(columns.predictor, gdp)
    return row
