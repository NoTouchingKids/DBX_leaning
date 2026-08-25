import { describe, expect, it } from "vitest";

import { betaMean, betaPdf, betaSd, densityRows, densityWindow, logGamma } from "./beta";

describe("logGamma", () => {
  it("matches the factorials", () => {
    expect(Math.exp(logGamma(1))).toBeCloseTo(1, 9);
    expect(Math.exp(logGamma(5))).toBeCloseTo(24, 6);
    expect(Math.exp(logGamma(0.5))).toBeCloseTo(Math.sqrt(Math.PI), 9);
  });

  it("stays finite where the gamma function itself overflows", () => {
    // Beta(1200, 800) is an ordinary posterior for 2,000 trials, and
    // Γ(1200) is Infinity in double precision. Working in logs is the only
    // reason this model's real posteriors can be drawn at all.
    expect(Number.isFinite(logGamma(1200))).toBe(true);
    expect(Number.isFinite(logGamma(50_000))).toBe(true);
  });
});

describe("betaPdf", () => {
  it("is flat at 1 for the uniform prior", () => {
    expect(betaPdf(0.1, 1, 1)).toBeCloseTo(1, 9);
    expect(betaPdf(0.9, 1, 1)).toBeCloseTo(1, 9);
  });

  it("peaks at the mean of a symmetric posterior", () => {
    expect(betaPdf(0.5, 5, 5)).toBeGreaterThan(betaPdf(0.4, 5, 5));
    expect(betaPdf(0.5, 5, 5)).toBeGreaterThan(betaPdf(0.6, 5, 5));
  });

  it("integrates to one", () => {
    let total = 0;
    const step = 1 / 20_000;
    for (let x = step / 2; x < 1; x += step) total += betaPdf(x, 7, 3) * step;
    expect(total).toBeCloseTo(1, 3);
  });

  it("is finite for a posterior sharp enough to overflow naively", () => {
    const value = betaPdf(0.6, 1200, 800);
    expect(Number.isFinite(value)).toBe(true);
    expect(value).toBeGreaterThan(0);
  });

  it("returns zero outside the open unit interval", () => {
    // A display convention, not mathematics: below alpha 1 the density really
    // does diverge at the boundary, and plotting Infinity is one spike and no
    // chart.
    expect(betaPdf(0, 0.5, 0.5)).toBe(0);
    expect(betaPdf(1, 0.5, 0.5)).toBe(0);
    expect(betaPdf(0.5, 0, 1)).toBe(0);
  });
});

describe("betaMean / betaSd", () => {
  it("agree with the closed forms", () => {
    expect(betaMean(3, 1)).toBeCloseTo(0.75, 12);
    expect(betaSd(1, 1)).toBeCloseTo(Math.sqrt(1 / 12), 12);
  });
});

describe("densityWindow", () => {
  it("narrows onto sharp posteriors instead of showing the whole unit interval", () => {
    const [low, high] = densityWindow([{ alpha: 1200, beta: 800 }]);
    expect(high - low).toBeLessThan(0.1);
    expect(low).toBeLessThan(0.6);
    expect(high).toBeGreaterThan(0.6);
  });

  it("covers both arms when they are far apart", () => {
    const [low, high] = densityWindow([
      { alpha: 90, beta: 10 },
      { alpha: 10, beta: 90 },
    ]);
    expect(low).toBeLessThan(0.1);
    expect(high).toBeGreaterThan(0.9);
  });

  it("falls back to the unit interval when nothing is fitted", () => {
    expect(densityWindow([])).toEqual([0, 1]);
    expect(densityWindow([{ alpha: 0, beta: 0 }])).toEqual([0, 1]);
  });

  it("never leaves [0,1]", () => {
    const [low, high] = densityWindow([{ alpha: 1.2, beta: 1.1 }]);
    expect(low).toBeGreaterThanOrEqual(0);
    expect(high).toBeLessThanOrEqual(1);
  });
});

describe("densityRows", () => {
  it("evaluates every series on one shared grid", () => {
    const rows = densityRows(
      [
        { key: "a", alpha: 3, beta: 5 },
        { key: "b", alpha: 5, beta: 3 },
      ],
      [0, 1],
      21,
    );
    expect(rows).toHaveLength(21);
    expect(rows[0]?.x).toBe(0);
    expect(rows.at(-1)?.x).toBeCloseTo(1, 12);
    expect(rows.every((row) => typeof row.a === "number" && typeof row.b === "number")).toBe(true);
  });
});
