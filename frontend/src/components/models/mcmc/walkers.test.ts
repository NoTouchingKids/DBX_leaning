import { describe, expect, it } from "vitest";

import { SPREAD_MAX, SPREAD_MIN } from "./payload";
import { phaseFor, walkerPositions } from "./walkers";

const layout = { phase: "walk" as const, count: 8, spread: SPREAD_MAX, tick: 0 };

describe("phaseFor", () => {
  it("hides the ensemble before there is one", () => {
    expect(phaseFor(null)).toBe("hidden");
    expect(phaseFor("QUEUED")).toBe("hidden");
  });

  it("treats every terminal state as one flat settled frame", () => {
    // The contract's rule: no per-element meaning survives the end of a run.
    for (const state of ["SUCCEEDED", "FAILED", "CANCELLED", "INFEASIBLE"] as const) {
      expect(phaseFor(state)).toBe("settled");
    }
  });

  it("moves only while the run is running", () => {
    expect(phaseFor("STARTING")).toBe("scatter");
    expect(phaseFor("RUNNING")).toBe("walk");
  });
});

describe("walkerPositions", () => {
  it("stays inside the canvas at the widest spread", () => {
    for (let tick = 0; tick < 50; tick += 1) {
      for (const point of walkerPositions({ ...layout, tick })) {
        expect(point.x).toBeGreaterThanOrEqual(0);
        expect(point.x).toBeLessThanOrEqual(100);
        expect(point.y).toBeGreaterThanOrEqual(0);
        expect(point.y).toBeLessThanOrEqual(100);
      }
    }
  });

  it("is a pure function of its inputs", () => {
    // Positions are derived on every render rather than stored, so an
    // unrelated re-render must not teleport the ensemble.
    expect(walkerPositions({ ...layout, tick: 4 })).toEqual(
      walkerPositions({ ...layout, tick: 4 }),
    );
  });

  it("draws in as the spread shrinks", () => {
    const spreadOf = (spread: number) => {
      const points = walkerPositions({ ...layout, spread });
      return Math.max(...points.map((p) => Math.hypot(p.x - 50, p.y - 50)));
    };
    expect(spreadOf(SPREAD_MIN)).toBeLessThan(spreadOf(SPREAD_MAX));
  });

  it("keeps the spread under reduced motion, where the tick never advances", () => {
    // The rule: reduced motion disables the transition, never the
    // information. Frozen at tick 0, a converged run must still look
    // converged and an unconverged one must still look unconverged.
    const converged = walkerPositions({ ...layout, spread: SPREAD_MIN, tick: 0 });
    const scattered = walkerPositions({ ...layout, spread: SPREAD_MAX, tick: 0 });
    const radius = (points: ReturnType<typeof walkerPositions>) =>
      Math.max(...points.map((p) => Math.hypot(p.x - 50, p.y - 50)));
    expect(radius(converged)).toBeLessThan(radius(scattered));
  });

  it("moves between ticks while running", () => {
    const a = walkerPositions({ ...layout, tick: 1 });
    const b = walkerPositions({ ...layout, tick: 2 });
    expect(a).not.toEqual(b);
  });

  it("settles onto one ring regardless of the last r-hat seen", () => {
    const settled = { ...layout, phase: "settled" as const };
    expect(walkerPositions({ ...settled, spread: SPREAD_MIN })).toEqual(
      walkerPositions({ ...settled, spread: SPREAD_MAX }),
    );
  });

  it("places queued walkers but does not show them", () => {
    const points = walkerPositions({ ...layout, phase: "hidden" });
    expect(points).toHaveLength(8);
    expect(points.every((p) => !p.visible)).toBe(true);
  });
});

describe("STARTING versus RUNNING", () => {
  it("holds the starting cloud still while the running one moves", () => {
    // Six distinguishable states is the design's requirement, and a wide
    // static cloud and a wide moving one are the same picture if the tick
    // reaches both.
    const scatter = { ...layout, phase: "scatter" as const };
    expect(walkerPositions({ ...scatter, tick: 0 })).toEqual(
      walkerPositions({ ...scatter, tick: 9 }),
    );
    expect(walkerPositions({ ...layout, tick: 0 })).not.toEqual(
      walkerPositions({ ...layout, tick: 9 }),
    );
  });
});
