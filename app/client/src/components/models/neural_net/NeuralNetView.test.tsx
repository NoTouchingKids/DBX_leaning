/**
 * Behaviour of the neural_net view under the awkward cases.
 *
 * The signature is hand-written SVG and renders in jsdom, so its accessible
 * name is the assertion surface — it states the same thing the picture does,
 * which is also what a reduced-motion or screen-reader user gets.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ProgressMessage, UiRunState } from "@/lib/envelope";
import type { RunSnapshot } from "@/transport/runStore";
import { AccuracyChart } from "./AccuracyChart";
import { LossChart } from "./LossChart";
import { NeuralNetSignature } from "./NeuralNetSignature";
import neuralNetView from "./index";

const BATCHES = 6;

function snapshot(partial: Partial<RunSnapshot> = {}): RunSnapshot {
  const progress = partial.progress ?? [];
  return {
    run_id: "run-nn",
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

function sample(
  seq: number,
  level: "epoch" | "batch",
  epoch: number,
  batch: number,
  best: number | null,
): ProgressMessage {
  return {
    type: "progress",
    run_id: "run-nn",
    seq,
    ts: 1_700_000_000_000 + seq,
    elapsed_seconds: seq * 0.05,
    percent_complete: (100 * (epoch * BATCHES + batch + 1)) / (BATCHES * 3),
    primary_metric: 0.63,
    primary_metric_label: "val_accuracy",
    payload: {
      level,
      epoch,
      epochs_total: 3,
      batch,
      batches_per_epoch: BATCHES,
      train_loss: 0.8,
      val_loss: 0.79,
      macro_f1: 0.52,
      grad_norm: 1.1,
      learning_rate: 0.01,
      best_val_accuracy: best,
      baseline_accuracy: 0.55,
      device: "cpu",
      data_synthetic: false,
    },
  };
}

describe("the neural_net view's contract obligations", () => {
  it("names the model it plugs into and carries an honesty note", () => {
    expect(neuralNetView.model).toBe("neural_net");
    expect(neuralNetView.honesty.length).toBeGreaterThan(80);
    expect(neuralNetView.charts).toHaveLength(2);
  });

  it("says in the honesty note which half of the animation is a sketch", () => {
    // The note is the thing that stops the decorative hidden-layer widths
    // being read as this run's architecture. If it stops saying so, the
    // visual becomes a quiet lie.
    expect(neuralNetView.honesty).toMatch(/decorative/i);
    expect(neuralNetView.honesty).toMatch(/hidden/i);
  });
});

describe("NeuralNetSignature", () => {
  it("renders with no run at all", () => {
    render(<NeuralNetSignature state={null} snapshot={snapshot()} />);
    expect(screen.getByRole("img")).toHaveAccessibleName(/no run selected/i);
  });

  it("counts completed epochs from epoch-level messages only", () => {
    // A batch-level message carries the epoch it is INSIDE. Counting it would
    // fill a cell two thirds of an epoch early, every epoch.
    render(
      <NeuralNetSignature
        state="RUNNING"
        snapshot={snapshot({ progress: [sample(1, "batch", 0, 1, null)] })}
      />,
    );
    expect(screen.getByRole("img")).toHaveAccessibleName(/0 of 3 epochs complete/i);
  });

  it("fills a cell once the epoch-level message arrives", () => {
    render(
      <NeuralNetSignature
        state="RUNNING"
        snapshot={snapshot({
          progress: [sample(1, "batch", 0, 1, null), sample(2, "epoch", 0, BATCHES - 1, 0.62)],
        })}
      />,
    );
    expect(screen.getByRole("img")).toHaveAccessibleName(/1 of 3 epochs complete/i);
  });

  it("reports the batch position inside the epoch in flight", () => {
    render(
      <NeuralNetSignature
        state="RUNNING"
        snapshot={snapshot({
          progress: [
            sample(1, "batch", 0, 1, null),
            sample(2, "epoch", 0, BATCHES - 1, 0.62),
            sample(3, "batch", 1, 2, 0.62),
          ],
        })}
      />,
    );
    expect(screen.getByRole("img")).toHaveAccessibleName(/batch 3 of 6 in the current epoch/i);
  });

  it("stops reporting a batch in flight once the run settles", () => {
    render(
      <NeuralNetSignature
        state="CANCELLED"
        snapshot={snapshot({ progress: [sample(3, "batch", 1, 2, 0.62)] })}
      />,
    );
    expect(screen.getByRole("img")).not.toHaveAccessibleName(/in the current epoch/i);
  });
});

describe("AccuracyChart", () => {
  it("says what an unobserved settled run is, rather than showing a spinner", () => {
    render(<AccuracyChart state="SUCCEEDED" snapshot={snapshot()} />);
    expect(screen.getByText(/no progress messages were seen/i)).toBeInTheDocument();
  });

  it("distinguishes 'not read yet' from 'nothing happened'", () => {
    render(<AccuracyChart state="RUNNING" snapshot={snapshot({ hydrated: false })} />);
    expect(screen.getByText(/reading cached history/i)).toBeInTheDocument();
  });

  it("says best_val_accuracy is absent, not zero, during the first epoch", () => {
    render(
      <AccuracyChart
        state="RUNNING"
        snapshot={snapshot({ progress: [sample(1, "batch", 0, 1, null)] })}
      />,
    );
    expect(screen.getByText(/no epoch finished/i)).toBeInTheDocument();
  });
});

describe("LossChart", () => {
  it("names both levels in its empty state", () => {
    render(<LossChart state="QUEUED" snapshot={snapshot()} />);
    expect(screen.getByText(/level: "batch"/i)).toBeInTheDocument();
    expect(screen.getByText(/level: "epoch"/i)).toBeInTheDocument();
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
    const { unmount } = render(<NeuralNetSignature state={state} snapshot={snapshot()} />);
    expect(screen.getByRole("img")).toBeInTheDocument();
    unmount();
  });
});
