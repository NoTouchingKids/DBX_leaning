/**
 * The `panel_fit` signature: a field of groups resolving one at a time.
 *
 * Two things are stacked here and they are not the same kind of object.
 *
 * **The lattice** is the animation. Its structure is real — one cell per group
 * where the panel is small enough, a stated proportion where it is not — and
 * the frontier cell marks the group being fitted right now, which is
 * `groups_done` and nothing else. Per `contract.ts` it collapses to ONE flat
 * frame the moment the run is over: no cell means anything different from any
 * other cell at that point.
 *
 * **The outcome bar and the readout** under it are data, not animation. They
 * are identical in every state including terminal, which is where the
 * fitted/failed split goes on living after the lattice has flattened. That
 * split is this model's whole reason for existing — it is the only model on
 * the platform where individual units can FAIL while the run SUCCEEDS, and a
 * view that showed only `percent_complete` and a metric would draw a healthy
 * run and one quietly failing a third of its groups identically.
 *
 * A failed group is styled as information, never as alarm. `FailureTone` has
 * no `bad` member, so that cannot be undone by an edit that does not notice it
 * is making a judgement.
 *
 * ## The phases of `motion.ts`, as frames
 *
 *   idle       nothing moves, and nothing is drawn that would imply a size.
 *              The panel is grouped inside the job, so before the first
 *              progress message this view does not know how many groups exist
 *              and must not sketch a lattice it would then have to resize.
 *   starting   the field brightens once over DURATION.inhale while a single
 *              band of light crosses it, then HOLDS at the brighter tint. The
 *              hold is the load-bearing half: a cold Free Edition job sits in
 *              this phase for tens of seconds, so what is on screen after the
 *              gesture finishes has to be visibly different from idle on its
 *              own.
 *   running    the same band, looping at DURATION.ambient over the rows that
 *              still hold pending groups, plus the frontier cell's breath.
 *   settled    one flat frame, arrived at as a left-to-right wave inside
 *              STAGGER.budget. After it lands nothing moves at all.
 *
 * Three things the previous version got wrong, none of them visible in a
 * screenshot:
 *
 *  - STARTING and the early part of RUNNING had no motion whatsoever. The
 *    lattice needs `groups_total`, which arrives with the FIRST progress
 *    message, so until then this view rendered one static sentence in a dashed
 *    box — for the whole of a cold start. A live run and a page whose stream
 *    had died were pixel-identical, which is the one thing a live view owes
 *    the person watching it.
 *  - The frontier pulse was the only moving element and it exists only while a
 *    cell is still pending. A run that has processed every group but not yet
 *    flushed its final chunk also went completely still, several seconds
 *    before it was over.
 *  - `duration-300` was a number chosen in this file and used nowhere else in
 *    the app. It is `DURATION.base` now, and the flatten it drives is
 *    staggered so the end of a run reads as one gesture rather than 96 cells
 *    changing their minds at once.
 *
 * `frames.ts` carries an `animated` flag, true in exactly the state `phaseOf`
 * calls `running`. This file no longer reads it: `motion.ts` is the single
 * authority on which phases move, and two sources for that is how eleven views
 * end up disagreeing. The flag stays declared over there because `frames.ts`
 * belongs to the state machine, not to this component.
 */

import { motion } from "motion/react";
import type { CSSProperties } from "react";

import { isSettled, type ModelViewProps } from "@/components/models/contract";
import { DURATION, EASE, phaseOf, staggerFor } from "@/components/models/motion";
import { usePrefersReducedMotion } from "@/components/models/useReducedMotion";
import { EMPTY, formatCount } from "@/lib/format";

import { frameFor, TONE_CLASS, TONE_NOTE } from "./frames";
import {
  buildGroupPoints,
  buildLattice,
  describeReason,
  failureBreakdown,
  formatRSquared,
  formatShare,
  LATTICE_COLUMNS,
  readCounts,
  readProvenance,
  type CellKind,
  type FailureTone,
} from "./panelModel";

const FITTED_CELL = "border-good/60 bg-good-soft";
const PENDING_CELL = "border-line bg-paper";

/** Failed cells take the run's tone — cool blue while failures are a minority,
 *  amber once they are not. Never red: `FailureTone` cannot express it. */
const FAILED_CELL: Record<FailureTone, string> = {
  none: "border-info/60 bg-info-soft",
  routine: "border-info/60 bg-info-soft",
  notable: "border-warn/70 bg-warn-soft",
};

