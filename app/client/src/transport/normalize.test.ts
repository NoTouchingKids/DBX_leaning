import { describe, expect, it } from "vitest";

import { normalizeMessage, parseFrame } from "./normalize";

describe("normalizeMessage", () => {
  it("fills the same defaults the server's model would", () => {
    const msg = normalizeMessage({ type: "log", run_id: "r", seq: 3, ts: 9, message: "hi" });
    expect(msg).toEqual({
      type: "log",
      run_id: "r",
      seq: 3,
      ts: 9,
      message: "hi",
      level: "INFO",
      source: "model",
      phase: "run",
      client_visible: true,
    });
  });

  it("keeps null percent_complete and primary_metric as null", () => {
    // gurobi_scheduling emits percent_complete: null for its entire run. A
    // normaliser that coerced these to 0 would render a 0% bar for minutes.
    const msg = normalizeMessage({
      type: "progress",
      run_id: "r",
      seq: 1,
      ts: 1,
      elapsed_seconds: 4,
      percent_complete: null,
      primary_metric: null,
    });
    expect(msg).toMatchObject({ percent_complete: null, primary_metric: null });
  });

  it("keeps row_count 0 as 0", () => {
    // 0 means "wrote nothing, possibly because the write failed" — a real
    // and important result, not an empty state.
    const msg = normalizeMessage({
      type: "result",
      run_id: "r",
      seq: 1,
      ts: 1,
      row_count: 0,
    });
    expect(msg).toMatchObject({ row_count: 0, preview: [], final: true, chunk_index: 0 });
  });

  it("accepts a numeric string where the wire round-trip produced one", () => {
    expect(normalizeMessage({ type: "log", run_id: "r", seq: "12", ts: "1", message: "x" }))
      .toMatchObject({ seq: 12, ts: 1 });
  });

  it.each([
    ["not an object", 42],
    ["null", null],
    ["an unknown type", { type: "telemetry", run_id: "r", seq: 1, ts: 1 }],
    ["a missing run_id", { type: "log", seq: 1, ts: 1, message: "x" }],
    ["a missing seq", { type: "log", run_id: "r", ts: 1, message: "x" }],
    ["a non-numeric seq", { type: "log", run_id: "r", seq: "abc", ts: 1, message: "x" }],
    ["an unknown status", { type: "status", run_id: "r", seq: 1, ts: 1, status: "PAUSED" }],
  ])("returns null for %s", (_label, input) => {
    expect(normalizeMessage(input)).toBeNull();
  });

  it("survives a missing ts, because ordering comes from seq", () => {
    expect(normalizeMessage({ type: "log", run_id: "r", seq: 1, message: "x" }))
      .toMatchObject({ ts: 0 });
  });

  it("rejects a NaN seq rather than storing an unorderable message", () => {
    expect(normalizeMessage({ type: "log", run_id: "r", seq: Number.NaN, ts: 1, message: "x" }))
      .toBeNull();
  });
});

describe("parseFrame", () => {
  it("parses a well-formed SSE data payload", () => {
    const frame = JSON.stringify({ type: "status", run_id: "r", seq: 1, ts: 1, status: "QUEUED" });
    expect(parseFrame(frame)).toMatchObject({ status: "QUEUED" });
  });

  it("returns null rather than throwing on malformed JSON", () => {
    expect(parseFrame("{oops")).toBeNull();
  });
});
