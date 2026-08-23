"""Rolling-origin backtest — the incremental/chunked results case."""

from .model import StreamingResultsModel, build_model

__all__ = ["StreamingResultsModel", "build_model"]
