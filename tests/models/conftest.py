"""A model's test harness is two callables. That is the entire coupling."""

from __future__ import annotations

import pytest


class Recorder:
    """Mock ``emit`` — records messages, and validates them against the real
    envelope so a model cannot drift from the contract unnoticed."""

    def __init__(self, cancel_after: int | None = None, cancel_on: str | None = None):
        self.messages: list[tuple[str, dict]] = []
        self.cancel_after = cancel_after
        self.cancel_on = cancel_on
        self._cancelled = False

    def emit(self, type: str, **fields):
        self.messages.append((type, fields))
        if self.cancel_on == type or (
            self.cancel_after is not None and len(self.messages) >= self.cancel_after
        ):
            self._cancelled = True

    def should_cancel(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def of(self, type: str) -> list[dict]:
        return [f for t, f in self.messages if t == type]

    def attach(self, model):
        model.emit = self.emit
        model.should_cancel = self.should_cancel
        return model

    def validate_all(self) -> None:
        """Every message must be a legal envelope message once the harness
        stamps run_id/seq/ts — checked here so a model's own suite catches
        drift without needing the harness."""
        from shared.envelope import make_message

        for seq, (type, fields) in enumerate(self.messages):
            payload = dict(fields)
            rows = payload.pop("rows", None)
            if rows is not None:  # the harness turns rows into a count
                payload.setdefault("row_count", len(rows))
                payload.setdefault("preview", [])
            make_message(type, run_id="t", seq=seq, **payload)


@pytest.fixture
def recorder():
    return Recorder
