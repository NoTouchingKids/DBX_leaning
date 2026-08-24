import { describe, expect, it } from "vitest";

import type { ProgressMessage } from "@/lib/envelope";

import {
  buildTrace,
  deriveChainHealth,
  downsample,
  MAX_HEALTH_BARS,
  mcmcPayload,
  RHAT_WIDE,
  SPREAD_MAX,
  SPREAD_MIN,
  spreadForRhat,
} from "./payload";

function progress(
  seq: number,
  payload: Record<string, unknown>,
  metric: number | null = null,
): ProgressMessage {
  return {
    type: "progress",
    run_id: "run-1",
    seq,
    ts: 1_700_000_000_000 + seq,
    elapsed_seconds: seq,
    percent_complete: null,
    primary_metric: metric,
    primary_metric_label: "max_rhat",
    payload,
  };
}

describe("spreadForRhat", () => {
  it("is widest when r-hat is unknown", () => {
    // Null is a real value here — non-finite r-hat is sanitised to null
    // server-side, and fewer than four post-burn-in draws produces no r-hat
    // at all. Both mean "least converged thing we know", not "no information".
    expect(spreadForRhat(null)).toBe(SPREAD_MAX);
    expect(spreadForRhat(undefined)).toBe(SPREAD_MAX);
    expect(spreadForRhat(Number.NaN)).toBe(SPREAD_MAX);
  });

  it("is tightest at perfect convergence and clamps below it", () => {
    expect(spreadForRhat(1)).toBe(SPREAD_MIN);
    // r-hat below 1 happens; it is not more converged than converged.
    expect(spreadForRhat(0.98)).toBe(SPREAD_MIN);
  });

  it("clamps at the wide end rather than running off the canvas", () => {
    expect(spreadForRhat(RHAT_WIDE)).toBeCloseTo(SPREAD_MAX, 10);
    expect(spreadForRhat(40)).toBe(SPREAD_MAX);
  });

  it("shrinks monotonically as r-hat falls toward 1", () => {
    const descending = [3, 1.5, 1.3, 1.1, 1.05, 1.01, 1.0];
    const spreads = descending.map(spreadForRhat);
    for (let i = 1; i < spreads.length; i += 1) {
      expect(spreads[i]).toBeLessThanOrEqual(spreads[i - 1] as number);
    }
    expect(spreads.at(-1)).toBeLessThan(spreads[0] as number);
  });

  it("spends real range on the 1.0-1.1 band a run actually lives in", () => {
    // The reason for the square root. A linear map would put 1.0 and 1.1
    // within 3% of the range of each other and the animation would look
    // frozen for the whole useful part of a run.
    const band = spreadForRhat(1.1) - spreadForRhat(1.0);
    expect(band / (SPREAD_MAX - SPREAD_MIN)).toBeGreaterThan(0.25);
  });
});

describe("deriveChainHealth", () => {
  it("returns no bars when nothing has arrived", () => {
    const health = deriveChainHealth(mcmcPayload(null));
    expect(health.bars).toEqual([]);
    expect(health.stuck).toBeNull();
    expect(health.mean).toBeNull();
  });

  it("tones a zero-acceptance chain as the alarm and a cool one as a warning", () => {
    const health = deriveChainHealth(
      mcmcPayload(progress(1, { per_chain_acceptance: [0.42, 0, 0.05, 0.38] })),
    );
    expect(health.bars.map((b) => b.tone)).toEqual(["good", "bad", "warn", "good"]);
  });

  it("prefers the model's own stuck_chains over counting the bars", () => {
    // The bars can be a truncated view of a large ensemble; stuck_chains is
    // computed over all of it. Trusting the count on screen would understate
    // the problem exactly when there is most of it.
    const health = deriveChainHealth(
      mcmcPayload(progress(1, { per_chain_acceptance: [0.3, 0.3], stuck_chains: 7 })),
    );
    expect(health.stuck).toBe(7);
  });

  it("falls back to counting only when stuck_chains is absent", () => {
    const health = deriveChainHealth(
      mcmcPayload(progress(1, { per_chain_acceptance: [0, 0, 0.3] })),
    );
    expect(health.stuck).toBe(2);
  });

  it("caps the bars and reports how many it is not drawing", () => {
    const acceptance = Array.from({ length: MAX_HEALTH_BARS + 12 }, () => 0.3);
    const health = deriveChainHealth(mcmcPayload(progress(1, { per_chain_acceptance: acceptance })));
    expect(health.bars).toHaveLength(MAX_HEALTH_BARS);
    expect(health.hidden).toBe(12);
    expect(health.chainsTotal).toBe(MAX_HEALTH_BARS + 12);
  });

  it("survives a payload whose declared types are wrong", () => {
    // The interface is a hand-written claim about a Record<string, unknown>.
    // A stale claim must render an empty chart, not throw inside one.
    const health = deriveChainHealth(
      mcmcPayload(progress(1, { per_chain_acceptance: "0.4", mean_acceptance: null })),
    );
    expect(health.bars).toEqual([]);
    expect(health.mean).toBeNull();
  });
});

