"""Many small independent curve fits over panel data — the per-unit-outcome case.

**Why this model exists.** Every other model on this platform is one
computation: a MILP with a closing gap, a chain with an R-hat, a backtest with
a rolling error. Their progress is a scalar and their outcome is a single
verdict. This one is *180 outcomes* — one per country — and individual units
are allowed to fail without failing the run. That shape (a model per store,
per region, per SKU) is extremely common in real work and the envelope had
never been tested against it. The question it answers is:

    what does a run that SUCCEEDED with 12 of 180 units FAILED look like on
    the wire, and can a client tell that apart from a healthy run?

So the failures are the point, not an edge case to tidy away. **Unfittable
groups are recorded, never dropped** — with a reason, and with null
coefficients — because "we could not fit Chad" and "Chad was never in the
data" are different answers and only one of them is a data problem. The
answer to the question above is: `payload.groups_fitted` and
`payload.groups_failed` are on *every* progress message, `payload
.failure_counts` breaks the failures down by reason, and the results table
carries one row per group with a `status`.

## The data, and what is really going on with it

The natural source is the OWID collection (`https://github.com/owid/owid-datasets`)
— country x year panels, long and clean, and external data is now permitted
(`docs/ml-datasets.md`). Two things follow, and both matter:

1. **A model cannot fetch it at run time.** Free Edition restricts outbound
   traffic from job compute, so an HTTP call here would work on a laptop and
   hang on the job *after* claiming one of five account-wide task slots. The
   data has to be in Unity Catalog first — a volume upload, Marketplace, or
   Delta Sharing (`docs/free-edition-constraints.md`, "Getting data in from
   outside").
2. **Nobody has landed it.** There is no OWID table in Unity Catalog today.
   `DEFAULT_PANEL_TABLE` names one that does not exist, so at the default
   configuration every run falls back to the deterministic generator in
   `panel_data.py` and says so in its provenance — logged at the `input`
   phase and stamped on every result row as `data_source` / `data_synthetic`
   / `data_rows` / `data_fallback_reason`.

**To point this at real data:** download an OWID CSV, land it in Unity
Catalog under any name, and set `{"table": "<catalog>.<schema>.<table>"}` in
the model config — plus `group_column` / `period_column` / `response_column`
/ `predictor_column` if the columns are not named as OWID exports them. The
fallback is not a placeholder to be replaced when that happens: it stays, and
it is what keeps this model runnable offline, which is how its tests run.

The generator is built to be *worth* fitting rather than merely present —
realistic per-country trends, and deliberately varied group sizes including
groups too small to fit. A fallback that only ever produced fittable groups
would make this model's entire reason for existing untestable.

## The fit

Least squares on a Vandermonde design over the **raw** predictor — not
centred, not scaled — so `intercept`, `slope` and `coefficients` mean exactly
what their column names say against the raw values, and a reader can apply a
stored coefficient without knowing about a hidden offset the results table has
nowhere to record. That choice has a consequence worth knowing before it looks
like a bug: a Vandermonde over calendar years is badly conditioned above the
quadratic, so `{"degree": 3}` on the default panel reports `singular_design`
for most groups rather than returning coefficients. That is the intended
behaviour — the failure is *recorded* instead of a quietly wrong fit being
returned — but it means high-degree fits want a predictor with a sane range,
not a year number.

## Telemetry

- `percent_complete` is groups done / groups total. Genuinely honest here,
  unlike a solver's gap.
- `primary_metric` is the **median R-squared across the groups fitted so
  far**, labelled `median_r_squared`. Higher is better, and the median rather
  than the mean because one group with a pathological fit should not drag the
  headline number of a 180-group run. It is null until something has been
  fitted, which is a legal value and the honest one.
- `payload` always carries `groups_done` / `groups_total` / `groups_fitted` /
  `groups_failed` and the current group's identity.

Results are chunked (`chunk_size` groups per emission) so a long run is
watchable rather than silent until the end.
"""

