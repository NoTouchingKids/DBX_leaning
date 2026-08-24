"""A Bayesian A/B test in closed form — the lineup's non-iterative model.

``models/mcmc/`` is a sampler stress test: many chains, high-frequency draws,
convergence diagnostics. This one is the opposite inference shape. There is no
sampler. The posteriors are conjugate and land in one line of arithmetic, the
whole run finishes in milliseconds, and what its telemetry is *about* is a
decision — is B better than A, by how much, and what does guessing wrong cost —
rather than whether an iteration has settled down.

That makes it a different exercise for the envelope. ``percent_complete`` is a
stage counter over five named steps, not a curve anyone would watch.
``primary_metric`` is a probability in [0, 1] rather than an error or a gap.
The results are a three-row decision table, not a series, so there is nothing
for ``preview_axes``/LTTB to downsample.

The Bayesian arithmetic lives in :mod:`models.bayesian_ab.conjugate`, which
documents which quantities are exact and which are approximated.

The model
---------

**Beta-Binomial, conjugate.** Each arm's outcome is a genuine per-unit binary
event, so the likelihood is Binomial in that arm's success count; the Beta is
the conjugate prior for a Binomial rate, and the posterior is
``Beta(alpha + successes, beta + failures)`` with no sampler and no
approximation. The default prior is ``Beta(1, 1)`` — uniform on [0, 1], no
thumb on the scale, and worth a fraction of one observation against arms of
hundreds. Jeffreys' ``Beta(0.5, 0.5)`` is available via config and the
arithmetic handles it, at the cost of falling back to quadrature for one
integral.

What the arms are
-----------------

Two comparisons, both derived from real observations in ``models/_data``
(Databricks' ``samples`` catalog on a workspace, a deterministic generator
off it — and provenance says which, in the logs *and* on every result row):

``weekend_fare`` (default)
    Hourly taxi demand. Arm A is weekday hours, arm B is weekend hours, split
    on the actual calendar weekday of each hour. Success is "this hour's
    average fare was above the pooled median".

``long_trip_speed``
    Individual trips. Arm A is trips under ``split_miles``, arm B is trips at
    or over it. Success is "the trip averaged more than ``speed_threshold_mph``
    miles per hour" — a fixed, externally meaningful cutoff (roughly a New York
    taxi's average speed), not one read off the data.

Honest limitations
------------------

These are stated because the alternative is a decision table that reads more
certain than the method supports.

1. **This is not a randomised experiment.** Nothing assigned an hour to the
   weekend or a trip to being long; the arms are defined by observing the data.
   So ``P(B > A)`` is a statement about two observed populations under this
   model. It is not the causal effect of anything, and the usual A/B vocabulary
   ("lift", "which variant wins") is borrowed, not earned.

2. **The continuous quantity is deliberately thrown away.** Average fare and
   average speed are continuous. Dichotomising them at a threshold to get a
   Bernoulli outcome the Beta-Binomial genuinely fits means answering a coarser
   question — "is this hour in the expensive half?" rather than "is the mean
   fare higher?" — and losing power in the process. That is a real cost, taken
   knowingly: it is better than fitting a rate model to a continuous quantity
   and calling the result a rate. A Normal-Normal conjugate model on the raw
   values would use more of the data and answer the sharper question; it is the
   obvious upgrade if this model ever stops being a platform exercise.

3. **Observations are not independent.** Consecutive hours are strongly
   autocorrelated (a busy, expensive hour is followed by another), and trips
   cluster by driver, hour and weather. The Binomial likelihood treats trials
   as exchangeable and independent, so the effective sample size is smaller
   than the trial count and the posterior intervals here are **too narrow** —
   the direction of the error is known even though the size is not.

4. **The ``weekend_fare`` threshold is chosen from the data being tested.** The
   pooled median makes the comparison scale-free and works on any data, but it
   couples the arms: their rates are constrained to average about 0.5, so a
   high estimate for one forces a low one for the other. Pass ``fare_threshold``
   to fix an external cutoff instead. ``long_trip_speed`` has one already,
   which is the better-behaved of the two comparisons statistically.

5. **With thousands of observations, a real difference is decisive.**
   ``P(B > A)`` saturates at 0 or 1 long before the effect is interesting.
   Where that happens the numbers worth reading are the lift and the expected
   loss, not the tail probability — which is precisely why the decision rule
   below consults both.

The decision rule
-----------------

An arm leads when ``P(B > A)`` clears ``decision_threshold`` (or falls below
its complement). The call is *conclusive* only if the leader's expected loss is
also under ``loss_tolerance`` — the rate you would forgo, on average, by
picking it and being wrong. Probability alone says which arm is ahead; expected
loss says whether being ahead matters.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from models._data import Dataset, epoch_ms, nyc_taxi_hourly, nyc_taxi_trips

from .conjugate import Beta, difference_summary, expected_loss, prob_greater

__all__ = ["BayesianAbModel", "Arm", "COMPARISONS", "STAGES", "build_model"]

#: What ``data`` is called when the caller supplied it instead of the loader.
CALLER_SUPPLIED = "caller-supplied"

#: The named comparisons. See the module docstring for what each one asks.
COMPARISONS = ("weekend_fare", "long_trip_speed")

#: Progress here is a stage counter, not an iteration curve — five named steps,
#: each cheap, each a point at which cancellation is checked. The order matters:
#: everything before ``lift_interval`` is exact arithmetic, so a run cancelled
#: partway keeps genuinely finished numbers rather than half-converged ones.
STAGES = ("posteriors", "comparison", "expected_loss", "lift_interval", "decision")

#: Which role gets which label in the results table.
ROLE_A, ROLE_B = "A", "B"


class Arm:
    """One side of the comparison: a label, a count of trials and successes."""

    __slots__ = ("label", "role", "trials", "successes", "posterior")

    def __init__(self, label: str, role: str, trials: int, successes: int):
        if successes < 0 or trials < successes:
            raise ValueError(f"arm {label!r}: {successes} successes out of {trials} trials")
        self.label = label
        self.role = role
        self.trials = int(trials)
        self.successes = int(successes)
        self.posterior: Beta | None = None

    @property
    def failures(self) -> int:
        return self.trials - self.successes

    @property
    def observed_rate(self) -> float | None:
        """The raw proportion, or None with no trials — deliberately not 0.0,
        which would read as "we measured zero" rather than "we measured
        nothing"."""
        return self.successes / self.trials if self.trials else None

    def fit(self, prior_alpha: float, prior_beta: float) -> Beta:
        self.posterior = Beta(prior_alpha + self.successes, prior_beta + self.failures)
        return self.posterior

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Arm({self.label!r}, {self.successes}/{self.trials})"


