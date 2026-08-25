/**
 * The rendering invariants, not the markup.
 *
 * Six things here are worth a regression test and nothing else in this file
 * is:
 *
 *  - The fitted/failed split is on screen at all, in every state including
 *    terminal. It is the reason the model exists and the reason this view is
 *    not the generic one.
 *  - A failed group is never dressed as an error. `FailureTone` makes that a
 *    type error, but the classes are written by hand, so this is the half a
 *    type cannot check.
 *  - Each of the four failure reasons renders distinctly.
 *  - A run failing everything and a run failing nothing look different, and
 *    neither looks like a crash.
 *  - The synthetic panel is visible. The default table has never been created,
 *    so the DEFAULT run is generated data and a chart of it is otherwise
 *    indistinguishable from a chart of measured data.
 *  - A terminal run flattens the lattice to one frame, per `contract.ts`, and
 *    reduced motion removes the pulse without removing the frontier.
 */

import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ProgressMessage, ResultMessage, UiRunState } from "@/lib/envelope";
import type { RunSnapshot } from "@/transport/runStore";

import { FIXTURE_NAMES, hasScript, makeSnapshot } from "@/dev/fixtures";

import { FailureReasons } from "./FailureReasons";
import { FitQualityChart } from "./FitQualityChart";
import { PanelLattice } from "./PanelLattice";
import view from "./index";
import { accumulateGroups, readCounts } from "./panelModel";

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

const SYNTHETIC = {
  data_source: "synthetic:owid-panel",
  data_synthetic: true,
  data_rows: 1_284,
  data_fallback_reason: "main.dbx_leaning.owid_country_year does not exist",
};

function progressFor(
  done: number,
  fitted: number,
  failed: number,
  extra: Record<string, unknown> = {},
  metric: number | null = 0.88,
): ProgressMessage {
  return {
    type: "progress",
    run_id: "run-000000000001",
    seq: 100 + done,
    ts: 1_700_000_000_000 + done,
    elapsed_seconds: done * 0.02,
    percent_complete: (100 * done) / 48,
    primary_metric: metric,
    primary_metric_label: "median_r_squared",
    payload: {
      groups_done: done,
      groups_total: 48,
      groups_fitted: fitted,
      groups_failed: failed,
      failure_counts: {},
      group_key: `country-${done}`,
      group_label: `C${done}`,
      group_status: "fitted",
      group_failure_reason: null,
      group_r_squared: 0.9,
      n_observations: 40,
      rows_seen: 42,
      metric_higher_is_better: true,
      degree: 1,
      chunks_emitted: 0,
      ...SYNTHETIC,
      ...extra,
    },
  };
}

/** A whole run at `progress_every: 1`, failures first. */
function runOf(total: number, failed: number, reason = "too_few_observations"): ProgressMessage[] {
  const out: ProgressMessage[] = [];
  const counts: Record<string, number> = {};
  let f = 0;
  let ok = 0;
  for (let done = 1; done <= total; done += 1) {
    const fails = done <= failed;
    if (fails) {
      f += 1;
      counts[reason] = (counts[reason] ?? 0) + 1;
    } else ok += 1;
    out.push(
      progressFor(done, ok, f, {
        groups_total: total,
        failure_counts: { ...counts },
        group_status: fails ? "failed" : "fitted",
        group_failure_reason: fails ? reason : null,
        group_r_squared: fails ? null : 0.75 + (done % 20) / 100,
        n_observations: fails ? 2 : 40,
        rows_seen: fails ? 21 : 42,
      }),
    );
  }
  return out;
}

function chunkOf(index: number, groups: number, final = false): ResultMessage {
  return {
    type: "result",
    run_id: "run-000000000001",
    seq: 900 + index,
    ts: 1_700_000_000_000,
    preview: Array.from({ length: groups }, (_, i) => ({
      group_key: `country-${index * 12 + i}`,
      group_label: `C${index * 12 + i}`,
      status: i === 0 ? "failed" : "fitted",
      failure_reason: i === 0 ? "too_few_observations" : null,
      r_squared: i === 0 ? null : 0.9,
      n_observations: i === 0 ? 2 : 40,
    })),
    row_count: groups,
    fetch_hint: { table: "main.dbx_leaning.results_panel_fit" },
    chunk_index: index,
    final,
  };
}

