/**
 * bayesian_ab's signature: the decision assembling itself, one stage at a time.
 *
 * This model breaks the assumption every other signature here is built on.
 * There is no iteration to watch: five closed-form stages, the whole run over
 * in milliseconds, and a client that will usually see the terminal status
 * with an empty `snapshot.progress`. An animation that only reaches its
 * populated form by catching an intermediate frame would be blank on almost
 * every real run.
 *
 * So the state machine is driven by `deriveStages`, which reads the status
 * first and the payload second: SUCCEEDED means five completed stages whether
 * or not a single progress message arrived. The normal case is this panel
 * arriving fully lit in one frame, and that is the case it is designed for —
 * the animated path is the exception.
 *
 * What is real: which chips are filled, the arm labels, the decision word.
 * What is not: the pacing, and the fact that there is any pacing at all.
 * Nothing here is positioned or sized by a numeric value — the numbers are in
 * the two charts.
 */

import { motion } from "motion/react";

import { isSettled, type ModelViewProps } from "@/components/models/contract";
import { usePrefersReducedMotion } from "@/components/models/useReducedMotion";
import type { UiRunState } from "@/lib/envelope";
import { BAYESIAN_AB_STAGES } from "@/lib/models";

import {
  armsFromSnapshot,
  decisionFromSnapshot,
  deriveStages,
  looksLikeInputError,
  STAGE_COUNT,
} from "./derive";

/** Short human labels for `STAGES`, in the model's own order. The wire names
 *  are kept as the title so a reader can join this to a log line. */
const STAGE_LABELS: Record<string, string> = {
  posteriors: "Posteriors",
  comparison: "P(B>A)",
  expected_loss: "Expected loss",
  lift_interval: "Lift interval",
  decision: "Decision",
};

const DONE_CLASS: Record<UiRunState, string> = {
  STARTING: "border-accent bg-accent-soft text-accent-ink",
  QUEUED: "border-info bg-info-soft text-info",
  RUNNING: "border-info bg-info-soft text-info",
  SUCCEEDED: "border-good bg-good-soft text-good",
  FAILED: "border-bad bg-bad-soft text-bad",
  CANCELLED: "border-idle bg-idle-soft text-idle",
  INFEASIBLE: "border-warn bg-warn-soft text-warn",
};

const CAPTION: Record<UiRunState, [string, string]> = {
  STARTING: ["Starting", "loading observations and counting the two arms"],
  QUEUED: ["Queued", "waiting for compute"],
  RUNNING: ["Deciding", "five closed-form stages, no sampler"],
  SUCCEEDED: ["Decided", "all five stages completed"],
  FAILED: ["Failed", "the decision was not reached"],
  CANCELLED: ["Cancelled", "stages completed before the stop are kept"],
  INFEASIBLE: ["Stopped", "reported infeasible"],
};

