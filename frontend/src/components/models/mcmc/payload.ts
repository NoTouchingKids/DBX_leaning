/**
 * MCMC's progress payload, and the pure derivations both charts and the
 * signature animation read it through.
 *
 * Two things here are deliberate:
 *
 * 1. `McmcPayload` extends the interface in `@/lib/models` rather than
 *    replacing it. `models/mcmc/model.py` gained `chain_positions` and
 *    `chain_positions_truncated` after `models.ts` was derived, and that file
 *    is owned by another track. Extending locally keeps the addition visible
 *    as an addition — when `models.ts` catches up, this interface collapses to
 *    an alias and nothing else changes.
 *
 * 2. Nothing in here trusts the payload's declared types at runtime. A payload
 *    interface is a hand-written claim about a `Record<string, unknown>` that
 *    crossed a network; `payloadOf` gives it a shape, not a guarantee. Every
 *    reader below narrows before it uses a value, because a stale claim should
 *    render an empty state rather than throw inside a chart.
 */

import { payloadOf } from "@/components/models/contract";
import type { ProgressMessage } from "@/lib/envelope";
import type { McmcProgressPayload } from "@/lib/models";

export interface McmcPayload extends McmcProgressPayload {
  /** Current position of each chain, one inner list per chain, in the order
   *  of `parameters`. Bounded server-side at `MAX_TRACE_CHAINS` (32) so a
   *  high-walker configuration cannot make this the reason a live message
   *  gets dropped. */
  chain_positions: number[][];
  /** True when the run has more chains than `chain_positions` describes.
   *  Say so rather than showing a partial ensemble as the whole thing. */
  chain_positions_truncated: boolean;
}

export function mcmcPayload(
  message: ProgressMessage | null | undefined,
): Partial<McmcPayload> {
  return payloadOf<McmcPayload>(message);
}

