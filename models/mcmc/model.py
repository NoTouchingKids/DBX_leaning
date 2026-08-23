"""Bayesian inference by MCMC — the streaming path's stress test.

Long sampling runs, several chains, and a progress shape that looks like
nothing else here: draws, acceptance, convergence diagnostics. If the envelope
and the transport handle this cleanly, they handle the other four.

``emcee`` rather than a full PPL: this model's job is to stress the *platform*,
not to showcase Bayesian modelling, so the lighter dependency wins.

One honest deviation from the brief, flagged rather than faked: **emcee has no
divergences.** Divergences are an HMC/NUTS diagnostic; emcee is an
affine-invariant ensemble sampler. The per-chain health signal it does have is
acceptance — a walker that accepts nothing is stuck, which is the same
"something is wrong with this chain" signal a divergence count carries. The
payload reports that instead of a fabricated zero.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

__all__ = ["McmcModel", "build_model", "gaussian_problem", "split_rhat"]


def gaussian_problem(
    n: int = 200, *, true_mu: float = 3.0, true_sigma: float = 2.0, seed: int = 11
):
    """Data whose posterior for ``mu`` is analytic — so tests can check the
    sampler landed somewhere correct, not merely somewhere."""
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
        #: ndarray of observations. Any, because numpy is imported lazily in
        #: this module — the [mcmc] extra is optional.
        self.data: Any = cfg.get("data")
        if self.data is None:
            self.data = gaussian_problem(
                n=int(cfg.get("n", 200)), seed=int(cfg.get("seed", 11))
            )
        self.n_chains = int(cfg.get("chains", 8))     # emcee walkers
        self.n_draws = int(cfg.get("draws", 800))
        self.burn_in = int(cfg.get("burn_in", 200))
        self.seed = int(cfg.get("seed", 11))
        self.progress_every = int(cfg.get("progress_every", 50))
        self.progress_every_s = float(cfg.get("progress_every_s", 2.0))
        self.param_names = ("mu", "log_sigma")

        self.emit: Callable[..., Any] | None = None
        self.should_cancel: Callable[[], bool] | None = None

        self.draws_done = 0
        self._sampler: Any = None

    # --- the model --------------------------------------------------------

    def log_prob(self, theta) -> float:
        import numpy as np

        mu, log_sigma = theta
        if not (-50 < mu < 50) or not (-5 < log_sigma < 5):
            return -np.inf  # a flat prior with support
        sigma = np.exp(log_sigma)
        residual = (self.data - mu) / sigma
        return float(-0.5 * np.sum(residual**2) - len(self.data) * log_sigma)

    def build(self) -> None:
        import emcee
        import numpy as np

        rng = np.random.default_rng(self.seed)
        start = np.column_stack(
            [
                rng.normal(float(np.mean(self.data)), 1.0, self.n_chains),
                rng.normal(float(np.log(np.std(self.data) or 1.0)), 0.2, self.n_chains),
            ]
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
                "post_burn_in_draws": int(usable.shape[0]),
                "mean_acceptance": round(float(acceptance.mean()), 4),
                "min_acceptance": round(float(acceptance.min()), 4),
                # emcee's analogue of a divergence count: a chain accepting
                # nothing is not exploring. See this module's docstring.
                "stuck_chains": int((acceptance == 0).sum()),
                "per_chain_acceptance": [round(float(a), 4) for a in acceptance],
            },
        )

    # --- results ----------------------------------------------------------

    def results(self) -> list[dict[str, Any]]:
        """Summary statistics per parameter — the primary result rows.

        Raw draws are deliberately not written: at 8 chains x 800 draws they
        are 6,400 rows per parameter of mostly-redundant detail, and the
        preview a client actually renders comes from the summary.
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
                    "complete": self.draws_done >= self.n_draws,
                }
            )
        return rows

    def _log(self, message: str, *, phase: str = "run", level: str = "INFO") -> None:
        if self.emit is not None:
            self.emit("log", message=message, level=level, source="model", phase=phase)


def _rhat_per_param(chains):
    import numpy as np

    x = np.asarray(chains, dtype=float)
    return np.array([split_rhat(x[:, :, i : i + 1]) for i in range(x.shape[2])])


def build_model(config: dict[str, Any] | None = None) -> McmcModel:
    return McmcModel(config)
