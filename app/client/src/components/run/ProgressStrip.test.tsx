import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ProgressMessage } from "@/lib/envelope";
import { ProgressStrip } from "./ProgressStrip";

function progress(over: Partial<ProgressMessage> = {}): ProgressMessage {
  return {
    type: "progress",
    run_id: "run-a",
    seq: 1,
    ts: 1_700_000_000_000,
    elapsed_seconds: 12,
    percent_complete: null,
    primary_metric: null,
    primary_metric_label: null,
    payload: {},
    ...over,
  };
}

describe("ProgressStrip", () => {
  it("renders an indeterminate bar for percent_complete: null", () => {
    // Null is a real value — gurobi_scheduling reports it for the whole run.
    // The accessibility tree must say "in progress, amount unknown", which is
    // spelled as a progressbar with no aria-valuenow.
    render(<ProgressStrip progress={progress()} running />);
    const bar = screen.getByRole("progressbar");
    expect(bar).not.toHaveAttribute("aria-valuenow");
    expect(bar).toHaveAttribute("aria-valuetext", "unknown");
  });

  it("renders a real 0% as determinate, not as unknown", () => {
    render(<ProgressStrip progress={progress({ percent_complete: 0 })} running />);
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "0");
    expect(screen.getByText("0.0%")).toBeInTheDocument();
  });

  it("renders a percentage when one is reported", () => {
    render(<ProgressStrip progress={progress({ percent_complete: 41.6667 })} running />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "41.6667");
    expect(screen.getByText("41.7%")).toBeInTheDocument();
  });

  it("shows a null primary_metric as absent, keeping its label slot", () => {
    render(
      <ProgressStrip
        progress={progress({ primary_metric: null, primary_metric_label: "max_rhat" })}
        running
      />,
    );
    // The label keeps its slot so the layout does not reflow every time a
    // model emits a message without a metric.
    expect(screen.getByText("max_rhat")).toBeInTheDocument();
    expect(screen.getAllByText("—")).toHaveLength(2);
  });

  it("survives having no progress message at all", () => {
    render(<ProgressStrip progress={null} running={false} />);
    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
    expect(screen.getByText("primary_metric")).toBeInTheDocument();
  });
});
