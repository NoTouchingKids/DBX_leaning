"""Simulated annealing over a knapsack — the zero-dependency control case.

**This package depends on nothing.** Not the harness, not the envelope, not
Databricks, not a third-party library. Importing it loads this file, `model.py`
and `data.py`, and stops — `pyspark` is imported inside the two functions that
need it, so a laptop pays for none of it.

    from annealing import Annealing

    m = Annealing(iterations=5_000)
    m.attach(emit=print, should_cancel=lambda: False)
    m.run()

Off a workspace that reads the deterministic fallback trips and writes no
results table, which is exactly what it should do: the model runs, and it does
not pretend to have persisted anything. On a workspace it reads
`samples.nyctaxi.trips` and appends its shift to `results_annealing`.

The harness finds it through the `dbx_leaning.models` entry point declared in
`pyproject.toml`, so nothing has to know where this directory is. A model in
another repository works the same way.
"""

from .data import Dataset, nyc_taxi_trips, write_rows
from .model import RESULT_SCHEMA, Annealing, Problem, build_model

__all__ = [
    "Annealing",
    "Problem",
    "build_model",
    "RESULT_SCHEMA",
    "Dataset",
    "nyc_taxi_trips",
    "write_rows",
]
