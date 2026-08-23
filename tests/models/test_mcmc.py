"""MCMC — the model most likely to stress the streaming path.

Two problems are exercised deliberately. The taxi regression (``fare ~
distance``) is the real workload and the one whose data provenance has to
survive into the results. The Gaussian toy is kept because it is the only one
with a known analytic posterior, which is how the sampler is checked for
landing somewhere *correct* rather than merely somewhere.
"""

from __future__ import annotations

import math

import pytest

pytest.importorskip("emcee", reason="needs the [mcmc] extra")

from models.mcmc import build_model, gaussian_problem, split_rhat  # noqa: E402


def fitted(recorder, **config):
    r = recorder()
    model = r.attach(build_model(config))
    model.build()
    model.run()
    return r, model


def gaussian(recorder, *, n=400, seed=11, **config):
    """The analytic toy problem, passed in through the ``data`` config key."""
    return fitted(recorder, data=gaussian_problem(n=n, seed=seed), seed=seed, **config)


def taxi(recorder, **config):
    """The real workload, small enough to keep the suite quick. Offline this
    runs on the loader's deterministic fallback, which is the point."""
    config.setdefault("rows", 400)
    return fitted(recorder, **config)


# --- correctness: the known-analytic posterior ------------------------------


def test_recovers_a_known_analytic_posterior(recorder):
    # mu=3, sigma=2 by construction; the sampler should land near both.
    r, model = gaussian(recorder, draws=1500, burn_in=500, chains=10, n=400, seed=11)
    by_name = {row["parameter"]: row for row in model.results()}

    assert by_name["mu"]["mean"] == pytest.approx(3.0, abs=0.3)
    assert math.exp(by_name["log_sigma"]["mean"]) == pytest.approx(2.0, abs=0.3)
    assert by_name["mu"]["q05"] < 3.0 < by_name["mu"]["q95"]
    r.validate_all()


def test_chains_converge_on_a_run_long_enough_to_expect_it(recorder):
    _, model = gaussian(recorder, draws=2000, burn_in=800, chains=10)
    rhats = [row["rhat"] for row in model.results()]
    assert all(rh is not None and rh < 1.1 for rh in rhats), rhats


# --- the real workload: fare ~ distance -------------------------------------


def test_fits_the_taxi_regression_offline_on_the_fallback(recorder):
    """No workspace here, so the loader falls back — and the model still runs."""
    r, model = taxi(recorder, draws=600, burn_in=200)

    assert model.dataset.synthetic is True
    assert model.param_names == ("intercept", "slope", "log_sigma")
    assert len(model.dataset) == 400
    r.validate_all()


def test_fare_rises_with_distance(recorder):
    """The posterior has to say something true about the world: a longer trip
    costs more, and the credible interval for the slope excludes zero."""
    _, model = taxi(recorder, draws=800, burn_in=300)
    by_name = {row["parameter"]: row for row in model.results()}

    slope = by_name["slope"]
    assert slope["mean"] > 0.5, slope
    assert slope["q05"] > 0, slope
    # A dollar-per-mile figure, not a runaway one.
    assert 1.0 < slope["mean"] < 6.0, slope
    assert by_name["intercept"]["mean"] > 0, by_name["intercept"]


def test_provenance_is_logged_at_the_input_phase(recorder):
    r, model = taxi(recorder, draws=100, burn_in=20)
    input_logs = [line for line in r.of("log") if line["phase"] == "input"]

    provenance = [line for line in input_logs if line["message"] == model.dataset.provenance]
    assert provenance, [line["message"] for line in input_logs]
    # Falling back is not a silent event.
    assert provenance[0]["level"] == "WARNING"
    assert "synthetic" in provenance[0]["message"]
    assert any("fare_amount ~ trip_distance" in line["message"] for line in input_logs)
    r.validate_all()


def test_provenance_is_carried_into_every_result_row(recorder):
    """A run on real trips and one that fell back must not look identical
    afterwards, long after the logs have scrolled away."""
    _, model = taxi(recorder, draws=200, burn_in=50)

    for row in model.results():
        assert row["data_synthetic"] is True
        assert row["data_source"] == "synthetic:trips"
        assert row["data_rows"] == 400
        assert "no Spark session" in row["data_fallback_reason"]
        assert row["model"] == "fare_amount ~ trip_distance"


def test_result_rows_have_one_shape_whichever_data_path_ran(recorder):
    """Real data has no fallback reason; the column exists regardless, so the
    results table does not change schema between runs."""
    _, fallback = taxi(recorder, draws=100, burn_in=20)
    _, supplied = fitted(
        recorder,
        draws=100,
        burn_in=20,
        data=[{"trip_distance": 1.0 + i * 0.1, "fare_amount": 3.0 + 2.5 * i * 0.1}
              for i in range(50)],
    )

    assert set(fallback.results()[0]) == set(supplied.results()[0])
    assert supplied.results()[0]["data_synthetic"] is False
    assert supplied.results()[0]["data_fallback_reason"] is None
    assert supplied.results()[0]["data_source"] == "caller-supplied"


