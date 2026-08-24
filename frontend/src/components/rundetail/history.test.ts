/**
 * The parts of the past-run page that can be wrong.
 *
 * Fixtures rather than invented data: `src/dev/fixtures.ts` is built through
 * the real `RunStore` against each model's declared payload interface, so a
 * test written against it fails when the contract moves rather than when this
 * file's imagination does.
 */

import { describe, expect, it } from "vitest";

import { makeMessages } from "@/dev/fixtures";
import { isSettled } from "@/components/models/contract";
import { isLog, isResult, type Message } from "@/lib/envelope";

import {
  assembleHistory,
  findGaps,
  lockedViewState,
  nextCursor,
  normalizeBackfilled,
  resultsCompleteness,
  snapshotSeqs,
  summariseGaps,
  totalRowCount,
} from "./history";

/**
 * What `GET /api/runs/{id}/messages` really returns, as opposed to what
 * `BackfillResponse` claims.
 *
 * Two deliberate distortions of the fixture messages, both taken from
 * `app/repository.py`:
 *
 *  - `run_id` is dropped. `_rehydrate` builds `{"type","seq","ts", **fields}`
 *    and `messages_since` selects neither `run_id` nor packs it into `body`.
 *  - `client_visible: false` logs are dropped. `messages_since` has
 *    `AND client_visible` on its log branch, so those seqs are unreachable.
 */
function asServerPage(messages: readonly Message[]) {
  return messages
    .filter((message) => !isLog(message) || message.client_visible)
    .map((message) => {
      const { run_id: _dropped, ...rest } = message;
      return rest as unknown;
    });
}

describe("nextCursor", () => {
  it("stops when the server says there is no more", () => {
    expect(nextCursor({ more: false, next_after_seq: 900 }, 100)).toBeNull();
  });

  it("advances while the server says there is more", () => {
    expect(nextCursor({ more: true, next_after_seq: 900 }, 100)).toBe(900);
  });

  it("stops when the cursor does not advance, even if the server says more", () => {
    // An empty page echoes `after_seq` back as `next_after_seq`. Without this
    // guard the same offset is requested forever.
    expect(nextCursor({ more: true, next_after_seq: 100 }, 100)).toBeNull();
    expect(nextCursor({ more: true, next_after_seq: 40 }, 100)).toBeNull();
  });

  it("terminates over a whole run rather than chasing seq contiguity", () => {
    // The load-bearing property: paging is driven by `more` and by cursor
    // movement, never by "are the seqs I hold contiguous yet". The seqs here
    // are deliberately full of holes and it makes no difference.
    const pages = [
      { more: true, next_after_seq: 10 },
      { more: true, next_after_seq: 25 },
      { more: true, next_after_seq: 400 },
      { more: false, next_after_seq: 401 },
    ];
    let cursor = -1;
    let requests = 0;
    for (const page of pages) {
      requests += 1;
      const next = nextCursor(page, cursor);
      if (next === null) break;
      cursor = next;
    }
    expect(requests).toBe(4);
    expect(cursor).toBe(400);
  });
});

describe("normalizeBackfilled", () => {
  it("puts back the run_id the server does not send", () => {
    // Without this every backfilled row is rejected by `normalizeMessage`,
    // which requires a run_id — and the page silently renders an empty run.
    const rows = asServerPage(makeMessages("mcmc", "typical", "SUCCEEDED"));
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.every((row) => !("run_id" in (row as object)))).toBe(true);

    const { messages, unusable } = normalizeBackfilled("run-xyz", rows);
    expect(unusable).toBe(0);
    expect(messages).toHaveLength(rows.length);
    expect(messages.every((message) => message.run_id === "run-xyz")).toBe(true);
  });

  it("counts rows it cannot use instead of throwing", () => {
    const { messages, unusable } = normalizeBackfilled("run-xyz", [
      { type: "log", seq: 1, ts: 1, message: "fine", client_visible: true },
      { type: "nonsense", seq: 2, ts: 2 },
      null,
      { type: "status", seq: 3, ts: 3, status: "NOT_A_STATUS" },
    ]);
    expect(messages).toHaveLength(1);
    expect(unusable).toBe(3);
  });
});

describe("assembleHistory", () => {
  it("rebuilds a live-shaped snapshot from backfilled pages", () => {
    const source = makeMessages("forecasting", "typical", "SUCCEEDED");
    const rows = asServerPage(source);
    const half = Math.ceil(rows.length / 2);

    const { snapshot } = assembleHistory({
      runId: "run-forecast",
      pages: [{ messages: rows.slice(0, half) }, { messages: rows.slice(half) }],
      rowStatus: "SUCCEEDED",
    });

    expect(snapshot.run_id).toBe("run-forecast");
    // Derived by the real store, not by this test.
    expect(snapshot.hydrated).toBe(true);
    expect(snapshot.terminal).toBe(true);
    expect(snapshot.status).toBe("SUCCEEDED");
    expect(snapshot.progress.length).toBeGreaterThan(0);
    expect(snapshot.latestProgress).not.toBeNull();
    expect(snapshot.lastSeq).toBe(
      rows.reduce<number>((max, row) => Math.max(max, (row as { seq: number }).seq), -1),
    );
    // No live channel was ever opened, and the snapshot says so.
    expect(snapshot.connection).toBe("idle");
  });

  it("does not double-count a page delivered twice", () => {
    const rows = asServerPage(makeMessages("annealing", "sparse", "SUCCEEDED"));
    const once = assembleHistory({
      runId: "r",
      pages: [{ messages: rows }],
      rowStatus: "SUCCEEDED",
    });
    const twice = assembleHistory({
      runId: "r",
      pages: [{ messages: rows }, { messages: rows }],
      rowStatus: "SUCCEEDED",
    });
    expect(twice.snapshot.progress).toHaveLength(once.snapshot.progress.length);
    expect(twice.snapshot.logs).toHaveLength(once.snapshot.logs.length);
  });

  it("marks a run terminal from the registry row when Delta holds no status message", () => {
    // Real: bayesian_ab is closed form and can finish having emitted nothing.
    // The row is then the only thing that knows the run is over, and without
    // this the per-model view would animate forever.
    const { snapshot } = assembleHistory({
      runId: "run-empty",
      pages: [{ messages: [] }],
      rowStatus: "SUCCEEDED",
    });
    expect(snapshot.hydrated).toBe(true);
    expect(snapshot.terminal).toBe(true);
    expect(snapshot.status).toBe("SUCCEEDED");
  });

  it("lets a real status message win over the registry row", () => {
    const rows = asServerPage(makeMessages("mcmc", "typical", "CANCELLED"));
    const { snapshot } = assembleHistory({
      runId: "r",
      pages: [{ messages: rows }],
      rowStatus: "SUCCEEDED",
    });
    expect(snapshot.status).toBe("CANCELLED");
  });
});

