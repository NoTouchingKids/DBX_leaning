/**
 * Everything this model's view knows, read from wherever it actually is.
 *
 * This file exists because of one fact about `models/bayesian_ab`: it is
 * closed-form and the whole run takes milliseconds. A client will routinely
 * see a terminal status having observed NONE of the five progress messages —
 * the run was over before the SSE stream delivered anything. So no view here
 * may reach its populated form only by way of a progress event.
 *
 * The rule applied throughout: read the latest progress payload if there is
 * one, fall back to the `result` preview rows if there is not, and say which
 * of the two it was. A backfilled decision and a live one are the same
 * numbers but not the same evidence, and a reader deciding whether to trust a
 * blank panel needs to know which they are looking at.
 *
 * The other fact this file is built around: five payload keys —
 * `prob_b_beats_a`, `expected_loss`, `lift`, `decision`, `conclusive` — are
 * ADDED as their stages complete. They are genuinely absent from earlier
 * messages, not null in them. Everything below tests for absence, and
 * everything below returns `null` for "not known", so a caller never has to
 * tell `undefined` from `null` a second time.
 */

import { isSettled, payloadOf } from "@/components/models/contract";
import type { ResultMessage, UiRunState } from "@/lib/envelope";
import { BAYESIAN_AB_STAGES, type BayesianAbProgressPayload } from "@/lib/models";
import type { RunSnapshot } from "@/transport/runStore";

export const STAGE_COUNT = BAYESIAN_AB_STAGES.length;

/** Where a fact came from. Rendered, not just logged. */
export type Provenance = "progress" | "results" | "terminal" | "none";

/* ------------------------------------------------------------------ *
 * Narrowing helpers. Result preview rows are `Record<string, unknown>`
 * and payloads are only nominally typed, so nothing is read raw.
 * ------------------------------------------------------------------ */

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function bool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** Every preview row across every result chunk, newest chunk first. */
function previewRows(results: readonly ResultMessage[]): Array<Record<string, unknown>> {
  const rows: Array<Record<string, unknown>> = [];
  for (let i = results.length - 1; i >= 0; i -= 1) {
    const preview = results[i]?.preview;
    if (Array.isArray(preview)) rows.push(...preview);
  }
  return rows;
}

/* ------------------------------------------------------------------ *
 * Stages
 * ------------------------------------------------------------------ */

export interface StageProgress {
  /** Stages known to have completed, 0..5. */
  done: number;
  /** 1-based index of the stage a FAILED run did not get through, when that
   *  can be inferred. Null otherwise. */
  failedAt: number | null;
  source: Provenance;
}

/**
 * How far the run got — from the status first, the payload second.
 *
 * The status is the stronger signal and is the one that survives a run that
 * outran its own telemetry: `run()` only returns after all five stages, so a
 * SUCCEEDED status *is* five completed stages, whether or not a single
 * progress message was seen. A CANCELLED run broke out of the loop, so its
 * count is whatever the payload got to and no more. A FAILED run stopped
 * inside the stage after the last one it reported — including stage 1 when it
 * reported nothing, which is the shape of a config that the model's own
 * constructor rejected.
 */
export function deriveStages(
  state: UiRunState | null,
  snapshot: RunSnapshot,
): StageProgress {
  const payload = payloadOf<BayesianAbProgressPayload>(snapshot.latestProgress);
  const fromProgress = num(payload.stage_index) ?? 0;
  const complete = previewRows(snapshot.results).some((row) => row.complete === true);

  if (state === "SUCCEEDED") {
    return {
      done: STAGE_COUNT,
      failedAt: null,
      source: fromProgress > 0 ? "progress" : "terminal",
    };
  }
  if (state === "FAILED" || state === "INFEASIBLE") {
    const done = Math.min(fromProgress, STAGE_COUNT);
    return {
      done,
      failedAt: done < STAGE_COUNT ? done + 1 : null,
      source: done > 0 ? "progress" : "terminal",
    };
  }

  // Cancelled, running, queued, starting, or nothing selected. A cancelled
  // run that nevertheless wrote a complete result set is possible — the
  // cancel landed after the last stage — and the results say so.
  const done = Math.max(fromProgress, complete ? STAGE_COUNT : 0);
  return {
    done: Math.min(done, STAGE_COUNT),
    failedAt: null,
    source: fromProgress > 0 ? "progress" : complete ? "results" : "none",
  };
}

/* ------------------------------------------------------------------ *
 * Arms
 * ------------------------------------------------------------------ */

export interface ArmView {
  /** "A" or "B" on the wire; kept as a string because it is a label here. */
  role: string;
  label: string;
  trials: number | null;
  successes: number | null;
  /** Null before the `posteriors` stage. With the prior, these two are the
   *  whole posterior. */
  posteriorAlpha: number | null;
  posteriorBeta: number | null;
  posteriorMean: number | null;
}

export interface ArmsView {
  arms: ArmView[];
  source: Provenance;
}

function armFromRecord(row: Record<string, unknown>, fallbackRole: string): ArmView {
  return {
    role: str(row.role) ?? fallbackRole,
    label: str(row.label) ?? `arm ${fallbackRole}`,
    trials: num(row.trials),
    successes: num(row.successes),
    posteriorAlpha: num(row.posterior_alpha),
    posteriorBeta: num(row.posterior_beta),
    posteriorMean: num(row.posterior_mean),
  };
}

