"""MCMC — the model most likely to stress the streaming path."""

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


def test_recovers_a_known_analytic_posterior(recorder):
    # mu=3, sigma=2 by construction; the sampler should land near both.
    r, model = fitted(recorder, draws=1500, burn_in=500, chains=10, n=400, seed=11)
    by_name = {row["parameter"]: row for row in model.results()}

    assert by_name["mu"]["mean"] == pytest.approx(3.0, abs=0.3)
    assert math.exp(by_name["log_sigma"]["mean"]) == pytest.approx(2.0, abs=0.3)
    assert by_name["mu"]["q05"] < 3.0 < by_name["mu"]["q95"]
    r.validate_all()


def test_chains_converge_on_a_run_long_enough_to_expect_it(recorder):
    _, model = fitted(recorder, draws=2000, burn_in=800, chains=10)
    rhats = [row["rhat"] for row in model.results()]
    assert all(rh is not None and rh < 1.1 for rh in rhats), rhats


def test_progress_keeps_the_generic_fields_usable(recorder):
    """A non-MCMC-aware view must still be able to render this."""
    r, _ = fitted(recorder, draws=400, burn_in=100, progress_every=50)
    progress = r.of("progress")

    assert progress
    for p in progress:
        assert 0 < p["percent_complete"] <= 100
        assert p["primary_metric_label"] == "max_rhat"
    assert progress[-1]["percent_complete"] == 100.0


def test_payload_carries_the_per_chain_detail(recorder):
    r, model = fitted(recorder, draws=400, burn_in=100, chains=6)
    payload = r.of("progress")[-1]["payload"]

    assert payload["chains"] == 6
    assert len(payload["per_chain_acceptance"]) == 6
    # emcee has no divergences (an HMC diagnostic) — stuck chains are the
    # honest analogue, and the model says so rather than reporting a fake 0.
    assert "stuck_chains" in payload and "divergences" not in payload


def test_progress_does_not_fire_on_every_draw(recorder):
    r, _ = fitted(recorder, draws=500, burn_in=100, progress_every=100, progress_every_s=999)
    assert len(r.of("progress")) <= 10


def test_cancelling_mid_sampling_keeps_a_partial_posterior(recorder):
    r = recorder(cancel_after=3)
    model = r.attach(build_model({"draws": 5000, "burn_in": 50, "progress_every": 20}))
    model.build()
    model.run()

    assert model.draws_done < 5000, "cancellation was ignored"
    rows = model.results()
    assert len(rows) == 2
    assert all(row["complete"] is False for row in rows)
    assert all(row["draws_used"] > 0 for row in rows)


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
