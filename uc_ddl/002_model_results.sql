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
    data_rows            BIGINT,
    data_fallback_reason STRING
)
USING DELTA
COMMENT 'One row per staff/day/shift assignment.';

-- One row per stop visit, in the order the vehicle serves them. The routes
-- are reconstructed from the incumbent, so a cancelled run lands here too --
-- a suboptimal set of routes is still a set of routes.
--
-- The data_* columns are the trips the geometry came from: stop radii,
-- service times and the price of distance are derived from real trips in the
-- `samples` catalog when the job can read it, and generated when it cannot.
CREATE TABLE IF NOT EXISTS main.dbx_leaning.results_gurobi_routing (
    run_id                   STRING  NOT NULL,
    chunk_index              INT     NOT NULL,
    -- Which vehicle, and where in its round this stop falls (1 = first).
    route                    INT     NOT NULL,
    visit_order              INT     NOT NULL,
    stop                     STRING  NOT NULL,
    -- 'depot' for the first stop of a route, otherwise the previous stop.
    previous_stop            STRING,
    x                        DOUBLE,
    y                        DOUBLE,
    service_minutes          DOUBLE,
    leg_distance             DOUBLE,
    leg_cost                 DOUBLE,
    distance_to_depot        DOUBLE,
    -- Repeated per row rather than kept in a second table: the results tables
    -- are read flat, and a route total is what a reader wants next to a stop.
    route_stops              INT,
    route_load_minutes       DOUBLE,
    route_distance           DOUBLE,
    vehicle_capacity_minutes DOUBLE,
    data_source              STRING,
    data_synthetic           BOOLEAN,
    data_rows                BIGINT,
    data_fallback_reason     STRING
)
USING DELTA
COMMENT 'One row per stop visit on a vehicle route.';

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
    -- A systematically thinned, hard-capped slice of this parameter's
    -- post-burn-in draws, per chain, as JSON:
    -- {thin, draws_per_chain, chains_included, chains_total, draws_available,
    --  chains: [[...], ...]}. Enough to redraw a trace or a density after the
    -- run; deliberately not the raw posterior, which is ~6,400 draws per
    -- parameter at the defaults. A STRING rather than VARIANT or a nested
    -- array, per CLAUDE.md: VARIANT is nice-to-have, JSON text works anywhere.
    draws_sample STRING,
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

-- The zero-dependency control case: simulated annealing over a shift-planning
-- knapsack. One row per trip the search chose to take, so the chosen solution
-- is readable as data rather than as a blob. The solution-level columns
-- (objective, totals, baseline) repeat on every row of a run deliberately —
-- Delta is columnar and append-only, and one flat table beats a header/detail
-- join for a reader with a SQL prompt and a question.
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
    -- Provenance of the trips (models._data). A run over real `samples` rows
    -- and one that fell back to the deterministic generator must not look
    -- identical after the fact.
    data_source          STRING,
    data_synthetic       BOOLEAN,
    data_rows            BIGINT,
    data_fallback_reason STRING
)
USING DELTA
COMMENT 'One row per trip in the annealed shift; solution-level columns repeat per row.';

