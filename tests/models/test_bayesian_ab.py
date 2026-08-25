"""Conjugate Bayesian A/B test — the model with no sampler in it.

The point of this suite is that the numbers are *right*, not merely present.
Everything the model reports has a closed form, so most of what follows is a
comparison against a value worked out by hand rather than a smoke test with
generous tolerances:

- ``P(X2 > X1)`` for uniforms and for Beta(2,1) against Beta(1,1) — 1/2, 1/3,
  2/3, each a one-line integral.
- expected loss between two uniforms — E[(U2-U1)+] = 1/6.
- the credible interval of the difference of two uniforms — the symmetric
  triangular distribution, whose 2.5% point is 1/sqrt(20) - 1 = -0.7764.
- Beta(1,1) is uniform, so its 95% interval is exactly (0.025, 0.975).

The rest checks the platform contract: provenance in and out, stage-shaped
progress that a non-Bayesian view can still render, and cancellation keeping a
partial decision table.
"""

from __future__ import annotations

import math

import pytest

pytest.importorskip("numpy", reason="needs the [mcmc] extra")

from job.models.bayesian_ab import (  # noqa: E402
    COMPARISONS,
    STAGES,
    Beta,
    beta_quantile,
    build_model,
    difference_summary,
    expected_loss,
    prob_greater,
    regularized_incomplete_beta,
)

UNIFORM = (1.0, 1.0)


def fitted(recorder, **config):
    r = recorder()
    model = r.attach(build_model(config))
    model.build()
    model.run()
    return r, model


def counted(recorder, a: tuple[int, int], b: tuple[int, int], **config):
    """Run against arms given as ``(successes, trials)`` — the analytic path."""
    return fitted(
        recorder,
        arms=[
            {"label": "control", "successes": a[0], "trials": a[1]},
            {"label": "variant", "successes": b[0], "trials": b[1]},
        ],
        **config,
    )


def rows_by_label(model) -> dict[str, dict]:
    return {row["label"]: row for row in model.results()}


# --- the arithmetic, against answers calculable by hand ---------------------


def test_the_incomplete_beta_matches_its_elementary_cases():
    # I_x(1,1) = x; I_x(2,1) = x^2; I_x(1,2) = 1-(1-x)^2. All by direct
    # integration of a polynomial density.
    for x in (0.01, 0.25, 0.5, 0.75, 0.99):
        assert regularized_incomplete_beta(x, 1, 1) == pytest.approx(x, abs=1e-12)
        assert regularized_incomplete_beta(x, 2, 1) == pytest.approx(x**2, abs=1e-12)
        assert regularized_incomplete_beta(x, 1, 2) == pytest.approx(1 - (1 - x) ** 2, abs=1e-12)


def test_the_incomplete_beta_is_symmetric_about_a_symmetric_beta():
    # Beta(a, a) is symmetric about 1/2, so I_x + I_{1-x} = 1 exactly.
    for a in (0.5, 1.0, 3.0, 40.0):
        for x in (0.1, 0.37, 0.5):
            total = regularized_incomplete_beta(x, a, a) + regularized_incomplete_beta(1 - x, a, a)
            assert total == pytest.approx(1.0, abs=1e-12)


def test_beta_quantile_inverts_the_cdf():
    assert beta_quantile(0.3, 1, 1) == pytest.approx(0.3, abs=1e-9)
    # Beta(2,1) has CDF x^2, so its median is sqrt(0.5).
    assert beta_quantile(0.5, 2, 1) == pytest.approx(math.sqrt(0.5), abs=1e-9)
    for a, b, q in ((5.0, 9.0, 0.05), (0.5, 0.5, 0.4), (300.0, 700.0, 0.975)):
        x = beta_quantile(q, a, b)
        assert regularized_incomplete_beta(x, a, b) == pytest.approx(q, abs=1e-9)


def test_a_uniform_posterior_has_the_uniform_interval():
    low, high = Beta(*UNIFORM).interval(0.95)
    assert low == pytest.approx(0.025, abs=1e-9)
    assert high == pytest.approx(0.975, abs=1e-9)


def test_prob_greater_matches_hand_integrals():
    # Two independent uniforms: a coin flip.
    assert prob_greater(1, 1, 1, 1) == pytest.approx(0.5, abs=1e-12)
    # X1 ~ Beta(2,1) has density 2p, X2 uniform: P = int 2p(1-p) dp = 1/3.
    assert prob_greater(2, 1, 1, 1) == pytest.approx(1 / 3, abs=1e-12)
    # The mirror image.
    assert prob_greater(1, 1, 2, 1) == pytest.approx(2 / 3, abs=1e-12)


