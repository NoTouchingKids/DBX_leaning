/**
 * The contract every per-model view implements.
 *
 * Frozen before the per-model pages are built, and for the same reason
 * `shared/` is frozen on the Python side: five or more people (or agents)
 * building against a shape that is still moving produces five variations of
 * it. A model view is a plug, not a page — the generic run page owns layout,
 * the trigger form, the log pane and every piece of chrome. A model view
 * supplies only the parts that could not be generic.
 *
 * ## The principle that decides what goes where
 *
 * **A signature animation is a state machine keyed to the run lifecycle —
 * never a rendering of live numeric values.** Real telemetry has a home: the
 * diagnostics charts. The animation's job is to gesture at *what kind of
 * computation this is*, so someone who has used the platform once recognises
 * which model is running without reading a number.
 *
 * Consequences, applied uniformly:
 *
 * - Positions of cells, dots and segments during starting/running may be
 *   decorative. Pacing and structure should track something real where a
 *   natural hook exists.
 * - A terminal state applies as ONE flat state to the whole visual. No
 *   per-element meaning survives past the end of the run.
 * - Every view carries an `honesty` note saying which parts are real and
 *   which are decorative. It is not garnish — it is the thing that stops a
 *   decorative visual being read as data. A view without one is incomplete.
 */

import type { ComponentType } from "react";

import type { UiRunState } from "@/lib/envelope";
import type { RunSnapshot } from "@/transport/runStore";

export interface ModelViewProps {
  /**
   * The lifecycle state to render, INCLUDING the client-only `STARTING`.
   * Null means nothing is known yet (no run selected).
   *
   * `STARTING` is not a `RunStatus`; the server has never heard of it. It
   * exists so there is a spin-up frame between the 202 and the first real
   * message, which on a cold Databricks job can be tens of seconds.
   */
  state: UiRunState | null;
  /** Everything the live stream holds — already split by message type. */
  snapshot: RunSnapshot;
}

export interface ChartSpec {
  /** Stable key. Used for React keys and, later, for remembering which
   *  charts a user collapsed. */
  id: string;
  title: string;
  /** One-line answer to "what am I looking at". Rendered as a subtitle. */
  caption?: string;
  Chart: ComponentType<ModelViewProps>;
}

export interface ModelView {
  /** Must equal the `name` of the matching entry in `MODEL_SPECS`. */
  model: string;
  /**
   * The signature animation. Fills the width it is given and sets its own
   * height; it must not assume a viewport size or a fixed pixel width.
   */
  Signature: ComponentType<ModelViewProps>;
  /**
   * Zero to two diagnostics cards, laid out side by side above ~640px by the
   * page. Two is the maximum on purpose: a third is a dashboard, and the
   * durable results table is the better surface for that.
   */
  charts: readonly ChartSpec[];
  /**
   * Required. Plain prose stating which parts of the signature are real and
   * which are decorative. The page renders it near the animation; the
   * animation must not render it itself, so it cannot be styled into
   * invisibility.
   */
  honesty: string;
}

/**
 * Whether the run is over, from the state the page derived.
 *
 * A view should freeze in one flat terminal frame rather than continuing to
 * animate — a still animation is how a finished run reads as finished from
 * across a room.
 */
export function isSettled(state: UiRunState | null): boolean {
  return (
    state === "SUCCEEDED" ||
    state === "FAILED" ||
    state === "CANCELLED" ||
    state === "INFEASIBLE"
  );
}

/** Whether the run is doing anything — the states an animation should move in. */
export function isAnimating(state: UiRunState | null): boolean {
  return state === "STARTING" || state === "QUEUED" || state === "RUNNING";
}

/**
 * Typed access to a progress message's model-specific extras.
 *
 * `payload` is `Record<string, unknown>` on the wire because the server
 * neither knows nor validates its shape. Each model's payload interface lives
 * in `@/lib/models`, hand-derived from that model's own `emit("progress", ...)`
 * calls, and every field is therefore a claim that can go stale. Reading
 * through this function keeps the cast in one place and keeps the result
 * `Partial`: a field that has not been emitted yet — bayesian_ab's
 * `prob_b_beats_a` before stage 2, neural_net's `best_val_accuracy` during
 * the first epoch — is genuinely absent, not null, and code that assumes
 * otherwise renders `undefined`.
 */
export function payloadOf<T>(
  message: { payload: Record<string, unknown> } | null | undefined,
): Partial<T> {
  return (message?.payload ?? {}) as Partial<T>;
}
