"""The Gurobi driver, against a stub. No gurobipy needed to test the parts
that are ours: callback composition, log line reassembly, the ±1e100 sentinel,
and cancellation via terminate().
"""

from __future__ import annotations

from types import SimpleNamespace

from job.drivers.gurobi import GurobiDriver
from job.loader import describe_object
from job.shared.envelope import RunStatus


class Callback:
    POLLING, MESSAGE, MIP, SIMPLEX = 0, 1, 2, 3
    MSG_STRING = 10
    RUNTIME = 11
    MIP_OBJBST, MIP_OBJBND = 12, 13
    MIP_NODCNT, MIP_NODLFT, MIP_SOLCNT = 14, 15, 16


class GRB:
    Callback = Callback
    INFINITY = 1e100
    OPTIMAL, SUBOPTIMAL, INFEASIBLE, INF_OR_UNBD = 2, 13, 3, 4
    UNBOUNDED, TIME_LIMIT, INTERRUPTED = 5, 9, 11
    NODE_LIMIT, SOLUTION_LIMIT, ITERATION_LIMIT, USER_OBJ_LIMIT = 6, 7, 8, 10


class StubModel:
    """Stands in for a gurobipy.Model. ``script`` is the callback sequence."""

    def __init__(self, script=(), status=GRB.OPTIMAL):
        self.script = list(script)
        self.Status = status
        self.params = {}
        self.terminated = False
        self._cb_values: dict[int, object] = {}

    def setParam(self, k, v):
        self.params[k] = v

    def terminate(self):
        self.terminated = True
        self.Status = GRB.INTERRUPTED

    def cbGet(self, what):
        return self._cb_values[what]

    def optimize(self, callback):
        for where, values in self.script:
            if self.terminated:
                break
            self._cb_values = values
            callback(self, where)


def driver_for(model, emitted, should_cancel=lambda: False, **kw):
    handle = describe_object(SimpleNamespace(grb_model=model), "stub")
    return GurobiDriver(
        handle,
        lambda type, **f: emitted.append((type, f)),
        should_cancel,
        grb=GRB,
        **kw,
    )


def test_output_params_stop_double_logging():
    model = StubModel()
    driver_for(model, []).run()
    assert model.params == {"OutputFlag": 1, "LogToConsole": 0}


def test_log_chunks_are_reassembled_into_whole_lines():
    # MESSAGE fires on arbitrary text chunks, not line boundaries.
    model = StubModel(
        script=[
            (Callback.MESSAGE, {Callback.MSG_STRING: "Optimize a model with "}),
            (Callback.MESSAGE, {Callback.MSG_STRING: "840 rows\nPresolve removed"}),
            (Callback.MESSAGE, {Callback.MSG_STRING: " 12 rows\n"}),
        ]
    )
    emitted = []
    driver_for(model, emitted).run()

    lines = [f["message"] for t, f in emitted if t == "log"]
    assert lines == ["Optimize a model with 840 rows", "Presolve removed 12 rows"]
    assert all(f["source"] == "gurobi" for t, f in emitted if t == "log")


def test_a_trailing_partial_line_is_flushed_at_the_end():
    model = StubModel(script=[(Callback.MESSAGE, {Callback.MSG_STRING: "no newline here"})])
    emitted = []
    driver_for(model, emitted).run()
    assert [f["message"] for t, f in emitted if t == "log"] == ["no newline here"]


def _mip(runtime=1.0, best=100.0, bound=90.0, nodes=5, left=2, sols=1):
    return (
        Callback.MIP,
        {
            Callback.RUNTIME: runtime,
            Callback.MIP_OBJBST: best,
            Callback.MIP_OBJBND: bound,
            Callback.MIP_NODCNT: nodes,
            Callback.MIP_NODLFT: left,
            Callback.MIP_SOLCNT: sols,
        },
    )


def test_the_pre_incumbent_sentinel_never_reaches_a_progress_message():
    # ±1e100 is finite, so no NaN/inf guard catches it. It must become null.
    model = StubModel(script=[_mip(best=1e100, bound=-1e100)])
    emitted = []
    driver_for(model, emitted, progress_every_s=0).run()

    progress = [f for t, f in emitted if t == "progress"]
    assert progress, "no progress emitted"
    assert progress[0]["payload"]["incumbent"] is None
    assert progress[0]["payload"]["best_bound"] is None
    assert progress[0]["primary_metric"] is None
    assert not any(abs(v) >= 1e100 for v in _numbers(progress[0]))


def _numbers(d):
    for v in d.values():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            yield v
        elif isinstance(v, dict):
            yield from _numbers(v)


def test_the_mip_gap_is_the_primary_metric():
    model = StubModel(script=[_mip(best=100.0, bound=90.0)])
    emitted = []
    driver_for(model, emitted, progress_every_s=0).run()

    p = [f for t, f in emitted if t == "progress"][0]
    assert p["primary_metric_label"] == "mip_gap"
    assert abs(p["primary_metric"] - 0.1) < 1e-9
    assert p["percent_complete"] is None, "MIP progress is not a percentage"
    assert p["payload"]["nodes_explored"] == 5


def test_progress_is_throttled_because_mip_fires_constantly():
    model = StubModel(script=[_mip() for _ in range(500)])
    emitted = []
    driver_for(model, emitted, progress_every_s=60).run()
    assert len([1 for t, _ in emitted if t == "progress"]) == 1


def test_cancellation_terminates_the_solve_and_is_a_clean_outcome():
    model = StubModel(script=[(Callback.POLLING, {}), _mip()], status=GRB.OPTIMAL)
    result = driver_for(model, [], should_cancel=lambda: True).run()

    assert model.terminated is True
    assert result.status is RunStatus.CANCELLED  # not FAILED


def test_the_models_own_callback_is_composed_not_replaced():
    # Gurobi allows one callback slot; the harness needs it and so may the model.
    seen = []
    obj = SimpleNamespace(
        grb_model=StubModel(script=[_mip()]),
        gurobi_callback=lambda m, where: seen.append(where),
    )
    handle = describe_object(obj, "with-callback")
    GurobiDriver(handle, lambda *a, **k: None, lambda: False, grb=GRB, progress_every_s=0).run()

    assert seen == [Callback.MIP]


def test_a_raising_model_callback_does_not_break_the_solve():
    def boom(m, where):
        raise RuntimeError("model callback bug")

    obj = SimpleNamespace(grb_model=StubModel(script=[_mip()]), gurobi_callback=boom)
    result = GurobiDriver(
        describe_object(obj, "x"), lambda *a, **k: None, lambda: False, grb=GRB
    ).run()
    assert result.status is RunStatus.SUCCEEDED


def test_status_mapping():
    cases = {
        GRB.OPTIMAL: RunStatus.SUCCEEDED,
        GRB.SUBOPTIMAL: RunStatus.SUCCEEDED,
        GRB.TIME_LIMIT: RunStatus.SUCCEEDED,
        GRB.INFEASIBLE: RunStatus.INFEASIBLE,
        GRB.INF_OR_UNBD: RunStatus.INFEASIBLE,
        GRB.UNBOUNDED: RunStatus.INFEASIBLE,
        GRB.INTERRUPTED: RunStatus.CANCELLED,
        99: RunStatus.FAILED,
    }
    for gurobi_status, expected in cases.items():
        model = StubModel(status=gurobi_status)
        assert driver_for(model, []).run().status is expected, gurobi_status
