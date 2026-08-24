/**
 * The elapsed clock.
 *
 * Harder than it looks, because there are three different clocks in play and
 * only one of them is trustworthy:
 *
 *  - The job's own `elapsed_seconds`, on every `progress` message. This is
 *    the authority: it is measured inside the run, so it is immune to clock
 *    skew between the Databricks worker and the browser, and immune to the
 *    app being down for part of the run.
 *  - The message's `ts` (epoch ms, job-side clock). Used only as the anchor
 *    point that `elapsed_seconds` was true at.
 *  - The browser's wall clock, which only ever supplies the *delta* since
 *    that anchor.
 *
 * So: elapsed = anchor.elapsedSeconds + (now - anchor.ts)/1000, re-anchored on
 * every progress message. Any skew between the two machines cancels out of
 * the subtraction as long as both terms come from the same pair of clocks,
 * and it is corrected outright on the next progress message.
 *
 * Before the first progress message there is no anchor, so it counts from
 * `started_ts` — which does cross clocks, and can therefore be slightly wrong
 * for the first few seconds. That is the honest best available.
 *
 * On a terminal run the clock freezes: `frozenAt` is the terminal message's
 * `ts`, so the number stops at the run's real duration rather than drifting
 * upward forever while someone reads a finished page.
 */

export interface ElapsedAnchor {
  /** `elapsed_seconds` from the message. */
  elapsedSeconds: number;
  /** That message's `ts`, epoch ms. */
  ts: number;
}

export interface ElapsedInput {
  /** `run.started_ts`, epoch ms. Fallback when no progress has arrived. */
  startedTs?: number | null;
  /** Latest progress message, or null. */
  anchor?: ElapsedAnchor | null;
  /** Epoch ms of the terminal message. Freezes the clock there. */
  frozenAt?: number | null;
}

/**
 * Pure so it can be tested against a fixed `now` instead of a real timer.
 * Returns null when nothing is known — a run that has not started has no
 * elapsed time, and rendering 00:00 for it would be a claim, not a blank.
 */
export function computeElapsedSeconds(input: ElapsedInput, now: number): number | null {
  const { startedTs, anchor, frozenAt } = input;
  const at = frozenAt ?? now;

  if (anchor) {
    // Clamp: a frozen point before the last anchor (a terminal status that
    // overtook the progress message it followed) must not read negative.
    return anchor.elapsedSeconds + Math.max(0, (at - anchor.ts) / 1000);
  }
  if (startedTs) return Math.max(0, (at - startedTs) / 1000);
  return null;
}
