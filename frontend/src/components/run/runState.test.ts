import { describe, expect, it } from "vitest";

import { canCancel, deriveUiState, isStrandedRun } from "./runState";

describe("canCancel", () => {
  // `live` is a live-WebSocket check, not a stored column. Cancel is
  // forwarded over that socket and nowhere else, so it is the deciding field.
  it("is enabled only with a live socket and a non-terminal run", () => {
    expect(canCancel({ live: true, state: "RUNNING", cancelRequested: false })).toBe(true);
    expect(canCancel({ live: true, state: "QUEUED", cancelRequested: false })).toBe(true);
    expect(canCancel({ live: true, state: "STARTING", cancelRequested: false })).toBe(true);
  });

  it("is disabled for a RUNNING run with no socket — that 409s every time", () => {
    expect(canCancel({ live: false, state: "RUNNING", cancelRequested: false })).toBe(false);
  });

  it("is disabled while `live` is still unknown", () => {
    expect(canCancel({ live: undefined, state: "RUNNING", cancelRequested: false })).toBe(false);
  });

  it("is disabled for every terminal status, socket or not", () => {
    for (const state of ["SUCCEEDED", "FAILED", "CANCELLED", "INFEASIBLE"] as const) {
      expect(canCancel({ live: true, state, cancelRequested: false })).toBe(false);
    }
  });

  it("stays disabled once a cancel has been accepted", () => {
    // Optimistic: the 200 only means the frame reached the job. The CANCELLED
    // status message is the real confirmation, and it may take a while.
    expect(canCancel({ live: true, state: "RUNNING", cancelRequested: true })).toBe(false);
  });
});

describe("deriveUiState", () => {
  const base = {
    streamStatus: null,
    rowStatus: null,
    optimisticStartingFor: null,
    runId: "run-a",
    sawAnyMessage: false,
  };

  it("shows STARTING between the 202 and the first message", () => {
    expect(deriveUiState({ ...base, optimisticStartingFor: "run-a" })).toBe("STARTING");
  });

  it("drops STARTING the moment anything real arrives", () => {
    expect(
      deriveUiState({
        ...base,
        optimisticStartingFor: "run-a",
        sawAnyMessage: true,
        rowStatus: "QUEUED",
      }),
    ).toBe("QUEUED");
  });

  it("does not apply one run's optimistic state to another run", () => {
    expect(deriveUiState({ ...base, optimisticStartingFor: "run-b", rowStatus: "RUNNING" })).toBe(
      "RUNNING",
    );
  });

  it("prefers the stream's status over the registry row", () => {
    expect(deriveUiState({ ...base, streamStatus: "SUCCEEDED", rowStatus: "RUNNING" })).toBe(
      "SUCCEEDED",
    );
  });

  it("falls back to the row when there is no stream", () => {
    expect(deriveUiState({ ...base, rowStatus: "FAILED" })).toBe("FAILED");
  });
});

describe("isStrandedRun", () => {
  it("flags RUNNING with no socket — nothing will ever finish that row", () => {
    expect(isStrandedRun("RUNNING", false)).toBe(true);
  });

  it("does not flag QUEUED with no socket: the job has simply not attached", () => {
    expect(isStrandedRun("QUEUED", false)).toBe(false);
  });

  it("does not flag anything while `live` is unknown", () => {
    expect(isStrandedRun("RUNNING", undefined)).toBe(false);
  });
});