/**
 * Cell geometry, in pixels rather than in `rem`.
 *
 * The columns stay fluid (`1fr`) because the contract says a signature fills
 * the width it is given, but the ROW pitch has to be a number this file knows:
 * the sweep is clipped to the rows that still hold pending groups, and
 * deriving that from `0.85rem` means guessing the root font size. Height and
 * gap are set inline from these so the geometry and the layout cannot drift.
 */
const CELL_PX = 14;
const GAP_PX = 3;

/** The band's width as a share of the surface it crosses, and its peak
 *  opacity. Wide and faint on purpose: this is the only element on screen that
 *  repeats forever, and anything with real presence at that cadence becomes
 *  the thing you cannot stop looking at over a ten-minute panel. */
const SWEEP_WIDTH_PCT = 18;
const SWEEP_PEAK = 0.14;

/**
 * The band's travel, in percentages of its OWN width — which is how `motion`
 * reads a percentage `x`. It starts fully off the left edge and ends fully off
 * the right, so the loop's restart is never seen.
 *
 * Derived from `SWEEP_WIDTH_PCT` rather than written out, so widening the band
 * cannot silently leave it stopping short of the far edge.
 */
const SWEEP_TIMES = [0, 0.15, 0.85, 1];
/** Fully off the left edge, and fully off the right. */
const SWEEP_START = -100;
const SWEEP_END = (100 / SWEEP_WIDTH_PCT) * 100;
/** Evenly spaced against `SWEEP_TIMES`, so the travel stays linear while the
 *  opacity gets its ramps off the same keyframe list. */
const sweepX = (t: number) => `${(SWEEP_START + t * (SWEEP_END - SWEEP_START)).toFixed(1)}%`;
const SWEEP_X = SWEEP_TIMES.map(sweepX);
const SWEEP_OPACITY = [0, SWEEP_PEAK, SWEEP_PEAK, 0];

/** The field before the inhale. Dim enough to read as "not asked yet", not so
 *  dim that the sentence inside it stops being legible. */
const REST_OPACITY = 0.45;

function cellClass(kind: CellKind, tone: FailureTone): string {
  if (kind === "fitted") return FITTED_CELL;
  if (kind === "failed") return FAILED_CELL[tone];
  return PENDING_CELL;
}

/**
 * One pass of light, left to right, over whatever surface is on screen.
 *
 * The same arrangement the two Gurobi views use for their boards, deliberately:
 * one shape for "asked, not yet answered" and for "working", differing only in
 * whether it loops. Eleven views each inventing their own idea of ambient is
 * what `motion.ts` exists to prevent.
 *
 * The gradient is asymmetric — a bright leading edge with the fade behind it —
 * because the symmetric band is the skeleton shimmer every loading screen in
 * the world uses, and this is not a loading state. Left to right because that
 * is the direction the lattice fills.
 *
 * `opacity` rides the same keyframe list as `x` rather than getting its own
 * looping transition: two independently scheduled loops of nominally equal
 * length drift apart, and a band whose fade has slipped out of step with its
 * travel reads as a glitch some minutes into a solve.
 */
function Sweep({ loop, style }: { loop: boolean; style: CSSProperties }) {
  return (
    <div className="pointer-events-none absolute overflow-hidden" style={style} aria-hidden="true">
      <motion.div
        // `currentColor` off a token-bound class, never a literal: the dark
        // palette re-points these at runtime.
        className="text-accent"
        style={{
          position: "absolute",
          insetBlock: 0,
          left: 0,
          width: `${SWEEP_WIDTH_PCT}%`,
          background: "linear-gradient(90deg, transparent, currentColor)",
        }}
        initial={{ x: sweepX(0), opacity: 0 }}
        animate={{ x: SWEEP_X, opacity: SWEEP_OPACITY }}
        transition={{
          duration: loop ? DURATION.ambient : DURATION.inhale,
          times: SWEEP_TIMES,
          // Linear, and the only linear thing in this file. A loop that eases
          // pumps once a cycle, which is precisely what becomes irritating on
          // a run that sits open for ten minutes.
          ease: "linear",
          ...(loop ? { repeat: Infinity } : {}),
        }}
      />
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: string | undefined;
}) {
  return (
    <span>
      {label} <span className={`font-semibold ${tone ?? "text-ink"}`}>{value}</span>
    </span>
  );
}

