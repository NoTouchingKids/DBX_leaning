"""Bayesian inference by MCMC — the streaming path's stress test.

Long sampling runs, several chains, and a progress shape that looks like
nothing else here: draws, acceptance, convergence diagnostics. If the envelope
and the transport handle this cleanly, they handle the other four.

``emcee`` rather than a full PPL: this model's job is to stress the *platform*,
not to showcase Bayesian modelling, so the lighter dependency wins.

**What it fits.** ``fare ~ distance`` over NYC taxi trips — a Bayesian linear
regression in ``intercept``, ``slope`` and ``log_sigma``, with weakly
informative normal priors. The data comes from ``models._data``, which reads
Databricks' free ``samples`` catalog on a workspace and falls back to
deterministic synthetic trips off it; the fallback is a real fallback, so the
provenance is logged at the ``input`` phase and carried into every result row.
A run on real trips and a run that fell back must not look identical
afterwards.

A caller can pass its own observations through the ``data`` config key. Give it
rows (dicts, or a ``Dataset``) and it regresses ``y_column`` on ``x_column``;
give it a plain sequence of numbers and it fits the location/scale model
instead — which is what ``gaussian_problem()`` exists for, since a posterior
with a known analytic answer is how the tests check the sampler lands somewhere
*correct* rather than merely somewhere.

One honest deviation from the brief, flagged rather than faked: **emcee has no
divergences.** Divergences are an HMC/NUTS diagnostic; emcee is an
affine-invariant ensemble sampler. The per-chain health signal it does have is
acceptance — a walker that accepts nothing is stuck, which is the same
"something is wrong with this chain" signal a divergence count carries. The
payload reports that instead of a fabricated zero.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from models._data import Dataset, nyc_taxi_trips

__all__ = ["McmcModel", "build_model", "gaussian_problem", "split_rhat"]

#: What ``data`` is called when the caller supplied it instead of the loader.
CALLER_SUPPLIED = "caller-supplied"

#: How many chains the two trace fields describe. Both are bounded by this
#: rather than by the chain count, so a run with 200 walkers cannot turn a
#: progress message into something the live path has to drop. The default is
#: 8 chains, so nothing is truncated in the normal case; when it is, the
#: payload says so (``chain_positions_truncated``).
MAX_TRACE_CHAINS = 32

#: Post-burn-in draws kept per chain in the results sample, and the ceiling
#: on the whole sample for one parameter. 8 x 100 = 800 floats per parameter
#: at the defaults; at three parameters that is roughly 20 KB of JSON on the
#: one result message a run sends, not a copy of the posterior.
TRACE_DRAWS_PER_CHAIN = 100
TRACE_SAMPLE_CAP = 800

#: Column the location/scale model reads once a bare sequence has been wrapped
#: in a :class:`Dataset`, so provenance works the same way for both problems.
OBSERVATION_COLUMN = "observation"


def gaussian_problem(
    n: int = 200, *, true_mu: float = 3.0, true_sigma: float = 2.0, seed: int = 11
):
    """Data whose posterior for ``mu`` is analytic — so tests can check the
    sampler landed somewhere correct, not merely somewhere.

    Kept deliberately: the taxi regression is the real workload, but nothing
    about it has a closed-form answer to check the sampler against.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    return rng.normal(true_mu, true_sigma, size=n)


