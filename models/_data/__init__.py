"""Sample-data access for models.

Reads Databricks' free `samples` catalog when running on a workspace, and
falls back to deterministic synthetic data otherwise — so every model runs
standalone, and says which of the two it did.

Imported by models only. Nothing here touches `app/`, `job/` or `shared/`.
"""

from .datasets import TAXI_TRIPS_TABLE, nyc_taxi_hourly, nyc_taxi_trips
from .sample_data import SAMPLES_CATALOG, Dataset, load, query, samples_available, spark_session

__all__ = [
    "Dataset",
    "SAMPLES_CATALOG",
    "TAXI_TRIPS_TABLE",
    "load",
    "query",
    "samples_available",
    "spark_session",
    "nyc_taxi_hourly",
    "nyc_taxi_trips",
]
