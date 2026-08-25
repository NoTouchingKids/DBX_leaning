"""The per-unit-outcome case — the one model where units fail and the run does not.

What these tests are actually protecting, in order of importance:

1. **A failed group is recorded, not dropped.** That is the model's whole
   reason to exist; if a group can vanish, everything else here is decoration.
2. **Every failure reason is reachable**, by the route the code claims reaches
   it, and produces the right row.
3. **The fitted/failed split is on every progress message** and always sums to
   `groups_done` — the number no other model on this platform reports.
4. **The terminal status when every group fails** is a considered answer, not
   an accident of falling off the end of `run()`.
"""

from __future__ import annotations

import pytest

from job.models.panel_fit import (
    FAILURE_REASONS,
    REASON_NON_FINITE_RESULT,
    REASON_SINGULAR_DESIGN,
    REASON_TOO_FEW_OBSERVATIONS,
    REASON_ZERO_PREDICTOR_VARIANCE,
    STATUS_FAILED,
    STATUS_FITTED,
    build_model,
)

PROVENANCE_FIELDS = {
    "data_source",
    "data_synthetic",
    "data_rows",
    "data_fallback_reason",
}

#: Exactly the columns of `results_panel_fit` in `uc_ddl/002_model_results.sql`,
#: minus the two the harness stamps (`run_id`, `chunk_index`). A row with a
#: column the DDL lacks passes every offline test and then fails its first
#: real write on a workspace, which is the failure this set exists to catch.
DDL_COLUMNS = {
    "group_key",
    "group_label",
    "n_observations",
    "first_period",
    "last_period",
    "status",
    "failure_reason",
    "intercept",
    "slope",
    "coefficients",
    "degree",
    "r_squared",
    "rmse",
    "groups_total",
    "groups_fitted",
    "groups_failed",
    "response",
    "predictor",
    *PROVENANCE_FIELDS,
}


def panel(*groups: tuple[str, list[tuple[float, float | None]]]) -> list[dict]:
    """A hand-built panel: `(entity, [(year, life_expectancy), ...])`."""
    return [
        {"entity": entity, "code": entity[:3].upper(), "year": year, "life_expectancy": value}
        for entity, points in groups
        for year, value in points
    ]


def trend(entity: str, n: int, *, start: int = 1990) -> tuple[str, list[tuple[float, float]]]:
    return entity, [(start + i, 60.0 + 0.4 * i) for i in range(n)]


def run(config: dict, recorder, **recorder_kwargs):
    r = recorder(**recorder_kwargs)
    model = r.attach(build_model(config))
    status = model.run()
    return r, model, status


def rows_by_key(model) -> dict[str, dict]:
    return {row["group_key"]: row for row in model.group_rows}


# --- the point of the model ------------------------------------------------


def test_a_group_that_cannot_be_fitted_gets_a_row_rather_than_disappearing(recorder):
    """ "We could not fit Chad" and "Chad was never in the data" are different
    answers. Dropping the group would make them the same one."""
    _, model, _ = run({"rows": panel(trend("Fine", 20), ("Chad", [(2001, 55.0)]))}, recorder)

    rows = rows_by_key(model)
    assert set(rows) == {"Fine", "Chad"}
    assert rows["Chad"]["status"] == STATUS_FAILED
    assert rows["Chad"]["failure_reason"] == REASON_TOO_FEW_OBSERVATIONS
    assert rows["Chad"]["intercept"] is None and rows["Chad"]["slope"] is None
    assert rows["Chad"]["r_squared"] is None


def test_the_default_run_succeeds_with_some_units_failed(recorder):
    """The headline scenario: a SUCCEEDED run carrying real per-unit failures.

    If the synthetic fallback ever stops producing unfittable groups, this
    model's entire subject matter becomes untestable — so that is asserted
    here rather than left to the generator's good intentions."""
    r, model, status = run({}, recorder)

    assert status is None, "a run with fits in it is a plain success"
    assert model.groups_fitted > 0
    assert model.groups_failed > 0, "the fallback panel must exercise the failure paths"
    assert model.groups_fitted + model.groups_failed == len(model.groups)
    r.validate_all()