-- The conjugate Bayesian A/B test: three rows per run, not a series. Two arm
-- rows and one comparison row share this schema deliberately — a decision
-- table is read across its rows, and a reader should not have to join two
-- shapes to see both sides of the comparison and the difference between them.
-- `row_type` says which kind of row you are looking at, and the columns that
-- do not apply to it are NULL rather than absent.
CREATE TABLE IF NOT EXISTS main.dbx_leaning.results_bayesian_ab (
    run_id      STRING NOT NULL,
    chunk_index INT    NOT NULL,
    -- 'arm' | 'comparison'.
    row_type    STRING NOT NULL,
    -- 'A' | 'B' for an arm row, 'B_minus_A' for the comparison.
    role        STRING NOT NULL,
    -- The arm's name, e.g. 'weekend_hours'; '<B>_vs_<A>' on the comparison.
    label       STRING NOT NULL,
    -- Counts. On the comparison row these are the pooled totals.
    trials      BIGINT,
    successes   BIGINT,
    -- The raw proportion. NULL for an arm with no observations at all, which
    -- is not the same fact as a rate of zero, and for the comparison row.
    observed_rate DOUBLE,
    -- The conjugate posterior, Beta(prior + successes, prior + failures).
    -- NULL on the comparison row and not by omission: the difference of two
    -- Betas is not a Beta, so it has no such parameters.
    posterior_alpha DOUBLE,
    posterior_beta  DOUBLE,
    -- Arm row: the posterior rate. Comparison row: the lift, E[p_B] - E[p_A].
    -- Both exact.
    posterior_mean  DOUBLE,
    posterior_sd    DOUBLE,
    -- Equal-tailed credible interval holding `credible_mass`. Exact on an arm
    -- row (a Beta quantile); on the comparison row it comes from a grid
    -- convolution of the two posteriors, accurate to a small multiple of the
    -- grid step (models/bayesian_ab/conjugate.py).
    ci_low        DOUBLE,
    ci_high       DOUBLE,
    credible_mass DOUBLE,
    -- P(this arm's rate > the other's). The same number on the comparison
    -- row, read as P(B > A) — the run's primary metric.
    prob_beats_other DOUBLE,
    -- Posterior expected regret, in units of the rate: E[max(other - this, 0)].
    -- On the comparison row, the regret carried by whichever arm leads.
    expected_loss DOUBLE,
    -- The prior actually used. Recorded per run because it is a modelling
    -- choice, and a posterior cannot be re-read later without it.
    prior_alpha DOUBLE,
    prior_beta  DOUBLE,
    -- Which named comparison ran, and what 'success' was defined as —
    -- including the threshold, which for the default comparison is derived
    -- from the data and therefore differs run to run.
    comparison STRING,
    outcome    STRING,
    -- The winning arm's label, or 'inconclusive'. Repeated on every row of the
    -- run so a single row is self-describing.
    decision   STRING,
    conclusive BOOLEAN,
    -- False when the run was cancelled partway: the arm posteriors are still
    -- exact, but the comparison may be missing or incomplete.
    complete   BOOLEAN,
    -- Provenance of the observations (models/_data). A comparison drawn from
    -- real `samples` hours and one drawn from the deterministic fallback must
    -- not look identical after the fact.
    data_source          STRING,
    data_synthetic       BOOLEAN,
    data_rows            BIGINT,
    -- Null on the real path; why the fallback ran otherwise.
    data_fallback_reason STRING
)
USING DELTA
COMMENT 'Conjugate Beta-Binomial A/B decision table: one row per arm plus the comparison.';

-- The heavy-dependency model: a torch feed-forward classifier. A new result
-- shape for this platform — not a series and not a solution, but a
-- classification report: one row per class, with the run-level metrics
-- repeated on each so "was this better than a constant?" is answerable
-- without a join (same flat-table reasoning as results_annealing).
--
-- Target is pace_class (minutes per mile), and the columns that were
-- deliberately withheld from the features are recorded in excluded_features:
-- on the real table any two of the three trip columns predict the third
-- almost exactly, so the leakage decision is part of the result, not a
-- footnote in a docstring.
CREATE TABLE IF NOT EXISTS main.dbx_leaning.results_neural_net (
    run_id      STRING NOT NULL,
    chunk_index INT    NOT NULL,
    -- Per-class rows. class_index is what the network predicts; class_label
    -- is the same value spelled for a human.
    class_index INT    NOT NULL,
    class_label STRING NOT NULL,
    precision   DOUBLE,
    recall      DOUBLE,
    f1          DOUBLE,
    support     INT,
    true_positives  INT,
    false_positives INT,
    false_negatives INT,
    -- This class's row of the confusion matrix, {predicted_label: count} as
    -- JSON. VARIANT is nice-to-have (CLAUDE.md); a JSON string is the
    -- portable floor, and the tp/fp/fn columns above stay queryable as ints.
    confusion_row STRING,
    -- Run-level metrics, repeated per row.
    accuracy          DOUBLE,
    macro_f1          DOUBLE,
    balanced_accuracy DOUBLE,
    -- What predicting the majority class alone would have scored. Without
    -- it a headline accuracy on imbalanced classes hides an expensive
    -- constant function.
    baseline_accuracy   DOUBLE,
    lift_over_baseline  DOUBLE,
    val_loss            DOUBLE,
    val_rows            INT,
    train_rows          INT,
    -- Planned vs trained: a cancelled run stops early and still reports from
    -- its best checkpoint, so these two disagreeing is the record of that.
    epochs_trained      INT,
    epochs_planned      INT,
    cancelled           BOOLEAN,
    -- Reproducibility, in the row rather than in the logs: without the seed
    -- and the device a neural net's numbers cannot be checked. The device is
    -- here because this is the model that would later want GPU compute, and
    -- a CPU run and a GPU run must stay distinguishable after the fact.
    seed                BIGINT,
    device              STRING,
    torch_version       STRING,
    train_time_seconds  DOUBLE,
    -- What was learned, and on what.
    target              STRING,
    pace_cut_low        DOUBLE,
    pace_cut_high       DOUBLE,
    features            STRING,
    -- Columns withheld to avoid target leakage, comma separated.
    excluded_features   STRING,
    -- Provenance of the trips (models/_data). A run on real `samples` rows
    -- and a run that fell back to the deterministic generator must not look
    -- identical after the fact.
    data_source          STRING,
    data_synthetic       BOOLEAN,
    data_rows            BIGINT,
    data_fallback_reason STRING
)
USING DELTA
COMMENT 'Classification report: one row per pace class, run-level metrics repeated.';