export function armsFromSnapshot(snapshot: RunSnapshot): ArmsView {
  const payload = payloadOf<BayesianAbProgressPayload>(snapshot.latestProgress);
  if (Array.isArray(payload.arms) && payload.arms.length > 0) {
    return {
      arms: payload.arms.map((arm, index) =>
        armFromRecord(record(arm) ?? {}, index === 0 ? "A" : "B"),
      ),
      source: "progress",
    };
  }

  // No progress: the run finished before the stream caught up. The result
  // rows carry the same arms — `row_type: "arm"`, two of them — and a run
  // that produced no rows at all produced no posteriors either, which is a
  // real answer and not a missing one.
  const rows = previewRows(snapshot.results).filter((row) => row.row_type === "arm");
  if (rows.length === 0) return { arms: [], source: "none" };

  const arms = rows.slice(0, 2).map((row, index) => armFromRecord(row, index === 0 ? "A" : "B"));
  arms.sort((left, right) => left.role.localeCompare(right.role));
  return { arms, source: "results" };
}

/* ------------------------------------------------------------------ *
 * The decision
 * ------------------------------------------------------------------ */

export interface LiftView {
  mean: number | null;
  sd: number | null;
  ciLow: number | null;
  ciHigh: number | null;
}

export interface DecisionView {
  comparison: string | null;
  /** Prose from the model: what "success" means here, threshold included. The
   *  only place the decision table's units are stated, so it gets shown. */
  outcome: string | null;
  prior: { alpha: number | null; beta: number | null } | null;
  credibleMass: number | null;
  /** `primary_metric`. Absent until the `comparison` stage; a probability, so
   *  neither high nor low is automatically the good news. */
  probBBeatsA: number | null;
  expectedLossA: number | null;
  expectedLossB: number | null;
  lift: LiftView | null;
  /** An arm LABEL, or the literal string "inconclusive". Never "A"/"B". */
  decision: string | null;
  conclusive: boolean | null;
  source: Provenance;
}

const EMPTY_DECISION: DecisionView = {
  comparison: null,
  outcome: null,
  prior: null,
  credibleMass: null,
  probBBeatsA: null,
  expectedLossA: null,
  expectedLossB: null,
  lift: null,
  decision: null,
  conclusive: null,
  source: "none",
};

function decisionFromPayload(payload: Partial<BayesianAbProgressPayload>): DecisionView {
  const loss = record(payload.expected_loss);
  const lift = record(payload.lift);
  const prior = record(payload.prior);

  return {
    comparison: str(payload.comparison),
    outcome: str(payload.outcome),
    prior: prior ? { alpha: num(prior.alpha), beta: num(prior.beta) } : null,
    credibleMass: num(payload.credible_mass),
    probBBeatsA: num(payload.prob_b_beats_a),
    expectedLossA: loss ? num(loss.A) : null,
    expectedLossB: loss ? num(loss.B) : null,
    lift: lift
      ? {
          mean: num(lift.mean),
          sd: num(lift.sd),
          ciLow: num(lift.ci_low),
          ciHigh: num(lift.ci_high),
        }
      : null,
    decision: str(payload.decision),
    conclusive: bool(payload.conclusive),
    source: "progress",
  };
}

/**
 * The decision numbers from the results table.
 *
 * The shape is not the payload's: the comparison row is the lift, its
 * `prob_beats_other` is `prob_b_beats_a`, and each arm row carries its own
 * `expected_loss`. Reconstructed here rather than anywhere a component can
 * see, so a live view and a backfilled one render from one type.
 */
function decisionFromResults(rows: Array<Record<string, unknown>>): DecisionView {
  const comparison = rows.find((row) => row.row_type === "comparison");
  const arms = rows.filter((row) => row.row_type === "arm");
  const any = comparison ?? arms[0];
  if (any === undefined) return EMPTY_DECISION;

  const armA = arms.find((row) => row.role === "A") ?? arms[0];
  const armB = arms.find((row) => row.role === "B") ?? arms[1];

  return {
    comparison: str(any.comparison),
    outcome: str(any.outcome),
    prior: { alpha: num(any.prior_alpha), beta: num(any.prior_beta) },
    credibleMass: num(any.credible_mass),
    probBBeatsA: comparison ? num(comparison.prob_beats_other) : null,
    expectedLossA: armA ? num(armA.expected_loss) : null,
    expectedLossB: armB ? num(armB.expected_loss) : null,
    lift: comparison
      ? {
          mean: num(comparison.posterior_mean),
          sd: num(comparison.posterior_sd),
          ciLow: num(comparison.ci_low),
          ciHigh: num(comparison.ci_high),
        }
      : null,
    decision: str(any.decision),
    conclusive: bool(any.conclusive),
    source: "results",
  };
}

export function decisionFromSnapshot(snapshot: RunSnapshot): DecisionView {
  if (snapshot.latestProgress !== null) {
    return decisionFromPayload(
      payloadOf<BayesianAbProgressPayload>(snapshot.latestProgress),
    );
  }
  const rows = previewRows(snapshot.results);
  return rows.length > 0 ? decisionFromResults(rows) : EMPTY_DECISION;
}

/* ------------------------------------------------------------------ *
 * Failure reading
 * ------------------------------------------------------------------ */

/**
 * Whether a FAILED run most likely failed on its input rather than crashing.
 *
 * `BayesianAbModel.__init__` validates: an unknown `comparison`, a
 * non-positive prior, or an `arms` override that is not exactly two entries
 * all raise before a single stage runs. It is the only model here that does
 * this, and it means a typo in the form is a FAILED run rather than a
 * surprising default. A failure with zero completed stages is therefore much
 * more likely to be a bad config than a broken model, and saying so turns a
 * confusing red box into an instruction.
 */
export function looksLikeInputError(
  state: UiRunState | null,
  stages: StageProgress,
): boolean {
  return isSettled(state) && state !== "SUCCEEDED" && state !== "CANCELLED" && stages.done === 0;
}