def test_the_synthetic_panel_has_deliberately_varied_group_sizes(recorder):
    """A panel where every group is the same size hides every bug that depends
    on group size — including the one this model is about."""
    _, model, _ = run({}, recorder)
    sizes = sorted(len(group.x) for group in model.groups)
    assert min(sizes) <= 2, "some groups must be too small to fit"
    assert max(sizes) >= 40, "some groups must be long enough for a real trend"
    assert len(set(sizes)) >= 10, f"only {len(set(sizes))} distinct group sizes"


def test_the_run_is_deterministic(recorder):
    first = run({}, recorder)[1]
    second = run({}, recorder)[1]
    assert first.group_rows == second.group_rows


# --- every failure reason, by the route that reaches it --------------------


def test_too_few_observations_from_a_group_that_is_simply_short(recorder):
    _, model, _ = run(
        {"rows": panel(trend("Long", 12), ("Short", [(2001, 50.0), (2002, 51.0)]))}, recorder
    )
    assert rows_by_key(model)["Short"]["failure_reason"] == REASON_TOO_FEW_OBSERVATIONS


def test_too_few_observations_after_null_responses_are_dropped(recorder):
    """The route that matters. The group has nine rows and looks perfectly
    healthy right up until the nulls go — which is why `job/models/README.md` says
    never to assume a column is non-null."""
    gappy = ("Gappy", [(1990 + i, 61.0 if i == 0 else None) for i in range(9)])
    _, model, _ = run({"rows": panel(trend("Long", 12), gappy)}, recorder)

    row = rows_by_key(model)["Gappy"]
    assert row["failure_reason"] == REASON_TOO_FEW_OBSERVATIONS
    assert row["n_observations"] == 1, "n_observations counts usable rows, not rows"
    # It still says when the group existed, which is the difference between a
    # data-quality answer and a shrug.
    assert (row["first_period"], row["last_period"]) == (1990.0, 1998.0)


def test_zero_predictor_variance_from_one_reporting_year(recorder):
    once = ("Once", [(2004, 60.0), (2004, 61.0), (2004, 59.5), (2004, 60.2)])
    _, model, _ = run({"rows": panel(trend("Long", 12), once)}, recorder)

    row = rows_by_key(model)["Once"]
    assert row["failure_reason"] == REASON_ZERO_PREDICTOR_VARIANCE
    assert row["n_observations"] == 4, "the observations are usable; the predictor is not"


def test_zero_predictor_variance_is_reported_ahead_of_singular_design(recorder):
    """One distinct predictor value *is* a rank-deficient design. The specific
    reason has to win, because it tells a reader what to do and the general
    one does not."""
    once = ("Once", [(2004, 60.0 + i) for i in range(6)])
    _, model, _ = run({"degree": 2, "rows": panel(trend("Long", 12), once)}, recorder)
    assert rows_by_key(model)["Once"]["failure_reason"] == REASON_ZERO_PREDICTOR_VARIANCE


def test_singular_design_from_fewer_distinct_periods_than_coefficients(recorder):
    """Two distinct years fit a line and cannot fit a parabola. Same group,
    same data, different degree — so this is genuinely about the design matrix
    and not about the group being short."""
    two_years = ("Two", [(2000, 60.0), (2000, 61.0), (2010, 66.0), (2010, 65.0)])
    rows = panel(trend("Long", 12), two_years)

    _, linear, _ = run({"degree": 1, "rows": rows}, recorder)
    assert rows_by_key(linear)["Two"]["status"] == STATUS_FITTED

    _, quadratic, _ = run({"degree": 2, "rows": rows}, recorder)
    assert rows_by_key(quadratic)["Two"]["failure_reason"] == REASON_SINGULAR_DESIGN


def test_non_finite_result_from_arithmetic_that_overflows(recorder):
    """Every input here is a finite float. The residual sum of squares is not,
    and a fit that came back as infinity must not be handed on as a number."""
    huge = ("Huge", [(2000 + i, 1e200 * (i + 1)) for i in range(10)])
    _, model, _ = run({"rows": panel(trend("Long", 12), huge)}, recorder)

    row = rows_by_key(model)["Huge"]
    assert row["failure_reason"] == REASON_NON_FINITE_RESULT
    assert row["rmse"] is None and row["r_squared"] is None