function snapshotOf(
  progress: readonly ProgressMessage[],
  results: readonly ResultMessage[] = [],
): RunSnapshot {
  return {
    run_id: "run-000000000001",
    logs: [],
    progress,
    statuses: [],
    results,
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

function renderLattice(state: UiRunState | null, snapshot: RunSnapshot) {
  return render(<PanelLattice state={state} snapshot={snapshot} />).container;
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", StubResizeObserver);
  setReducedMotion(false);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/* ------------------------------------------------------------------ *
 * The headline
 * ------------------------------------------------------------------ */

describe("PanelLattice — the split is the headline", () => {
  it("shows fitted and failed counts, not just a percentage", () => {
    // The whole failure this model exists to prevent: a view that renders only
    // percent_complete and a metric draws a healthy run and a run failing a
    // third of its groups identically.
    const container = renderLattice("RUNNING", snapshotOf(runOf(30, 9)));
    const text = container.textContent ?? "";
    expect(text).toContain("fitted");
    expect(text).toContain("failed");
    // Twice on purpose: the lattice and the outcome bar carry the same label,
    // because the bar is what still says it once the lattice has flattened.
    expect(
      screen.getAllByRole("img", { name: /21 fitted, 9 failed of 30 groups processed/ }),
    ).toHaveLength(2);
  });

  it("keeps the split visible after the run is over", () => {
    // The lattice flattens per contract.ts; the outcome bar and the numbers
    // are data rather than animation and must survive, because "SUCCEEDED
    // with 9 of 48 groups failed" is a thing to know about a finished run.
    const container = renderLattice("SUCCEEDED", snapshotOf(runOf(48, 9)));
    expect(container.textContent).toContain("failure rate");
    expect(screen.getAllByRole("img", { name: /39 fitted, 9 failed/ }).length).toBeGreaterThan(0);
  });

  it("renders a zero-failure run without inventing a warning", () => {
    const container = renderLattice("SUCCEEDED", snapshotOf(runOf(48, 0)));
    expect(container.textContent).toContain("no group has failed to fit");
    expect(container.querySelector("[data-cell]")).toBeTruthy();
    // No failure-rate stat at all when there is nothing to rate.
    expect(container.textContent).not.toContain("failure rate");
  });

  it("renders an all-failed run as the model's own verdict, not a crash", () => {
    const container = renderLattice("INFEASIBLE", snapshotOf(runOf(12, 12)));
    const text = container.textContent ?? "";
    expect(text).toContain("No group could be fitted");
    // INFEASIBLE is a designed state here: the run completed and its results
    // are durable. It must not read as a failure of the run.
    expect(text).toContain("not a crash");
    expect(text).toContain("reported INFEASIBLE");
  });

  it("says an all-failed run is heading for INFEASIBLE while it is still live", () => {
    const container = renderLattice("RUNNING", snapshotOf(runOf(6, 6)));
    expect(container.textContent).toContain("INFEASIBLE rather than a success with nothing in it");
  });
});

describe("PanelLattice — a failed group is information, not alarm", () => {
  it("uses no error colour anywhere in a run with failures", () => {
    // `FailureTone` has no `bad` member, so this cannot be reached through the
    // tone. It can still be reached by someone typing `text-bad` into a class
    // string, which is exactly what this asserts against.
    for (const failed of [1, 9, 24, 30]) {
      cleanup();
      const container = renderLattice("RUNNING", snapshotOf(runOf(30, failed)));
      expect(container.innerHTML, `failed ${failed}`).not.toMatch(/(^|[\s"])[a-z-]*-bad\b/);
    }
  });

  it("keeps a minority of failures cool and a majority merely notable", () => {
    const routine = renderLattice("RUNNING", snapshotOf(runOf(30, 6)));
    expect(routine.textContent).toContain("normal for a panel with short units");
    cleanup();
    const notable = renderLattice("RUNNING", snapshotOf(runOf(30, 20)));
    expect(notable.textContent).toContain("at least half the completed groups");
    expect(notable.innerHTML).toContain("warn");
  });

  it("names the dominant reason next to the count", () => {
    const container = renderLattice("RUNNING", snapshotOf(runOf(30, 9, "singular_design")));
    expect(container.textContent).toContain("mostly singular design (9)");
  });

  it("shows usable rows against rows seen for the last group reported", () => {
    // "This unit is small" and "this unit did not report" are different
    // answers, and this pair is the only place the difference shows.
    const container = renderLattice("RUNNING", snapshotOf(runOf(3, 3)));
    expect(container.textContent).toContain("2 usable of 21 rows");
  });
});

/* ------------------------------------------------------------------ *
 * The invariant
 * ------------------------------------------------------------------ */

describe("PanelLattice — the invariant", () => {
  it("says so when groups_fitted + groups_failed does not equal groups_done", () => {
    const container = renderLattice("RUNNING", snapshotOf([progressFor(10, 6, 2)]));
    expect(container.textContent).toContain("Inconsistent payload");
    expect(container.textContent).toContain("cannot be trusted");
  });

  it("says nothing when the numbers agree", () => {
    const container = renderLattice("RUNNING", snapshotOf(runOf(20, 4)));
    expect(container.textContent).not.toContain("Inconsistent payload");
  });
});

/* ------------------------------------------------------------------ *
 * Provenance
 * ------------------------------------------------------------------ */

describe("PanelLattice — provenance", () => {
  it("says loudly when the panel is synthetic", () => {
    // The default table has never been created, so this is the DEFAULT run.
    const container = renderLattice("RUNNING", snapshotOf(runOf(10, 2)));
    expect(container.textContent).toContain("Synthetic panel");
    expect(container.textContent).toContain("synthetic:owid-panel");
    expect(container.textContent).toContain("does not exist");
  });

  it("names the real table when the panel is real", () => {
    const real = runOf(10, 2).map((message) => ({
      ...message,
      payload: {
        ...message.payload,
        data_source: "main.dbx_leaning.owid_country_year",
        data_synthetic: false,
        data_fallback_reason: null,
      },
    }));
    const container = renderLattice("RUNNING", snapshotOf(real));
    expect(container.textContent).toContain("main.dbx_leaning.owid_country_year");
    expect(container.textContent).not.toContain("Synthetic panel");
  });

  it("claims nothing when the run has not reported provenance", () => {
    const container = renderLattice("RUNNING", snapshotOf([progressFor(1, 1, 0, {
      data_source: undefined,
      data_synthetic: undefined,
      data_fallback_reason: undefined,
    })]));
    expect(container.textContent).not.toContain("Synthetic panel");
    expect(container.textContent).not.toContain("Real rows");
  });
});

/* ------------------------------------------------------------------ *
 * Lifecycle
 * ------------------------------------------------------------------ */

describe("PanelLattice — lifecycle", () => {
  it("collapses to one flat frame when the run is over", () => {
    for (const state of ["SUCCEEDED", "FAILED", "CANCELLED", "INFEASIBLE"] as const) {
      cleanup();
      const container = renderLattice(state, snapshotOf(runOf(48, 9)));
      const kinds = new Set(
        [...container.querySelectorAll("[data-cell]")].map((el) => el.getAttribute("data-cell")),
      );
      expect(kinds, state).toEqual(new Set(["flat"]));
    }
  });

  it("draws the outcome split while the run is live", () => {
    // Twelve of twenty done, so there is a pending block to draw.
    const container = renderLattice("RUNNING", snapshotOf(runOf(20, 5).slice(0, 12)));
    const kinds = [...container.querySelectorAll("[data-cell]")].map((el) =>
      el.getAttribute("data-cell"),
    );
    expect(new Set(kinds)).toEqual(new Set(["fitted", "failed", "pending"]));
  });

  it("pulses the frontier cell, and stops pulsing under reduced motion", () => {
    const moving = renderLattice("RUNNING", snapshotOf(runOf(20, 5).slice(0, 12)));
    expect(moving.querySelectorAll(".animate-pulse")).toHaveLength(1);

    cleanup();
    setReducedMotion(true);
    const still = renderLattice("RUNNING", snapshotOf(runOf(20, 5).slice(0, 12)));
    // Reduced motion drops the transition, never the information: the cells,
    // the counts and the frontier's position are all still there.
    expect(still.querySelectorAll(".animate-pulse")).toHaveLength(0);
    expect(still.querySelectorAll("[data-cell]")).toHaveLength(20);
    expect(still.textContent).toContain("failed");
    // The frontier is still marked, just not moving.
    expect(still.querySelectorAll(".border-accent")).toHaveLength(1);
  });

  it("has something to say before the first group arrives", () => {
    expect(renderLattice("STARTING", snapshotOf([])).textContent).toContain(
      "Waiting for the first group",
    );
    cleanup();
    expect(renderLattice(null, snapshotOf([])).textContent).toContain("No run selected");
  });

  it("calls an empty panel an empty run, not an empty chart", () => {
    // `total == 0` never emits a progress message at all — the loop does not
    // run — so a settled run with nothing in it is a real shape.
    expect(renderLattice("SUCCEEDED", snapshotOf([])).textContent).toContain(
      "reported no groups at all",
    );
  });
});

/* ------------------------------------------------------------------ *
 * Charts
 * ------------------------------------------------------------------ */

describe("FitQualityChart", () => {
  it("reports the median and how many groups are plotted", () => {
    const { container } = render(
      <FitQualityChart state="RUNNING" snapshot={snapshotOf(runOf(30, 6))} />,
    );
    expect(container.textContent).toContain("median R²");
    expect(container.textContent).toContain("higher is better");
    expect(container.textContent).toContain("groups plotted");
  });

  it("says why failed groups are absent rather than leaving gaps unexplained", () => {
    const { container } = render(
      <FitQualityChart state="SUCCEEDED" snapshot={snapshotOf(runOf(30, 6))} />,
    );
    expect(container.textContent).toContain("6 groups");
    expect(container.textContent).toContain("no R-squared");
  });

  it("points at the other card when nothing could be fitted at all", () => {
    const { container } = render(
      <FitQualityChart state="INFEASIBLE" snapshot={snapshotOf(runOf(12, 12))} />,
    );
    expect(container.textContent).toContain("Every group processed so far failed");
    expect(container.textContent).toContain("card beside this one");
  });

  it("distinguishes a fitted group with no R-squared from a failure", () => {
    // A flat response has no variance to explain, so R-squared is undefined on
    // a perfectly good fit. Folding those in with the failures would be wrong.
    const flat = runOf(6, 0).map((m) => ({
      ...m,
      payload: { ...m.payload, group_r_squared: null },
    }));
    const { container } = render(<FitQualityChart state="RUNNING" snapshot={snapshotOf(flat)} />);
    expect(container.textContent).toContain("no variance to explain");
  });

  it("takes the metric direction from the run, not from another model", () => {
    const inverted = runOf(6, 0).map((m) => ({
      ...m,
      payload: { ...m.payload, metric_higher_is_better: false },
    }));
    const { container } = render(
      <FitQualityChart state="RUNNING" snapshot={snapshotOf(inverted)} />,
    );
    expect(container.textContent).toContain("lower is better");
  });

  it("says nothing has been reported rather than drawing empty axes", () => {
    const { container } = render(<FitQualityChart state="STARTING" snapshot={snapshotOf([])} />);
    expect(container.textContent).toContain("No groups reported yet");
  });
});

describe("FailureReasons", () => {
  it("renders each of the four reasons distinctly", () => {
    // "Failed how" is the question this card answers, so two reasons that read
    // the same are the same as not answering it.
    const messages = [
      progressFor(10, 0, 10, {
        failure_counts: {
          too_few_observations: 4,
          zero_predictor_variance: 3,
          singular_design: 2,
          non_finite_result: 1,
        },
      }),
    ];
    const { container } = render(
      <FailureReasons state="SUCCEEDED" snapshot={snapshotOf(messages)} />,
    );
    const list = container.querySelector("ul");
    expect(list).toBeTruthy();
    const items = within(list as HTMLElement).getAllByRole("img");
    expect(items).toHaveLength(4);
    for (const reason of [
      "too_few_observations",
      "zero_predictor_variance",
      "singular_design",
      "non_finite_result",
    ]) {
      expect(container.textContent, reason).toContain(reason);
    }
    // Distinct labels, not four copies of "failed".
    expect(container.textContent).toContain("Too few observations");
    expect(container.textContent).toContain("No predictor variance");
    expect(container.textContent).toContain("Singular design");
    expect(container.textContent).toContain("Non-finite result");
  });

  it("shows a reason the model should not be able to emit", () => {
    const { container } = render(
      <FailureReasons
        state="SUCCEEDED"
        snapshot={snapshotOf([progressFor(3, 0, 3, { failure_counts: { made_up: 3 } })])}
      />,
    );
    expect(container.textContent).toContain("Unrecognised reason");
    expect(container.textContent).toContain("made_up");
  });

  it("lists the reasons a run did NOT hit, because a zero is an answer", () => {
    const { container } = render(
      <FailureReasons
        state="SUCCEEDED"
        snapshot={snapshotOf([progressFor(10, 6, 4, { failure_counts: { singular_design: 4 } })])}
      />,
    );
    expect(container.textContent).toContain("Not seen in this run");
    expect(container.textContent).toContain("too_few_observations");
  });

  it("names recent failures with usable rows against rows seen", () => {
    const { container } = render(<FailureReasons state="RUNNING" snapshot={snapshotOf(runOf(4, 4))} />);
    expect(container.textContent).toContain("Most recent failures");
    expect(container.textContent).toContain("2 usable of 21 rows");
  });

  it("says nothing has failed without implying something will", () => {
    const { container } = render(
      <FailureReasons state="SUCCEEDED" snapshot={snapshotOf(runOf(20, 0))} />,
    );
    expect(container.textContent).toContain("Every group was fitted");
  });

  it("counts groups across chunks and reports the final flag", () => {
    // This model chunks its results, so the durable count accumulates and is
    // complete only once a `final: true` chunk has been seen.
    const snapshot = snapshotOf(runOf(28, 4), [
      chunkOf(0, 12),
      chunkOf(1, 12),
      chunkOf(2, 4, true),
    ]);
    const { container } = render(<FailureReasons state="SUCCEEDED" snapshot={snapshot} />);
    expect(container.textContent).toContain("groups written");
    expect(container.textContent).toContain("28");
    expect(container.textContent).toContain("final chunk seen");
  });

  it("calls a run that ended without a final chunk incomplete", () => {
    const snapshot = snapshotOf(runOf(24, 4), [chunkOf(0, 12), chunkOf(1, 12)]);
    const { container } = render(<FailureReasons state="CANCELLED" snapshot={snapshot} />);
    expect(container.textContent).toContain("ended before a final chunk");
  });

  it("reports a missing chunk as a hole in the tally, not in the table", () => {
    const snapshot = snapshotOf(runOf(24, 4), [chunkOf(0, 12), chunkOf(2, 12)]);
    const { container } = render(<FailureReasons state="RUNNING" snapshot={snapshot} />);
    expect(container.textContent).toContain("Chunk 1 never arrived");
    expect(container.textContent).toContain("not from the table");
  });
});

/* ------------------------------------------------------------------ *
 * The view itself
 * ------------------------------------------------------------------ */

describe("the ModelView", () => {
  it("binds to panel_fit and carries two charts and an honesty note", () => {
    expect(view.model).toBe("panel_fit");
    expect(view.charts).toHaveLength(2);
    expect(view.honesty.length).toBeGreaterThan(200);
  });

  it("declares in the honesty note which parts are real", () => {
    // The note is what stops a decorative visual being read as data, and the
    // two claims that matter most here are that positions are a proportion and
    // that the split outlives the animation.
    expect(view.honesty).toContain("proportion");
    expect(view.honesty).toContain("flat");
  });
});

/* ------------------------------------------------------------------ *
 * Against the shared dev fixtures
 * ------------------------------------------------------------------ */

describe("against src/dev/fixtures", () => {
  // The fixtures are the shared harness the gallery renders, and a script for
  // this model landed after these tests were written. Asserting against it as
  // well as against hand-built payloads is worth it for one reason: a fixture
  // that stops matching the model is a change nobody else's test would catch,
  // and every state x fixture combination is a shape this view has to survive.
  const STATES: readonly (UiRunState | null)[] = [
    null,
    "STARTING",
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "INFEASIBLE",
  ];

  it("has a script, so these assertions mean something", () => {
    expect(hasScript("panel_fit")).toBe(true);
  });

  it("keeps the invariant on every fixture the harness can produce", () => {
    // The fixture claims it, the model guarantees it, and this view is built
    // on it. Three places to notice if it ever stops being true.
    for (const fixture of FIXTURE_NAMES) {
      for (const message of makeSnapshot("panel_fit", fixture, "SUCCEEDED").progress) {
        const counts = readCounts(message);
        expect(counts.consistent, `${fixture} at seq ${message.seq}`).not.toBe(false);
      }
    }
  });

  it("renders the signature in every state x fixture without inventing a fault", () => {
    for (const fixture of FIXTURE_NAMES) {
      for (const state of STATES) {
        cleanup();
        const container = renderLattice(state, makeSnapshot("panel_fit", fixture, state));
        expect((container.textContent ?? "").length).toBeGreaterThan(20);
        // A failed RUN is an error and is allowed to be red. A failed GROUP is
        // not, and the distinction is the whole point — so the check is that
        // red appears in exactly one state and nowhere else.
        expect(
          /(^|[\s"])[a-z-]*-bad\b/.test(container.innerHTML),
          `${fixture}/${state}`,
        ).toBe(state === "FAILED");
      }
    }
  });

  it("renders both charts on the chunked fixture, which is the one that streams", () => {
    const snapshot = makeSnapshot("panel_fit", "chunked", "SUCCEEDED");
    const quality = render(<FitQualityChart state="SUCCEEDED" snapshot={snapshot} />).container;
    expect((quality.textContent ?? "").length).toBeGreaterThan(20);
    cleanup();
    const reasons = render(<FailureReasons state="SUCCEEDED" snapshot={snapshot} />).container;
    expect(reasons.textContent).toContain("groups written");
    // Several chunks, and the durable count is their sum rather than the last
    // one's row_count.
    expect(snapshot.results.length).toBeGreaterThan(1);
    expect(accumulateGroups(snapshot.results).rowsWritten).toBe(
      snapshot.results.reduce((sum, chunk) => sum + chunk.row_count, 0),
    );
  });
});
