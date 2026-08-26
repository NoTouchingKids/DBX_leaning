/**
 * Walker placement — pure, so the one part of the animation that carries
 * information can be tested without a DOM.
 *
 * Everything here works in percentages of the canvas, so the signature fills
 * whatever width the page gives it, as the contract requires.
 */

import type { UiRunState } from "@/lib/envelope";
import { isSettled } from "@/components/models/contract";

export type WalkerPhase = "hidden" | "scatter" | "walk" | "settled";

export interface WalkerPoint {
  x: number;
  y: number;
  visible: boolean;
}

/** Half the canvas, minus room for a dot. Positions are `50 ± EXTENT_*`. */
const EXTENT_X = 45;
/** The canvas is much wider than it is tall, so an isotropic cloud has to be
 *  squashed vertically or it reads as a vertical band. Cosmetic. */
const EXTENT_Y = 45 * 0.55;

const TAU = Math.PI * 2;

/** The radius the ensemble settles onto in a terminal frame. Fixed: a
 *  finished run's spread is not news, its outcome is. */
const SETTLED_RADIUS = 0.18;

export function phaseFor(state: UiRunState | null): WalkerPhase {
  if (state === null || state === "QUEUED") return "hidden";
  if (state === "STARTING") return "scatter";
  if (isSettled(state)) return "settled";
  return "walk";
}

/**
 * Integer hash to [0,1). Deterministic in both arguments, which is what lets
 * positions be *derived* on every render rather than stored in state: the
 * same (walker, tick) always lands in the same place, so a re-render for an
 * unrelated reason does not teleport the ensemble.
 */
function hash01(a: number, b: number): number {
  let h = Math.imul(a ^ 0x9e3779b9, 0x85ebca6b) ^ Math.imul(b + 0x165667b1, 0xc2b2ae35);
  h = Math.imul(h ^ (h >>> 13), 0x27d4eb2f);
  h ^= h >>> 16;
  return (h >>> 0) / 0x1_0000_0000;
}

export interface WalkerLayout {
  phase: WalkerPhase;
  count: number;
  /** 0..1, from `spreadForRhat`. The only real quantity in the picture. */
  spread: number;
  /** Advances the random walk.
   *
   *  `McmcSignature` pins this to 0 in EVERY phase, not just under reduced
   *  motion. Re-seeding positions per tick teleported walkers across the disc
   *  between frames, which is noise rather than exploration, so the running
   *  motion is now a per-walker drift the component adds on top and this
   *  function is left to place the cloud. Holding it at 0 leaves the spread —
   *  the one real quantity — intact, which is what reduced motion needed
   *  anyway. Still honoured here, and still tested, so a caller that wants a
   *  stepped walk gets one. */
  tick: number;
}

export function walkerPositions({
  phase,
  count,
  spread,
  tick,
}: WalkerLayout): WalkerPoint[] {
  const n = Math.max(0, Math.floor(count));
  return Array.from({ length: n }, (_, i) => {
    if (phase === "settled") {
      const angle = (i / Math.max(1, n)) * TAU;
      return {
        x: 50 + Math.cos(angle) * SETTLED_RADIUS * EXTENT_X,
        y: 50 + Math.sin(angle) * SETTLED_RADIUS * EXTENT_Y,
        visible: true,
      };
    }

    // STARTING is a wide, STILL cloud: the chains exist but have not taken a
    // step. Freezing the tick keeps that true for any caller that does advance
    // it — but the signature no longer does, so what separates STARTING from
    // RUNNING on screen is the drift the component layers over these points,
    // not this line.
    const step = phase === "walk" ? tick : 0;
    const angle = hash01(i, step * 2 + 1) * TAU;
    // sqrt of a uniform draw, so the cloud is uniform over the disc rather
    // than bunched at the centre — a bunched cloud makes the spread read
    // smaller than it is.
    const radius = Math.sqrt(hash01(i, step * 2 + 2)) * spread;
    return {
      x: 50 + Math.cos(angle) * radius * EXTENT_X,
      y: 50 + Math.sin(angle) * radius * EXTENT_Y,
      // Queued has no chains yet. They are placed, not absent, so that the
      // first frame of STARTING is a fade rather than a pop.
      visible: phase !== "hidden",
    };
  });
}
