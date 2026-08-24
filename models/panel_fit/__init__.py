"""Many small independent curve fits over panel data, one per group.

The model on this platform where **individual units may fail without failing
the run** — see `model.py` for why that shape earns its own model, and what
its telemetry does about it.
"""

from .model import (
    FAILURE_REASONS,
    GROUP_STATUSES,
    REASON_NON_FINITE_RESULT,
    REASON_SINGULAR_DESIGN,
    REASON_TOO_FEW_OBSERVATIONS,
    REASON_ZERO_PREDICTOR_VARIANCE,
    STATUS_FAILED,
    STATUS_FITTED,
    PanelFitModel,
    build_model,
)
from .panel_data import DEFAULT_PANEL_TABLE, PanelColumns, load_panel, synthetic_panel

__all__ = [
    "DEFAULT_PANEL_TABLE",
    "FAILURE_REASONS",
    "GROUP_STATUSES",
    "PanelColumns",
    "PanelFitModel",
    "REASON_NON_FINITE_RESULT",
    "REASON_SINGULAR_DESIGN",
    "REASON_TOO_FEW_OBSERVATIONS",
    "REASON_ZERO_PREDICTOR_VARIANCE",
    "STATUS_FAILED",
    "STATUS_FITTED",
    "build_model",
    "load_panel",
    "synthetic_panel",
]