export function PanelLattice({ state, snapshot }: ModelViewProps) {
  const reducedMotion = usePrefersReducedMotion();
  const frame = frameFor(state);
  const phase = phaseOf(state);
  const settled = isSettled(state);

  const counts = readCounts(snapshot.latestProgress);
  const lattice = buildLattice(counts);
  const provenance = readProvenance(snapshot.latestProgress);
  const failures = failureBreakdown(snapshot.latestProgress);
  const tone = TONE_CLASS[counts.tone];

  const latest = buildGroupPoints(snapshot.progress).at(-1) ?? null;

  // The frontier only breathes while the run is genuinely working. Reduced
  // motion drops the breath and keeps the cell, because the cell is where the
  // information is — the movement only draws the eye to it.
  const pulseFrontier = phase === "running" && !reducedMotion;

  // Purely ambient, so reduced motion drops it outright rather than snapping
  // it: it states nothing the header does not already say in words.
  const sweeping = !reducedMotion && (phase === "starting" || phase === "running");
  // The inhale is a transition, so reduced motion skips straight to its end
  // state — which is the half that carries the information.
  const inhaling = !reducedMotion && phase === "starting";
  const awake = phase === "starting" || phase === "running";

  const cols = Math.min(LATTICE_COLUMNS, lattice.cells.length);
  const rows = cols > 0 ? Math.ceil(lattice.cells.length / cols) : 0;
  // The first row still holding a pending group. STARTING has no frontier yet
  // (it has no counts at all in the normal case), so it sweeps the whole
  // field; RUNNING sweeps only what is left to do, and sweeps nothing once
  // nothing is pending — a run in its final flush has no work left to gesture
  // at, and the header's live dot is what says it is still going.
  const sweepFromRow =
    phase === "starting"
      ? 0
      : lattice.frontier === null
        ? null
        : Math.floor(lattice.frontier / cols);

  const sweepBox: CSSProperties | null =
    sweepFromRow === null || rows === 0
      ? null
      : {
          left: 0,
          right: 0,
          top: `${sweepFromRow * (CELL_PX + GAP_PX)}px`,
          height: `${(rows - sweepFromRow) * CELL_PX + (rows - sweepFromRow - 1) * GAP_PX}px`,
        };

  /**
   * The settle, as one wave rather than 96 simultaneous colour changes.
   *
   * Nonzero ONLY in a terminal state. A per-cell delay applied while the run
   * is live would land on the frontier's own advance, which is the one colour
   * change in this view that is an event rather than a gesture — cell 90 of 96
   * would report its group half a second after the group finished.
   */
  const flattenStep = settled && !reducedMotion ? staggerFor(lattice.cells.length) : 0;

  // Built once per render, not per cell: RUNNING is where this component lives
  // for minutes and re-renders on every progress message, and it is also the
  // phase with no per-cell delay to vary.
  const sharedCellStyle: CSSProperties = reducedMotion
    ? // Not the `motion-reduce:` variant — an inline `transitionProperty`
      // outranks a class, so the class would silently lose this argument.
      { height: `${CELL_PX}px`, transitionProperty: "none" }
    : {
        height: `${CELL_PX}px`,
        transitionProperty: "background-color, border-color, opacity",
        transitionDuration: `${DURATION.base}s`,
        transitionTimingFunction: `cubic-bezier(${EASE.standard.join(", ")})`,
      };

  const denominator = counts.total ?? counts.done ?? 0;
  const fittedWidth = denominator > 0 ? ((counts.fitted ?? 0) / denominator) * 100 : 0;
  const failedWidth = denominator > 0 ? ((counts.failed ?? 0) / denominator) * 100 : 0;

  const splitLabel =
    counts.done === null
      ? "No groups reported yet"
      : `${counts.fitted ?? 0} fitted, ${counts.failed ?? 0} failed of ${counts.done} groups processed` +
        (counts.total === null ? "" : ` out of ${counts.total}`);

  return (
    <div>
      <div className="mb-3 flex items-start gap-2.5">
        <span
          className={
            `mt-1 inline-block h-2 w-2 shrink-0 rounded-full ${frame.dotClass} ` +
            (state === "RUNNING" ? "live-dot" : "")
          }
        />
        <div className="min-w-0">
          <div className="text-[0.82rem] font-semibold">{frame.headline}</div>
          <div className="text-[0.72rem] text-dim">{frame.detail}</div>
        </div>
      </div>

      {/*
        Provenance, above everything it qualifies. `main.dbx_leaning
        .owid_country_year` has never been created, so the DEFAULT run falls
        back to the synthetic panel — and a chart of invented data is
        indistinguishable from a chart of real data unless something says so.
      */}
      {provenance.synthetic === true && (
        <p className="mb-3 rounded-[6px] border border-warn bg-warn-soft px-2.5 py-1.5 text-[0.7rem] leading-relaxed text-warn">
          <span className="font-semibold">Synthetic panel.</span> Every number below is generated,
          not measured — <span className="font-mono">{provenance.source ?? "unknown source"}</span>
          {provenance.fallbackReason === null ? "" : `, because ${provenance.fallbackReason}`}.
        </p>
      )}
      {provenance.synthetic === false && (
        <p className="mb-3 text-[0.7rem] text-faint">
          Real rows: <span className="font-mono text-dim">{provenance.source}</span>
          {provenance.rows === null ? "" : ` · ${formatCount(provenance.rows)} rows read`}
        </p>
      )}

      {lattice.cells.length === 0 ? (
        /*
          No lattice yet, which is not the same as nothing to draw. This is the
          frame a cold start sits in, so it inhales once and then holds at the
          brighter tint rather than relying on a gesture that has already
          finished.

          `initial={false}` in every other phase: a view mounting straight into
          a terminal run must not replay an entrance for something that is
          already over.
        */
        <motion.div
          className={
            "relative flex h-[3.4rem] items-center overflow-hidden rounded-[6px] border border-dashed px-3 text-[0.72rem] " +
            (awake ? "border-accent/50 text-dim" : "border-edge text-faint")
          }
          initial={inhaling ? { opacity: REST_OPACITY } : false}
          animate={{ opacity: 1 }}
          transition={{ duration: DURATION.inhale, ease: EASE.decelerate }}
        >
          {settled
            ? "This run reported no groups at all — an empty panel, not an empty chart."
            : "Waiting for the first group. The panel is grouped before any fit runs, so the total arrives with the first progress message."}
          {/* Keyed on the phase so the loop restarts cleanly at the handover
              from the single inhale to the ambient cycle, rather than
              retiming a pass already half way across. */}
          {sweeping && <Sweep key={phase} loop={phase === "running"} style={{ inset: 0 }} />}
        </motion.div>
      ) : (
        <motion.div
          role="img"
          aria-label={splitLabel}
          className="relative grid"
          style={{
            gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
            gap: `${GAP_PX}px`,
          }}
          initial={inhaling ? { opacity: REST_OPACITY } : false}
          animate={{ opacity: 1 }}
          transition={{ duration: DURATION.inhale, ease: EASE.decelerate }}
        >
          {lattice.cells.map((kind, index) => {
            const frontier = frame.split && index === lattice.frontier;
            // Exclusive, never additive. Appending `border-accent` to a cell
            // that already carries `border-line` leaves which one wins up to
            // the order Tailwind happened to emit them in, which is not a
            // thing to leave to chance on the one cell that means something.
            const base = !frame.split
              ? frame.flatCellClass
              : frontier
                ? "border-accent bg-accent-soft"
                : cellClass(kind, counts.tone);
            return (
              <div
                key={index}
                data-cell={frame.split ? kind : "flat"}
                style={
                  flattenStep === 0
                    ? sharedCellStyle
                    : { ...sharedCellStyle, transitionDelay: `${(index * flattenStep).toFixed(3)}s` }
                }
                className={
                  `rounded-[2px] border ${base}` +
                  // Reduced motion drops the breath and keeps the accent cell:
                  // the frontier's POSITION is the information, the movement
                  // only draws the eye to it.
                  //
                  // The one loop here not timed from `motion.ts`. Its 2s cycle
                  // is inside the unhurried band the vocabulary asks for, and
                  // it is a symmetric ease-in-out — which a breath needs and
                  // which none of the `EASE` curves are, since those exist for
                  // one-way moves. Re-timing it with `EASE.standard` would
                  // make it worse, not more consistent.
                  (frontier && pulseFrontier ? " animate-pulse" : "")
                }
              />
            );
          })}

          {/* Last in the DOM so the light passes OVER the cells rather than
              being hidden behind their opaque fills. */}
          {sweeping && sweepBox !== null && (
            <Sweep key={phase} loop={phase === "running"} style={sweepBox} />
          )}
        </motion.div>
      )}

      {/*
        The outcome bar. Data, not animation: unchanged in every state, and
        deliberately still drawn after the lattice above has gone flat, because
        "succeeded with 12 of 180 groups failed" is a thing to know about a
        run that is over.
      */}
      <div className="mt-3">
        <div
          className="flex h-1.5 w-full overflow-hidden rounded-full bg-idle-soft"
          role="img"
          aria-label={splitLabel}
        >
          <span className="h-full bg-good" style={{ width: `${fittedWidth}%` }} />
          <span className={`h-full ${tone.bar}`} style={{ width: `${failedWidth}%` }} />
        </div>

        <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 font-mono text-[0.7rem] text-dim">
          <Stat
            label="fitted"
            value={counts.fitted === null ? EMPTY : formatCount(counts.fitted)}
            tone="text-good"
          />
          <Stat
            label="failed"
            value={counts.failed === null ? EMPTY : formatCount(counts.failed)}
            tone={tone.text}
          />
          <Stat
            label="remaining"
            value={counts.remaining === null ? EMPTY : formatCount(counts.remaining)}
          />
          {counts.failedShare !== null && counts.failedShare > 0 && (
            <Stat
              label="failure rate"
              value={formatShare(counts.failedShare) ?? EMPTY}
              tone={tone.text}
            />
          )}
        </div>

        <p className={`mt-1.5 text-[0.7rem] leading-relaxed ${tone.text}`}>
          {TONE_NOTE[counts.tone]}
          {failures.dominant !== null && counts.tone !== "none"
            ? ` — mostly ${failures.dominant.label.toLowerCase()} (${formatCount(failures.dominant.count)})`
            : ""}
          {counts.allFailed && !settled
            ? ". If that holds to the end the model reports INFEASIBLE rather than a success with nothing in it."
            : ""}
        </p>

        {/*
          `groups_fitted + groups_failed === groups_done` is an invariant of
          every progress message this model emits. If it ever stops holding,
          the bar above is a lie — and the honest response is to say which
          three numbers disagreed, not to pick one to believe.

          The one place in this view that uses an error colour, and
          deliberately: this is not a group that could not be fitted, it is the
          wire contract being broken. Those are different things and only one
          of them is a fault.
        */}
        {counts.consistent === false && (
          <p className="mt-1.5 rounded-[6px] border border-bad bg-bad-soft px-2.5 py-1.5 text-[0.7rem] leading-relaxed text-bad">
            Inconsistent payload: {formatCount(counts.fitted ?? 0)} fitted +{" "}
            {formatCount(counts.failed ?? 0)} failed does not equal{" "}
            {formatCount(counts.done ?? 0)} done. The split above cannot be trusted for this run.
          </p>
        )}

        <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[0.68rem] text-faint">
          <span>
            {lattice.cells.length === 0
              ? "no lattice yet"
              : lattice.oneCellPerGroup
                ? "one cell per group"
                : // Stated as a ratio rather than "N groups per cell", because
                // that number is rarely a whole one — 180 groups over 96 cells
                // is 1.875, and rounding it to 2 is a claim the lattice does
                // not make.
                `${formatCount(counts.total ?? 0)} groups across ${formatCount(lattice.cells.length)} cells`}
          </span>
          {/*
            Usable rows against rows seen, for whichever group reported last.
            "This unit is small" and "this unit did not report" are different
            answers and this is the only place the pair appears — the durable
            results table has `n_observations` but not `rows_seen`.
          */}
          {latest !== null && (
            <span>
              last group{" "}
              <span className="font-mono text-dim">{latest.label ?? latest.key ?? EMPTY}</span>
              {latest.status === "failed" && latest.reason !== null
                ? ` — ${describeReason(latest.reason).label.toLowerCase()}, ${formatCount(latest.nObservations ?? 0)} usable of ${formatCount(latest.rowsSeen ?? 0)} rows`
                : latest.rSquared !== null
                  ? ` — R² ${formatRSquared(latest.rSquared)} over ${formatCount(latest.nObservations ?? 0)} of ${formatCount(latest.rowsSeen ?? 0)} rows`
                  : latest.status === "fitted"
                    ? " — fitted, R² undefined (the response never moves, so there is no variance to explain)"
                    : ""}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
