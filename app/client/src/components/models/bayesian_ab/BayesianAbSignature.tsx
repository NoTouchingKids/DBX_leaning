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
 * The lifecycle phases of `motion.ts` land here as:
 *
 *   idle      a flat empty rail, five dashed chips, nothing in motion.
 *   starting  the rail draws itself once, left to right, over DURATION.inhale
 *             and then HOLDS lit. The hold is the whole point: a cold job can
 *             sit in this phase for tens of seconds, and the frame it sits in
 *             must not be the idle frame.
 *   running   one soft pass along the rail per DURATION.ambient, and a solid
 *             outline on the stage the payload says is in flight. Rarely seen,
 *             since this model outruns its own telemetry — but it is the phase
 *             a wedged run would sit in for minutes, so it is paced for that.
 *   settled   the chips wash in as one staggered wave, the decision lands, and
 *             everything stops. Nothing loops past the end of a run.
 *
 * The previous version animated the chips on mount only. That is wrong here in
 * a way that is easy to miss: `RunWorkspace` keeps this component mounted for
 * the life of a run, so a run watched from QUEUED through to SUCCEEDED played
 * its cascade once, at QUEUED, with all five chips still empty — and then
 * silently recoloured. The wash below is driven by `animate` rather than
 * `initial`, so it fires when the stage count actually changes, whether that
 * is on mount or four seconds later.
 */

import { motion } from "motion/react";

import { isSettled, type ModelViewProps } from "@/components/models/contract";
import {
  DURATION,
  EASE,
  phaseOf,
  staggerFor,
} from "@/components/models/motion";
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

/* The lit chip is two layers, not one class: the border and text switch by CSS
 * transition, and the background arrives separately as a wash that scales in
 * from the left. Splitting them is what lets the fill be the animated thing —
 * a background-colour crossfade cannot be staggered into a direction. */
const DONE_EDGE: Record<UiRunState, string> = {
  STARTING: "border-accent text-accent-ink",
  QUEUED: "border-info text-info",
  RUNNING: "border-info text-info",
  SUCCEEDED: "border-good text-good",
  FAILED: "border-bad text-bad",
  CANCELLED: "border-idle text-idle",
  INFEASIBLE: "border-warn text-warn",
};

const DONE_WASH: Record<UiRunState, string> = {
  STARTING: "bg-accent-soft",
  QUEUED: "bg-info-soft",
  RUNNING: "bg-info-soft",
  SUCCEEDED: "bg-good-soft",
  FAILED: "bg-bad-soft",
  CANCELLED: "bg-idle-soft",
  INFEASIBLE: "bg-warn-soft",
};

