import { describe, expect, it } from "vitest";

import type { ProgressMessage, ResultMessage } from "@/lib/envelope";

import {
  accumulateChunks,
  arrivalState,
  NO_CHUNKS,
  NO_WINDOW,
  placeWindow,
  TIMELINE_SEGMENTS,
  WINDOW_SEGMENTS,
} from "./streamingModel";

const LAST_START = TIMELINE_SEGMENTS - WINDOW_SEGMENTS;

function progress(payload: Record<string, unknown>): ProgressMessage {
  return {
    type: "progress",
    run_id: "run-a",
    seq: 1,
    ts: 1_700_000_000_000,
    elapsed_seconds: 3,
    percent_complete: null,
    primary_metric: 6.1,
    primary_metric_label: "window_mae",
    payload,
  };
}

function chunk(index: number, over: Partial<ResultMessage> = {}): ResultMessage {
  const origin = 120 + index * 40;
  const preview = Array.from({ length: 3 }, (_, step) => ({
    origin,
    step,
    predicted: 10 + step,
    actual: 11 + step,
    abs_error: 1,
  }));
  return {
    type: "result",
    run_id: "run-a",
    seq: 10 + index,
    ts: 1_700_000_000_000,
    preview,
    row_count: preview.length,
    fetch_hint: { table: "main.default.results_streaming", key: "run_id" },
    chunk_index: index,
    final: false,
    ...over,
  };
}

describe("placeWindow", () => {
  it("knows nothing from nothing", () => {
    expect(placeWindow(null)).toEqual(NO_WINDOW);
    expect(placeWindow(progress({}))).toEqual(NO_WINDOW);
  });

  it("steps one segment per window when the run fits the track", () => {
    // windows_total 10 is exactly the number of places a 3-wide window has on
    // a 12-segment track, so every increment must move it exactly one.
    const starts = Array.from({ length: 10 }, (_, i) =>
      placeWindow(progress({ windows_done: i + 1, windows_total: 10 })).start,
    );
    expect(starts).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
    expect(placeWindow(progress({ windows_done: 1, windows_total: 10 })).lockstep).toBe(true);
  });

  it("compresses, and says so, when a run has more windows than places", () => {
    // The real default is roughly 33 windows (window 120, step 40, horizon 12
    // over 60 days of hourly data) — never the twelve the wireframe implies.
    const place = placeWindow(progress({ windows_done: 1, windows_total: 33 }));
    expect(place.start).toBe(0);
    expect(place.lockstep).toBe(false);
    expect(placeWindow(progress({ windows_done: 33, windows_total: 33 })).start).toBe(LAST_START);
  });

  it("pins the window at the right edge rather than running off the track", () => {
    for (let done = 1; done <= 12; done += 1) {
      const place = placeWindow(progress({ windows_done: done, windows_total: 12 }));
      expect(place.start).toBeLessThanOrEqual(LAST_START);
      expect(place.start).toBeGreaterThanOrEqual(0);
    }
  });

  it("fills the whole track only when every window is done", () => {
    expect(placeWindow(progress({ windows_done: 6, windows_total: 12 })).segmentsDone).toBe(6);
    expect(placeWindow(progress({ windows_done: 12, windows_total: 12 })).segmentsDone).toBe(
      TIMELINE_SEGMENTS,
    );
    expect(placeWindow(progress({ windows_done: 33, windows_total: 33 })).segmentsDone).toBe(
      TIMELINE_SEGMENTS,
    );
  });

  it("carries the origin through for the readout", () => {
    expect(placeWindow(progress({ windows_done: 2, windows_total: 12, origin: 640 })).origin).toBe(640);
  });

  it("survives a payload whose counts are missing but whose extras are not", () => {
    // The payload is spread with `**self._provenance`, so extra keys are the
    // norm and an exhaustive read of it would be wrong.
    const place = placeWindow(
      progress({ origin: 160, data_source: "samples", data_synthetic: true, data_rows: 1440 }),
    );
    expect(place.start).toBeNull();
    expect(place.origin).toBe(160);
  });
});

describe("accumulateChunks", () => {
  it("is empty for a run with no results", () => {
    expect(accumulateChunks([])).toEqual(NO_CHUNKS);
  });

  it("appends chunks rather than replacing them", () => {
    const view = accumulateChunks([chunk(0), chunk(1), chunk(2)]);
    expect(view.chunks).toHaveLength(3);
    expect(view.totalRows).toBe(9);
    expect(view.points).toHaveLength(9);
  });

  it("orders points by absolute series position, not by arrival", () => {
    const view = accumulateChunks([chunk(2), chunk(0), chunk(1)]);
    expect(view.points.map((p) => p.x)).toEqual([120, 121, 122, 160, 161, 162, 200, 201, 202]);
  });

  it("is complete only once a final chunk has been seen", () => {
    expect(accumulateChunks([chunk(0), chunk(1)]).complete).toBe(false);
    expect(accumulateChunks([chunk(0), chunk(1, { final: true })]).complete).toBe(true);
  });

  it("does not double-count a chunk delivered twice under a new seq", () => {
    const view = accumulateChunks([chunk(0), chunk(0, { seq: 900 }), chunk(1, { final: true })]);
    expect(view.chunks).toHaveLength(2);
    expect(view.totalRows).toBe(6);
    expect(view.points).toHaveLength(6);
    expect(view.complete).toBe(true);
  });

  it("keeps the final flag even when the final chunk is the duplicated one", () => {
    expect(accumulateChunks([chunk(0, { final: true }), chunk(0, { seq: 900 })]).complete).toBe(true);
  });

  it("reports a zero-row chunk instead of dropping it", () => {
    // The model emits exactly this when the series is too short to backtest:
    // one chunk, no rows, final. Zero is the answer, not the absence of one.
    const view = accumulateChunks([chunk(0, { preview: [], row_count: 0, final: true })]);
    expect(view.chunks).toHaveLength(1);
    expect(view.totalRows).toBe(0);
    expect(view.emptyChunks).toEqual([0]);
    expect(view.complete).toBe(true);
  });

  it("names the chunks that never arrived", () => {
    expect(accumulateChunks([chunk(0), chunk(3)]).missing).toEqual([1, 2]);
  });
});

describe("arrivalState", () => {
  it("distinguishes still-arriving from ended-without-a-final-chunk", () => {
    const partial = accumulateChunks([chunk(0), chunk(1)]);
    expect(arrivalState(partial, false)).toBe("arriving");
    // A cancel returns between windows, so no final chunk ever comes. Waiting
    // for one would be waiting forever.
    expect(arrivalState(partial, true)).toBe("stopped");
  });

  it("stays complete once the final chunk is in, run over or not", () => {
    const done = accumulateChunks([chunk(0, { final: true })]);
    expect(arrivalState(done, false)).toBe("complete");
    expect(arrivalState(done, true)).toBe("complete");
  });

  it("separates a run that has not started emitting from one that never did", () => {
    expect(arrivalState(NO_CHUNKS, false)).toBe("none");
    expect(arrivalState(NO_CHUNKS, true)).toBe("stopped");
  });
});
