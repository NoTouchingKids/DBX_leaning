"""Conjugate Bayesian A/B test — closed form, no sampler, decision telemetry."""

from .conjugate import (
    Beta,
    beta_quantile,
    difference_summary,
    expected_loss,
    prob_greater,
    regularized_incomplete_beta,
)
from .model import COMPARISONS, STAGES, Arm, BayesianAbModel, build_model

__all__ = [
    "COMPARISONS",
    "STAGES",
    "Arm",
    "BayesianAbModel",
    "Beta",
    "beta_quantile",
    "build_model",
    "difference_summary",
    "expected_loss",
    "prob_greater",
    "regularized_incomplete_beta",
]