describe("downsample", () => {
  it("returns everything under the budget", () => {
    expect(downsample([1, 2, 3], 10)).toEqual([1, 2, 3]);
  });

  it("keeps the first and last point", () => {
    const items = Array.from({ length: 5000 }, (_, i) => i);
    const out = downsample(items, 240);
    expect(out).toHaveLength(240);
    expect(out[0]).toBe(0);
    expect(out.at(-1)).toBe(4999);
  });

  it("stays ordered", () => {
    const out = downsample(Array.from({ length: 977 }, (_, i) => i), 60);
    for (let i = 1; i < out.length; i += 1) {
      expect(out[i]).toBeGreaterThan(out[i - 1] as number);
    }
  });
});

describe("buildTrace", () => {
  const positions = (offset: number) => [
    [offset, offset + 0.1],
    [offset + 1, offset + 1.1],
    [offset + 2, offset + 2.1],
  ];

  const stream = (n: number) =>
    Array.from({ length: n }, (_, i) =>
      progress(i, {
        draws_done: (i + 1) * 200,
        draws_total: n * 200,
        parameters: ["mu", "log_sigma"],
        per_chain_acceptance: [0.4, 0, 0.35],
        chain_positions: positions(i),
      }),
    );

  it("is empty, not broken, before any positions arrive", () => {
    const trace = buildTrace([], 0);
    expect(trace.rows).toEqual([]);
    expect(trace.parameters).toEqual([]);
  });

  it("ignores progress messages that carry no positions", () => {
    // A run from before the model gained `chain_positions`, or a payload that
    // dropped it, contributes nothing rather than a row of holes.
    const trace = buildTrace([progress(0, { draws_done: 200 })], 0);
    expect(trace.rows).toEqual([]);
  });

  it("plots against draws_done and picks out the requested parameter", () => {
    const trace = buildTrace(stream(3), 1);
    expect(trace.rows.map((r) => r.x)).toEqual([200, 400, 600]);
    expect(trace.rows[0]?.c0).toBe(0.1);
    expect(trace.rows[0]?.c2).toBe(2.1);
    expect(trace.parameters).toEqual(["mu", "log_sigma"]);
  });

  it("marks the chains the acceptance figures call stuck", () => {
    const trace = buildTrace(stream(2), 0);
    expect([...trace.stuckChains]).toEqual([1]);
  });

  it("downsamples for display without dropping the ends", () => {
    const trace = buildTrace(stream(4000), 0, 240);
    expect(trace.rows).toHaveLength(240);
    expect(trace.sourcePoints).toBe(4000);
    expect(trace.rows[0]?.x).toBe(200);
    expect(trace.rows.at(-1)?.x).toBe(800_000);
  });

  it("reports upstream truncation rather than passing off a partial ensemble", () => {
    const trace = buildTrace(
      [progress(0, { chain_positions: [[1]], chain_positions_truncated: true })],
      0,
    );
    expect(trace.truncatedUpstream).toBe(true);
  });
});
