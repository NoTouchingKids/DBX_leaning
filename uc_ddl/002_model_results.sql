-- Per-model results tables.
--
-- One per model family, deliberately separate from each other and from the
-- core tables: different models serve different audiences, so they get
-- different Unity Catalog grants. That is also why grants are not in this file
-- — who should see a model's results is a decision for whoever owns that
-- model's audience, not a default.
--
-- ---------------------------------------------------------------------------
-- WHO WRITES THESE ROWS CHANGED IN v4, AND THE COLUMNS DID NOT.
--
-- In v3 a model returned rows from `results()` and the HARNESS wrote them,
-- stamping `run_id` and `chunk_index` on the way through (`job/emitter.py`).
-- There is no such writer any more: `job/delta.py` was deleted outright, the
-- harness owns telemetry and comms and nothing else, and each model reads its
-- own inputs and writes its own results table through Spark. So every column
-- below — including the two the harness used to stamp — is filled by the model
-- itself, in `poststep`.
--
-- The practical consequence for anyone changing a table here: the dict to diff
-- against is the one the model builds and its own schema tuple, e.g.
-- `models/annealing/annealing/model.py::RESULT_SCHEMA`. Nothing checks the two
-- against each other, and the two directions are not symmetric — a column no
-- model fills is harmless clutter, while a row key with no column is a
-- silently dropped field.
--
-- `main.dbx_leaning` is HARDCODED here and is `${var.catalog}` / `${var.schema}`
-- everywhere else. These files are applied by hand and `databricks sql query
-- --file` does no variable substitution, so retargeting a deployment means
-- editing this file in the same commit that changes the variable. See the
-- header of 001_core_tables.sql.
--
-- ONE TABLE ON THIS BRANCH. The other ten went to `dev` with the models they
-- belong to and come back with them, one at a time, as each is ported.
-- ---------------------------------------------------------------------------

-- The annealed shift: one row per trip taken, with the solution repeated on
-- every row. Flat on purpose — these tables are read flat, and a shift total
-- is what a reader wants next to a trip rather than one join away.
--
-- A cancelled run lands here too. The incumbent of a stopped search is still a
-- shift, and discarding it because someone pressed stop would be the wrong
-- behaviour; `cancelled` and the two iteration columns are how a reader tells
-- that case apart afterwards.
CREATE TABLE IF NOT EXISTS main.dbx_leaning.results_annealing (
    run_id      STRING NOT NULL,
    chunk_index INT    NOT NULL,
    -- The chosen trips, ranked by value density (fare per minute) — the order
    -- the preview curve is built in, not the search order.
    rank         INT    NOT NULL,
    item_index   INT    NOT NULL,
    value        DOUBLE,   -- fare for this trip
    weight       DOUBLE,   -- minutes it consumes of the shift
    distance     DOUBLE,
    value_density DOUBLE,
    -- The solution this row belongs to.
    objective    DOUBLE,
    total_value  DOUBLE,
    total_weight DOUBLE,
    items_selected INT,
    -- Planned vs run: a cancelled search stops early and still writes its
    -- incumbent, so these two disagreeing is the record of that.
    iterations_run     INT,
    iterations_planned INT,
    cancelled          BOOLEAN,
    -- The seed is part of the result, not a footnote: without it a
    -- stochastic search is not reproducible and the row cannot be checked.
    seed         BIGINT,
    -- What random-greedy shift-filling achieved on the same instance. The
    -- column that answers "was the search worth its iterations?" without
    -- re-running anything.
    baseline_objective            DOUBLE,
    improvement_over_baseline_pct DOUBLE,
    -- The instance the search ran on.
    items_offered        INT,
    capacity_minutes     DOUBLE,
    total_weight_offered DOUBLE,
    total_value_offered  DOUBLE,
    -- Provenance of the trips (annealing/data.py). A run over real `samples`
    -- rows and one that fell back to the deterministic generator must not look
    -- identical after the fact.
    data_source          STRING,
    data_synthetic       BOOLEAN,
    data_rows            BIGINT,
    data_fallback_reason STRING
)
USING DELTA
COMMENT 'One row per trip in the annealed shift; solution-level columns repeat per row.';
