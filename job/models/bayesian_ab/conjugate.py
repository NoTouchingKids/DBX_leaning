"""Closed-form Beta-Binomial arithmetic. No sampler, no scipy.

Everything in here is deterministic: the same posterior parameters give the
same number to the same precision every time it is called. That is the point
of this model — it is the one place in the lineup where "how is the run going"
is a *stage counter*, not a convergence curve, because there is nothing to
converge.

**Why not scipy.** ``scipy.stats.beta`` would supply ``ppf`` and
``scipy.special.betainc`` directly. It is also tens of megabytes into a job
environment that otherwise needs numpy alone, for exactly two functions. Both
are standard numerics: ``math.lgamma`` is in the stdlib, and the regularized
incomplete beta is a well-known continued fraction (Numerical Recipes §6.4).
numpy is used for one thing only — the grid convolution that gives the
difference distribution a credible interval — and nothing here samples.

**What is exact and what is not**, because a decision table should not imply
more precision than it has:

- posterior mean, variance, sd — exact, closed form.
- per-arm credible interval — the Beta quantile, to ~1e-12 by bisection on a
  continued-fraction CDF. Exact for reporting purposes.
- ``prob_greater`` — exact when the second arm's posterior alpha is a whole
  number (it is, for integer successes and a whole-number prior), via a finite
  sum of positive terms with no cancellation. Otherwise Simpson quadrature.
- ``expected_loss`` — exact, built from ``prob_greater`` by a change of
  parameters, not by integration.
- the *difference* between two Betas has no closed-form quantile. Its mean and
  sd are exact (they are just sums); its credible interval comes from a grid
  convolution of the two densities and is accurate to a small multiple of one
  grid step, which :func:`difference_summary` reports so a caller can judge it
  rather than take it on trust. The grid is scaled to the posteriors' own
  width, so for the concentrated posteriors this model actually produces the
  error is a fraction of a percent of one standard deviation.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, exp, lgamma, log, log1p, sqrt
from typing import Any

__all__ = [
    "Beta",
    "beta_quantile",
    "difference_summary",
    "expected_loss",
    "log_beta_fn",
    "prob_greater",
    "regularized_incomplete_beta",
]

#: Below this the continued fraction's denominators are rescaled rather than
#: allowed to underflow to zero (Lentz's method).
_FPMIN = 1e-300
_CF_EPS = 3e-16

#: Above this many terms the exact series stops being the cheap option and
#: quadrature takes over. Nothing in this repo comes close: the series length
#: is 1 + successes, and the largest arm here is a few thousand trips.
_MAX_SERIES_TERMS = 100_000


def log_beta_fn(a: float, b: float) -> float:
    """log B(a, b). The normalising constant, in logs, so it never overflows."""
    return lgamma(a) + lgamma(b) - lgamma(a + b)


def _betacf(a: float, b: float, x: float, *, max_iter: int = 400) -> float:
    """The continued fraction for the incomplete beta, by Lentz's method."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        # even step
        num = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + num * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + num / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        # odd step
        num = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + num * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + num / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _CF_EPS:
            return h
    # Not an error worth raising: 400 iterations is far past where this
    # converges for any parameters a Beta posterior can produce, and returning
    # the last iterate is better than failing a run over the 16th decimal.
    return h


def regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    """I_x(a, b) — the CDF of Beta(a, b) at ``x``."""
    if a <= 0 or b <= 0:
        raise ValueError(f"Beta parameters must be positive, got a={a}, b={b}")
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = exp(a * log(x) + b * log1p(-x) - log_beta_fn(a, b))
    # Continued fraction converges fast only on one side of the mode; the
    # symmetry I_x(a,b) = 1 - I_{1-x}(b,a) covers the other.
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def beta_quantile(q: float, a: float, b: float, *, tol: float = 1e-12) -> float:
    """The ``q``-quantile of Beta(a, b), by bisection on the CDF.

    Bisection rather than Newton: the CDF is monotone on [0, 1] so bisection
    cannot diverge, and ~40 iterations reach the tolerance. A Newton step needs
    the density, which underflows in exactly the tails where a credible
    interval's endpoints live.
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"quantile must be in [0, 1], got {q}")
    if q == 0.0:
        return 0.0
    if q == 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if regularized_incomplete_beta(mid, a, b) < q:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


@dataclass(frozen=True)
class Beta:
    """A Beta posterior. Immutable, and every summary on it is closed form."""

    alpha: float
    beta: float

    def __post_init__(self) -> None:
        if self.alpha <= 0 or self.beta <= 0:
            raise ValueError(f"Beta({self.alpha}, {self.beta}) is not a distribution")

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def variance(self) -> float:
        n = self.alpha + self.beta
        return self.alpha * self.beta / (n * n * (n + 1.0))

    @property
    def sd(self) -> float:
        return sqrt(self.variance)

    def cdf(self, x: float) -> float:
        return regularized_incomplete_beta(x, self.alpha, self.beta)

    def quantile(self, q: float) -> float:
        return beta_quantile(q, self.alpha, self.beta)

    def interval(self, mass: float = 0.95) -> tuple[float, float]:
        """The equal-tailed credible interval holding ``mass``.

        Equal-tailed, not highest-density: for a skewed posterior near 0 or 1
        the HDI is narrower, but the equal-tailed interval is the one whose
        endpoints are quantiles, which is what a decision table's readers
        compare across rows.
        """
        tail = (1.0 - mass) / 2.0
        return self.quantile(tail), self.quantile(1.0 - tail)


def _is_whole(value: float) -> bool:
    return value >= 1.0 and abs(value - round(value)) < 1e-9


def prob_greater(a1: float, b1: float, a2: float, b2: float) -> float:
    """P(X2 > X1) for independent X1 ~ Beta(a1, b1), X2 ~ Beta(a2, b2).

    Exact whenever ``a2`` is a whole number, via the standard finite sum

        P = sum_{i=0}^{a2-1} B(a1+i, b1+b2) / ((b2+i) B(1+i, b2) B(a1, b1))

    Every term is positive, so the sum has no cancellation and is stable at any
    length. ``a2`` whole is the normal case here: it is ``prior_alpha +
    successes``, and successes are counts.
    """
    if _is_whole(a2) and a2 <= _MAX_SERIES_TERMS:
        base = -log_beta_fn(a1, b1)
        total = 0.0
        for i in range(int(round(a2))):
            total += exp(
                log_beta_fn(a1 + i, b1 + b2) - log(b2 + i) - log_beta_fn(1.0 + i, b2) + base
            )
        # Floating-point accumulation can land a hair outside [0, 1]; a
        # probability that reads 1.0000000000000002 in a results table is a
        # bug report waiting to happen.
        return min(1.0, max(0.0, total))
    return _prob_greater_quadrature(a1, b1, a2, b2)


def _prob_greater_quadrature(
    a1: float, b1: float, a2: float, b2: float, *, points: int = 4001
) -> float:
    """P(X2 > X1) = integral of f2(p) I_p(a1, b1) dp, by Simpson's rule.

    Only reached for a non-whole ``a2`` — a fractional prior such as Jeffreys'
    Beta(0.5, 0.5). Integrated over the range holding all but 1e-12 of X2's
    mass, so the truncated tails cannot move the answer at reporting precision.
    """
    n = points if points % 2 == 1 else points + 1
    lo = beta_quantile(1e-12, a2, b2)
    hi = beta_quantile(1.0 - 1e-12, a2, b2)
    if hi <= lo:
        return 1.0 if regularized_incomplete_beta(lo, a1, b1) > 0.5 else 0.0
    step = (hi - lo) / (n - 1)
    norm = log_beta_fn(a2, b2)
    total = 0.0
    for i in range(n):
        p = min(max(lo + i * step, 1e-15), 1.0 - 1e-15)
        density = exp((a2 - 1.0) * log(p) + (b2 - 1.0) * log1p(-p) - norm)
        weight = 1.0 if i in (0, n - 1) else (4.0 if i % 2 else 2.0)
        total += weight * density * regularized_incomplete_beta(p, a1, b1)
    # Mass above `hi` sits where I_p(a1,b1) is at most 1, so it contributes at
    # most 1e-12; below `lo` it contributes at least 0. Both are past the
    # precision anything downstream reports.
    return min(1.0, max(0.0, total * step / 3.0))


def expected_loss(a1: float, b1: float, a2: float, b2: float) -> tuple[float, float]:
    """``(loss if you choose arm 1, loss if you choose arm 2)``.

    Loss is the posterior expected regret in the units of the rate itself:
    ``E[max(p_other - p_chosen, 0)]``. "If I pick this arm and I am wrong, how
    much rate do I give up, averaged over how wrong I might be." It is the
    number that makes a near-coin-flip P(B>A) actionable — two arms can be
    indistinguishable *and* the cost of guessing wrong can be negligible, which
    is a decision, not a stalemate.

    Exact, from the identity ``p f(p; a, b) = (a/(a+b)) f(p; a+1, b)``:

        E[p2 . 1{p2>p1}] = (a2/(a2+b2)) . P(Beta(a2+1, b2) > Beta(a1, b1))

    so each loss is a difference of two :func:`prob_greater` calls and needs no
    integration of its own.
    """
    m1 = a1 / (a1 + b1)
    m2 = a2 / (a2 + b2)
    loss1 = m2 * prob_greater(a1, b1, a2 + 1.0, b2) - m1 * prob_greater(a1 + 1.0, b1, a2, b2)
    loss2 = m1 * prob_greater(a2, b2, a1 + 1.0, b1) - m2 * prob_greater(a2 + 1.0, b2, a1, b1)
    # Both are expectations of a non-negative quantity; a negative here is
    # cancellation between two near-equal probabilities, not a real value.
    return max(loss1, 0.0), max(loss2, 0.0)


def difference_summary(
    a1: float,
    b1: float,
    a2: float,
    b2: float,
    *,
    mass: float = 0.95,
    points: int = 2000,
) -> dict[str, Any]:
    """The posterior of the lift ``X2 - X1``, summarised.

    The difference of two Betas is not a Beta and has no closed-form quantile,
    so this is the one quantity here that is approximated. Both densities are
    evaluated exactly on grids sharing a step, and their discrete convolution
    is the density of the difference — deterministic, and accurate to a small
    multiple of one step, which is returned as ``grid_step`` so nobody has to
    guess at it.

    ``mean`` and ``sd`` are *not* taken off the grid: they are exact
    (``E[X2] - E[X1]`` and ``sqrt(Var X1 + Var X2)``, the arms being
    independent under the model). ``prob_positive`` is off the grid and exists
    to cross-check :func:`prob_greater`, which is the number actually reported.
    """
    import numpy as np

    d1, d2 = Beta(a1, b1), Beta(a2, b2)
    lo1, hi1 = d1.quantile(1e-10), d1.quantile(1.0 - 1e-10)
    lo2, hi2 = d2.quantile(1e-10), d2.quantile(1.0 - 1e-10)
    span = max(hi1 - lo1, hi2 - lo2, 1e-12)
    step = span / points

    def pmf(lo: float, hi: float, a: float, b: float):
        count = max(int(ceil((hi - lo) / step)) + 1, 2)
        grid = lo + step * np.arange(count)
        # Clipped away from the open endpoints so `0 * log(0)` (which is NaN in
        # numpy, not 0) cannot appear when alpha or beta is exactly 1.
        safe = np.clip(grid, 1e-15, 1.0 - 1e-15)
        log_density = (a - 1.0) * np.log(safe) + (b - 1.0) * np.log1p(-safe) - log_beta_fn(a, b)
        weights = np.exp(log_density - log_density.max())
        return grid, weights / weights.sum()

    grid1, p1 = pmf(lo1, hi1, a1, b1)
    grid2, p2 = pmf(lo2, hi2, a2, b2)

    density = np.convolve(p2, p1[::-1])
    start = float(grid2[0] - grid1[-1])
    support = start + step * np.arange(density.size)
    # Midpoint convention: cdf[k] approximates P(D <= support[k]) for a
    # density binned at the grid points, rather than P(D <= support[k]) + half
    # a bin, which biases every quantile by step/2 in the same direction.
    cdf = np.cumsum(density) - 0.5 * density

    keep = np.concatenate(([True], np.diff(cdf) > 0))
    xs, ys = cdf[keep], support[keep]
    tail = (1.0 - mass) / 2.0

    return {
        "mean": d2.mean - d1.mean,
        "sd": sqrt(d1.variance + d2.variance),
        "ci_low": float(np.interp(tail, xs, ys)),
        "ci_high": float(np.interp(1.0 - tail, xs, ys)),
        "prob_positive": float(1.0 - np.interp(0.0, ys, xs)),
        "grid_step": step,
        "grid_points": int(density.size),
    }
