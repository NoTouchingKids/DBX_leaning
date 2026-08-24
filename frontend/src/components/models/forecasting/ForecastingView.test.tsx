/**
 * Behaviour, not markup: what each view SAYS when the data is awkward.
 *
 * jsdom gives recharts a zero-sized container, so nothing is asserted about
 * the drawn chart. The interesting cases here are the ones that never reach
 * recharts at all — an empty run, a run that wrote no rows — plus the
 * signature, which is hand-written SVG and does render.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ProgressMessage, ResultMessage, UiRunState } from "@/lib/envelope";
import type { RunSnapshot } from "@/transport/runStore";
import forecastingView from "./index";
import { ForecastRevealChart } from "./ForecastRevealChart";
import { ForecastingSignature } from "./ForecastingSignature";
import { TrainingLossChart } from "./TrainingLossChart";

function snapshot(partial: Partial<RunSnapshot> = {}): RunSnapshot {
  const progress = partial.progress ?? [];
  return {
    run_id: "run-abc",
    logs: [],
    progress,
    statuses: [],
    results: [],
    latestProgress: progress.at(-1) ?? null,
    status: null,
    terminal: false,
    lastSeq: null,
    connection: "open",
    consecutiveFailures: 0,
    gaps: [],
    hydrated: true,
    droppedLogs: 0,
    droppedProgress: 0,
    ...partial,
  };
}

function progressMessage(seq: number, percent: number | null, epoch: number): ProgressMessage {
  return {
    type: "progress",
    run_id: "run-abc",
    seq,
    ts: 1_700_000_000_000 + seq,
    elapsed_seconds: seq,
    percent_complete: percent,
    primary_metric: 0.4,
    primary_metric_label: "val_loss",
    payload: {
      epoch,
      epochs_total: 40,
      train_loss: 0.5,
      best_val_loss: 0.4,
      learning_rate: 0.01,
      data_synthetic: false,
    },
  };
}

function emptyResult(): ResultMessage {
  return {
    type: "result",
    run_id: "run-abc",
    seq: 99,
    ts: 1_700_000_000_099,
    preview: [],
    row_count: 0,
    fetch_hint: {},
    chunk_index: 0,
    final: true,
  };
}

describe("the forecasting view's contract obligations", () => {
  it("names the model it plugs into and carries an honesty note", () => {
    expect(forecastingView.model).toBe("forecasting");
    expect(forecastingView.honesty.length).toBeGreaterThan(80);
    expect(forecastingView.charts).toHaveLength(2);
  });
});

describe("ForecastingSignature", () => {
  it("renders with no run at all", () => {
    render(<ForecastingSignature state={null} snapshot={snapshot()} />);
    expect(screen.getByRole("img")).toHaveAccessibleName(/no run selected/i);
  });

  it("draws no markers when percent_complete is null", () => {
    // A null percentage is a real value, not a loading state. It must not
    // become 0% quietly, and it must not throw.
    render(
      <ForecastingSignature
        state="RUNNING"
        snapshot={snapshot({ progress: [progressMessage(1, null, 0)] })}
      />,
    );
    expect(screen.getByRole("img")).toHaveAccessibleName(/0 of 24 horizon markers/i);
  });

  it("tracks percent_complete rather than the message count", () => {
    render(
      <ForecastingSignature
        state="RUNNING"
        snapshot={snapshot({ progress: [progressMessage(1, 50, 19)] })}
      />,
    );
    expect(screen.getByRole("img")).toHaveAccessibleName(/12 of 24 horizon markers/i);
  });

  it("fills the horizon on SUCCEEDED whatever the last message said", () => {
    render(
      <ForecastingSignature
        state="SUCCEEDED"
        snapshot={snapshot({ progress: [progressMessage(1, 62, 24)] })}
      />,
    );
    expect(screen.getByRole("img")).toHaveAccessibleName(/24 of 24 horizon markers/i);
  });

  it("freezes a cancelled run where it actually stopped", () => {
    render(
      <ForecastingSignature
        state="CANCELLED"
        snapshot={snapshot({ progress: [progressMessage(1, 25, 9)] })}
      />,
    );
    expect(screen.getByRole("img")).toHaveAccessibleName(/6 of 24 horizon markers/i);
  });
});

describe("empty and zero-row states", () => {
  it("says a settled run was simply never observed, rather than showing a spinner", () => {
    render(<TrainingLossChart state="SUCCEEDED" snapshot={snapshot()} />);
    expect(screen.getByText(/no progress messages were seen/i)).toBeInTheDocument();
  });

  it("distinguishes 'not read yet' from 'nothing happened'", () => {
    render(<TrainingLossChart state="RUNNING" snapshot={snapshot({ hydrated: false })} />);
    expect(screen.getByText(/reading cached history/i)).toBeInTheDocument();
  });

  it("reports row_count 0 as an outcome, not as an empty chart", () => {
    render(
      <ForecastRevealChart state="SUCCEEDED" snapshot={snapshot({ results: [emptyResult()] })} />,
    );
    expect(screen.getByText(/0/)).toBeInTheDocument();
    expect(screen.getByText(/never kept a checkpoint/i)).toBeInTheDocument();
  });

  it("waits quietly for the forecast while the run is still going", () => {
    render(<ForecastRevealChart state="RUNNING" snapshot={snapshot()} />);
    expect(screen.getByText(/written once, at the end of the run/i)).toBeInTheDocument();
  });
});

describe("every lifecycle state renders", () => {
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

  it.each(states)("%s", (state) => {
    const { unmount } = render(<ForecastingSignature state={state} snapshot={snapshot()} />);
    expect(screen.getByRole("img")).toBeInTheDocument();
    unmount();
  });
});
