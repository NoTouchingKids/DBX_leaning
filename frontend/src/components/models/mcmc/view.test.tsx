/**
 * What the mcmc panel says when the numbers are missing, wrong, or alarming.
 *
 * The arithmetic is tested in `payload.test.ts` and `walkers.test.ts`; this
 * file is only about the cases where a chart has to say something rather than
 * draw something.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ProgressMessage } from "@/lib/envelope";
import type { RunSnapshot } from "@/transport/runStore";

import view from "./index";

const { Signature, charts } = view;
const TraceChart = charts[0]?.Chart;
const ChainHealthChart = charts[1]?.Chart;

function snapshotOf(progress: ProgressMessage[]): RunSnapshot {
  return {
    run_id: "run-1",
    logs: [],
    progress,
    statuses: [],
    results: [],
    latestProgress: progress.at(-1) ?? null,
    status: null,
    terminal: false,
    lastSeq: null,
    connection: "idle",
    consecutiveFailures: 0,
    gaps: [],
    hydrated: true,
    droppedLogs: 0,
    droppedProgress: 0,
  };
}

function progress(payload: Record<string, unknown>, rhat: number | null): ProgressMessage {
  return {
    type: "progress",
    run_id: "run-1",
    seq: 1,
    ts: 1_700_000_000_000,
    elapsed_seconds: 12,
    percent_complete: 40,
    primary_metric: rhat,
    primary_metric_label: "max_rhat",
    payload,
  };
}

describe("chain health", () => {
  it("explains an empty chart instead of drawing an empty axis", () => {
    if (ChainHealthChart === undefined) throw new Error("chain health chart is missing");
    render(<ChainHealthChart state="RUNNING" snapshot={snapshotOf([])} />);
    expect(screen.getByText(/no acceptance figures yet/i)).toBeInTheDocument();
  });

  it("surfaces stuck_chains as the diagnostic, and says what it means", () => {
    if (ChainHealthChart === undefined) throw new Error("chain health chart is missing");
    render(
      <ChainHealthChart
        state="RUNNING"
        snapshot={snapshotOf([
          progress({ per_chain_acceptance: [0.4, 0, 0.38], stuck_chains: 1, mean_acceptance: 0.26 }, 1.02),
        ])}
      />,
    );
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.getByText(/accepted nothing since the run began/i)).toBeInTheDocument();
  });
});

describe("live trace", () => {
  it("says why it is empty rather than showing a broken axis", () => {
    if (TraceChart === undefined) throw new Error("trace chart is missing");
    render(<TraceChart state="RUNNING" snapshot={snapshotOf([])} />);
    expect(screen.getByText(/no chain positions yet/i)).toBeInTheDocument();
  });

  it("is empty for a run whose payload predates chain_positions", () => {
    // The field is new. An older run's history has acceptance but no
    // positions, and that has to be an empty state, not a crash.
    if (TraceChart === undefined) throw new Error("trace chart is missing");
    render(
      <TraceChart
        state="SUCCEEDED"
        snapshot={snapshotOf([progress({ per_chain_acceptance: [0.4, 0.38] }, 1.01)])}
      />,
    );
    expect(screen.getByText(/no chain positions yet/i)).toBeInTheDocument();
  });
});

describe("the signature", () => {
  it("reports a null max_rhat as absent, not as zero", () => {
    // r-hat is null whenever it is non-finite or there are too few
    // post-burn-in draws. That is a real value.
    render(
      <Signature
        state="RUNNING"
        snapshot={snapshotOf([progress({ chains: 8, draws_done: 400, draws_total: 3000 }, null)])}
      />,
    );
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("describes a run with no telemetry at all without inventing any", () => {
    render(<Signature state="QUEUED" snapshot={snapshotOf([])} />);
    expect(screen.getByText(/waiting for compute/i)).toBeInTheDocument();
  });
});
