/**
 * The motion vocabulary every signature animation shares.
 *
 * Frozen before the eleven signatures were reworked, for the reason
 * `contract.ts` and the Python `shared/` are frozen: a set of people — or
 * agents — animating against a moving target produces a set of unrelated
 * animations. Eleven views that each invented their own 400ms-ish duration
 * and their own ease curve is exactly what "looks machine-made" is made of.
 *
 * ## The rule these encode
 *
 * `contract.ts` already says a signature is **a state machine keyed to the
 * run lifecycle, not a rendering of live numbers**. This file is the timing
 * half of that: each lifecycle phase has one characteristic motion, and every
 * model expresses the same phase the same way.
 *
 *   idle       nothing moves. No run, nothing to say.
 *   starting   one slow inhale. A cold Databricks job can take tens of
 *              seconds; this is the frame that says "asked, not yet answered".
 *   running    ambient, continuous, unhurried. It must be watchable for
 *              minutes without becoming irritating, which is the constraint
 *              that kills most loops — anything under ~1.5s reads as urgent
 *              and turns into a strobe on a long solve.
 *   settling   a single decisive gesture as the terminal message lands.
 *   terminal   ONE flat frozen frame. Per `contract.ts`, no per-element
 *              meaning survives the end of a run, so nothing may still move.
 *
 * ## Reduced motion
 *
 * `usePrefersReducedMotion` is the switch, and the rule is unchanged: the
 * TRANSITION goes, never the information. A view that says "converged" by
 * turning green still turns green — instantly. Anything purely ambient
 * (drift, pulse, shimmer, orbit) stops entirely rather than snapping, because
 * a state it carries no information about is a state not worth a frame.
 */

/** Seconds. Named for what they are for, not for how long they are — a
 *  duration picked by feel gets renamed the moment someone reuses it. */
export const DURATION = {
  /** State swaps that must not be perceived as motion at all. */
  instant: 0.12,
  /** Hover, focus, a chip filling in. */
  fast: 0.18,
  /** The default. Anything that moves a real distance. */
  base: 0.32,
  /** Entrances, and the settling gesture. Long enough to be followed. */
  slow: 0.55,
  /** One cycle of an ambient running loop. Deliberately unhurried. */
  ambient: 2.4,
  /** The starting inhale. Matched to how long a cold job actually takes to
   *  say anything, so it does not finish and leave dead air. */
  inhale: 1.6,
} as const;

/**
 * Cubic-bezier control points, as `motion` takes them.
 *
 * `standard` is the only one most things need. The other two exist because
 * entrances and exits are not symmetric: something arriving should decelerate
 * into place, something leaving should accelerate away, and using one curve
 * for both is a tell.
 */
export const EASE = {
  standard: [0.4, 0, 0.2, 1],
  decelerate: [0, 0, 0.2, 1],
  accelerate: [0.4, 0, 1, 1],
  /** For a settling gesture that should overshoot very slightly. */
  emphasis: [0.2, 0, 0, 1.2],
} as const;

/**
 * Springs, for anything a user could think of as physical — a value landing,
 * a bar reaching its height, a node snapping to a position.
 *
 * Under-damped on purpose but only just: a visible bounce on eleven panels at
 * once is a toy, and `damping` below about 18 at this stiffness gets there.
 */
export const SPRING = {
  soft: { type: "spring", stiffness: 170, damping: 26, mass: 1 },
  snappy: { type: "spring", stiffness: 320, damping: 30, mass: 0.8 },
} as const;

/**
 * Seconds between neighbours in a staggered group.
 *
 * The cap matters more than the step. A 24-cell grid at 60ms takes 1.4s to
 * finish, by which time the run has moved on and the animation is describing
 * the past — so a stagger over a large set must compute its step as
 * `min(step, budget / count)` rather than multiplying blindly.
 */
export const STAGGER = { step: 0.04, budget: 0.5 } as const;

/** The step to use for `count` items so the whole group finishes inside
 *  `STAGGER.budget`. */
export function staggerFor(count: number): number {
  if (count <= 1) return 0;
  return Math.min(STAGGER.step, STAGGER.budget / count);
}

/** The five phases every signature switches on. Derived from `UiRunState` by
 *  `phaseOf` so no view re-derives it and they cannot disagree. */
export type MotionPhase = "idle" | "starting" | "running" | "settled";

export function phaseOf(state: string | null): MotionPhase {
  if (state === null || state === "QUEUED") return "idle";
  if (state === "STARTING") return "starting";
  if (state === "RUNNING") return "running";
  return "settled";
}

/** True when the phase should hold one flat frame — nothing may animate. */
export function isFrozen(phase: MotionPhase): boolean {
  return phase === "settled" || phase === "idle";
}