class BayesianAbModel:
    results_table = "results_bayesian_ab"
    # No `preview_axes`, deliberately. LTTB downsamples a series; this model's
    # results are three rows of a decision table with no x-axis, so the
    # harness's bounded preview carries all of them as they are.

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})

        self.comparison = str(cfg.get("comparison", COMPARISONS[0]))
        if self.comparison not in COMPARISONS:
            raise ValueError(
                f"unknown comparison {self.comparison!r}; expected one of {COMPARISONS}"
            )

        #: Pre-counted arms, bypassing the data path entirely. Two dicts of
        #: ``{label, trials, successes}``. This is how a caller brings its own
        #: experiment, and how the tests pin the arithmetic to hand-calculable
        #: cases.
        self.arms_config: Any = cfg.get("arms")
        #: Raw rows for the chosen comparison, instead of loading them.
        self.data: Any = cfg.get("data")

        self.hours = int(cfg.get("hours", 60 * 24))     # weekend_fare
        self.rows = int(cfg.get("rows", 2000))          # long_trip_speed
        self.data_seed = int(cfg.get("data_seed", 7))

        self.prior_alpha = float(cfg.get("prior_alpha", 1.0))
        self.prior_beta = float(cfg.get("prior_beta", 1.0))
        if self.prior_alpha <= 0 or self.prior_beta <= 0:
            raise ValueError("prior_alpha and prior_beta must both be positive")

        self.credible_mass = float(cfg.get("credible_mass", 0.95))
        self.decision_threshold = float(cfg.get("decision_threshold", 0.95))
        #: In units of the rate itself: 0.002 means "I do not care about a
        #: quarter of a percentage point of expected regret".
        self.loss_tolerance = float(cfg.get("loss_tolerance", 0.002))

        #: None means "the pooled median", which is the honest default but a
        #: data-derived one — see limitation 4 in the module docstring.
        self.fare_threshold: float | None = (
            None if cfg.get("fare_threshold") is None else float(cfg["fare_threshold"])
        )
        self.speed_threshold_mph = float(cfg.get("speed_threshold_mph", 12.0))
        self.split_miles = float(cfg.get("split_miles", 2.0))

        self.emit: Callable[..., Any] | None = None
        self.should_cancel: Callable[[], bool] | None = None

        self.dataset: Dataset | None = None
        self.arms: list[Arm] = []
        self.outcome: str = ""
        self.stages_done = 0
        self.cancelled = False

        self.prob_b_beats_a: float | None = None
        self.losses: tuple[float, float] | None = None
        self.lift: dict[str, Any] | None = None
        self.decision: str | None = None
        self.conclusive: bool | None = None

    # --- data -------------------------------------------------------------

    def build(self) -> None:
        """Load the observations and count the two arms.

        Loading happens here rather than in ``__init__`` because the harness
        wires ``emit`` after constructing the model — a provenance log emitted
        from a constructor goes nowhere.
        """
        if self.arms_config is not None:
            self.arms = self._arms_from_counts(self.arms_config)
            self.outcome = "caller-supplied successes and trials"
            self.dataset = Dataset(
                rows=[{"trials": arm.trials, "successes": arm.successes} for arm in self.arms],
                source=CALLER_SUPPLIED,
                synthetic=False,
            )
        else:
            dataset = self._load()
            self.dataset = dataset
            # Loud when it is a fallback: a run that quietly compared synthetic
            # hours must not read like one that compared real ones.
            self._log(
                dataset.provenance,
                phase="input",
                level="WARNING" if dataset.synthetic else "INFO",
            )
            builder = (
                self._weekend_fare_arms
                if self.comparison == "weekend_fare"
                else self._long_trip_speed_arms
            )
            self.arms, self.outcome = builder(dataset)

        self._log(f"comparison {self.comparison!r}: success = {self.outcome}", phase="input")
        for arm in self.arms:
            rate = arm.observed_rate
            observed = "no observations" if rate is None else f"observed rate {rate:.4f}"
            self._log(
                f"arm {arm.role} {arm.label!r}: {arm.successes}/{arm.trials}, {observed}",
                phase="input",
                level="WARNING" if arm.trials == 0 else "INFO",
            )
        self._log(
            f"prior Beta({self.prior_alpha:g}, {self.prior_beta:g}) on each arm's rate; "
            "posteriors are conjugate, so no sampling is required",
            phase="input",
        )

    def _load(self) -> Dataset:
        if self.data is not None:
            if isinstance(self.data, Dataset):
                return self.data
            rows = [dict(row) for row in self.data if isinstance(row, Mapping)]
            return Dataset(rows=rows, source=CALLER_SUPPLIED, synthetic=False)
        if self.comparison == "weekend_fare":
            return nyc_taxi_hourly(days=max(1, self.hours // 24), seed=self.data_seed)
        return nyc_taxi_trips(limit=self.rows, seed=self.data_seed)

    def _arms_from_counts(self, supplied: Any) -> list[Arm]:
        items = list(supplied)
        if len(items) != 2:
            raise ValueError(f"expected exactly two arms, got {len(items)}")
        roles = (ROLE_A, ROLE_B)
        return [
            Arm(
                label=str(item.get("label", f"arm_{role.lower()}")),
                role=role,
                trials=int(item["trials"]),
                successes=int(item["successes"]),
            )
            for item, role in zip(items, roles, strict=True)
        ]

    def _weekend_fare_arms(self, dataset: Dataset) -> tuple[list[Arm], str]:
        """Weekday hours vs weekend hours, on whether the hour was expensive."""
        usable = dataset.dropna("hour_ts", "avg_fare")
        fares = usable.floats("avg_fare")
        threshold = (
            self.fare_threshold
            if self.fare_threshold is not None
            else (statistics.median(fares) if fares else 0.0)
        )
        counts = {ROLE_A: [0, 0], ROLE_B: [0, 0]}   # [trials, successes]
        for row, fare in zip(usable.rows, fares, strict=True):
            weekday = datetime.fromtimestamp(epoch_ms(row["hour_ts"]) / 1000.0, UTC).weekday()
            role = ROLE_B if weekday >= 5 else ROLE_A
            counts[role][0] += 1
            counts[role][1] += int(fare > threshold)

        source = "fixed" if self.fare_threshold is not None else "pooled median"
        return (
            [
                Arm("weekday_hours", ROLE_A, counts[ROLE_A][0], counts[ROLE_A][1]),
                Arm("weekend_hours", ROLE_B, counts[ROLE_B][0], counts[ROLE_B][1]),
            ],
            f"the hour's average fare exceeded {threshold:.4f} ({source})",
        )

    def _long_trip_speed_arms(self, dataset: Dataset) -> tuple[list[Arm], str]:
        """Short trips vs long trips, on whether the trip beat a fixed speed."""
        usable = dataset.dropna("trip_distance", "duration_min")
        distances = usable.floats("trip_distance")
        durations = usable.floats("duration_min")
        counts = {ROLE_A: [0, 0], ROLE_B: [0, 0]}
        for distance, duration in zip(distances, durations, strict=True):
            if duration <= 0:
                # A zero-length trip has no speed. Dropping it is the only
                # honest option; `_data` already filters these on the real
                # path, so this is for caller-supplied rows.
                continue
            role = ROLE_B if distance >= self.split_miles else ROLE_A
            counts[role][0] += 1
            counts[role][1] += int(distance / (duration / 60.0) > self.speed_threshold_mph)

        return (
            [
                Arm(f"under_{self.split_miles:g}mi", ROLE_A, counts[ROLE_A][0], counts[ROLE_A][1]),
                Arm(f"over_{self.split_miles:g}mi", ROLE_B, counts[ROLE_B][0], counts[ROLE_B][1]),
            ],
            f"the trip averaged more than {self.speed_threshold_mph:g} mph "
            "(a fixed cutoff, not read off the data)",
        )

    # --- inference --------------------------------------------------------

    def run(self) -> None:
        """Five closed-form stages, cancellable between each.

        The whole thing takes milliseconds, which is exactly why the
        cancellation check is here anyway: a harness that only honours
        cancellation on models slow enough to notice is a harness with a race
        in it, not a fast model.
        """
        started = time.monotonic()
        for index, stage in enumerate(STAGES, start=1):
            if self.should_cancel is not None and self.should_cancel():
                self.cancelled = True
                self._log(
                    f"cancelled before the {stage!r} stage; keeping "
                    f"{self.stages_done} of {len(STAGES)} completed stages",
                    level="WARNING",
                )
                break
            getattr(self, f"_stage_{stage}")()
            self.stages_done = index
            self._progress(stage, index, time.monotonic() - started)

    def _stage_posteriors(self) -> None:
        for arm in self.arms:
            arm.fit(self.prior_alpha, self.prior_beta)

    def _stage_comparison(self) -> None:
        a, b = self._posteriors()
        self.prob_b_beats_a = prob_greater(a.alpha, a.beta, b.alpha, b.beta)

    def _stage_expected_loss(self) -> None:
        a, b = self._posteriors()
        self.losses = expected_loss(a.alpha, a.beta, b.alpha, b.beta)

    def _stage_lift_interval(self) -> None:
        a, b = self._posteriors()
        self.lift = difference_summary(
            a.alpha, a.beta, b.alpha, b.beta, mass=self.credible_mass
        )

    def _stage_decision(self) -> None:
        prob = self.prob_b_beats_a
        losses = self.losses
        assert prob is not None and losses is not None   # staged in order
        loss_a, loss_b = losses

        if prob >= self.decision_threshold:
            leader, leader_loss = self.arms[1], loss_b
        elif prob <= 1.0 - self.decision_threshold:
            leader, leader_loss = self.arms[0], loss_a
        else:
            leader, leader_loss = None, min(loss_a, loss_b)

        self.conclusive = leader is not None and leader_loss <= self.loss_tolerance
        self.decision = leader.label if (leader is not None and self.conclusive) else "inconclusive"
        self._log(
            f"P(B>A) = {prob:.6f}, expected loss A = {loss_a:.6f}, B = {loss_b:.6f} "
            f"-> {self.decision}",
            phase="results",
        )

    def _posteriors(self) -> tuple[Beta, Beta]:
        a, b = self.arms[0].posterior, self.arms[1].posterior
        if a is None or b is None:  # pragma: no cover - stages run in order
            raise RuntimeError("posteriors have not been fitted yet")
        return a, b

    # --- telemetry --------------------------------------------------------

    def _progress(self, stage: str, index: int, elapsed: float) -> None:
        if self.emit is None:
            return
        self.emit(
            "progress",
            elapsed_seconds=elapsed,
            # Honest for what it is: a count of completed stages. There is no
            # iteration to be part-way through, so a generic view watching this
            # sees five steps rather than a curve.
            percent_complete=100.0 * index / len(STAGES),
            # Null until the comparison stage computes it. A probability, not
            # an error or a gap — the label says which way to read it.
            primary_metric=self.prob_b_beats_a,
            primary_metric_label="prob_b_beats_a",
            payload=self._payload(stage, index),
        )

    def _payload(self, stage: str, index: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stage": stage,
            "stage_index": index,
            "stages_total": len(STAGES),
            # So a progress view knows not to draw a line through these points.
            "progress_shape": "stages",
            "comparison": self.comparison,
            "outcome": self.outcome,
            "prior": {"alpha": self.prior_alpha, "beta": self.prior_beta},
            "credible_mass": self.credible_mass,
            "arms": [
                {
                    "role": arm.role,
                    "label": arm.label,
                    "trials": arm.trials,
                    "successes": arm.successes,
                    # The posterior parameters themselves: with these and the
                    # prior, a richer view can redraw both densities without
                    # another round trip.
                    "posterior_alpha": None if arm.posterior is None else arm.posterior.alpha,
                    "posterior_beta": None if arm.posterior is None else arm.posterior.beta,
                    "posterior_mean": None if arm.posterior is None else arm.posterior.mean,
                }
                for arm in self.arms
            ],
        }
        if self.prob_b_beats_a is not None:
            payload["prob_b_beats_a"] = self.prob_b_beats_a
        if self.losses is not None:
            payload["expected_loss"] = {ROLE_A: self.losses[0], ROLE_B: self.losses[1]}
        if self.lift is not None:
            payload["lift"] = dict(self.lift)
        if self.decision is not None:
            payload["decision"] = self.decision
            payload["conclusive"] = self.conclusive
        return payload

    def _log(self, message: str, *, phase: str = "run", level: str = "INFO") -> None:
        if self.emit is not None:
            self.emit("log", message=message, level=level, source="model", phase=phase)

    # --- results ----------------------------------------------------------

    def results(self) -> list[dict[str, Any]]:
        """One row per arm, plus the comparison. Three rows, one schema.

        A cancelled run keeps whatever it reached: the arm rows appear as soon
        as the posteriors exist, the comparison row only once ``P(B > A)`` has
        actually been computed, and every row carries ``complete`` so a reader
        can tell a finished decision from a stopped one. If nothing was fitted
        at all there is nothing to write, and that is what zero rows means.
        """
        if not self.arms or any(arm.posterior is None for arm in self.arms):
            return []

        complete = self.stages_done == len(STAGES)
        rows = [self._arm_row(index, complete) for index in (0, 1)]
        if self.prob_b_beats_a is not None:
            rows.append(self._comparison_row(complete))
        return rows

    def _shared(self, complete: bool) -> dict[str, Any]:
        # Seeded so the caller-supplied path produces the same columns as a
        # loaded one; Dataset.describe() always includes every key.
        provenance: dict[str, Any] = {
            "data_source": CALLER_SUPPLIED,
            "data_synthetic": False,
            "data_rows": 0,
            "data_fallback_reason": None,
        }
        if self.dataset is not None:
            provenance.update(self.dataset.describe())
        return {
            "comparison": self.comparison,
            "outcome": self.outcome,
            "prior_alpha": self.prior_alpha,
            "prior_beta": self.prior_beta,
            "credible_mass": self.credible_mass,
            # Run-level facts, repeated on every row: one flat table beats a
            # header/detail join for a reader with a SQL prompt and a question,
            # and it means a single row is still self-describing.
            "decision": self.decision,
            "conclusive": self.conclusive,
            "complete": complete,
            **provenance,
        }

    def _arm_row(self, index: int, complete: bool) -> dict[str, Any]:
        arm = self.arms[index]
        posterior = arm.posterior
        assert posterior is not None   # guarded in results()
        low, high = posterior.interval(self.credible_mass)

        prob_beats_other = None
        if self.prob_b_beats_a is not None:
            prob_beats_other = (
                1.0 - self.prob_b_beats_a if arm.role == ROLE_A else self.prob_b_beats_a
            )
        loss = None if self.losses is None else self.losses[index]

        return {
            "row_type": "arm",
            "role": arm.role,
            "label": arm.label,
            "trials": arm.trials,
            "successes": arm.successes,
            "observed_rate": _round(arm.observed_rate),
            "posterior_alpha": posterior.alpha,
            "posterior_beta": posterior.beta,
            "posterior_mean": _round(posterior.mean),
            "posterior_sd": _round(posterior.sd),
            "ci_low": _round(low),
            "ci_high": _round(high),
            "prob_beats_other": _round(prob_beats_other),
            "expected_loss": _round(loss),
            **self._shared(complete),
        }

    def _comparison_row(self, complete: bool) -> dict[str, Any]:
        """The lift, B minus A.

        ``posterior_alpha``/``posterior_beta`` are null here and that is not an
        omission: the difference of two Betas is not a Beta, so there are no
        such parameters to report. Its mean and sd are exact; its interval
        comes off the grid convolution described in
        :func:`~models.bayesian_ab.conjugate.difference_summary`.
        """
        a, b = self.arms
        lift = self.lift
        # The regret carried by whichever arm is nominally ahead. That is
        # always the smaller of the two losses, conclusive or not.
        loss = None if self.losses is None else min(self.losses)

        return {
            "row_type": "comparison",
            "role": "B_minus_A",
            "label": f"{b.label}_vs_{a.label}",
            "trials": a.trials + b.trials,
            "successes": a.successes + b.successes,
            "observed_rate": None,
            "posterior_alpha": None,
            "posterior_beta": None,
            "posterior_mean": _round(None if lift is None else lift["mean"]),
            "posterior_sd": _round(None if lift is None else lift["sd"]),
            "ci_low": _round(None if lift is None else lift["ci_low"]),
            "ci_high": _round(None if lift is None else lift["ci_high"]),
            "prob_beats_other": _round(self.prob_b_beats_a),
            "expected_loss": _round(loss),
            **self._shared(complete),
        }


def _round(value: float | None, places: int = 8) -> float | None:
    """Round for the results table, keeping None as None.

    Eight places: these are probabilities, and a P(B>A) of 0.99999999 and one
    of 1.0 are different answers that a decision table must not merge.
    """
    return None if value is None else round(float(value), places)


def build_model(config: dict[str, Any] | None = None) -> BayesianAbModel:
    return BayesianAbModel(config)