def test_every_failure_reason_is_reachable():
    """Guards the closed set itself: a reason nothing can produce is either
    dead code or a check that silently stopped firing."""
    reached = set()
    for config, rows in (
        ({}, panel(("A", [(2001, 50.0)]))),
        ({}, panel(("A", [(2004, 60.0 + i) for i in range(5)]))),
        ({"degree": 2}, panel(("A", [(2000, 60.0), (2000, 61.0), (2010, 66.0), (2010, 65.0)]))),
        ({}, panel(("A", [(2000 + i, 1e200 * (i + 1)) for i in range(10)]))),
    ):
        model = build_model({**config, "rows": rows})
        model.emit = lambda *a, **k: None
        model.should_cancel = lambda: False
        model.run()
        reached.add(model.group_rows[0]["failure_reason"])

    assert reached == set(FAILURE_REASONS)


def test_a_failed_row_never_carries_a_reason_outside_the_closed_set(recorder):
    """Free text per group would be unusable: the first thing a UI does with
    failures is group by reason."""
    _, model, _ = run({}, recorder)
    for row in model.group_rows:
        if row["status"] == STATUS_FAILED:
            assert row["failure_reason"] in FAILURE_REASONS
        else:
            assert row["failure_reason"] is None


# --- a run where nothing fails, and one where everything does --------------


def test_a_run_where_no_group_fails_reports_no_failures_anywhere(recorder):
    r, model, status = run(
        {"rows": panel(trend("A", 20), trend("B", 15), trend("C", 30))}, recorder
    )

    assert status is None
    assert (model.groups_fitted, model.groups_failed) == (3, 0)
    assert all(row["status"] == STATUS_FITTED for row in model.group_rows)
    for progress in r.of("progress"):
        assert progress["payload"]["groups_failed"] == 0
        assert progress["payload"]["failure_counts"] == {}


def test_a_run_where_every_group_fails_reports_infeasible_not_success(recorder):
    """Not SUCCEEDED: zero fits is not a success, and `row_count` cannot tell
    the difference either — the failures are recorded, so the count looks
    healthy. Not FAILED: nothing went wrong and a retry would produce the
    same thing. INFEASIBLE already means "it ran, and the answer is that there
    isn't one"."""
    r, model, status = run(
        {"rows": panel(("A", [(2001, 50.0)]), ("B", [(2002, 51.0), (2003, 52.0)]))}, recorder
    )

    assert status == "INFEASIBLE"
    assert model.groups_fitted == 0
    assert len(model.group_rows) == 2, "the failures are still recorded"
    r.validate_all()


def test_the_all_failed_status_is_a_real_run_status_member():
    """The trap in `job/models/README.md`: a returned string that is not a
    `RunStatus` member silently becomes a *detail* on a SUCCEEDED run, so a
    typo would degrade into the exact ambiguity this status exists to remove."""
    from shared.envelope import RunStatus

    model = build_model({"rows": panel(("A", [(2001, 50.0)]))})
    model.emit = lambda *a, **k: None
    model.should_cancel = lambda: False
    assert RunStatus(model.run()) is RunStatus.INFEASIBLE


def test_an_empty_panel_is_infeasible_and_still_closes_its_results(recorder):
    r, model, status = run({"rows": []}, recorder)

    assert status == "INFEASIBLE"
    assert model.group_rows == []
    (result,) = r.of("result")
    assert result["rows"] == [] and result["final"] is True
    r.validate_all()


def test_rows_that_cannot_be_placed_are_dropped_before_grouping(recorder):
    """No key means no group; no period means no place on the axis. Grouping
    these would put a unit called None in a table whose whole promise is one
    row per real unit."""
    rows = panel(trend("A", 12))
    rows += [
        {"entity": None, "code": "XXX", "year": 1990, "life_expectancy": 60.0},
        {"entity": "B", "code": "B", "year": None, "life_expectancy": 60.0},
    ]
    _, model, _ = run({"rows": rows}, recorder)

    assert [group.key for group in model.groups] == ["A"]
    assert model.rows_unplaceable == 2