/* ------------------------------------------------------------------ *
 * Runtime narrowing
 * ------------------------------------------------------------------ */

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function finiteList(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  const out: number[] = [];
  for (const item of value) {
    const n = finite(item);
    if (n !== null) out.push(n);
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * r-hat -> spread, the one real quantity the signature animation uses
 * ------------------------------------------------------------------ */

/** r-hat at or above this reads as "these chains are not the same
 *  distribution yet", and the animation is at its widest. 1.5 rather than the
 *  conventional 1.01 reporting threshold because this is a visual range, not
 *  a convergence test: the interesting motion has to be visible across the
 *  whole of a run, and a run starts far above 1.01. */
export const RHAT_WIDE = 1.5;

/** Fractions of the canvas half-extent. The floor is not zero on purpose —
 *  converged chains still move; they move *together*. A cloud that collapses
 *  to a point would say "finished", which is the terminal frame's job. */
export const SPREAD_MIN = 0.14;
export const SPREAD_MAX = 0.92;

/**
 * Map `max_rhat` onto the radius the walkers scatter within.
 *
 * Square-rooted, not linear. Linear in `rhat - 1` spends nearly all of its
 * range on values a sampler passes through in its first few hundred draws and
 * leaves the 1.0-1.1 band — where the run actually spends its time and where
 * a reader wants to see something happening — as a few indistinguishable
 * pixels. Monotonic either way, which is the property that has to hold.
 *
 * A null r-hat means "not computable yet" (fewer than four post-burn-in
 * draws) or "non-finite" — the server sanitises NaN to null. Both are the
 * least-converged thing the view knows, so both go to the widest spread
 * rather than to a neutral middle that would imply information.
 */
export function spreadForRhat(rhat: number | null | undefined): number {
  const value = finite(rhat);
  if (value === null) return SPREAD_MAX;
  const excess = Math.max(0, value - 1);
  const t = Math.min(1, Math.sqrt(excess / (RHAT_WIDE - 1)));
  return SPREAD_MIN + t * (SPREAD_MAX - SPREAD_MIN);
}

/* ------------------------------------------------------------------ *
 * Chain health
 * ------------------------------------------------------------------ */

export type ChainTone = "good" | "warn" | "bad";

/** emcee's stuck-chain test, verbatim from the model: `acceptance == 0`. */
export const STUCK_ACCEPTANCE = 0;
/** Below this a walker is accepting so rarely that it is barely exploring.
 *  A soft warning, not the model's own diagnostic — `stuck_chains` is. */
export const COOL_ACCEPTANCE = 0.1;

/** More bars than this in one card is a smear, not a diagnostic. The default
 *  is 8 chains, so this only engages on a deliberately large ensemble. */
export const MAX_HEALTH_BARS = 64;

export interface ChainBar {
  chain: number;
  /** X-axis label. Short — there can be dozens of these. */
  label: string;
  acceptance: number;
  tone: ChainTone;
}

export interface ChainHealth {
  bars: ChainBar[];
  chainsTotal: number;
  /** Chains real but not drawn, because of `MAX_HEALTH_BARS`. */
  hidden: number;
  /** The model's own count, over ALL chains — not a count of the bars, which
   *  may be a truncated view. Null when no progress has arrived. */
  stuck: number | null;
  mean: number | null;
  min: number | null;
  drawsDone: number | null;
  drawsTotal: number | null;
}

export function toneForAcceptance(acceptance: number): ChainTone {
  if (acceptance <= STUCK_ACCEPTANCE) return "bad";
  if (acceptance < COOL_ACCEPTANCE) return "warn";
  return "good";
}

export function deriveChainHealth(payload: Partial<McmcPayload>): ChainHealth {
  const acceptance = finiteList(payload.per_chain_acceptance);
  const shown = acceptance.slice(0, MAX_HEALTH_BARS);

  return {
    bars: shown.map((value, index) => ({
      chain: index,
      label: String(index),
      acceptance: value,
      tone: toneForAcceptance(value),
    })),
    // `chains` is the configured count; the acceptance list is what the
    // sampler actually has. They agree in practice, and where they do not the
    // list is the one that came from the sampler.
    chainsTotal: acceptance.length || (finite(payload.chains) ?? 0),
    hidden: Math.max(0, acceptance.length - shown.length),
    stuck:
      finite(payload.stuck_chains) ??
      (acceptance.length > 0
        ? acceptance.filter((a) => a <= STUCK_ACCEPTANCE).length
        : null),
    mean: finite(payload.mean_acceptance),
    min: finite(payload.min_acceptance),
    drawsDone: finite(payload.draws_done),
    drawsTotal: finite(payload.draws_total),
  };
}

/* ------------------------------------------------------------------ *
 * Trace
 * ------------------------------------------------------------------ */

/** X-points handed to Recharts. A long mcmc run is the platform's streaming
 *  stress test — `MAX_PROGRESS` is 10,000 — and 10,000 x-points times a dozen
 *  chains is roughly 120,000 SVG path segments per frame. This is a display
 *  budget, not a data budget: the store keeps everything. */
export const MAX_TRACE_POINTS = 240;

/** Lines drawn. The wire cap is 32 chains; beyond about a dozen overlapping
 *  traces the chart stops answering "are these mixing?" and starts answering
 *  nothing at all. */
export const MAX_TRACE_LINES = 12;

export interface TraceSeries {
  /** Parameter names, in payload order. Empty when nothing has arrived. */
  parameters: string[];
  /** One row per sampled progress message: `{ x, c0, c1, ... }`. */
  rows: Array<Record<string, number>>;
  /** Keys present in `rows`, one per drawn chain. */
  chainKeys: string[];
  /** Chains the payload carried but this chart is not drawing. */
  hiddenChains: number;
  /** The model truncated the ensemble before it reached us. */
  truncatedUpstream: boolean;
  /** Chains whose latest acceptance is zero — drawn as alarm, because a flat
   *  trace and a stuck chain are the same fact seen twice. */
  stuckChains: ReadonlySet<number>;
  /** Progress messages that existed, before display downsampling. */
  sourcePoints: number;
}

/**
 * Every `stride`-th item, first and last always kept.
 *
 * Nearest-neighbour rather than LTTB, which is what the server uses for
 * result previews. LTTB picks the indices that best preserve *one* series'
 * silhouette; here a dozen chains share one x-axis and are read against each
 * other, so every series has to be sampled at the same x or the comparison
 * the chart exists for stops being valid.
 */
export function downsample<T>(items: readonly T[], max: number): T[] {
  if (max <= 0) return [];
  if (items.length <= max) return [...items];
  const stride = (items.length - 1) / (max - 1);
  const out: T[] = [];
  for (let i = 0; i < max; i += 1) {
    const item = items[Math.round(i * stride)];
    if (item !== undefined) out.push(item);
  }
  return out;
}

export function chainKey(index: number): string {
  return `c${index}`;
}

/**
 * Accumulate the live trace for one parameter.
 *
 * The model sends a *snapshot* — where each walker is right now — not a
 * history, which is what keeps the payload at ~300 bytes. The history is the
 * client's, assembled here from the progress messages it happens to hold. A
 * tab that joined late, or a run with a `seq` gap, therefore has a shorter
 * trace than the run really had; that is the honest rendering of what this
 * client saw, and the gap markers on the page say the rest.
 */
export function buildTrace(
  progress: readonly ProgressMessage[],
  parameterIndex: number,
  maxPoints: number = MAX_TRACE_POINTS,
): TraceSeries {
  const latest = mcmcPayload(progress.at(-1));
  const parameters = Array.isArray(latest.parameters)
    ? latest.parameters.filter((p): p is string => typeof p === "string")
    : [];

  const withPositions = progress.filter(
    (m) => Array.isArray(mcmcPayload(m).chain_positions),
  );
  const sampled = downsample(withPositions, maxPoints);

  let widest = 0;
  const rows: Array<Record<string, number>> = [];
  for (const message of sampled) {
    const payload = mcmcPayload(message);
    const positions = payload.chain_positions;
    if (!Array.isArray(positions)) continue;

    // `draws_done` is the meaningful x for a sampler. `seq` is the fallback,
    // not a default: it is monotonic but its spacing is arbitrary, so a chart
    // drawn against it is readable but not proportional.
    const row: Record<string, number> = {
      x: finite(payload.draws_done) ?? message.seq,
    };
    const drawn = Math.min(positions.length, MAX_TRACE_LINES);
    for (let c = 0; c < drawn; c += 1) {
      const value = finite(positions[c]?.[parameterIndex]);
      if (value !== null) row[chainKey(c)] = value;
    }
    widest = Math.max(widest, positions.length);
    rows.push(row);
  }

  const drawnChains = Math.min(widest, MAX_TRACE_LINES);
  const acceptance = finiteList(latest.per_chain_acceptance);
  const stuck = new Set<number>();
  for (let c = 0; c < drawnChains; c += 1) {
    const value = acceptance[c];
    if (value !== undefined && value <= STUCK_ACCEPTANCE) stuck.add(c);
  }

  return {
    parameters,
    rows,
    chainKeys: Array.from({ length: drawnChains }, (_, c) => chainKey(c)),
    hiddenChains: Math.max(0, widest - drawnChains),
    truncatedUpstream: latest.chain_positions_truncated === true,
    stuckChains: stuck,
    sourcePoints: withPositions.length,
  };
}
