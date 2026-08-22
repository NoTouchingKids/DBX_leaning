-- Per-model results tables.
--
-- One per model family, deliberately separate from each other and from the
-- core tables: different models serve different audiences, so they get
-- different Unity Catalog grants. Every one carries run_id and chunk_index,
-- stamped by the harness (job/emitter.py) rather than by the model.

CREATE TABLE IF NOT EXISTS main.dbx_leaning.results_gurobi_scheduling (
    run_id      STRING  NOT NULL,
    chunk_index INT     NOT NULL,
    staff       STRING  NOT NULL,
    day         INT     NOT NULL,
    shift       STRING  NOT NULL,
    cost        DOUBLE,
    preferred   BOOLEAN
)
USING DELTA
COMMENT 'One row per staff/day/shift assignment.';

CREATE TABLE IF NOT EXISTS main.dbx_leaning.results_scenario (
    run_id         STRING NOT NULL,
    chunk_index    INT    NOT NULL,
    scenario_index INT    NOT NULL,
    demand         DOUBLE,
    capacity       DOUBLE,
    unit_cost      DOUBLE,
    served         DOUBLE,
    shortfall      DOUBLE,
    idle           DOUBLE,
    objective      DOUBLE
)
USING DELTA
COMMENT 'One row per evaluated scenario.';

CREATE TABLE IF NOT EXISTS main.dbx_leaning.results_forecasting (
    run_id         STRING NOT NULL,
    chunk_index    INT    NOT NULL,
    step           INT    NOT NULL,
    forecast       DOUBLE NOT NULL,
    val_mae        DOUBLE,
    val_rmse       DOUBLE,
    epochs_trained INT
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
    complete    BOOLEAN
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
    abs_error   DOUBLE
)
USING DELTA
COMMENT 'Rolling-origin backtest, written incrementally.';