# --- the telemetry contract ------------------------------------------------


def test_every_progress_message_carries_the_fitted_failed_split(recorder):
    """The thing no other model on this platform reports. A client has to be
    able to tell a healthy run from one quietly failing a third of its units
    without waiting for the results."""
    r, model, _ = run({}, recorder)

    progresses = r.of("progress")
    assert progresses
    for message in progresses:
        payload = message["payload"]
        assert {"groups_done", "groups_total", "groups_fitted", "groups_failed"} <= set(payload)


def test_the_fitted_and_failed_counts_always_sum_to_groups_done(recorder):
    r, _, _ = run({}, recorder)
    for message in r.of("progress"):
        payload = message["payload"]
        assert payload["groups_fitted"] + payload["groups_failed"] == payload["groups_done"]
        assert sum(payload["failure_counts"].values()) == payload["groups_failed"]


def test_percent_complete_is_groups_done_over_groups_total(recorder):
    r, model, _ = run({}, recorder)
    total = len(model.groups)
    for message in r.of("progress"):
        expected = 100.0 * message["payload"]["groups_done"] / total
        assert message["percent_complete"] == pytest.approx(expected)
    assert r.of("progress")[-1]["percent_complete"] == 100.0


def test_progress_names_the_group_it_is_about(recorder):
    r, model, _ = run({}, recorder)
    keys = [message["payload"]["group_key"] for message in r.of("progress")]
    assert keys == [group.key for group in model.groups]


def test_the_primary_metric_is_the_median_r_squared_of_what_is_fitted(recorder):
    import statistics

    r, model, _ = run({}, recorder)
    last = r.of("progress")[-1]
    assert last["primary_metric_label"] == "median_r_squared"
    fitted = [
        row["r_squared"]
        for row in model.group_rows
        if row["status"] == STATUS_FITTED and row["r_squared"] is not None
    ]
    assert last["primary_metric"] == pytest.approx(statistics.median(fitted))
    assert last["payload"]["metric_higher_is_better"] is True


def test_the_primary_metric_is_null_until_something_has_been_fitted(recorder):
    """Null is legal on the envelope and honest here. A zero would read as
    "fitted, and terrible"."""
    r, _, _ = run({"rows": panel(("A", [(2001, 50.0)]), trend("B", 20))}, recorder)
    assert r.of("progress")[0]["primary_metric"] is None
    assert r.of("progress")[-1]["primary_metric"] is not None


def test_every_message_validates_against_the_real_envelope(recorder):
    r, _, _ = run({}, recorder)
    r.validate_all()


# --- chunked results -------------------------------------------------------


def test_results_arrive_in_more_than_one_chunk(recorder):
    r, model, _ = run({"chunk_size": 5}, recorder)
    results = r.of("result")
    assert len(results) > 1
    assert model.chunks_emitted == len(results)


def test_only_the_last_chunk_is_marked_final(recorder):
    r, _, _ = run({"chunk_size": 5}, recorder)
    flags = [result["final"] for result in r.of("result")]
    assert flags[-1] is True
    assert not any(flags[:-1])


def test_the_chunks_together_are_exactly_the_groups_in_order(recorder):
    """Nothing lost at a boundary, nothing written twice."""
    r, model, _ = run({"chunk_size": 5}, recorder)
    emitted = [row for result in r.of("result") for row in result["rows"]]
    assert [row["group_key"] for row in emitted] == [group.key for group in model.groups]


def test_a_chunk_boundary_never_splits_or_repeats_a_group(recorder):
    r, model, _ = run({"chunk_size": 5}, recorder)
    keys = [row["group_key"] for result in r.of("result") for row in result["rows"]]
    assert len(keys) == len(set(keys)) == len(model.groups)


def test_the_model_supplies_neither_chunk_index_nor_row_count(recorder):
    """Both are the harness's to assign; a model that guessed them would drift
    from what was actually written."""
    r, _, _ = run({"chunk_size": 5}, recorder)
    for result in r.of("result"):
        assert "chunk_index" not in result and "row_count" not in result


