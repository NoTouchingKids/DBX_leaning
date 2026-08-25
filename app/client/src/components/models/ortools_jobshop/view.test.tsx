/**
 * The view as a whole: every lifecycle state rendered once, plus the pieces of
 * copy that can be wrong without anything throwing.
 *
 * The grid of states crossed with "has result rows" / "has none" is where the
 * cheap failures live — indexing an empty lane list, dividing by a span of
 * zero, an INFEASIBLE frame reading like a crash. `noUncheckedIndexedAccess`
 * catches some of it; the rest is only reachable at runtime.
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import type { LogMessage, ProgressMessage, ResultMessage, UiRunState } from "@/lib/envelope";
import { ORTOOLS_JOBSHOP } from "@/lib/models";
import type { RunSnapshot } from "@/transport/runStore";
import view from "./index";
import { describeJobshopState } from "./stateCopy";
import { resolveInstance, resolveSchedule } from "./schedule";

const BASIS = "elapsed_solver_time_against_time_limit";

const logs: LogMessage[] = [
  {
    type: "log",
    run_id: "r",
    seq: 1,
    ts: 1,
    message:
      "60 jobs / 247 operations over 5 machines (mix, rest, bake, decorate, pack); " +
      "4210 machine-minutes of work, lower bound on makespan 1100 min; " +
      "the jobs stand for 1203 sales transactions",
    level: "INFO",
    source: "model",
    phase: "input",
    client_visible: true,
  },
  {
    type: "log",
    run_id: "r",
    seq: 2,
    ts: 2,
    message:
      "built: 247 interval variables, 5 no-overlap constraints, horizon 900 min, deadline 900 min.",
    level: "INFO",
    source: "model",
    phase: "build",
    client_visible: true,
  },
];

const progress: ProgressMessage[] = [
  {
    type: "progress",
    run_id: "r",
    seq: 3,
    ts: 3,
    elapsed_seconds: 0.4,
    percent_complete: 0.7,
    primary_metric: null,
    primary_metric_label: "relative_gap",
    payload: {
      incumbent: 1420,
      best_bound: null,
      gap: null,
      solutions_found: 1,
      wall_time: 0.4,
      n_jobs: 60,
      n_machines: 5,
      n_operations: 247,
      percent_complete_basis: BASIS,
      final: false,
      conflicts: 12,
      branches: 44,
    },
  },
  {
    type: "progress",
    run_id: "r",
    seq: 4,
    ts: 4,
    elapsed_seconds: 2.6,
    percent_complete: 4.3,
    primary_metric: 0.061,
    primary_metric_label: "relative_gap",
    payload: {
      incumbent: 1180,
      best_bound: 1108,
      gap: 0.061,
      solutions_found: 5,
      wall_time: 2.6,
      n_jobs: 60,
      n_machines: 5,
      n_operations: 247,
      percent_complete_basis: BASIS,
      final: false,
      conflicts: 980,
      branches: 3100,
    },
  },
];

const base: RunSnapshot = {
  run_id: "run-00000000abcd",
  logs,
  progress,
  statuses: [],
  results: [],
  latestProgress: progress[1] ?? null,
  status: "RUNNING",
  terminal: false,
  lastSeq: 4,
  connection: "open",
  consecutiveFailures: 0,
  gaps: [],
  hydrated: true,
  droppedLogs: 0,
  droppedProgress: 0,
};

function operationRow(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    job_id: 0,
    job_label: "Reykjavik Rye x40 @f3001 2026-01-01",
    operation_index: 0,
    machine_id: 0,
    machine_label: "mix",
    start_minute: 0,
    duration_minutes: 20,
    end_minute: 20,
    makespan: 1120,
    best_bound: 1120,
    solver_status: "OPTIMAL",
    solutions_found: 5,
    wall_time_seconds: 3.2,
    n_jobs: 60,
    n_machines: 5,
    n_operations: 247,
    seed: 20260824,
    data_source: "samples.bakehouse.sales_transactions",
    data_synthetic: false,
    data_rows: 400,
    data_fallback_reason: null,
    ...over,
  };
}

const finalSample: ProgressMessage = {
  type: "progress",
  run_id: "r",
  seq: 9,
  ts: 9,
  elapsed_seconds: 3.2,
  percent_complete: 100,
  primary_metric: 0,
  primary_metric_label: "relative_gap",
  payload: {
    incumbent: 1120,
    best_bound: 1120,
    gap: 0,
    solutions_found: 5,
    wall_time: 3.2,
    n_jobs: 60,
    n_machines: 5,
    n_operations: 247,
    percent_complete_basis: BASIS,
    final: true,
    solver_status: "OPTIMAL",
  },
};

const result: ResultMessage = {
  type: "result",
  run_id: "r",
  seq: 10,
  ts: 10,
  chunk_index: 0,
  final: true,
  row_count: 3,
  fetch_hint: {},
  preview: [
    operationRow(),
    operationRow({
      operation_index: 1,
      machine_id: 2,
      machine_label: "bake",
      start_minute: 20,
      duration_minutes: 24,
      end_minute: 44,
    }),
    // Deliberately another model's row shape in the same preview: the resolver
    // must skip it rather than throw.
    { staff: "staff-00", day: 0, shift: "morning", preferred: true },
  ],
};

const settled: RunSnapshot = {
  ...base,
  status: "SUCCEEDED",
  terminal: true,
  progress: [...progress, finalSample],
  statuses: [
    {
      type: "status",
      run_id: "r",
      seq: 11,
      ts: 11,
      status: "SUCCEEDED",
      detail: "optimal: makespan 1120 min",
    },
  ],
  results: [result],
};

const truncated: RunSnapshot = {
  ...settled,
  results: [{ ...result, row_count: 1700 }],
};

const states: (UiRunState | null)[] = [
  null,
  "STARTING",
  "QUEUED",
  "RUNNING",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "INFEASIBLE",
];

const empty: RunSnapshot = {
  ...base,
  logs: [],
  progress: [],
  latestProgress: null,
  status: null,
  lastSeq: null,
  hydrated: false,
};

describe("the ortools_jobshop view", () => {
  it("is registered under the name in MODEL_SPECS", () => {
    expect(view.model).toBe(ORTOOLS_JOBSHOP.name);
    expect(view.charts).toHaveLength(2);
  });

  it("carries an honesty note that says which half is decorative", () => {
    expect(view.honesty.length).toBeGreaterThan(200);
    expect(view.honesty).toMatch(/DECORATIVE/);
    // The two things this model must not be read as claiming.
    expect(view.honesty).toMatch(/TIME fraction/);
    expect(view.honesty).toMatch(/improving solution/);
  });

  it("renders in every lifecycle state, with and without result rows", () => {
    for (const snapshot of [empty, base, settled, truncated]) {
      for (const state of states) {
        expect(() =>
          render(<view.Signature state={state} snapshot={snapshot} />),
        ).not.toThrow();
        for (const chart of view.charts) {
          expect(() => render(<chart.Chart state={state} snapshot={snapshot} />)).not.toThrow();
        }
      }
    }
  });

  it("labels the clock as a clock, not as search progress", () => {
    render(<view.Signature state="RUNNING" snapshot={base} />);
    expect(screen.getByText(/clock 4%/)).toBeDefined();
    expect(screen.getByText(/not search progress/)).toBeDefined();
  });

  it("says there is no honest fraction when no time limit was configured", () => {
    const noLimit: RunSnapshot = {
      ...base,
      progress: progress.map((message) => ({ ...message, percent_complete: null })),
    };
    render(<view.Signature state="RUNNING" snapshot={noLimit} />);
    expect(screen.getByText(/No time limit configured/)).toBeDefined();
  });

  it("draws the real operations on a settled run", () => {
    render(<view.Signature state="SUCCEEDED" snapshot={settled} />);
    // One title per real bar, carrying the job label and its real minutes.
    expect(screen.getByTitle(/Reykjavik Rye x40.*mix, 0–20 min/)).toBeDefined();
    expect(screen.getByTitle(/bake, 20–44 min/)).toBeDefined();
  });

  it("shows a decorative floor while running and an empty one when infeasible", () => {
    // The decorative layer is the only thing drawn while the run is live, and
    // it must be gone in every terminal frame: an INFEASIBLE run's lanes have
    // to read as "no schedule exists", not as a schedule nobody labelled.
    const running = render(<view.Signature state="RUNNING" snapshot={base} />);
    expect(running.container.querySelectorAll(".bg-info-soft").length).toBeGreaterThan(0);

    const infeasible = render(<view.Signature state="INFEASIBLE" snapshot={base} />);
    expect(infeasible.container.querySelectorAll(".bg-info-soft")).toHaveLength(0);
    expect(infeasible.container.querySelectorAll(".border-dashed.border-warn").length).toBe(5);
  });

  it("says how much of a downsampled preview it is showing", () => {
    render(<view.Signature state="SUCCEEDED" snapshot={truncated} />);
    expect(screen.getByText(/Showing 3 of 1,700 written/)).toBeDefined();
    expect(screen.getByText(/utilisation is withheld/)).toBeDefined();
  });
});

describe("describeJobshopState", () => {
  const shape = resolveInstance(logs, null);
  const schedule = resolveSchedule([result]);
  const noSchedule = resolveSchedule([]);

  const context = {
    schedule,
    shape,
    solutionsFound: 5,
    improvements: 2,
    detail: "optimal: makespan 1120 min",
    solverStatus: "OPTIMAL" as string | null,
  };

  it("returns copy for every state, including no-run-selected", () => {
    for (const state of states) {
      const copy = describeJobshopState(state, context);
      expect(copy.title.length).toBeGreaterThan(0);
      expect(copy.detail.length).toBeGreaterThan(0);
    }
  });

  it("explains INFEASIBLE as a fact about the deadline, not a failure", () => {
    const copy = describeJobshopState("INFEASIBLE", context);
    expect(copy.tone).toBe("warn");
    expect(copy.detail).toMatch(/900 min deadline/);
    expect(copy.detail).toMatch(/1,100 min/);
    expect(copy.detail).toMatch(/not a solver failure/);
    // FAILED must not look like it.
    expect(describeJobshopState("FAILED", context).tone).toBe("bad");
  });

  it("still explains INFEASIBLE when no deadline was parsed out of the log", () => {
    const copy = describeJobshopState("INFEASIBLE", {
      ...context,
      shape: resolveInstance([], null),
    });
    expect(copy.detail).toMatch(/open horizon always has a schedule/);
  });

  it("tells a proof of optimality apart from the best found in the time limit", () => {
    expect(describeJobshopState("SUCCEEDED", context).title).toBe("Proven optimal");
    const feasible = describeJobshopState("SUCCEEDED", { ...context, solverStatus: "FEASIBLE" });
    expect(feasible.title).toBe("Best schedule found");
    expect(feasible.detail).toMatch(/not a proof/);
  });

  it("does not report a zero-row success as an empty schedule", () => {
    const copy = describeJobshopState("SUCCEEDED", {
      ...context,
      schedule: noSchedule,
      detail: "no schedule found (UNKNOWN)",
      solverStatus: "UNKNOWN",
    });
    expect(copy.title).toMatch(/no schedule written/);
    expect(copy.detail).toMatch(/zero rows/);
  });

  it("says a running search with no solution yet is the solver working", () => {
    const copy = describeJobshopState("RUNNING", { ...context, solutionsFound: 0 });
    expect(copy.detail).toMatch(/not a stalled stream/);
    expect(copy.detail).toMatch(/decorative/);
  });

  it("names MODEL_INVALID as a code defect rather than an answer about the bakery", () => {
    const copy = describeJobshopState("FAILED", { ...context, solverStatus: "MODEL_INVALID" });
    expect(copy.detail).toMatch(/defect in the model code/);
  });

  it("keeps a cancelled run's incumbent visible", () => {
    expect(describeJobshopState("CANCELLED", context).detail).toMatch(/still written/);
    expect(
      describeJobshopState("CANCELLED", { ...context, schedule: noSchedule }).detail,
    ).toMatch(/before any feasible schedule/);
  });
});