export function BayesianAbSignature({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const stages = deriveStages(state, snapshot);
  const { arms } = armsFromSnapshot(snapshot);
  const decision = decisionFromSnapshot(snapshot);
  const settled = isSettled(state);
  const key = state ?? "QUEUED";
  const [headline, sub] = CAPTION[key];

  return (
    <section className="overflow-hidden rounded-[10px] border border-edge bg-raised">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 px-4 pt-3.5">
        <div>
          <div className="text-[0.86rem] font-bold">{headline}</div>
          <div className="text-[0.7rem] text-dim">{sub}</div>
        </div>
        <div className="font-mono text-[0.68rem] text-dim">
          <span className="text-faint">stages </span>
          {stages.done} / {STAGE_COUNT}
          {stages.source === "terminal" && (
            <span
              className="text-faint"
              title="No progress message reached this tab. The run finished faster than the stream could deliver, so the stage count comes from the terminal status."
            >
              {" "}
              (from status)
            </span>
          )}
        </div>
      </div>

      <div className="px-4 pt-3 pb-1">
        <ol className="flex flex-wrap gap-2">
          {BAYESIAN_AB_STAGES.map((stage, index) => {
            const done = index < stages.done;
            const failed = stages.failedAt === index + 1;
            return (
              <motion.li
                key={stage}
                title={stage}
                // The stagger exists because of this model specifically. Five
                // chips arriving lit in the same frame — the normal case here,
                // since the run outruns the stream — reads as a static
                // diagram, and a reader learns nothing about the shape of the
                // computation from it. A 70ms cascade says "these happened in
                // this order" once, on arrival. Under reduced motion the
                // cascade is removed and the chips are simply already lit:
                // the transition goes, the state does not.
                initial={reduced ? false : { opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                transition={reduced ? { duration: 0 } : { duration: 0.22, delay: index * 0.07 }}
                className={
                  `flex-1 basis-[8.5rem] rounded-md border px-2.5 py-2 text-[0.72rem] ` +
                  `transition-colors duration-300 motion-reduce:transition-none ` +
                  (failed
                    ? "border-bad bg-bad-soft text-bad"
                    : done
                      ? DONE_CLASS[key]
                      : "border-dashed border-line bg-paper text-faint")
                }
              >
                <div className="font-mono text-[0.6rem] opacity-70">{index + 1}</div>
                <div className="font-semibold">{STAGE_LABELS[stage] ?? stage}</div>
              </motion.li>
            );
          })}
        </ol>
      </div>

      <div className="px-4 pt-3 pb-4">
        {/* The arms, named. Labels are the model's own — "weekend_hours",
            "long_trips" — and are what `decision` will be one of, so showing
            them here is what makes the decision word legible later. */}
        <div className="flex flex-wrap items-stretch gap-2">
          {arms.length === 0 ? (
            <p className="text-[0.72rem] text-faint">
              Arms are counted in <code className="font-mono">build()</code>, before
              the first stage. Nothing has reported them yet.
            </p>
          ) : (
            arms.map((arm) => (
              <div
                key={arm.role}
                className="min-w-[9rem] flex-1 rounded-md border border-line bg-paper px-3 py-2"
              >
                <div className="font-mono text-[0.6rem] text-faint">arm {arm.role}</div>
                <div className="truncate text-[0.8rem] font-semibold" title={arm.label}>
                  {arm.label}
                </div>
                <div className="font-mono text-[0.66rem] text-dim">
                  {arm.successes ?? "—"} / {arm.trials ?? "—"}
                </div>
              </div>
            ))
          )}
        </div>

        {decision.decision !== null && (
          <div
            className={
              `mt-3 rounded-md border px-3 py-2 text-[0.78rem] ` +
              `transition-colors duration-300 motion-reduce:transition-none ` +
              (decision.conclusive === true
                ? "border-good bg-good-soft text-good"
                : "border-warn bg-warn-soft text-warn")
            }
          >
            {decision.decision === "inconclusive" ? (
              <>
                <strong>Inconclusive.</strong> No arm cleared both the probability
                threshold and the expected-loss tolerance.
              </>
            ) : (
              <>
                <strong>{decision.decision}</strong> leads, conclusively.
              </>
            )}
            {decision.source === "results" && (
              <span className="ml-1 text-[0.68rem] opacity-80">
                (read from the result rows — no progress message arrived)
              </span>
            )}
          </div>
        )}

        {looksLikeInputError(state, stages) && (
          <div className="mt-3 rounded-md border border-bad bg-bad-soft px-3 py-2 text-[0.75rem] text-bad">
            <strong>No stage ran.</strong> This model validates its own config
            before any arithmetic: an unknown <code className="font-mono">comparison</code>,
            a prior with a non-positive <code className="font-mono">alpha</code> or{" "}
            <code className="font-mono">beta</code>, or an{" "}
            <code className="font-mono">arms</code> override that is not exactly two
            entries each raise on construction. A failure this early is usually bad
            input rather than a crash — the ERROR line in the log below names it.
          </div>
        )}

        {settled && stages.done < STAGE_COUNT && state === "CANCELLED" && (
          <p className="mt-3 text-[0.7rem] text-dim">
            The stages are ordered so everything before{" "}
            <code className="font-mono">lift_interval</code> is exact arithmetic, so
            the {stages.done} completed here are finished numbers, not
            half-converged ones.
          </p>
        )}
      </div>
    </section>
  );
}