def test_prob_greater_and_its_mirror_sum_to_one():
    """Continuous distributions tie with probability zero, so these are
    complements — which also checks the series against itself at a length the
    hand-calculable cases never reach."""
    for params in ((3.0, 7.0, 11.0, 4.0), (250.0, 300.0, 260.0, 280.0), (1.0, 900.0, 4.0, 40.0)):
        a1, b1, a2, b2 = params
        assert prob_greater(a1, b1, a2, b2) + prob_greater(a2, b2, a1, b1) == pytest.approx(
            1.0, abs=1e-9
        )


def test_the_quadrature_path_agrees_with_the_exact_series():
    """A fractional prior (Jeffreys) takes the quadrature branch. It has to
    give the same answer the series does where both apply."""
    from job.models.bayesian_ab.conjugate import _prob_greater_quadrature

    for a1, b1, a2, b2 in ((1.0, 1.0, 2.0, 1.0), (12.0, 30.0, 18.0, 25.0)):
        assert _prob_greater_quadrature(a1, b1, a2, b2) == pytest.approx(
            prob_greater(a1, b1, a2, b2), abs=1e-8
        )


def test_expected_loss_between_two_uniforms_is_one_sixth():
    # E[max(U2 - U1, 0)] = 1/6 for independent uniforms, both ways round.
    loss1, loss2 = expected_loss(1, 1, 1, 1)
    assert loss1 == pytest.approx(1 / 6, abs=1e-9)
    assert loss2 == pytest.approx(1 / 6, abs=1e-9)


def test_the_two_losses_differ_by_exactly_the_lift():
    """E[(x)+] - E[(-x)+] = E[x], so loss(A) - loss(B) = E[p_B] - E[p_A].

    An identity, not a tolerance: it ties the two loss calculations together
    and to the posterior means, so a sign error in any one of the three shows
    up here."""
    for a1, b1, a2, b2 in ((4.0, 9.0, 7.0, 6.0), (120.0, 90.0, 100.0, 110.0)):
        loss1, loss2 = expected_loss(a1, b1, a2, b2)
        lift = Beta(a2, b2).mean - Beta(a1, b1).mean
        assert loss1 - loss2 == pytest.approx(lift, abs=1e-9)


def test_a_hopeless_arm_carries_no_regret_and_a_winning_one_carries_it_all():
    loss1, loss2 = expected_loss(2.0, 200.0, 190.0, 12.0)
    assert loss2 == pytest.approx(0.0, abs=1e-9)
    # Choosing the near-certain loser costs almost the whole gap.
    assert loss1 == pytest.approx(Beta(190.0, 12.0).mean - Beta(2.0, 200.0).mean, abs=1e-6)


def test_the_difference_of_two_uniforms_is_the_triangular_distribution():
    """D = U2 - U1 has CDF (1+d)^2/2 below zero, so its 2.5% point is
    sqrt(0.05) - 1 = -0.776393 and the interval is symmetric."""
    summary = difference_summary(1, 1, 1, 1, mass=0.95)

    assert summary["mean"] == pytest.approx(0.0, abs=1e-12)
    assert summary["sd"] == pytest.approx(math.sqrt(2 / 12), abs=1e-12)
    assert summary["ci_low"] == pytest.approx(math.sqrt(0.05) - 1, abs=2e-3)
    assert summary["ci_high"] == pytest.approx(1 - math.sqrt(0.05), abs=2e-3)
    assert summary["prob_positive"] == pytest.approx(0.5, abs=1e-6)
    # The grid's resolution is reported, not implied.
    assert 0 < summary["grid_step"] < 1e-3


def test_the_grid_and_the_exact_series_agree_on_the_same_probability():
    """``prob_positive`` off the convolution is a cross-check on
    ``prob_greater``: two independent routes to one number."""
    for a1, b1, a2, b2 in ((30.0, 45.0, 40.0, 38.0), (5.0, 2.0, 3.0, 9.0)):
        summary = difference_summary(a1, b1, a2, b2)
        assert summary["prob_positive"] == pytest.approx(prob_greater(a1, b1, a2, b2), abs=1e-4)


# --- the model, against a known analytic case -------------------------------