def test_walkers_start_inside_the_prior_even_on_a_near_perfect_fit(recorder):
    """Noiseless input puts log(residual sd) at about -35, far outside the
    prior's support. Unclamped, every walker starts at -inf, nothing is ever
    accepted, and the "posterior" is just the starting point — a silent
    failure that still looks like a plausible answer."""
    rows = [{"trip_distance": float(i), "fare_amount": 3.0 + 2.5 * i} for i in range(40)]
    r, model = fitted(recorder, data=rows, draws=300, burn_in=100)

    assert all(math.isfinite(model.log_prob(theta)) for theta in model._start)
    assert r.of("progress")[-1]["payload"]["min_acceptance"] > 0
    assert r.of("progress")[-1]["payload"]["stuck_chains"] == 0


def test_a_caller_can_bring_its_own_columns(recorder):
    rows = [{"x": float(i), "y": 10.0 - 0.5 * i} for i in range(60)]
    _, model = fitted(
        recorder, data=rows, x_column="x", y_column="y", draws=400, burn_in=150
    )
    by_name = {row["parameter"]: row for row in model.results()}

    assert by_name["slope"]["mean"] == pytest.approx(-0.5, abs=0.2)
    assert by_name["intercept"]["mean"] == pytest.approx(10.0, abs=1.0)


# --- telemetry --------------------------------------------------------------


def test_progress_keeps_the_generic_fields_usable(recorder):
    """A non-MCMC-aware view must still be able to render this."""
    r, _ = taxi(recorder, draws=400, burn_in=100, progress_every=50)
    progress = r.of("progress")

    assert progress
    for p in progress:
        assert 0 < p["percent_complete"] <= 100
        assert p["primary_metric_label"] == "max_rhat"
    assert progress[-1]["percent_complete"] == 100.0


def test_payload_carries_the_per_chain_detail(recorder):
    r, _ = taxi(recorder, draws=400, burn_in=100, chains=6)
    payload = r.of("progress")[-1]["payload"]

    assert payload["chains"] == 6
    assert len(payload["per_chain_acceptance"]) == 6
    assert payload["parameters"] == ["intercept", "slope", "log_sigma"]
    # emcee has no divergences (an HMC diagnostic) — stuck chains are the
    # honest analogue, and the model says so rather than reporting a fake 0.
    assert "stuck_chains" in payload and "divergences" not in payload


def test_progress_does_not_fire_on_every_draw(recorder):
    r, _ = taxi(recorder, draws=500, burn_in=100, progress_every=100, progress_every_s=999)
    assert len(r.of("progress")) <= 10


def test_cancelling_mid_sampling_keeps_a_partial_posterior(recorder):
    r = recorder(cancel_after=3)
    model = r.attach(
        build_model({"draws": 5000, "burn_in": 50, "progress_every": 20, "rows": 400})
    )
    model.build()
    model.run()

    assert model.draws_done < 5000, "cancellation was ignored"
    rows = model.results()
    assert len(rows) == 3
    assert all(row["complete"] is False for row in rows)
    assert all(row["draws_used"] > 0 for row in rows)
    # A partial posterior still knows where its data came from.
    assert all(row["data_source"] == "synthetic:trips" for row in rows)


def test_cancelling_the_toy_problem_keeps_its_two_parameters(recorder):
    r = recorder(cancel_after=3)
    model = r.attach(
        build_model({"data": gaussian_problem(n=200), "draws": 5000, "progress_every": 20})
    )
    model.build()
    model.run()

    assert model.draws_done < 5000
    assert [row["parameter"] for row in model.results()] == ["mu", "log_sigma"]
    assert all(row["complete"] is False for row in model.results())


# --- the diagnostic itself --------------------------------------------------


def test_rhat_is_near_one_for_chains_that_agree():
    import numpy as np

    rng = np.random.default_rng(0)
    agreeing = rng.normal(0, 1, size=(8, 500, 1))
    assert split_rhat(agreeing) < 1.05


def test_rhat_is_large_for_chains_that_do_not_agree():
    import numpy as np

    offsets = np.arange(8).reshape(8, 1, 1) * 10.0
    disagreeing = np.random.default_rng(0).normal(0, 1, size=(8, 500, 1)) + offsets
    assert split_rhat(disagreeing) > 1.5


def test_rhat_is_nan_rather_than_a_lie_when_there_is_not_enough_data():
    assert math.isnan(split_rhat([[[1.0]], [[2.0]]]))


def test_the_problem_is_deterministic_for_a_seed():
    import numpy as np

    assert np.array_equal(gaussian_problem(seed=5), gaussian_problem(seed=5))