describe("findGaps", () => {
  it("reports interior holes only", () => {
    expect(findGaps([4, 5, 6])).toEqual([]);
    expect(findGaps([4, 9, 10])).toEqual([{ from: 5, to: 8 }]);
    // A run whose first backfilled seq is not 0 is normal, not a gap: the
    // job's own non-client-visible startup logs were filtered out.
    expect(findGaps([7, 8, 9])).toEqual([]);
  });

  it("is order- and duplicate-insensitive", () => {
    expect(findGaps([10, 2, 10, 2, 5])).toEqual([
      { from: 3, to: 4 },
      { from: 6, to: 9 },
    ]);
  });

  it("finds the permanent gap a fully backfilled run still has", () => {
    // The `gappy` fixture carries both a real hole and client_visible:false
    // logs. Backfill filters the latter, so even a complete read leaves holes
    // — which is why nothing on this page offers to close them.
    const rows = asServerPage(makeMessages("mcmc", "gappy", "SUCCEEDED"));
    const { snapshot } = assembleHistory({
      runId: "r",
      pages: [{ messages: rows }],
      rowStatus: "SUCCEEDED",
    });
    const gaps = findGaps(snapshotSeqs(snapshot));
    expect(gaps.length).toBeGreaterThan(0);
    expect(summariseGaps(gaps).missing).toBeGreaterThan(0);
  });

  it("summarises seq values missing, not runs of them", () => {
    expect(summariseGaps([{ from: 5, to: 8 }, { from: 20, to: 20 }])).toEqual({
      count: 2,
      missing: 5,
    });
  });
});

describe("resultsCompleteness", () => {
  const results = (source: readonly Message[]) => source.filter(isResult);

  it("is complete once a final chunk exists", () => {
    const chunks = results(makeMessages("streaming_results", "chunked", "SUCCEEDED"));
    expect(chunks.length).toBeGreaterThan(1);
    expect(chunks.some((chunk) => chunk.final)).toBe(true);
    expect(
      resultsCompleteness(chunks, { runTerminal: true, fullyLoaded: true }),
    ).toBe("complete");
  });

  it("is incomplete — not 'still arriving' — when a finished run has no final chunk", () => {
    // A cancelled streaming_results run returns between windows, keeping every
    // chunk it finished and never emitting the final one. Nothing is arriving;
    // the run has stopped.
    const chunks = results(makeMessages("streaming_results", "chunked", "SUCCEEDED")).filter(
      (chunk) => !chunk.final,
    );
    expect(chunks.length).toBeGreaterThan(0);
    expect(
      resultsCompleteness(chunks, { runTerminal: true, fullyLoaded: true }),
    ).toBe("incomplete");
  });

  it("will not claim incompleteness while pages are still unread", () => {
    const chunks = results(makeMessages("streaming_results", "chunked", "SUCCEEDED")).filter(
      (chunk) => !chunk.final,
    );
    expect(
      resultsCompleteness(chunks, { runTerminal: true, fullyLoaded: false }),
    ).toBe("unknown");
    expect(
      resultsCompleteness(chunks, { runTerminal: false, fullyLoaded: true }),
    ).toBe("unknown");
  });

  it("distinguishes 'never reached its result write' from 'wrote zero rows'", () => {
    expect(resultsCompleteness([], { runTerminal: true, fullyLoaded: true })).toBe("none");

    const zero = results(makeMessages("scenario", "typical", "SUCCEEDED")).map((chunk) => ({
      ...chunk,
      row_count: 0,
    }));
    expect(zero.length).toBeGreaterThan(0);
    // A zero-row chunk still exists, so it is a result, not an absence.
    expect(resultsCompleteness(zero, { runTerminal: true, fullyLoaded: true })).not.toBe(
      "none",
    );
    expect(totalRowCount(zero)).toBe(0);
  });
});

describe("lockedViewState", () => {
  it("hands the view the registry row's status and nothing derived", () => {
    // No stream means nothing to derive from, and STARTING is meaningless for
    // a run that already happened.
    for (const status of ["SUCCEEDED", "FAILED", "CANCELLED", "INFEASIBLE"] as const) {
      expect(lockedViewState(status)).toBe(status);
      expect(isSettled(lockedViewState(status))).toBe(true);
    }
    expect(lockedViewState(null)).toBeNull();
  });

  it("does not fake a terminal state for a stranded RUNNING row", () => {
    // The row says RUNNING. Substituting something settled would be a lie
    // about a RunStatus; the page says "stranded" in prose instead.
    expect(lockedViewState("RUNNING")).toBe("RUNNING");
    expect(isSettled(lockedViewState("RUNNING"))).toBe(false);
  });
});