def test_exactly_one_final_chunk_on_every_path(recorder):
    """Completed, cancelled or empty — a client's "results are complete"
    condition has to be reachable, or it waits forever."""
    for kwargs in ({}, {"cancel_on": "result"}, {"cancel_after": 3}):
        r, _, _ = run({"chunk_size": 5}, recorder, **kwargs)
        assert sum(1 for result in r.of("result") if result["final"]) == 1

    r, _, _ = run({"rows": []}, recorder)
    assert sum(1 for result in r.of("result") if result["final"]) == 1


# --- cancellation ----------------------------------------------------------


def test_cancelling_keeps_every_group_already_fitted(recorder):
    r, model, status = run({"chunk_size": 5}, recorder, cancel_after=30)

    assert model.cancelled
    assert 0 < len(model.group_rows) < len(model.groups)
    emitted = [row for result in r.of("result") for row in result["rows"]]
    assert [row["group_key"] for row in emitted] == [
        row["group_key"] for row in model.group_rows
    ], "everything completed before the stop is written, and nothing else"
    assert status is None, "cancellation is the harness's call, not the model's"


def test_cancelling_flushes_the_partial_chunk_rather_than_discarding_it(recorder):
    """Results are not best-effort. A buffered group that was fitted before
    the stop is a real result and must not evaporate with the buffer."""
    r, model, _ = run({"chunk_size": 1000}, recorder, cancel_after=20)

    assert model.cancelled
    (result,) = r.of("result")
    assert result["rows"], "the buffer was flushed, not dropped"
    assert len(result["rows"]) == len(model.group_rows)


def test_cancellation_happens_between_groups_not_mid_fit(recorder):
    """Every row written is a complete row: a status, and either coefficients
    or a reason. There is no half-fitted group."""
    _, model, _ = run({}, recorder, cancel_after=20)
    for row in model.group_rows:
        assert row["status"] in (STATUS_FITTED, STATUS_FAILED)
        if row["status"] == STATUS_FITTED:
            assert row["coefficients"] and row["slope"] is not None
        else:
            assert row["failure_reason"] in FAILURE_REASONS


def test_a_cancelled_run_with_no_fits_does_not_claim_infeasible(recorder):
    """INFEASIBLE means "it ran and there is no answer". A run stopped before
    it got to a fittable group has not established that."""
    _, model, status = run({}, recorder, cancel_after=1)
    assert model.cancelled and status is None


# --- the result rows -------------------------------------------------------


def test_result_rows_carry_exactly_the_ddl_columns(recorder):
    _, model, _ = run({}, recorder)
    for row in model.group_rows:
        assert set(row) == DDL_COLUMNS


def test_provenance_rides_on_the_rows_not_only_in_a_log(recorder):
    """Logs are droppable by contract. "Was this real data?" has to survive to
    the durable record."""
    _, model, _ = run({}, recorder)
    for row in model.group_rows:
        assert PROVENANCE_FIELDS <= set(row)
        assert row["data_synthetic"] is True
        assert row["data_fallback_reason"], "a fallback that does not say why is a silent one"


def test_the_fallback_is_reported_rather_than_hidden(recorder):
    r, _, _ = run({}, recorder)
    inputs = [message["message"] for message in r.of("log") if message["phase"] == "input"]
    assert any("synthetic" in line for line in inputs)


def test_coefficients_are_stored_in_increasing_powers(recorder):
    """A delimited string rather than N columns, because `degree` is
    configurable — so the order has to be stated somewhere a reader can check."""
    _, model, _ = run({"degree": 2, "rows": panel(trend("A", 25))}, recorder)
    row = model.group_rows[0]
    parts = [float(part) for part in row["coefficients"].split(",")]

    assert len(parts) == 3 == row["degree"] + 1
    assert parts[0] == pytest.approx(row["intercept"])
    assert parts[1] == pytest.approx(row["slope"])


def test_a_clean_linear_trend_recovers_its_slope(recorder):
    """The arithmetic itself, on data whose answer is known: 0.4 per year."""
    _, model, _ = run({"rows": panel(trend("A", 30))}, recorder)
    row = model.group_rows[0]
    assert row["slope"] == pytest.approx(0.4)
    assert row["r_squared"] == pytest.approx(1.0)
    assert row["rmse"] == pytest.approx(0.0, abs=1e-6)


