/**
 * The rendering invariants, not the markup.
 *
 * Three things here are worth a regression test and nothing else in this file
 * is: that a search outside the shift is never dressed as an error, that a
 * terminal run collapses to ONE flat frame as `contract.ts` requires, and that
 * reduced motion removes the transition without removing the hot/cold reading.
 * Plus the charts' empty state, because an empty `snapshot.progress` is the
 * normal condition for the first several seconds of every run.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ProgressMessage, UiRunState } from "@/lib/envelope";
import type { RunSnapshot } from "@/transport/runStore";

import { AnnealingSignature } from "./AnnealingSignature";
import { CoolingChart } from "./CoolingChart";
import { SearchTraceChart } from "./SearchTraceChart";

/** Recharts measures its container; jsdom has no ResizeObserver at all, so
 *  without this the chart throws before it can be asserted on. */
class StubResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

function setReducedMotion(reduce: boolean): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: reduce,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
}

function snapshotOf(progress: readonly ProgressMessage[]): RunSnapshot {
  return {
    run_id: "run-000000000001",
    logs: [],
    progress,
    statuses: [],
    results: [],
    latestProgress: progress.at(-1) ?? null,
    status: null,
    terminal: false,
    lastSeq: progress.at(-1)?.seq ?? null,
    connection: "open",
    consecutiveFailures: 0,
    gaps: [],
    hydrated: true,
    droppedLogs: 0,
    droppedProgress: 0,
  };
}

function progressAt(
  seq: number,
  payload: Record<string, unknown>,
  primaryMetric: number | null = 500,
): ProgressMessage {
  return {
    type: "progress",
    run_id: "run-000000000001",
    seq,
    ts: 1_700_000_000_000 + seq,
    elapsed_seconds: seq,
    percent_complete: 10 * seq,
    primary_metric: primaryMetric,
    primary_metric_label: "best_fare",
    payload: {
      iteration: seq * 1000,
      iterations_total: 30_000,
      temperature: 12 / seq,
      current_objective: 300,
      current_value: 320,
      current_weight: 400,
      capacity: 480,
      feasible: true,
      acceptance_rate: 0.5,
      accepted_total: 100,
      items_selected: 21,
      ...payload,
    },
  };
}

/** Any class that would read as an error or a warning. */
const ALARM = /(?:bg|text|border|stroke|fill)-(?:bad|warn)\b/;