def test_a_run_on_counted_arms_reproduces_the_analytic_answer(recorder):
    """No data loading, no threshold, no sampler: with 0 successes out of 0
    trials in each arm the posteriors *are* the uniform prior, and every
    reported number is one of the hand-calculated constants above."""
    r, model = counted(recorder, (0, 0), (0, 0))
    rows = rows_by_label(model)

    for label in ("control", "variant"):
        assert rows[label]["posterior_alpha"] == 1.0
        assert rows[label]["posterior_beta"] == 1.0
        assert rows[label]["posterior_mean"] == pytest.approx(0.5, abs=1e-7)
        assert rows[label]["ci_low"] == pytest.approx(0.025, abs=1e-6)
        assert rows[label]["ci_high"] == pytest.approx(0.975, abs=1e-6)
        assert rows[label]["prob_beats_other"] == pytest.approx(0.5, abs=1e-7)
        assert rows[label]["expected_loss"] == pytest.approx(1 / 6, abs=1e-6)
        assert rows[label]["observed_rate"] is None

    comparison = rows["variant_vs_control"]
    assert comparison["posterior_mean"] == pytest.approx(0.0, abs=1e-7)
    assert comparison["ci_low"] == pytest.approx(math.sqrt(0.05) - 1, abs=2e-3)
    # Two arms with no data cannot possibly settle anything.
    assert comparison["decision"] == "inconclusive"
    assert comparison["conclusive"] is False
    r.validate_all()


def test_one_success_against_nothing_gives_the_beta_two_one_answer(recorder):
    """Arm B saw one success in one trial, arm A saw nothing. Posteriors are
    Beta(1,1) and Beta(2,1), so P(B > A) = 2/3 exactly."""
    _, model = counted(recorder, (0, 0), (1, 1))
    rows = rows_by_label(model)

    assert rows["variant"]["posterior_alpha"] == 2.0
    assert rows["variant"]["posterior_beta"] == 1.0
    # Result rows are rounded for the table, so 1e-7 rather than 1e-9.
    assert rows["variant"]["prob_beats_other"] == pytest.approx(2 / 3, abs=1e-7)
    assert rows["control"]["prob_beats_other"] == pytest.approx(1 / 3, abs=1e-7)
    # Two thirds is nowhere near a decision, and one observation should not
    # buy one.
    assert model.decision == "inconclusive"


def test_a_large_clean_difference_is_called(recorder):
    _, model = counted(recorder, (300, 1000), (500, 1000))
    comparison = rows_by_label(model)["variant_vs_control"]

    assert model.decision == "variant"
    assert model.conclusive is True
    assert comparison["prob_beats_other"] > 0.999
    assert comparison["posterior_mean"] == pytest.approx(0.2, abs=0.01)
    assert comparison["ci_low"] > 0
    assert comparison["expected_loss"] < 1e-4


def test_identical_arms_are_never_called(recorder):
    _, model = counted(recorder, (500, 1000), (500, 1000))
    comparison = rows_by_label(model)["variant_vs_control"]

    assert model.decision == "inconclusive"
    assert comparison["prob_beats_other"] == pytest.approx(0.5, abs=1e-7)
    assert comparison["ci_low"] < 0 < comparison["ci_high"]


def test_a_tiny_difference_over_many_trials_is_significant_but_not_worth_acting_on(recorder):
    """The case the decision rule exists for: P(B>A) is overwhelming and the
    effect is a fifth of a percentage point. Probability says B leads; expected
    loss says nobody should care, so with a wide tolerance it is not called."""
    _, model = counted(recorder, (50_000, 100_000), (50_600, 100_000), loss_tolerance=0.0)
    assert model.prob_b_beats_a > 0.99
    assert model.decision == "inconclusive", "a zero tolerance must veto every call"

    _, generous = counted(recorder, (50_000, 100_000), (50_600, 100_000), loss_tolerance=0.01)
    assert generous.decision == "variant"


def test_the_prior_moves_the_posterior_when_the_data_is_thin(recorder):
    """A prior that is doing work should be visible. Two failures out of two
    trials leaves the flat prior's posterior at Beta(1, 3) — mean 1/4, not the
    observed 0 — while Beta(20, 2) barely moves off its own mean."""
    _, flat = counted(recorder, (0, 2), (0, 2))
    _, opinionated = counted(recorder, (0, 2), (0, 2), prior_alpha=20.0, prior_beta=2.0)

    assert rows_by_label(flat)["control"]["posterior_mean"] == pytest.approx(0.25, abs=1e-7)
    assert rows_by_label(opinionated)["control"]["posterior_mean"] == pytest.approx(
        20 / 24, abs=1e-7
    )