-- ---------------------------------------------------------------------------
-- ortools_jobshop
--
-- One row per scheduled operation, which is the natural grain: a job-shop
-- solution IS an assignment of (job, operation) to a machine and a start
-- time, and a flat table of those is directly renderable as a Gantt chart
-- without a join.
--
-- Deliberately parallel to results_gurobi_scheduling in shape, because the
-- interesting comparison between the two solvers is on the same problem
-- expressed the same way. What differs is `solver_status` and the bound
-- columns: CP-SAT reports OPTIMAL/FEASIBLE/INFEASIBLE where Gurobi reports
-- its own set, and only one of the two has a licence-imposed size cap.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS main.dbx_leaning.results_ortools_jobshop (
    run_id             STRING NOT NULL,
    chunk_index        INT,
    -- The schedule itself.
    job_id             INT,
    job_label          STRING,
    operation_index    INT,
    machine_id         INT,
    machine_label      STRING,
    start_minute       INT,
    duration_minutes   INT,
    end_minute         INT,
    -- Run-level, repeated per row so a single SELECT answers "how good was
    -- this schedule" without a join — the same flat-table reasoning the other
    -- results tables use.
    makespan           INT,
    best_bound         DOUBLE,
    solver_status      STRING,
    solutions_found    INT,
    wall_time_seconds  DOUBLE,
    -- Instance size, so a run can be told apart from one that solved a
    -- different problem. Also the number that matters when comparing against
    -- the Gurobi models, which cannot exceed 2000 variables at all.
    n_jobs             INT,
    n_machines         INT,
    n_operations       INT,
    seed               BIGINT,
    -- Provenance (models/_data): a run on real rows and a run that fell back
    -- to the generator must not look identical afterwards.
    data_source          STRING,
    data_synthetic       BOOLEAN,
    data_rows            BIGINT,
    data_fallback_reason STRING
)
USING DELTA
COMMENT 'Job-shop schedule: one row per scheduled operation, run-level metrics repeated.';

-- ---------------------------------------------------------------------------
-- panel_fit
--
-- One row per GROUP, not per observation. The model fits each group
-- independently, so the group is the unit of both work and outcome.
--
-- `status` is the column that makes this table different from every other
-- results table here: a group can FAIL — too few points, a singular design
-- matrix, a non-finite fit — while the RUN succeeds. Those rows are written
-- with their reason and null coefficients rather than dropped, because "we
-- could not fit Chad" and "Chad was never in the data" are different answers
-- and only one of them is a data problem.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS main.dbx_leaning.results_panel_fit (
    run_id             STRING NOT NULL,
    chunk_index        INT,
    -- The unit of work.
    group_key          STRING,
    group_label        STRING,
    n_observations     INT,
    first_period       DOUBLE,
    last_period        DOUBLE,
    -- Outcome for THIS group. 'fitted' or a failure reason; see the comment
    -- above. Null coefficients are expected when this is not 'fitted'.
    status             STRING,
    failure_reason     STRING,
    -- The fit. Degree is configurable, so coefficients are stored as a
    -- delimited string rather than as N columns that would change shape with
    -- config — the two named ones are the ones every degree has.
    intercept          DOUBLE,
    slope              DOUBLE,
    coefficients       STRING,
    degree             INT,
    r_squared          DOUBLE,
    rmse               DOUBLE,
    -- Run-level, repeated per row.
    groups_total       INT,
    groups_fitted      INT,
    groups_failed      INT,
    response           STRING,
    predictor          STRING,
    -- Provenance (models/_data).
    data_source          STRING,
    data_synthetic       BOOLEAN,
    data_rows            BIGINT,
    data_fallback_reason STRING
)
USING DELTA
COMMENT 'Per-group curve fits: one row per group INCLUDING groups that failed to fit.';