const RAIL_FILL: Record<UiRunState, string> = {
  STARTING: "bg-accent",
  QUEUED: "bg-info",
  RUNNING: "bg-info",
  SUCCEEDED: "bg-good",
  FAILED: "bg-bad",
  CANCELLED: "bg-idle",
  INFEASIBLE: "bg-warn",
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

/* Five chips, so this is STAGGER.step untouched — the cap only bites on the
 * grid-sized sets. Against a DURATION.base fill it produces a diagonal wave
 * rather than five distinct arrivals, which is the honest reading: these
 * stages really did all happen at once, in this order. */
const STAGGER_STEP = staggerFor(BAYESIAN_AB_STAGES.length);

export function BayesianAbSignature({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const stages = deriveStages(state, snapshot);
  const { arms } = armsFromSnapshot(snapshot);
  const decision = decisionFromSnapshot(snapshot);
  const settled = isSettled(state);
  const phase = phaseOf(state);
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

      <div className="px-4 pt-3.5 pb-1">
        {/* The rail carries the phase machine. The chips carry the detail; this
            says the same thing from across the room, and it is the one element
            that has something to show during `starting`, when no stage has
            completed and every chip is still empty. */}
        <div
          aria-hidden
          className="relative h-[3px] overflow-hidden rounded-full bg-line"
        >
          <motion.div
            className={`absolute inset-0 origin-left rounded-full ${RAIL_FILL[key]}`}
            initial={reduced ? false : { scaleX: 0 }}
            animate={{ scaleX: stages.done / STAGE_COUNT }}
            transition={
              reduced
                ? { duration: 0 }
                : { duration: DURATION.base, ease: EASE.standard }
            }
          />

          {phase === "starting" && (
            /* The inhale. One draw, decelerating into a held tint — it does not
               loop and it does not fade back out, because the thing it has to
               survive is a forty-second cold start with nothing to report. A
               pulse here would be describing waiting as if it were work. */
            <motion.div
              className="absolute inset-0 origin-left rounded-full bg-accent/30"
              initial={reduced ? false : { scaleX: 0 }}
              animate={{ scaleX: 1 }}
              transition={
                reduced
                  ? { duration: 0 }
                  : { duration: DURATION.inhale, ease: EASE.decelerate }
              }
            />
          )}

          {phase === "running" && !reduced && (
            /* Ambient: one unhurried pass, seam-free because the highlight
               fades up after it enters and down before it leaves, so the wrap
               is never visible. Linear on purpose — an eased traverse spends
               its slow ends where the highlight is already invisible and its
               fast middle where it is not, which reads as a dart rather than a
               drift. Under reduced motion this is absent entirely; "running"
               is still legible from the caption and the outlined chip. */
            <motion.div
              className="absolute inset-y-0 left-0 w-1/5 rounded-full bg-accent"
              animate={{ x: ["-150%", "560%"], opacity: [0, 0.8, 0.8, 0] }}
              transition={{
                duration: DURATION.ambient,
                repeat: Infinity,
                ease: "linear",
              }}
            />
          )}
        </div>

        <ol className="mt-3.5 flex flex-wrap gap-2">
          {BAYESIAN_AB_STAGES.map((stage, index) => {
            const done = index < stages.done;
            const failed = stages.failedAt === index + 1;
            // Only claimed while RUNNING, where `stage_index` genuinely means
            // "this many finished, the next one is in flight". During starting
            // the job has not reached stage 1 yet and marking it would be a
            // guess dressed as a reading. No guard for done === STAGE_COUNT is
            // needed: there is no chip at index 5 to match it.
            const next = phase === "running" && index === stages.done;
            const washed = done || failed;

            return (
              <li
                key={stage}
                title={stage}
                className={
                  `relative flex-1 basis-[8.5rem] overflow-hidden rounded-md border ` +
                  `bg-paper px-2.5 py-2 text-[0.72rem] ` +
                  `transition-colors duration-300 motion-reduce:transition-none ` +
                  (failed
                    ? "border-bad text-bad"
                    : done
                      ? DONE_EDGE[key]
                      : next
                        ? "border-accent text-dim"
                        : "border-dashed border-line text-faint")
                }
              >
                <motion.span
                  aria-hidden
                  className={`absolute inset-0 origin-left ${failed ? "bg-bad-soft" : DONE_WASH[key]}`}
                  // `initial` would only ever fire on mount, and this component
                  // is mounted for the life of the run. Driving scaleX from
                  // `animate` means the wave replays whenever the stage count
                  // moves — including the usual case, where it moves from 0 to
                  // 5 in a single frame long after mount.
                  initial={reduced ? false : { scaleX: 0 }}
                  animate={{ scaleX: washed ? 1 : 0 }}
                  transition={
                    reduced
                      ? { duration: 0 }
                      : {
                          duration: DURATION.base,
                          ease: EASE.standard,
                          delay: washed ? index * STAGGER_STEP : 0,
                        }
                  }
                />
                <div className="relative font-mono text-[0.6rem] opacity-70">
                  {index + 1}
                </div>
                <div className="relative font-semibold">
                  {STAGE_LABELS[stage] ?? stage}
                </div>
              </li>
            );
          })}
        </ol>
      </div>

      <div className="px-4 pt-3 pb-4">
        {/* The arms, named. Labels are the model's own — "weekend_hours",
            "long_trips" — and are what `decision` will be one of, so showing
            them here is what makes the decision word legible later. Deliberately
            unanimated: they are the reference the wave above is read against,
            and a reference that moves is not one. */}
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
          /* The one settling gesture in the panel, and it belongs here: the
             decision word is what the other five stages were for. Delayed
             behind the chip wave so the two read in order rather than as one
             blur, and EASE.emphasis so it arrives with the slight overshoot of
             something landing rather than something fading up. Then it is
             still — per contract.ts, nothing survives the end of the run. */
          <motion.div
            initial={reduced ? false : { opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={
              reduced
                ? { duration: 0 }
                : { duration: DURATION.slow, ease: EASE.emphasis, delay: DURATION.fast }
            }
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
          </motion.div>
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