def test_a_fractional_prior_still_produces_a_full_decision_table(recorder):
    """Jeffreys' Beta(0.5, 0.5) takes the quadrature branch throughout. The
    answer has to stay close to the flat-prior one on data this size."""
    _, jeffreys = counted(recorder, (30, 100), (45, 100), prior_alpha=0.5, prior_beta=0.5)
    _, flat = counted(recorder, (30, 100), (45, 100))

    assert jeffreys.prob_b_beats_a == pytest.approx(flat.prob_b_beats_a, abs=0.02)
    assert len(jeffreys.results()) == 3


# --- the real data path -----------------------------------------------------


def test_the_default_comparison_runs_on_real_hourly_observations(recorder):
    """Offline there is no workspace, so the loader falls back — and that is
    the state the assertions below describe."""
    r, model = fitted(recorder)

    assert model.comparison == "weekend_fare"
    assert model.dataset.synthetic is True
    assert [arm.label for arm in model.arms] == ["weekday_hours", "weekend_hours"]
    # Every hour landed in exactly one arm.
    assert sum(arm.trials for arm in model.arms) == len(model.dataset)
    # Weekends are two days in seven, so arm B is the smaller one by a lot.
    assert 0 < model.arms[1].trials < model.arms[0].trials
    r.validate_all()


def test_the_median_split_puts_half_the_hours_above_the_line(recorder):
    """A direct check on limitation 4 in the docstring: with a pooled-median
    threshold the two arms' successes are constrained to sum to about half of
    all trials, which is exactly the coupling the docstring warns about."""
    _, model = fitted(recorder)
    trials = sum(arm.trials for arm in model.arms)
    successes = sum(arm.successes for arm in model.arms)

    assert successes == pytest.approx(trials / 2, abs=2)
    assert "pooled median" in model.outcome


def test_a_fixed_threshold_is_used_and_named_when_one_is_given(recorder):
    _, model = fitted(recorder, fare_threshold=14.0)
    assert "fixed" in model.outcome
    assert "14.0000" in model.outcome
    # Above the fallback's mean fare of about 15, so most hours succeed.
    assert sum(arm.successes for arm in model.arms) > 0.6 * sum(arm.trials for arm in model.arms)


def test_the_trip_speed_comparison_finds_that_long_trips_are_faster(recorder):
    """A claim about the world, not just about the arithmetic: short trips
    crawl and long ones do not, and with two thousand trips the posterior is
    emphatic about it."""
    r, model = fitted(recorder, comparison="long_trip_speed", rows=1200)
    rows = rows_by_label(model)

    assert [arm.label for arm in model.arms] == ["under_2mi", "over_2mi"]
    assert rows["over_2mi"]["posterior_mean"] > rows["under_2mi"]["posterior_mean"] + 0.2
    assert model.prob_b_beats_a > 0.999
    assert model.decision == "over_2mi"
    assert "fixed cutoff" in model.outcome
    r.validate_all()


def test_provenance_is_logged_at_the_input_phase(recorder):
    r, model = fitted(recorder)
    input_logs = [line for line in r.of("log") if line["phase"] == "input"]

    provenance = [line for line in input_logs if line["message"] == model.dataset.provenance]
    assert provenance, [line["message"] for line in input_logs]
    # Falling back is not a silent event.
    assert provenance[0]["level"] == "WARNING"
    assert "synthetic" in provenance[0]["message"]
    # The prior is part of the record, not a hidden default.
    assert any("prior Beta(1, 1)" in line["message"] for line in input_logs)


def test_provenance_is_carried_into_every_result_row(recorder):
    """Logs are droppable by contract; "was this real data?" has to survive
    into the durable record."""
    _, model = fitted(recorder)

    for row in model.results():
        assert row["data_synthetic"] is True
        assert row["data_source"] == "synthetic:hourly-demand"
        assert row["data_rows"] == len(model.dataset)
        assert "no Spark session" in row["data_fallback_reason"]


def test_result_rows_have_one_shape_whichever_path_ran(recorder):
    """The comparison row, an arm row, a loaded run and a counted run all
    produce the same columns — so the results table does not change schema
    depending on how a run was configured."""
    _, loaded = fitted(recorder)
    _, supplied = counted(recorder, (3, 10), (6, 10))

    shapes = {frozenset(row) for row in loaded.results() + supplied.results()}
    assert len(shapes) == 1, shapes
    assert supplied.results()[0]["data_synthetic"] is False
    assert supplied.results()[0]["data_fallback_reason"] is None
    assert supplied.results()[0]["data_source"] == "caller-supplied"


