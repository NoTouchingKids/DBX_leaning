import { describe, expect, it } from "vitest";

import { computeElapsedSeconds } from "./elapsed";

const T0 = 1_700_000_000_000;

describe("computeElapsedSeconds", () => {
  it("counts from started_ts before any progress message arrives", () => {
    expect(computeElapsedSeconds({ startedTs: T0 }, T0 + 12_000)).toBe(12);
  });

  it("returns null when nothing is known, rather than claiming zero", () => {
    expect(computeElapsedSeconds({}, T0)).toBeNull();
    expect(computeElapsedSeconds({ startedTs: 0 }, T0)).toBeNull();
  });

  it("anchors to the job's own elapsed_seconds plus wall-clock delta", () => {
    const anchor = { elapsedSeconds: 100, ts: T0 };
    expect(computeElapsedSeconds({ startedTs: T0 - 500_000, anchor }, T0 + 3_000)).toBe(103);
  });

  it("prefers the anchor over started_ts, so app downtime is not counted", () => {
    // The run started an hour ago but the app only attached a minute in: the
    // job's own clock is the authority, not the browser's arithmetic on
    // started_ts.
    const anchor = { elapsedSeconds: 65, ts: T0 };
    expect(computeElapsedSeconds({ startedTs: T0 - 3_600_000, anchor }, T0)).toBe(65);
  });

  it("re-anchoring corrects drift rather than accumulating it", () => {
    const first = { elapsedSeconds: 10, ts: T0 };
    // Browser clock is 30s ahead of the job's; the first reading is inflated.
    expect(computeElapsedSeconds({ anchor: first }, T0 + 30_000)).toBe(40);
    // The next progress message re-anchors and the error is gone, not doubled.
    const second = { elapsedSeconds: 20, ts: T0 + 10_000 };
    expect(computeElapsedSeconds({ anchor: second }, T0 + 10_000)).toBe(20);
  });

  it("freezes at the terminal message and ignores `now` after it", () => {
    const anchor = { elapsedSeconds: 200, ts: T0 };
    const input = { anchor, frozenAt: T0 + 5_000 };
    expect(computeElapsedSeconds(input, T0 + 5_000)).toBe(205);
    // An hour later, reading a finished run's page: the same number.
    expect(computeElapsedSeconds(input, T0 + 3_600_000)).toBe(205);
  });

  it("never runs backwards when a terminal status overtakes its progress message", () => {
    const anchor = { elapsedSeconds: 200, ts: T0 };
    expect(computeElapsedSeconds({ anchor, frozenAt: T0 - 2_000 }, T0)).toBe(200);
  });

  it("freezes on started_ts when a run ended without ever reporting progress", () => {
    expect(computeElapsedSeconds({ startedTs: T0, frozenAt: T0 + 9_000 }, T0 + 99_000)).toBe(9);
  });
});