from __future__ import annotations

import math
import statistics
import time
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

from models._data import Dataset

from .panel_data import DEFAULT_PANEL_TABLE, PanelColumns, load_panel

__all__ = [
    "DEFAULT_PANEL_TABLE",
    "FAILURE_REASONS",
    "GROUP_STATUSES",
    "PanelFitModel",
    "REASON_NON_FINITE_RESULT",
    "REASON_SINGULAR_DESIGN",
    "REASON_TOO_FEW_OBSERVATIONS",
    "REASON_ZERO_PREDICTOR_VARIANCE",
    "STATUS_FAILED",
    "STATUS_FITTED",
    "build_model",
]

# --- the closed set of failure reasons -------------------------------------
#
# Closed and exported on purpose. A UI's first question about a run with
# failures is "failed *how*, and how many of each" — which needs a small set
# it can group by and pre-declare colours/labels for, not free text that
# differs per group. Anything that does not fit one of these four is a bug in
# this model, not a fifth reason to invent inline.

#: Fewer usable observations than `min_observations`. Reached two ways: a
#: group that is simply short, and a group that looked long enough until its
#: null response values were dropped.
REASON_TOO_FEW_OBSERVATIONS = "too_few_observations"
#: Every observation has the same predictor value — one reporting year
#: repeated across export revisions, say. There is no slope to estimate.
REASON_ZERO_PREDICTOR_VARIANCE = "zero_predictor_variance"
#: The design matrix is rank-deficient: fewer distinct predictor values than
#: the fit has coefficients (a parabola through two distinct x), or an
#: ill-conditioned system NumPy's SVD reports as deficient.
REASON_SINGULAR_DESIGN = "singular_design"
#: The arithmetic completed and produced NaN or infinity — overflow in the
#: residual sum of squares on extreme values, most likely. A fit nobody should
#: be handed as if it were a number.
REASON_NON_FINITE_RESULT = "non_finite_result"

FAILURE_REASONS: tuple[str, ...] = (
    REASON_TOO_FEW_OBSERVATIONS,
    REASON_ZERO_PREDICTOR_VARIANCE,
    REASON_SINGULAR_DESIGN,
    REASON_NON_FINITE_RESULT,
)

#: Two values, not "fitted or a failure reason". The `uc_ddl` comment reads
#: the other way; see the note in `_fit_group` for why this is the shape that
#: leaves both columns useful.
STATUS_FITTED = "fitted"
STATUS_FAILED = "failed"
GROUP_STATUSES: tuple[str, ...] = (STATUS_FITTED, STATUS_FAILED)

DEFAULT_DEGREE = 1
#: Groups per result emission. Small enough that the default 48-group panel
#: produces four chunks — the watchability this chunking exists for — and
#: large enough that a 10,000-group run does not produce 10,000 writes.
DEFAULT_CHUNK_SIZE = 12
#: Stop logging individual failures after this many. The durable record
#: already has every one of them, with its reason, in the results table; past
#: a couple of dozen the log is chatter that a live channel would drop anyway.
DEFAULT_FAILURE_LOG_LIMIT = 12


class _Group:
    """One unit of work: a group key and the observations under it."""

    __slots__ = ("key", "label", "periods", "x", "y", "rows_seen")

    def __init__(self, key: str, label: str | None) -> None:
        self.key = key
        self.label = label
        #: Every period the group appears at, usable observation or not — so a
        #: group that failed can still say *when* it existed.
        self.periods: list[float] = []
        #: Predictor and response, only where both are present and finite.
        self.x: list[float] = []
        self.y: list[float] = []
        self.rows_seen = 0