def test_a_caller_can_bring_its_own_rows(recorder):
    rows = [{"trip_distance": 1.0, "duration_min": 30.0}] * 20 + [
        {"trip_distance": 10.0, "duration_min": 20.0}
    ] * 20
    _, model = fitted(recorder, comparison="long_trip_speed", data=rows)

    # 2 mph against 30 mph: nothing in arm A clears the cutoff, everything in B.
    assert (model.arms[0].successes, model.arms[0].trials) == (0, 20)
    assert (model.arms[1].successes, model.arms[1].trials) == (20, 20)
    assert model.decision == "over_2mi"


def test_a_zero_duration_trip_is_dropped_rather_than_given_a_speed(recorder):
    rows = [
        {"trip_distance": 1.0, "duration_min": 0.0},
        {"trip_distance": 1.0, "duration_min": 6.0},
    ]
    _, model = fitted(recorder, comparison="long_trip_speed", data=rows)
    assert model.arms[0].trials == 1


def test_an_unknown_comparison_fails_at_construction():
    with pytest.raises(ValueError, match="unknown comparison"):
        build_model({"comparison": "coin_flips"})
    assert "weekend_fare" in COMPARISONS


# --- telemetry --------------------------------------------------------------


def test_progress_keeps_the_generic_fields_usable(recorder):
    """A view that knows nothing about Bayesian inference must still render
    this: a percentage that climbs to 100 and a labelled metric."""
    r, _ = fitted(recorder)
    progress = r.of("progress")

    assert len(progress) == len(STAGES)
    percents = [p["percent_complete"] for p in progress]
    assert percents == sorted(percents)
    assert percents[-1] == 100.0
    for p in progress:
        assert 0 < p["percent_complete"] <= 100
        assert p["primary_metric_label"] == "prob_b_beats_a"
        assert p["elapsed_seconds"] >= 0
    r.validate_all()


def test_the_primary_metric_is_a_probability_and_is_null_before_it_is_known(recorder):
    """It is not an error or a gap. It is null until the comparison stage
    computes it — which is a legal envelope value and the honest one."""
    r, model = fitted(recorder)
    progress = r.of("progress")

    assert progress[0]["payload"]["stage"] == "posteriors"
    assert progress[0]["primary_metric"] is None
    for p in progress[1:]:
        assert p["primary_metric"] is not None
        assert 0.0 <= p["primary_metric"] <= 1.0
    assert progress[-1]["primary_metric"] == pytest.approx(model.prob_b_beats_a)


def test_the_payload_carries_the_posterior_parameters(recorder):
    r, model = fitted(recorder)
    payload = r.of("progress")[-1]["payload"]

    assert payload["progress_shape"] == "stages"
    assert payload["stage_index"] == len(STAGES) == payload["stages_total"]
    assert payload["prior"] == {"alpha": 1.0, "beta": 1.0}
    assert [arm["role"] for arm in payload["arms"]] == ["A", "B"]
    for reported, arm in zip(payload["arms"], model.arms, strict=True):
        assert reported["posterior_alpha"] == arm.posterior.alpha
        assert reported["posterior_beta"] == arm.posterior.beta
        assert reported["trials"] == arm.trials
    assert payload["expected_loss"].keys() == {"A", "B"}
    assert payload["lift"]["ci_low"] < payload["lift"]["ci_high"]
    assert payload["decision"] == model.decision


def test_the_payload_grows_as_the_stages_complete(recorder):
    """Each stage adds its own field rather than back-filling nulls, so a
    progress message says what was actually known when it was sent."""
    r, _ = fitted(recorder)
    keys = [set(p["payload"]) for p in r.of("progress")]

    assert "prob_b_beats_a" not in keys[0]
    assert "prob_b_beats_a" in keys[1]
    assert "expected_loss" not in keys[1]
    assert "expected_loss" in keys[2]
    assert "decision" in keys[-1]
    # Never loses anything either.
    for earlier, later in zip(keys, keys[1:]):  # noqa: B905 - deliberate offset pairing
        assert earlier <= later


def test_a_short_run_still_emits_progress(recorder):
    """Milliseconds end to end. The envelope should not need a slow model to
    be worth anything."""
    r, _ = counted(recorder, (1, 2), (2, 3))
    assert len(r.of("progress")) == len(STAGES)
    r.validate_all()


