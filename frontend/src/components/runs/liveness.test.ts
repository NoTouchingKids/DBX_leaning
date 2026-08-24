import { describe, expect, it } from "vitest";

import { isStrandedRun } from "@/components/run/runState";
import type { Run } from "@/lib/apiClient";

import { countStranded, rowDurationSeconds, runLiveness } from "./liveness";

function run(over: Partial<Run> = {}): Run {
  return {
    run_id: "run-000000000001",
    job_run_id: "9001",
    model: "mcmc",
    status: "RUNNING",
    detail: null,
    started_ts: 1_700_000_000_000,
    updated_ts: 1_700_000_060_000,
    requested_by: null,
    live: true,
    ...over,
  };
}

describe("runLiveness", () => {
  it("RUNNING with a socket is a healthy run", () => {
    expect(runLiveness(run({ status: "RUNNING", live: true }))).toBe("connected");
  });

  it("RUNNING with no socket is stranded — the whole point of the column", () => {
    expect(runLiveness(run({ status: "RUNNING", live: false }))).toBe("stranded");
  });

  it("agrees with the shared `isStrandedRun` predicate the model pages use", () => {
    // One definition, two surfaces: the model page disables cancel on exactly
    // the rows this page warns about.
    for (const live of [true, false]) {
      const row = run({ status: "RUNNING", live });
      expect(runLiveness(row) === "stranded").toBe(isStrandedRun(row.status, row.live));
    }
  });

  it("QUEUED with no socket is normal, not stranded — the job has not attached yet", () => {
    expect(runLiveness(run({ status: "QUEUED", live: false }))).toBe("awaiting");
  });

  it("ignores `live` on a terminal row, where it carries no information", () => {
    for (const status of ["SUCCEEDED", "FAILED", "CANCELLED", "INFEASIBLE"] as const) {
      expect(runLiveness(run({ status, live: true }))).toBe("finished");
      expect(runLiveness(run({ status, live: false }))).toBe("finished");
    }
  });
});

describe("countStranded", () => {
  it("counts only the RUNNING-with-no-socket rows", () => {
    expect(
      countStranded([
        run({ run_id: "a", status: "RUNNING", live: false }),
        run({ run_id: "b", status: "RUNNING", live: true }),
        run({ run_id: "c", status: "QUEUED", live: false }),
        run({ run_id: "d", status: "FAILED", live: false }),
      ]),
    ).toBe(1);
  });
});

describe("rowDurationSeconds", () => {
  it("is updated_ts − started_ts in seconds, from epoch milliseconds", () => {
    // Both columns are BIGINT epoch ms written with `now_ms()` — not ISO
    // strings, whatever an older copy of the contract said.
    expect(rowDurationSeconds(run({ started_ts: 1_000_000, updated_ts: 1_252_000 }))).toBe(252);
  });

  it("is null for QUEUED, where updated_ts == started_ts and 00:00 would lie", () => {
    expect(rowDurationSeconds(run({ status: "QUEUED", started_ts: 5, updated_ts: 5 }))).toBeNull();
  });

  it("refuses a negative duration rather than rendering one", () => {
    expect(
      rowDurationSeconds(run({ status: "RUNNING", started_ts: 2_000, updated_ts: 1_000 })),
    ).toBeNull();
  });
});
