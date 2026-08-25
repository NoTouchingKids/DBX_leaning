import { describe, expect, it } from "vitest";

import type { Run } from "@/lib/apiClient";
import { RUN_STATUSES, TERMINAL_STATUSES } from "@/lib/envelope";

import { MAX_CONCURRENT_RUNS, deriveCapacity, filledSlots, isActiveRow, isExact } from "./capacity";

function run(over: Partial<Run> = {}): Run {
  return {
    run_id: "run-000000000001",
    job_run_id: "9001",
    model: "mcmc",
    status: "SUCCEEDED",
    detail: null,
    started_ts: 1_700_000_000_000,
    updated_ts: 1_700_000_060_000,
    requested_by: null,
    live: false,
    ...over,
  };
}

const rowsFor = (statuses: readonly Run["status"][]): Run[] =>
  statuses.map((status, index) => run({ run_id: `run-${index}`, status }));

describe("the active predicate mirrors the server's", () => {
  // `active_run_count()` is `WHERE status NOT IN
  // ('SUCCEEDED','FAILED','CANCELLED','INFEASIBLE')`. If the two ever
  // disagree, the meter says there is room when POST /api/runs will 429.
  it("counts exactly the statuses the SQL does not exclude", () => {
    for (const status of RUN_STATUSES) {
      const excludedBySql = (TERMINAL_STATUSES as readonly string[]).includes(status);
      expect(isActiveRow({ status })).toBe(!excludedBySql);
    }
  });
});

describe("deriveCapacity", () => {
  it("counts non-terminal rows across every model, not just one", () => {
    const rows = [
      run({ run_id: "a", model: "mcmc", status: "RUNNING" }),
      run({ run_id: "b", model: "scenario", status: "QUEUED" }),
      run({ run_id: "c", model: "forecasting", status: "SUCCEEDED" }),
      run({ run_id: "d", model: "annealing", status: "FAILED" }),
    ];
    const capacity = deriveCapacity(rows, { windowLimit: 50 });
    expect(capacity.active).toBe(2);
    expect(capacity.ceiling).toBe(MAX_CONCURRENT_RUNS);
    expect(capacity.atCeiling).toBe(false);
    expect(isExact(capacity)).toBe(true);
  });

  it("counts a stranded RUNNING run — it holds a slot forever", () => {
    // The whole reason the meter can disagree with intuition: nothing will
    // ever move this row to terminal, so its slot is never released.
    const capacity = deriveCapacity([run({ status: "RUNNING", live: false })], {
      windowLimit: 50,
    });
    expect(capacity.active).toBe(1);
  });

  it("flags the ceiling, where the next trigger 429s", () => {
    const capacity = deriveCapacity(rowsFor(["RUNNING", "RUNNING", "QUEUED", "RUNNING", "QUEUED"]), {
      windowLimit: 50,
    });
    expect(capacity.active).toBe(5);
    expect(capacity.atCeiling).toBe(true);
  });

  it("is a lower bound once the window fills, because older rows were cut", () => {
    // Ordering is `updated_ts DESC`, so the rows that fall off the bottom are
    // the ones that stopped emitting — precisely the stranded ones.
    const capacity = deriveCapacity(rowsFor(["RUNNING", "SUCCEEDED"]), { windowLimit: 2 });
    expect(capacity.windowComplete).toBe(false);
    expect(isExact(capacity)).toBe(false);
  });

  it("is not exact when the rows came from a filtered window", () => {
    // A model-filtered window answers "active runs of this model"; the
    // ceiling is account-wide.
    const capacity = deriveCapacity(rowsFor(["RUNNING"]), { windowLimit: 50, unfiltered: false });
    expect(isExact(capacity)).toBe(false);
  });

  it("honours a ceiling other than the default", () => {
    expect(deriveCapacity(rowsFor(["RUNNING"]), { windowLimit: 50, ceiling: 1 }).atCeiling).toBe(true);
  });
});

describe("filledSlots", () => {
  it("never draws more slots than the meter has", () => {
    // A deployment that lowered DBX_MAX_CONCURRENT_RUNS can sit above its own
    // ceiling: it is only enforced at trigger time.
    const capacity = deriveCapacity(rowsFor(["RUNNING", "RUNNING", "RUNNING"]), {
      windowLimit: 50,
      ceiling: 2,
    });
    expect(capacity.active).toBe(3);
    expect(filledSlots(capacity)).toBe(2);
  });

  it("draws nothing at zero", () => {
    expect(filledSlots(deriveCapacity([], { windowLimit: 50 }))).toBe(0);
  });
});
