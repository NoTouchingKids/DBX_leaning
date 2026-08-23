-- Per-model results tables.
--
-- One per model family, deliberately separate from each other and from the
-- core tables: different models serve different audiences, so they get
-- different Unity Catalog grants. Every one carries run_id and chunk_index,
-- stamped by the harness (job/emitter.py) rather than by the model.

-- The data_* columns are the demand curve's provenance, carried on every row:
-- coverage is derived from real hourly volumes in the `samples` catalog when
-- the job can read it, and from a deterministic synthetic curve when it
-- cannot. A reader six months later must be able to tell those apart without
-- going back to the run's logs.
CREATE TABLE IF NOT EXISTS main.dbx_leaning.results_gurobi_scheduling (
    run_id               STRING  NOT NULL,
    chunk_index          INT     NOT NULL,
    staff                STRING  NOT NULL,
    day                  INT     NOT NULL,
    shift                STRING  NOT NULL,
    cost                 DOUBLE,
    preferred            BOOLEAN,
    demand               INT,
    data_source          STRING,
    data_synthetic       BOOLEAN,
    data_rows            INT,
    data_fallback_reason STRING
)
USING DELTA
COMMENT 'One row per staff/day/shift assignment.';

CREATE TABLE IF NOT EXISTS main.dbx_leaning.results_scenario (
    run_id         STRING NOT NULL,
    chunk_index    INT    NOT NULL,
    scenario_index INT    NOT NULL,
    -- What the sweep varied: multipliers on the observed baseline below.
    demand_multiplier    DOUBLE,
    capacity_multiplier  DOUBLE,
    unit_cost_multiplier DOUBLE,
    -- The absolute quantities those multipliers produced.
    demand         DOUBLE,
    capacity       DOUBLE,
    unit_cost      DOUBLE,
    served         DOUBLE,
    shortfall      DOUBLE,
    idle           DOUBLE,
    objective      DOUBLE,
    -- The observed baseline the sweep varied around (models/_data).
    baseline_demand       DOUBLE,
    baseline_peak_demand  DOUBLE,
    baseline_capacity     DOUBLE,
    baseline_unit_cost    DOUBLE,
    -- Provenance of that baseline. A run on real `samples` rows and a run
    -- that fell back to the deterministic generator must not look identical
    -- after the fact.
    data_source            STRING,
    data_synthetic         BOOLEAN,
    data_rows              BIGINT,
    data_fallback_reason   STRING
)
USING DELTA
COMMENT 'One row per evaluated scenario.';

CREATE TABLE IF NOT EXISTS main.dbx_leaning.results_forecasting (
    run_id         STRING NOT NULL,
    chunk_index    INT    NOT NULL,
    step           INT    NOT NULL,
    -- The hour being forecast, epoch ms. NULL only when the caller supplied a
    -- bare series with no timestamps.
    ts             BIGINT,
    forecast       DOUBLE NOT NULL,
    val_mae        DOUBLE,
    val_rmse       DOUBLE,
    epochs_trained INT,
    -- Provenance of the training data (models/_data). A run on real `samples`
    -- rows and a run that fell back to the deterministic generator must not
    -- look identical after the fact.
    data_source            STRING,
    data_synthetic         BOOLEAN,
    data_rows              BIGINT,
    data_fallback_reason   STRING
)
USING DELTA
COMMENT 'One row per forecasted timestep.';

CREATE TABLE IF NOT EXISTS main.dbx_leaning.results_mcmc (
    run_id      STRING  NOT NULL,
    chunk_index INT     NOT NULL,
    parameter   STRING  NOT NULL,
    mean        DOUBLE,
    sd          DOUBLE,
    q05         DOUBLE,
    q50         DOUBLE,
    q95         DOUBLE,
    rhat        DOUBLE,
    draws_used  BIGINT,
    -- False when the run was cancelled: a partial posterior is still usable,
    -- but a reader must be able to tell.
    complete    BOOLEAN,
    -- What was fitted, e.g. 'fare_amount ~ trip_distance'.
    model       STRING,
    -- Provenance of the observations. A posterior fitted to real sample-catalog
    -- trips and one fitted to the offline synthetic fallback must stay
    -- distinguishable here, not only in the run's logs (models/_data).
    data_source           STRING,
    data_synthetic        BOOLEAN,
    data_rows             BIGINT,
    -- Null on the real path; why the fallback ran otherwise.
    data_fallback_reason  STRING
)
USING DELTA
COMMENT 'Posterior summary statistics per parameter.';

CREATE TABLE IF NOT EXISTS main.dbx_leaning.results_streaming (
    run_id      STRING NOT NULL,
    -- Which backtest window this row came from. Written as each window
    -- completes, not at the end of the run.
    chunk_index INT    NOT NULL,
    origin      INT    NOT NULL,
    step        INT    NOT NULL,
    predicted   DOUBLE,
    actual      DOUBLE,
    abs_error   DOUBLE,
    -- Where the backtested series came from. Carried on every row, not only
    -- logged, so a run against `samples.nyctaxi.trips` and one that fell back
    -- to synthetic data stay distinguishable from the results table alone.
    data_source          STRING,
    data_synthetic       BOOLEAN,
    data_rows            BIGINT,
    -- Null when the real table was read. Always present, so the schema does
    -- not depend on whether a given run happened to fall back.
    data_fallback_reason STRING
)
USING DELTA
COMMENT 'Rolling-origin backtest over sample hourly demand, written incrementally.';