/** The lattice cells: the grid's direct children. */
function cellClasses(container: HTMLElement): string[] {
  const grid = container.querySelector("[aria-hidden='true']");
  return Array.from(grid?.children ?? []).map((cell) => cell.className);
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", StubResizeObserver);
  setReducedMotion(true);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("AnnealingSignature — feasible: false is the algorithm working", () => {
  it("uses no error or warning styling anywhere when the walk is over the shift", () => {
    const { container } = render(
      <AnnealingSignature
        state="RUNNING"
        snapshot={snapshotOf([
          progressAt(1, { feasible: false, current_weight: 512, capacity: 480 }),
        ])}
      />,
    );

    expect(container.innerHTML).not.toMatch(ALARM);
  });

  it("explains the overrun rather than flagging it", () => {
    render(
      <AnnealingSignature
        state="RUNNING"
        snapshot={snapshotOf([
          progressAt(1, { feasible: false, current_weight: 512, capacity: 480 }),
        ])}
      />,
    );

    expect(screen.getByText("512 / 480 min")).toBeInTheDocument();
    expect(screen.getByTitle(/on purpose/)).toBeInTheDocument();
  });

  it("does not present the current walk and the best selection as one solution", () => {
    // `items_selected` describes the incumbent; `current_value` describes the
    // walk. The readout has to say so or the two get read as a pair.
    render(
      <AnnealingSignature state="RUNNING" snapshot={snapshotOf([progressAt(1, {})])} />,
    );

    expect(
      screen.getByText(/from the best feasible selection, not the current walk/),
    ).toBeInTheDocument();
  });
});

describe("AnnealingSignature — lifecycle frames", () => {
  it.each<UiRunState>(["SUCCEEDED", "FAILED", "CANCELLED"])(
    "collapses to one flat frame in %s",
    (state) => {
      const { container } = render(
        <AnnealingSignature
          state={state}
          snapshot={snapshotOf([progressAt(1, {}), progressAt(2, {})])}
        />,
      );

      // The contract's rule: no per-element meaning survives the end of a run.
      expect(new Set(cellClasses(container)).size).toBe(1);
    },
  );

  it("keeps the lattice varied while the search is running", () => {
    const { container } = render(
      <AnnealingSignature state="RUNNING" snapshot={snapshotOf([progressAt(1, {})])} />,
    );

    expect(new Set(cellClasses(container)).size).toBeGreaterThan(1);
  });

  it("says nothing about temperature before any has been reported", () => {
    render(<AnnealingSignature state="RUNNING" snapshot={snapshotOf([])} />);

    expect(screen.getByText(/no temperature reported yet/)).toBeInTheDocument();
  });

  it("distinguishes no run selected from a queued one", () => {
    const { rerender } = render(
      <AnnealingSignature state={null} snapshot={snapshotOf([])} />,
    );
    expect(screen.getByText("No run selected")).toBeInTheDocument();

    rerender(<AnnealingSignature state="QUEUED" snapshot={snapshotOf([])} />);
    expect(screen.getByText(/Queued/)).toBeInTheDocument();
  });
});

describe("AnnealingSignature — reduced motion", () => {
  const hot = snapshotOf([progressAt(1, { temperature: 12, iteration: 1000 })]);
  const cold = snapshotOf([
    progressAt(1, { temperature: 12, iteration: 1000 }),
    progressAt(2, { temperature: 0.012, iteration: 29_000 }),
  ]);

  it("still tells hot from cold with the transition removed", () => {
    setReducedMotion(true);
    const { container, unmount } = render(
      <AnnealingSignature state="RUNNING" snapshot={hot} />,
    );

    expect(screen.getByText(/hot — uphill moves accepted freely/)).toBeInTheDocument();
    // The information survives; only the movement is gone.
    expect(container.innerHTML).not.toContain("transition-colors");
    const hotCells = new Set(cellClasses(container));
    unmount();

    render(<AnnealingSignature state="RUNNING" snapshot={cold} />);
    expect(
      screen.getByText(/cold — effectively hill-climbing now/),
    ).toBeInTheDocument();
    // A different palette, not merely different words.
    expect(cellClasses(document.body).some((c) => !hotCells.has(c))).toBe(true);
  });

  it("animates when reduced motion is off", () => {
    setReducedMotion(false);
    const { container } = render(<AnnealingSignature state="RUNNING" snapshot={hot} />);

    expect(container.innerHTML).toContain("transition-colors");
  });
});

describe("charts — empty and sparse progress", () => {
  it("shows a deliberate empty state rather than an empty axis", () => {
    render(<SearchTraceChart state="RUNNING" snapshot={snapshotOf([])} />);

    expect(screen.getByText(/No progress reported yet/)).toBeInTheDocument();
  });

  it("says a finished run with no progress is finished", () => {
    render(<CoolingChart state="SUCCEEDED" snapshot={snapshotOf([])} />);

    expect(screen.getByText(/finished without reporting any progress/)).toBeInTheDocument();
  });

  it("plots rather than apologising as soon as one point exists", () => {
    render(<SearchTraceChart state="RUNNING" snapshot={snapshotOf([progressAt(1, {})])} />);

    expect(screen.queryByText(/No progress reported yet/)).not.toBeInTheDocument();
  });

  it("still plots when every reported point is over the shift", () => {
    render(
      <CoolingChart
        state="RUNNING"
        snapshot={snapshotOf([
          progressAt(1, { feasible: false }),
          progressAt(2, { feasible: false }),
        ])}
      />,
    );

    expect(screen.queryByText(/No progress/)).not.toBeInTheDocument();
  });
});
