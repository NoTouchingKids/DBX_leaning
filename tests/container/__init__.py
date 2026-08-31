"""Container-based isolation tests.

A package rather than a bare directory so `conftest.py` and the tests can
`from .harness import ...` — the harness is shared code, not a fixture, and is
runnable on its own (`python -m tests.container.harness build`).
"""
