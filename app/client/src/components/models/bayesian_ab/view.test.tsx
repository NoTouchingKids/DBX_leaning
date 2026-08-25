/**
 * The behaviour this model's view exists to get right.
 *
 * `job/models/bayesian_ab` is closed-form: the whole run is five stages of
 * arithmetic and finishes in milliseconds. A browser will routinely be handed
 * the terminal status with `snapshot.progress` still empty, because the run
 * ended before the SSE stream delivered anything. So the test that matters is
 * not "does it render" — it is "does it render the *right* thing when it never
 * saw a single intermediate state".
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ResultMessage } from "@/lib/envelope";

import view from "./index";
import { snapshotOf } from "./testing";

const { Signature, charts } = view;
const DecisionChart = charts[1]?.Chart;

const ARM_A = {
  row_type: "arm",
  role: "A",
  label: "weekday_hours",
  trials: 1000,
  successes: 480,
  posterior_alpha: 481,
  posterior_beta: 521,
  posterior_mean: 0.48,
  expected_loss: 0.094,
  decision: "weekend_hours",
  conclusive: true,
  comparison: "weekend_fare",
  outcome: "the hour's average fare was above the pooled median",
  prior_alpha: 1,
  prior_beta: 1,
  credible_mass: 0.95,
  complete: true,
};

const ARM_B = {
  ...ARM_A,
  role: "B",
  label: "weekend_hours",
  trials: 400,
  successes: 230,
  posterior_alpha: 231,
  posterior_beta: 171,
  posterior_mean: 0.574,
  expected_loss: 0.0001,
};

const COMPARISON = {
  ...ARM_A,
  row_type: "comparison",
  role: "B_minus_A",
  label: "weekend_hours_vs_weekday_hours",
  posterior_alpha: null,
  posterior_beta: null,
  posterior_mean: 0.094,
  posterior_sd: 0.029,
  ci_low: 0.037,
  ci_high: 0.151,
  prob_beats_other: 0.9993,
  expected_loss: 0.0001,
};

function results(rows: Array<Record<string, unknown>>): ResultMessage {
  return {
    type: "result",
    run_id: "run-1",
    seq: 12,
    ts: 1_700_000_000_000,
    preview: rows,
    row_count: rows.length,
    fetch_hint: { table: "main.telemetry.bayesian_ab" },
    chunk_index: 0,
    final: true,
  };
}

describe("a run that finished before any progress arrived", () => {
  it("shows all five stages complete, from the status alone", () => {
    render(<Signature state="SUCCEEDED" snapshot={snapshotOf({})} />);
    expect(screen.getByText(/5\s*\/\s*5/)).toBeInTheDocument();
    expect(screen.getByText(/from status/i)).toBeInTheDocument();
  });

  it("names the winning arm from the result rows", () => {
    render(
      <Signature
        state="SUCCEEDED"
        snapshot={snapshotOf({ results: [results([ARM_A, ARM_B, COMPARISON])] })}
      />,
    );
    // The decision is an arm LABEL, not "A"/"B", and it is legible next to the
    // two arms it chose between.
    expect(screen.getAllByText("weekend_hours").length).toBeGreaterThan(0);
    expect(screen.getByText("weekday_hours")).toBeInTheDocument();
    expect(screen.getByText(/read from the result rows/i)).toBeInTheDocument();
  });

  it("reports the decision numbers from the results, in the results' own shape", () => {
    if (DecisionChart === undefined) throw new Error("decision chart is missing");
    render(
      <DecisionChart
        state="SUCCEEDED"
        snapshot={snapshotOf({ results: [results([ARM_A, ARM_B, COMPARISON])] })}
      />,
    );
    // prob_beats_other on the comparison row IS prob_b_beats_a.
    expect(screen.getByText("0.9993")).toBeInTheDocument();
    // Both expected losses, because the model's rule consults both.
    expect(screen.getByText("0.09400")).toBeInTheDocument();
    expect(screen.getByText("0.00010")).toBeInTheDocument();
  });

  it("says nothing is known rather than showing zeroes, when there are no results either", () => {
    if (DecisionChart === undefined) throw new Error("decision chart is missing");
    render(<DecisionChart state="SUCCEEDED" snapshot={snapshotOf({})} />);
    expect(screen.getByText(/nothing to decide from yet/i)).toBeInTheDocument();
  });
});

describe("a run that failed before its first stage", () => {
  it("says that this model validates its own config", () => {
    // The only model here that raises on a bad `comparison`, so a FAILED run
    // with zero completed stages is usually a typo in the form, not a crash.
    render(<Signature state="FAILED" snapshot={snapshotOf({})} />);
    expect(screen.getByText(/validates its own config/i)).toBeInTheDocument();
  });

  it("does not say it for a run that failed later on", () => {
    const progressed = snapshotOf({
      progress: [
        {
          type: "progress",
          run_id: "run-1",
          seq: 3,
          ts: 1,
          elapsed_seconds: 0.002,
          percent_complete: 60,
          primary_metric: 0.7,
          primary_metric_label: "prob_b_beats_a",
          payload: { stage: "expected_loss", stage_index: 3, stages_total: 5 },
        },
      ],
    });
    render(<Signature state="FAILED" snapshot={progressed} />);
    expect(screen.queryByText(/validates its own config/i)).not.toBeInTheDocument();
  });
});

describe("before anything at all", () => {
  it("renders a queued run without inventing arms or a decision", () => {
    render(<Signature state="QUEUED" snapshot={snapshotOf({})} />);
    expect(screen.getByText(/0\s*\/\s*5/)).toBeInTheDocument();
    expect(screen.queryByText(/leads, conclusively/i)).not.toBeInTheDocument();
  });
});