def test_a_group_whose_response_never_moves_is_fitted_with_a_null_r_squared(recorder):
    """No variance to explain means R-squared is undefined, not zero and not
    one. The fit itself is fine — a flat line through a constant — so this is
    a null metric on a *fitted* row, not a failure."""
    flat = ("Flat", [(2000 + i, 70.0) for i in range(10)])
    _, model, _ = run({"rows": panel(flat)}, recorder)

    row = model.group_rows[0]
    assert row["status"] == STATUS_FITTED
    assert row["r_squared"] is None
    assert row["rmse"] == pytest.approx(0.0, abs=1e-9)


def test_groups_total_is_the_true_total_on_every_row_including_the_first_chunk(recorder):
    r, model, _ = run({"chunk_size": 5}, recorder)
    for result in r.of("result"):
        for row in result["rows"]:
            assert row["groups_total"] == len(model.groups)


def test_the_denormalised_counts_are_consistent_within_a_chunk(recorder):
    """They cannot be run totals — chunk 0 is written long before the run has
    any. What they can be, and are, is the counts as of the end of the chunk
    the row belongs to, identical for every row in it and monotonic across
    chunks. The true totals are in the table by construction anyway, as
    `COUNT(*) WHERE status = 'fitted'`."""
    r, model, _ = run({"chunk_size": 5}, recorder)

    previous = (0, 0)
    for result in r.of("result"):
        if not result["rows"]:
            continue
        pairs = {(row["groups_fitted"], row["groups_failed"]) for row in result["rows"]}
        assert len(pairs) == 1, "one chunk, one pair of counts"
        current = pairs.pop()
        assert current >= previous
        previous = current

    assert previous == (model.groups_fitted, model.groups_failed)


def test_the_group_label_survives_being_null_on_some_rows(recorder):
    """OWID's `Code` is null for aggregates like "World" and null on some rows
    of entities that do have one."""
    rows = [
        {"entity": "World", "code": None, "year": 2000 + i, "life_expectancy": 60.0 + i}
        for i in range(8)
    ]
    rows += [
        {
            "entity": "Kenya",
            "code": None if i else "KEN",
            "year": 2000 + i,
            "life_expectancy": 55.0 + i,
        }
        for i in range(8)
    ]
    _, model, _ = run({"rows": rows}, recorder)

    labels = {row["group_key"]: row["group_label"] for row in model.group_rows}
    assert labels == {"World": None, "Kenya": "KEN"}


def test_group_order_does_not_depend_on_the_order_rows_came_back(recorder):
    """SQL promises no ordering. A model whose chunking moved with it would
    produce a different results table on every run of the same data."""
    rows = panel(trend("B", 12), trend("A", 15), trend("C", 9))
    forward = run({"rows": rows}, recorder)[1]
    backward = run({"rows": list(reversed(rows))}, recorder)[1]

    assert [row["group_key"] for row in forward.group_rows] == ["A", "B", "C"]
    assert forward.group_rows == backward.group_rows


# --- what the harness would discover ---------------------------------------


def test_the_harness_finds_a_factory_a_build_and_a_run():
    from job.loader import describe_object

    handle = describe_object(build_model(), "job.models.panel_fit")
    assert handle.run is not None
    assert handle.build is not None
    assert handle.results_table == "results_panel_fit"


def test_this_model_deliberately_exposes_no_results_accessor():
    """It has already streamed every group; a `results()` the harness also
    called would double-write the whole table."""
    from job.loader import describe_object

    assert describe_object(build_model(), "job.models.panel_fit").results is None
    assert not hasattr(build_model(), "results")


def test_build_is_idempotent_and_run_works_without_it(recorder):
    r = recorder()
    model = r.attach(build_model())
    model.build()
    groups = model.groups
    model.build()
    assert model.groups is groups

    standalone = recorder().attach(build_model())
    standalone.run()
    assert standalone.group_rows