def split_rhat(chains) -> float:
    """Max split-R-hat across parameters. One number worth watching.

    ``chains`` is (n_chains, n_draws, n_params). Splitting each chain in half
    catches a sampler that has not mixed *within* a chain, which the unsplit
    statistic misses.
    """
    import numpy as np

    x = np.asarray(chains, dtype=float)
    if x.ndim != 3 or x.shape[1] < 4:
        return float("nan")

    n_chains, n_draws, n_params = x.shape
    half = n_draws // 2
    split = np.concatenate([x[:, :half, :], x[:, half : 2 * half, :]], axis=0)
    n = split.shape[1]
    if n < 2:
        return float("nan")

    chain_means = split.mean(axis=1)
    chain_vars = split.var(axis=1, ddof=1)
    between = n * chain_means.var(axis=0, ddof=1)
    within = chain_vars.mean(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        var_hat = ((n - 1) / n) * within + between / n
        rhat = np.sqrt(var_hat / within)
    finite = rhat[np.isfinite(rhat)]
    return float(finite.max()) if finite.size else float("nan")


class McmcModel:
    results_table = "results_mcmc"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        #: Whatever the caller passed as observations: a Dataset, rows, a bare
        #: sequence of numbers, or None to load the taxi sample. Resolved into
        #: :attr:`dataset` by :meth:`build`, not here — loading data is work,
        #: and work belongs where the harness can already see the emit callback.
        self.data: Any = cfg.get("data")
        self.rows = int(cfg.get("rows", 2000))
        self.data_seed = int(cfg.get("data_seed", 11))   # the fallback's seed
        self.x_column = str(cfg.get("x_column", "trip_distance"))
        self.y_column = str(cfg.get("y_column", "fare_amount"))

        self.n_chains = int(cfg.get("chains", 8))     # emcee walkers
        # Chosen by measurement, not taste: on 2000 taxi trips this is where
        # split R-hat settles below 1.03 for all three parameters. An ensemble
        # sampler autocorrelates for longer than HMC does, so a default that
        # looked generous for the two-parameter toy is not one here.
        self.n_draws = int(cfg.get("draws", 3000))
        self.burn_in = int(cfg.get("burn_in", 1000))
        self.seed = int(cfg.get("seed", 11))
        # ~15 progress samples over the default run. Draws are far faster than
        # anyone can watch, so this is a cadence, not a per-draw feed.
        self.progress_every = int(cfg.get("progress_every", 200))
        self.progress_every_s = float(cfg.get("progress_every_s", 2.0))

        #: Set by :meth:`build`. ``("intercept", "slope", "log_sigma")`` for the
        #: regression, ``("mu", "log_sigma")`` for the location/scale model.
        self.param_names: tuple[str, ...] = ()
        self.dataset: Dataset | None = None

        self.emit: Callable[..., Any] | None = None
        self.should_cancel: Callable[[], bool] | None = None

        self.draws_done = 0
        self._sampler: Any = None
        self._x: Any = None   # None means the location/scale model
        self._y: Any = None
        self._start: Any = None

    # --- data -------------------------------------------------------------

    def _resolve_data(self) -> Dataset:
        """Decide what is being fitted, and to what. Never raises for a
        missing workspace — the loader falls back and says so."""
        import numpy as np

        data = self.data
        regression = True

        if data is None:
            dataset = nyc_taxi_trips(limit=self.rows, seed=self.data_seed)
        elif isinstance(data, Dataset):
            dataset = data
        else:
            items = list(data)
            if items and isinstance(items[0], Mapping):
                dataset = Dataset(
                    rows=[dict(row) for row in items],
                    source=CALLER_SUPPLIED,
                    synthetic=False,
                )
            else:
                regression = False
                dataset = Dataset(
                    rows=[{OBSERVATION_COLUMN: float(v)} for v in items],
                    source=CALLER_SUPPLIED,
                    synthetic=False,
                )

        self.dataset = dataset
        if regression:
            self._x = np.asarray(dataset.floats(self.x_column), dtype=float)
            self._y = np.asarray(dataset.floats(self.y_column), dtype=float)
            self.param_names = ("intercept", "slope", "log_sigma")
        else:
            self._x = None
            self._y = np.asarray(dataset.floats(OBSERVATION_COLUMN), dtype=float)
            self.param_names = ("mu", "log_sigma")
        return dataset

    # --- the model --------------------------------------------------------

    def log_prob(self, theta) -> float:
        import numpy as np

        if self._x is None:
            # Location/scale, flat priors with support: the posterior for mu is
            # then exactly the analytic one the tests check against.
            mu, log_sigma = theta
            if not (-50 < mu < 50) or not (-5 < log_sigma < 5):
                return -np.inf
            residual = (self._y - mu) / np.exp(log_sigma)
            return _finite(-0.5 * np.sum(residual**2) - self._y.size * log_sigma)

        # fare ~ Normal(intercept + slope * distance, sigma), weakly
        # informative normal priors — enough to keep the sampler out of
        # nonsense without the ~2000 trips ever noticing them.
        intercept, slope, log_sigma = theta
        if not (-500 < intercept < 500) or not (-500 < slope < 500):
            return -np.inf
        if not (-5 < log_sigma < 8):
            return -np.inf
        residual = (self._y - intercept - slope * self._x) / np.exp(log_sigma)
        log_likelihood = -0.5 * np.sum(residual**2) - self._y.size * log_sigma
        log_prior = -0.5 * ((intercept / 20.0) ** 2 + (slope / 20.0) ** 2 + (log_sigma / 2.0) ** 2)
        return _finite(log_likelihood + log_prior)

    def build(self) -> None:
        import emcee
        import numpy as np

        dataset = self._resolve_data()
        # Provenance first, and loudly when it is a fallback: a run that
        # quietly fitted synthetic trips must not read like one that did not.
        self._log(
            dataset.provenance,
            phase="input",
            level="WARNING" if dataset.synthetic else "INFO",
        )
        self._log(
            f"fitting {self._description()} over {len(dataset)} observations",
            phase="input",
        )

        rng = np.random.default_rng(self.seed)
        centre, scatter = self._starting_point()
        start = np.column_stack(
            [rng.normal(c, s, self.n_chains) for c, s in zip(centre, scatter, strict=True)]
        )
        self._start = start
        self._sampler = emcee.EnsembleSampler(
            self.n_chains, len(self.param_names), self.log_prob
        )
        self._log(
            f"{self.n_chains} chains x {self.n_draws} draws over "
            f"{len(self.param_names)} parameters, burn-in {self.burn_in}",
            phase="input",
        )

    def _description(self) -> str:
        if self._x is None:
            return "a normal location/scale model"
        return f"{self.y_column} ~ {self.x_column}"

    def _starting_point(self) -> tuple[list[float], list[float]]:
        """Walkers start near a least-squares fit, jittered. Cheap, and it
        buys back sampling time that would otherwise go on burn-in."""
        import numpy as np

        if self._x is None:
            return (
                [float(np.mean(self._y)), _start_log_sigma(float(np.std(self._y)))],
                [1.0, 0.2],
            )

        slope, intercept = np.polyfit(self._x, self._y, 1)
        residual = self._y - (intercept + slope * self._x)
        return (
            [float(intercept), float(slope), _start_log_sigma(float(np.std(residual)))],
            [0.2, 0.1, 0.1],
        )

    # --- sampling ---------------------------------------------------------

    def run(self) -> None:
        started = time.monotonic()
        last_emit = started
        state = self._start

        for draw, _ in enumerate(
            self._sampler.sample(state, iterations=self.n_draws, progress=False), start=1
        ):
            self.draws_done = draw
            # Between draws. Draws are fast, so this granularity is cheap.
            if self.should_cancel is not None and self.should_cancel():
                self._log(f"cancelled after {draw} of {self.n_draws} draws")
                break

            now = time.monotonic()
            if draw % self.progress_every == 0 or (now - last_emit) >= self.progress_every_s:
                last_emit = now
                self._progress(draw, now - started)

        self._progress(self.draws_done, time.monotonic() - started)

    def _progress(self, draw: int, elapsed: float) -> None:
        if self.emit is None or draw == 0:
            return
        import numpy as np

        chain = self._sampler.get_chain()  # (draws, chains, params)
        usable = chain[self.burn_in :] if chain.shape[0] > self.burn_in else chain
        rhat = split_rhat(np.transpose(usable, (1, 0, 2))) if usable.shape[0] >= 4 else None

        acceptance = np.asarray(self._sampler.acceptance_fraction, dtype=float)
        # Where each walker currently is, in the order of `parameters`. This
        # is the live trace: one point per chain per progress sample, not a
        # history — the client accumulates the history itself, and a client
        # that reconnects picks the trace up from wherever the run is now.
        #
        # Size at the defaults: 8 chains x 3 parameters = 24 floats, about
        # 300 bytes of JSON, on every progress emission (~15 per default run).
        # Capped at MAX_TRACE_CHAINS chains so a high-walker configuration
        # cannot make this the reason a message gets dropped.
        positions, truncated = self._chain_positions(chain)
        self.emit(
            "progress",
            elapsed_seconds=elapsed,
            # Knowable up front: a fixed draw count.
            percent_complete=100.0 * draw / self.n_draws,
            primary_metric=None if rhat is None or not np.isfinite(rhat) else float(rhat),
            primary_metric_label="max_rhat",
            payload={
                "draws_done": draw,
                "draws_total": self.n_draws,
                "chains": self.n_chains,
                "parameters": list(self.param_names),
                "post_burn_in_draws": int(usable.shape[0]),
                "mean_acceptance": round(float(acceptance.mean()), 4),
                "min_acceptance": round(float(acceptance.min()), 4),
                # emcee's analogue of a divergence count: a chain accepting
                # nothing is not exploring. See this module's docstring.
                "stuck_chains": int((acceptance == 0).sum()),
                "per_chain_acceptance": [round(float(a), 4) for a in acceptance],
                #: Current position of each chain, one list per chain in the
                #: order of `parameters`. Bounded — see above.
                "chain_positions": positions,
                "chain_positions_truncated": truncated,
            },
        )

    def _chain_positions(self, chain) -> tuple[list[list[float]], bool]:
        """The walkers' current coordinates, capped at MAX_TRACE_CHAINS.

        Read off the stored chain rather than from ``get_last_sample()`` so
        this stays correct for a sampler that has been resumed, and so the
        progress emission needs nothing from emcee it has not already asked
        for.
        """
        if chain.shape[0] == 0:
            return [], False
        latest = chain[-1]  # (chains, params)
        truncated = bool(latest.shape[0] > MAX_TRACE_CHAINS)
        return (
            [
                [round(float(v), 6) for v in walker]
                for walker in latest[:MAX_TRACE_CHAINS]
            ],
            truncated,
        )

    # --- results ----------------------------------------------------------

    def results(self) -> list[dict[str, Any]]:
        """Summary statistics per parameter — the primary result rows.

        Raw draws are deliberately not written: at 8 chains x 800 draws they
        are 6,400 rows per parameter of mostly-redundant detail, and the
        preview a client actually renders comes from the summary. What each
        row *does* carry is ``draws_sample``: a systematically thinned,
        hard-capped slice of the post-burn-in draws per chain, as JSON. That
        is enough to draw a trace or a density after the fact, and small
        enough to sit inside a result message's preview.

        Every row carries the data's provenance, so a posterior fitted to real
        trips and one fitted to the synthetic fallback stay distinguishable
        long after the run's logs have scrolled away.
        """
        import numpy as np

        if self._sampler is None or self.draws_done == 0:
            return []

        chain = self._sampler.get_chain()
        usable = chain[self.burn_in :] if chain.shape[0] > self.burn_in else chain
        if usable.shape[0] == 0:
            usable = chain
        flat = usable.reshape(-1, usable.shape[-1])
        rhat_by_param = _rhat_per_param(np.transpose(usable, (1, 0, 2)))
        # Seeded so the caller-supplied-numbers path (no Dataset at all) still
        # produces the same columns as a loaded one. Dataset.describe() now
        # always includes the key itself.
        provenance: dict[str, Any] = {"data_fallback_reason": None}
        if self.dataset is not None:
            provenance.update(self.dataset.describe())

        thinned = self._thinned_draws(usable)

        rows = []
        for i, name in enumerate(self.param_names):
            samples = flat[:, i]
            rows.append(
                {
                    "parameter": name,
                    "mean": round(float(samples.mean()), 6),
                    "sd": round(float(samples.std(ddof=1)), 6),
                    "q05": round(float(np.quantile(samples, 0.05)), 6),
                    "q50": round(float(np.quantile(samples, 0.50)), 6),
                    "q95": round(float(np.quantile(samples, 0.95)), 6),
                    "rhat": None if not np.isfinite(rhat_by_param[i]) else round(
                        float(rhat_by_param[i]), 6
                    ),
                    "draws_used": int(flat.shape[0]),
                    # A JSON string, not a nested array: VARIANT is
                    # nice-to-have here, a STRING column is everywhere.
                    "draws_sample": thinned[i],
                    "complete": self.draws_done >= self.n_draws,
                    "model": self._description(),
                    **provenance,
                }
            )
        return rows

    def _thinned_draws(self, usable) -> list[str]:
        """One JSON string per parameter: the post-burn-in draws, thinned.

        Systematic thinning (every ``thin``-th draw) rather than a random
        sample, because the point of keeping these is the *trace* — a random
        subsample destroys the ordering that makes a trace readable, and with
        it any visible sign of a chain that got stuck for a stretch.

        Bounded twice over: ``TRACE_DRAWS_PER_CHAIN`` per chain, and
        ``TRACE_SAMPLE_CAP`` across all chains for one parameter, so a run
        with more chains keeps fewer draws from each rather than growing. At
        the defaults that is 8 chains x 100 draws = 800 floats per parameter.
        """
        n_draws, n_chains, n_params = usable.shape
        if n_draws == 0 or n_chains == 0:
            return ["" for _ in range(n_params)]

        kept_chains = min(n_chains, MAX_TRACE_CHAINS)
        per_chain = max(1, min(TRACE_DRAWS_PER_CHAIN, TRACE_SAMPLE_CAP // kept_chains))
        thin = max(1, -(-n_draws // per_chain))  # ceil, so the cap always holds
        index = range(0, n_draws, thin)

        out = []
        for i in range(n_params):
            out.append(
                json.dumps(
                    {
                        "thin": thin,
                        "draws_per_chain": len(index),
                        "chains_included": kept_chains,
                        "chains_total": int(n_chains),
                        # How many draws the sample was taken from, which is
                        # the post-burn-in count except on a run cancelled
                        # before burn-in finished — there the summary keeps
                        # the whole chain rather than nothing, and this says so.
                        "draws_available": int(n_draws),
                        "chains": [
                            [round(float(usable[d, c, i]), 6) for d in index]
                            for c in range(kept_chains)
                        ],
                    }
                )
            )
        return out

    def _log(self, message: str, *, phase: str = "run", level: str = "INFO") -> None:
        if self.emit is not None:
            self.emit("log", message=message, level=level, source="model", phase=phase)


def _finite(value) -> float:
    """emcee copes with ``-inf`` (a rejected proposal) but not with ``nan``,
    which propagates into its acceptance arithmetic and silently poisons the
    chain. An overflowed sum of squares becomes a rejection, not a NaN."""
    import numpy as np

    return float(value) if np.isfinite(value) else float(-np.inf)


def _start_log_sigma(spread: float) -> float:
    """``log(spread)``, kept inside the priors' support.

    Not defensive padding: an almost perfectly linear input gives a residual
    standard deviation around 1e-15, whose log is far outside the prior. Every
    walker would then start at ``-inf``, no proposal would ever be accepted,
    and the run would report a "posterior" that is just its starting point.
    """
    import numpy as np

    return float(np.clip(np.log(max(spread, 1e-12)), -4.0, 4.0))


def _rhat_per_param(chains):
    import numpy as np

    x = np.asarray(chains, dtype=float)
    return np.array([split_rhat(x[:, :, i : i + 1]) for i in range(x.shape[2])])


def build_model(config: dict[str, Any] | None = None) -> McmcModel:
    return McmcModel(config)
