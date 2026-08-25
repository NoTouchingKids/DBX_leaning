/**
 * The `panel_fit` signature: a field of groups resolving one at a time.
 *
 * Two things are stacked here and they are not the same kind of object.
 *
 * **The lattice** is the animation. Its structure is real — one cell per group
 * where the panel is small enough, a stated proportion where it is not — and
 * the frontier cell marks the group being fitted right now, which is
 * `groups_done` and nothing else. Nothing in it runs on a timer. Per
 * `contract.ts` it collapses to ONE flat frame the moment the run is over: no
 * cell means anything different from any other cell at that point.
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
 */

import { isSettled, type ModelViewProps } from "@/components/models/contract";
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

function cellClass(kind: CellKind, tone: FailureTone): string {
  if (kind === "fitted") return FITTED_CELL;
  if (kind === "failed") return FAILED_CELL[tone];
  return PENDING_CELL;
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
  const settled = isSettled(state);

  const counts = readCounts(snapshot.latestProgress);
  const lattice = buildLattice(counts);
  const provenance = readProvenance(snapshot.latestProgress);
  const failures = failureBreakdown(snapshot.latestProgress);
  const tone = TONE_CLASS[counts.tone];

  const latest = buildGroupPoints(snapshot.progress).at(-1) ?? null;

  // The frontier only pulses while the run is genuinely working. Reduced
  // motion drops the pulse and keeps the cell, because the cell is where the
  // information is — the movement only draws the eye to it.
  const pulseFrontier = frame.animated && !reducedMotion && !settled;

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
        <div className="flex h-[3.4rem] items-center rounded-[6px] border border-dashed border-edge px-3 text-[0.72rem] text-faint">
          {settled
            ? "This run reported no groups at all — an empty panel, not an empty chart."
            : "Waiting for the first group. The panel is grouped before any fit runs, so the total arrives with the first progress message."}
        </div>
      ) : (
        <div
          role="img"
          aria-label={splitLabel}
          className="grid gap-[3px]"
          style={{
            gridTemplateColumns: `repeat(${Math.min(LATTICE_COLUMNS, lattice.cells.length)}, minmax(0, 1fr))`,
          }}
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
                className={
                  `h-[0.85rem] rounded-[2px] border transition-colors duration-300 motion-reduce:transition-none ${base}` +
                  // Reduced motion drops the pulse and keeps the accent cell:
                  // the frontier's POSITION is the information, the movement
                  // only draws the eye to it.
                  (frontier && pulseFrontier ? " animate-pulse" : "")
                }
              />
            );
          })}
        </div>
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