def test_the_provenance_line_is_emitted_from_build_not_the_constructor(recorder):
    """The harness constructs the model and attaches `emit` afterwards, so a
    log line from `__init__` goes nowhere."""
    r = recorder()
    model = build_model()  # not attached yet
    assert r.messages == []
    r.attach(model).build()
    assert any(message["phase"] == "input" for message in r.of("log"))


# --- configuration ---------------------------------------------------------


def test_a_configured_table_reaches_the_query_and_still_falls_back(recorder):
    """There is no OWID table in Unity Catalog, so pointing at one has to
    degrade to the generator rather than to an exception."""
    _, model, _ = run({"table": "main.somewhere.owid_life_expectancy"}, recorder)
    assert model.data is not None
    assert model.data.synthetic
    assert model.group_rows


@pytest.mark.parametrize(
    "config",
    [
        {"table": "main.dbx; DROP TABLE x"},
        {"group_column": "entity; --"},
        {"period_column": "1year"},
        {"response_column": ""},
    ],
)
def test_identifiers_are_rejected_rather_than_interpolated_into_sql(config):
    """These arrive as a job parameter and there is no bound-parameter form
    for an identifier, so the check has to happen here or nowhere."""
    with pytest.raises(ValueError):
        build_model(config)


def test_a_predictor_other_than_the_period_is_honoured(recorder):
    """ "Response against a covariate" is the other natural question on a
    panel, and the fallback has to generate that column too."""
    _, model, _ = run({"predictor_column": "gdp_per_capita"}, recorder)

    assert model.groups_fitted > 0
    assert all(row["predictor"] == "gdp_per_capita" for row in model.group_rows)
    # Every year is distinct within a group, so a per-year panel can never
    # reach zero predictor variance; a GDP series is what makes that check
    # depend on the data rather than on the column choice.
    assert {group.key for group in model.groups}


def test_min_observations_defaults_above_an_exactly_determined_fit(recorder):
    """At `degree + 1` points the fit passes through every one of them and
    R-squared is 1 by construction — a number that looks like a triumph and
    means nothing."""
    for degree in (1, 2, 3):
        model = build_model({"degree": degree})
        assert model.min_observations == degree + 2


def test_min_observations_is_configurable_and_actually_binds(recorder):
    rows = panel(trend("A", 6), trend("B", 20))
    _, lenient, _ = run({"min_observations": 3, "rows": rows}, recorder)
    _, strict, _ = run({"min_observations": 10, "rows": rows}, recorder)

    assert rows_by_key(lenient)["A"]["status"] == STATUS_FITTED
    assert rows_by_key(strict)["A"]["failure_reason"] == REASON_TOO_FEW_OBSERVATIONS
    assert rows_by_key(strict)["B"]["status"] == STATUS_FITTED


def test_max_groups_bounds_the_work_without_changing_the_shape(recorder):
    r, model, _ = run({"max_groups": 4}, recorder)
    assert len(model.groups) == 4
    assert all(message["payload"]["groups_total"] == 4 for message in r.of("progress"))


def test_progress_can_be_throttled_for_a_panel_with_many_groups(recorder):
    """One message per group is right for hundreds of units and wrong for
    thousands."""
    r, model, _ = run({"progress_every": 10}, recorder)
    progresses = r.of("progress")
    assert len(progresses) < len(model.groups)
    # The last group always reports, so the curve lands on 100 rather than
    # stopping short — which reads as a run that died.
    assert progresses[-1]["percent_complete"] == 100.0


def test_a_degree_below_one_is_rejected_at_construction():
    with pytest.raises(ValueError):
        build_model({"degree": 0})


def test_failure_logging_is_capped_but_the_rows_are_not(recorder):
    """The results table already has every failure with its reason; past a
    couple of dozen the log is chatter a live channel would drop anyway."""
    rows = [row for index in range(40) for row in panel((f"Tiny{index:02d}", [(2000, 50.0)]))]
    rows += panel(trend("Long", 20))
    r, model, _ = run({"failure_log_limit": 5, "rows": rows}, recorder)

    warnings = [message for message in r.of("log") if message["level"] == "WARNING"]
    assert len(warnings) == 6, "five failures, then one line saying it stopped"
    assert model.groups_failed == 40