class PanelFitModel:
    results_table = "results_panel_fit"
    # No `preview_axes`, deliberately. These rows are one-per-group, not a
    # series, so there is no shape for LTTB to preserve; and `r_squared` is
    # null on exactly the rows that matter most, which would make
    # `shared.downsample.downsample_rows` fall back to even spacing anyway —
    # silently. Better to take that fallback openly. It only ever engages for
    # a caller running `chunk_size` above the preview bound; at the default,
    # every row of every chunk is in the preview, failures included.

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})

        # --- where the panel comes from ---
        #: A caller-supplied panel wins over the loader — that is how the
        #: tests build a group with an exact defect.
        self._rows_config: Sequence[dict[str, Any]] | None = cfg.get("rows")
        self.table = str(cfg.get("table", DEFAULT_PANEL_TABLE))
        self.columns = PanelColumns(
            group=str(cfg.get("group_column", "entity")),
            label=cfg.get("label_column", "code"),
            period=str(cfg.get("period_column", "year")),
            response=str(cfg.get("response_column", "life_expectancy")),
            # Defaults to the period: a per-country time trend is the natural
            # question on a panel, and it is what makes
            # `zero_predictor_variance` a real condition rather than a
            # theoretical one.
            predictor=str(cfg.get("predictor_column", cfg.get("period_column", "year"))),
        )
        self.limit = int(cfg.get("limit", 20_000))
        self.seed = int(cfg.get("seed", 24))

        # --- the fit ---
        self.degree = int(cfg.get("degree", DEFAULT_DEGREE))
        if self.degree < 1:
            raise ValueError(f"degree must be at least 1, got {self.degree}")
        #: Default is one more than an exactly-determined fit needs. At
        #: `degree + 1` observations the fit passes through every point and
        #: R-squared is 1 by construction — a number that looks like a
        #: triumph and means nothing.
        self.min_observations = int(cfg.get("min_observations", self.degree + 2))
        self.max_groups: int | None = (
            int(cfg["max_groups"]) if cfg.get("max_groups") is not None else None
        )

        # --- telemetry cadence ---
        self.chunk_size = max(1, int(cfg.get("chunk_size", DEFAULT_CHUNK_SIZE)))
        #: One progress message per group by default. Group counts here are in
        #: the hundreds, and per-unit progress is the whole point of this
        #: model; a caller with thousands of groups should raise this.
        self.progress_every = max(1, int(cfg.get("progress_every", 1)))
        self.failure_log_limit = int(cfg.get("failure_log_limit", DEFAULT_FAILURE_LOG_LIMIT))

        self.emit: Callable[..., Any] | None = None
        self.should_cancel: Callable[[], bool] | None = None

        self.data: Dataset | None = None
        self._groups: list[_Group] | None = None
        self._provenance: dict[str, Any] = {}
        self.rows_unplaceable = 0

        #: Per-group result rows, in the order they were produced. Public for
        #: tests and standalone use — deliberately *not* named `results`,
        #: `get_results` or `result_rows`, because those are the names the
        #: harness discovers and it must not also call an accessor on a model
        #: that has already streamed its rows (`models/README.md`).
        self.group_rows: list[dict[str, Any]] = []
        self.groups_fitted = 0
        self.groups_failed = 0
        self.failure_counts: Counter[str] = Counter()
        self.chunks_emitted = 0
        self.rows_emitted = 0
        self.cancelled = False

    # --- input ------------------------------------------------------------

    def build(self) -> None:
        """Load and group the panel. Separate from `run` so the load happens
        with `emit` already wired — a provenance line emitted from `__init__`
        goes nowhere, because the harness constructs the model first and
        attaches the callbacks after."""
        self._ensure_groups()

    @property
    def groups(self) -> list[_Group]:
        return self._ensure_groups()

    def _load(self) -> Dataset:
        if self._rows_config is not None:
            return Dataset(
                rows=[dict(row) for row in self._rows_config],
                source="config:rows",
                synthetic=False,
            )
        return load_panel(
            table=self.table,
            columns=self.columns,
            limit=self.limit,
            seed=self.seed,
        )

    def _ensure_groups(self) -> list[_Group]:
        if self._groups is not None:
            return self._groups

        data = self._load()
        self.data = data
        # Always the same keys, including a null fallback reason on a
        # successful read, so this results table has one schema regardless of
        # how a given run went.
        self._provenance = data.describe()
        self._log(f"panel input: {data.provenance}", phase="input")

        cols = self.columns
        by_key: dict[str, _Group] = {}
        unplaceable = 0

        for row in data.rows:
            key = row.get(cols.group)
            period = _finite_float(row.get(cols.period))
            # No key means no group; no period means no place on the axis.
            # Manufacturing a group called None out of these would put a
            # fictitious unit in a table whose whole promise is one row per
            # real unit.
            if key is None or str(key) == "" or period is None:
                unplaceable += 1
                continue

            key = str(key)
            group = by_key.get(key)
            if group is None:
                label = row.get(cols.label) if cols.label else None
                group = by_key[key] = _Group(key, None if label is None else str(label))
            elif group.label is None and cols.label and row.get(cols.label) is not None:
                # OWID's `Code` is null on some rows of an entity and set on
                # others; take the first non-null one rather than whichever
                # row happened to come back first.
                group.label = str(row[cols.label])

            group.rows_seen += 1
            group.periods.append(period)

            response = _finite_float(row.get(cols.response))
            predictor = period if cols.predictor == cols.period else _finite_float(
                row.get(cols.predictor)
            )
            if response is not None and predictor is not None:
                group.x.append(predictor)
                group.y.append(response)

        self.rows_unplaceable = unplaceable

        # Sorted by key so the order a client sees units arrive in is stable
        # across runs, and so a resumed or re-run job produces the same
        # chunking. A dict's insertion order would depend on how the rows came
        # back, which SQL does not promise.
        groups = [by_key[key] for key in sorted(by_key)]
        if self.max_groups is not None:
            groups = groups[: self.max_groups]
        self._groups = groups

        self._log(
            f"{len(groups)} groups over {len(data.rows)} rows "
            f"({unplaceable} rows dropped for a missing "
            f"{cols.group}/{cols.period}); fitting "
            f"{cols.response} ~ poly({cols.predictor}, {self.degree}), "
            f"minimum {self.min_observations} observations per group",
            phase="input",
        )
        return groups

    # --- the run ----------------------------------------------------------

    def run(self) -> str | None:
        groups = self._ensure_groups()
        total = len(groups)
        started = time.monotonic()

        self.group_rows = []
        self.groups_fitted = self.groups_failed = 0
        self.failure_counts = Counter()
        self.chunks_emitted = self.rows_emitted = 0
        self.cancelled = False

        buffer: list[dict[str, Any]] = []
        r_squared_seen: list[float] = []
        failures_logged = 0

        if total == 0:
            self._log(
                "no groups in the panel: every row was missing a group key or a period",
                level="WARNING",
                phase="input",
            )

        for index, group in enumerate(groups):
            # Between groups, never mid-fit. A fit of a few dozen points is
            # microseconds; interrupting one would buy nothing and cost a
            # half-written row.
            if self._cancelled():
                self.cancelled = True
                self._log(
                    f"cancelled after {index} of {total} groups; keeping every group "
                    f"already fitted ({self.groups_fitted} fitted, {self.groups_failed} failed)"
                )
                break

            row = self._fit_group(group, total)
            self.group_rows.append(row)
            buffer.append(row)

            if row["status"] == STATUS_FITTED:
                self.groups_fitted += 1
                if row["r_squared"] is not None:
                    r_squared_seen.append(row["r_squared"])
            else:
                self.groups_failed += 1
                self.failure_counts[row["failure_reason"]] += 1
                if failures_logged < self.failure_log_limit:
                    failures_logged += 1
                    self._log(
                        f"{group.key}: {row['failure_reason']} "
                        f"({row['n_observations']} usable observations)",
                        level="WARNING",
                        phase="fit",
                    )
                elif failures_logged == self.failure_log_limit:
                    failures_logged += 1
                    self._log(
                        f"further per-group failures will not be logged; every one is "
                        f"in {self.results_table} with its reason",
                        level="WARNING",
                        phase="fit",
                    )

            done = index + 1
            # The last group always reports, so `percent_complete` lands on
            # 100 rather than stopping at 97 — a curve that stops short reads
            # as a run that died.
            if done % self.progress_every == 0 or done == total:
                self._progress(done, total, time.monotonic() - started, r_squared_seen, group, row)

            # Flush on the chunk boundary, but never on the last group: the
            # trailing flush below owns `final=True` and an empty final chunk
            # says less than a full one.
            if len(buffer) >= self.chunk_size and done < total:
                self._flush(buffer, total, final=False)
                buffer = []

        # Always exactly one `final=True` per run, on every path — completed,
        # cancelled, or a panel with no groups at all. A client's "results are
        # complete" condition has to be reachable even when there is nothing
        # to report, or it waits forever.
        self._flush(buffer, total, final=True)

        return self._terminal_status(total, r_squared_seen)

    def _terminal_status(self, total: int, r_squared_seen: list[float]) -> str | None:
        median = statistics.median(r_squared_seen) if r_squared_seen else None
        self._log(
            f"{self.groups_fitted} of {total} groups fitted, {self.groups_failed} failed"
            + (f" ({_counts_phrase(self.failure_counts)})" if self.failure_counts else "")
            + (f"; median R-squared {median:.4f}" if median is not None else ""),
            phase="results",
        )

        if self.cancelled:
            # Cancellation is the harness's call, not the model's — it
            # overrides whatever is returned here. Returning a status anyway
            # would be a claim about a run that never finished asking.
            return None

        if self.groups_fitted == 0:
            # **Every group failed.** Not SUCCEEDED: a run with 180 failure
            # rows and zero fits is not a success, and `row_count` cannot
            # distinguish it either — failures are recorded, so the count is a
            # healthy-looking 180. Not FAILED either: nothing went wrong, the
            # computation ran to completion, the results are correct and
            # durable, and a retry would deterministically produce the same
            # thing. INFEASIBLE is the member that already means exactly this
            # — "it ran, and the answer is that there isn't one" — which is
            # what the Gurobi models return when no feasible solution exists.
            #
            # It must be spelled as a real `RunStatus` member: a returned
            # string that is not one silently becomes a *detail* on a
            # SUCCEEDED run (`job/drivers/self_driving.py`), so a typo here
            # would degrade into the very ambiguity this decision removes.
            self._log(
                "no group could be fitted; reporting INFEASIBLE rather than a success "
                "with nothing in it" if total else "the panel had no groups to fit",
                level="ERROR",
                phase="results",
            )
            return "INFEASIBLE"

        return None

    def _cancelled(self) -> bool:
        return self.should_cancel is not None and self.should_cancel()

    # --- one group --------------------------------------------------------

    def _fit_group(self, group: _Group, total: int) -> dict[str, Any]:
        n = len(group.x)
        reason, coefficients, r_squared, rmse = self._fit(group.x, group.y)

        # `status` is two-valued and `failure_reason` carries the detail.
        # `uc_ddl/002_model_results.sql` comments status as "'fitted' or a
        # failure reason", which would make `failure_reason` a duplicate of
        # `status` on every failed row and leave nothing that answers "how
        # many failed" without enumerating the reason set. Two columns, two
        # jobs: `status` groups into two buckets, `failure_reason` groups into
        # four. Flagged rather than changed — the DDL is another track's file.
        status = STATUS_FAILED if reason else STATUS_FITTED

        # Periods of the observations actually fitted; the group's whole span
        # when there were none, so a `too_few_observations` row still says
        # when the group existed rather than showing two nulls.
        span = group.x if n else group.periods
        return {
            "group_key": group.key,
            "group_label": group.label,
            "n_observations": n,
            "first_period": min(span) if span else None,
            "last_period": max(span) if span else None,
            "status": status,
            "failure_reason": reason,
            # Not rounded. A coefficient's scale depends on the predictor's
            # — life expectancy per *dollar* of GDP per capita is legitimately
            # around 1e-4 — so a fixed number of decimal places would zero out
            # real slopes on some configurations and not others.
            "intercept": coefficients[0] if coefficients else None,
            "slope": coefficients[1] if coefficients else None,
            #: Increasing powers: c0 + c1*x + c2*x^2 + ... A delimited string
            #: rather than N columns because `degree` is configurable and a
            #: table whose shape moves with the config is a table nothing can
            #: query.
            "coefficients": ",".join(f"{c:.10g}" for c in coefficients) if coefficients else None,
            "degree": self.degree,
            # R-squared is bounded above by 1 by construction, so rounding it
            # is scale-safe; RMSE is on the response's scale and is not.
            "r_squared": None if r_squared is None else round(r_squared, 8),
            "rmse": rmse,
            "groups_total": total,
            # Filled in at flush time — see `_flush`.
            "groups_fitted": None,
            "groups_failed": None,
            "response": self.columns.response,
            "predictor": self.columns.predictor,
            # Provenance on the rows, not only in a log: logs are droppable by
            # contract, and "was this real data?" has to survive to the
            # durable record.
            **self._provenance,
        }

    def _fit(
        self, x: list[float], y: list[float]
    ) -> tuple[str | None, list[float] | None, float | None, float | None]:
        """`(failure_reason, coefficients, r_squared, rmse)`.

        The checks run in this order because the specific condition has to be
        reported before the general one that subsumes it: a group with one
        distinct predictor value *is* a rank-deficient design, but
        `zero_predictor_variance` tells a reader what to do about it and
        `singular_design` does not.
        """
        import numpy as np

        n = len(x)
        if n < self.min_observations:
            return REASON_TOO_FEW_OBSERVATIONS, None, None, None

        xs = np.asarray(x, dtype=float)
        ys = np.asarray(y, dtype=float)
        distinct = int(np.unique(xs).size)
        if distinct == 1:
            return REASON_ZERO_PREDICTOR_VARIANCE, None, None, None
        # A Vandermonde matrix over `distinct` distinct values has rank
        # min(distinct, degree + 1), so this is exact rather than a heuristic:
        # you cannot fit a parabola through two distinct x.
        if distinct <= self.degree:
            return REASON_SINGULAR_DESIGN, None, None, None

        # Raw predictor values, not centred. `intercept` and `slope` then mean
        # exactly what their column names say against the raw predictor, so a
        # reader can apply a stored coefficient without knowing about a hidden
        # offset. The cost is conditioning at high degree on large predictors
        # (year squared is ~4e6), and the point of the two checks below is
        # that this surfaces as a recorded failure rather than a quietly
        # wrong fit.
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            design = np.vander(xs, self.degree + 1, increasing=True)
            try:
                coef, _residuals, rank, _sv = np.linalg.lstsq(design, ys, rcond=None)
            except np.linalg.LinAlgError:
                # "SVD did not converge" — a design nothing can decompose.
                return REASON_SINGULAR_DESIGN, None, None, None
            if int(rank) < self.degree + 1:
                # Numerically deficient although `distinct` said otherwise:
                # predictor values that differ only below machine precision.
                return REASON_SINGULAR_DESIGN, None, None, None

            residuals = ys - design @ coef
            ss_res = float(residuals @ residuals)
            centred = ys - float(ys.mean())
            ss_tot = float(centred @ centred)
            rmse = float(np.sqrt(ss_res / n))
            # A group whose response never moves has no variance to explain,
            # so R-squared is undefined rather than zero or one. The fit
            # itself is perfectly good (a flat line through a constant), so
            # this is a null metric on a *fitted* row, not a failure.
            r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0.0 else None

        coefficients = [float(c) for c in coef]
        finite = (
            all(math.isfinite(c) for c in coefficients)
            and math.isfinite(rmse)
            and (r_squared is None or math.isfinite(r_squared))
        )
        if not finite:
            return REASON_NON_FINITE_RESULT, None, None, None

        return None, coefficients, r_squared, rmse

    # --- telemetry --------------------------------------------------------

    def _flush(self, rows: list[dict[str, Any]], total: int, *, final: bool) -> None:
        """Emit one result chunk. The harness assigns `chunk_index`, counts
        the rows into `row_count` and builds the preview; this model supplies
        none of the three.

        `groups_fitted` / `groups_failed` are stamped here rather than at fit
        time, so every row in a chunk carries the same, consistent pair: the
        counts **as of the end of this chunk**. They cannot be run totals —
        chunk 0 is written long before the run has any — and the run totals
        are in the table by construction anyway, as
        `COUNT(*) WHERE status = 'fitted'`. These columns are a denormalised
        convenience, and this is the only honest denormalisation available to
        a writer that streams.
        """
        for row in rows:
            row["groups_fitted"] = self.groups_fitted
            row["groups_failed"] = self.groups_failed
            row["groups_total"] = total

        if self.emit is None:
            return
        self.emit("result", rows=rows, final=final)
        self.chunks_emitted += 1
        self.rows_emitted += len(rows)

    def _progress(
        self,
        done: int,
        total: int,
        elapsed: float,
        r_squared_seen: list[float],
        group: _Group,
        row: dict[str, Any],
    ) -> None:
        if self.emit is None:
            return
        self.emit(
            "progress",
            elapsed_seconds=elapsed,
            # Groups completed over groups total. No estimation, no heuristic
            # — the denominator is known before the first fit.
            percent_complete=100.0 * done / total if total else 100.0,
            # Median, not mean: one pathological group should not move the
            # headline number of a 180-group run. Null until something has
            # been fitted, which is legal and honest.
            primary_metric=statistics.median(r_squared_seen) if r_squared_seen else None,
            primary_metric_label="median_r_squared",
            payload={
                "groups_done": done,
                "groups_total": total,
                # The split no other model on this platform reports, on every
                # single progress message: a client must be able to tell a
                # healthy run from one quietly failing a third of its units
                # without waiting for the results.
                "groups_fitted": self.groups_fitted,
                "groups_failed": self.groups_failed,
                "failure_counts": dict(self.failure_counts),
                # Which unit this message is about.
                "group_key": group.key,
                "group_label": group.label,
                "group_status": row["status"],
                "group_failure_reason": row["failure_reason"],
                "group_r_squared": row["r_squared"],
                "n_observations": row["n_observations"],
                "metric_higher_is_better": True,
                "degree": self.degree,
                "chunks_emitted": self.chunks_emitted,
                **self._provenance,
            },
        )

    def _log(self, message: str, *, phase: str = "fit", level: str = "INFO") -> None:
        if self.emit is not None:
            self.emit("log", message=message, source="model", phase=phase, level=level)


# --- helpers ---------------------------------------------------------------


def _finite_float(value: Any) -> float | None:
    """A usable number, or None. Never raises.

    Every column in a real panel is nullable and a real `AVG()` over an empty
    period returns NULL, so `float(None)` from deep inside a fit is a failure
    that only ever happens on a workspace — the worst shape a bug can have.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _counts_phrase(counts: Counter[str]) -> str:
    return ", ".join(f"{reason} {n}" for reason, n in sorted(counts.items()))


def build_model(config: dict[str, Any] | None = None) -> PanelFitModel:
    return PanelFitModel(config)