# --- cancellation -----------------------------------------------------------


def test_cancelling_mid_run_keeps_the_arms_it_already_fitted(recorder):
    """Cancelled after the posteriors exist but before the comparison: two arm
    rows, no comparison row, and `complete` False on both."""
    r = recorder()
    model = r.attach(
        build_model(
            {
                "arms": [
                    {"label": "control", "successes": 30, "trials": 100},
                    {"label": "variant", "successes": 45, "trials": 100},
                ]
            }
        )
    )
    model.build()
    original = model._stage_posteriors

    def cancel_after_posteriors():
        original()
        r.cancel()

    model._stage_posteriors = cancel_after_posteriors
    model.run()

    assert model.stages_done == 1
    assert model.cancelled is True
    rows = model.results()
    assert [row["row_type"] for row in rows] == ["arm", "arm"]
    assert all(row["complete"] is False for row in rows)
    # The posteriors that do exist are finished numbers, not partial ones.
    assert rows[0]["posterior_alpha"] == 31.0
    assert rows[0]["ci_low"] < rows[0]["posterior_mean"] < rows[0]["ci_high"]
    assert rows[0]["prob_beats_other"] is None
    assert rows[0]["decision"] is None


def test_cancelling_before_anything_ran_writes_nothing(recorder):
    """Zero rows means "did not get that far", and the harness's row_count is
    what makes that distinguishable from "succeeded, wrote nothing"."""
    r = recorder(cancel_after=1)
    model = r.attach(build_model({"comparison": "long_trip_speed", "rows": 200}))
    model.build()
    model.run()

    assert model.stages_done == 0
    assert model.results() == []
    r.validate_all()


def test_a_run_cancelled_late_keeps_its_comparison(recorder):
    r = recorder()
    model = r.attach(
        build_model(
            {
                "arms": [
                    {"label": "control", "successes": 30, "trials": 100},
                    {"label": "variant", "successes": 60, "trials": 100},
                ]
            }
        )
    )
    model.build()
    original = model._stage_expected_loss

    def cancel_after_loss():
        original()
        r.cancel()

    model._stage_expected_loss = cancel_after_loss
    model.run()

    rows = model.results()
    assert len(rows) == 3
    assert all(row["complete"] is False for row in rows)
    comparison = rows[-1]
    # P(B>A) survived; the interval that needed a later stage did not, and
    # says so with a null rather than a guess.
    assert comparison["prob_beats_other"] > 0.99
    assert comparison["posterior_mean"] is None
    assert comparison["ci_low"] is None
    assert comparison["decision"] is None


def test_a_completed_run_marks_itself_complete(recorder):
    _, model = counted(recorder, (10, 40), (20, 40))
    assert model.stages_done == len(STAGES)
    assert all(row["complete"] is True for row in model.results())


# --- the duck-typed surface -------------------------------------------------


def test_the_harness_finds_what_it_needs():
    from job.loader import describe_object

    handle = describe_object(build_model({}))
    assert handle.run is not None and handle.build is not None
    assert handle.results is not None
    assert handle.results_table == "results_bayesian_ab"
    # No Gurobi model and no preview axes: a decision table is not a series.
    assert handle.gurobi_model is None
    assert handle.preview_axes is None


def test_the_results_table_ddl_matches_the_rows_the_model_writes():
    """A decision table with a column the model never fills, or a field the
    table has nowhere to put, is a silently truncated result. Cheaper to catch
    here than in Unity Catalog."""
    import pathlib
    import re

    sql = pathlib.Path("uc_ddl/002_model_results.sql").read_text()
    block = sql.split("results_bayesian_ab (")[1].split(")\nUSING DELTA")[0]
    columns = {
        line.strip().split()[0]
        for line in block.splitlines()
        if line.strip() and not line.strip().startswith("--")
    }
    # The harness stamps these two; the model never sees them.
    assert {"run_id", "chunk_index"} <= columns
    assert re.search(r"COMMENT '.*'", sql)

    model = build_model(
        {
            "arms": [
                {"label": "control", "successes": 1, "trials": 4},
                {"label": "variant", "successes": 3, "trials": 4},
            ]
        }
    )
    model.emit = lambda *_, **__: None
    model.should_cancel = lambda: False
    model.build()
    model.run()

    for row in model.results():
        assert set(row) == columns - {"run_id", "chunk_index"}
