/**
 * The derivations `panel_fit` turns on, as pure functions.
 *
 * This is the only model on the platform with **per-unit outcomes**: it fits N
 * groups independently and individual groups are allowed to FAIL while the run
 * SUCCEEDS. Every other model is one computation with one verdict, so a view
 * that renders only `percent_complete` and `primary_metric` draws a healthy
 * run and a run quietly failing a third of its units identically. Making that
 * impossible is the entire reason the model exists, so the fitted/failed split
 * is derived here — once, testably — rather than being re-read out of a
 * payload in three components.
 *
 * Four things in here are places the view could be quietly wrong:
 *
 *  1. **The invariant.** `groups_fitted + groups_failed === groups_done` on
 *     every progress message. `PanelCounts.consistent` checks it rather than
 *     assuming it, because if it ever stops holding the split bar is showing a
 *     lie and the honest response is to say so, not to pick one of the three
 *     numbers to believe.
 *  2. **Where "normal" stops.** See `toneFor`. A failed group is information,
 *     not an error, and the tone type has no `bad` member so that stays true
 *     under later edits — the same guard `annealing`'s `CalmTone` uses for an
 *     over-capacity walk.
 *  3. **Rounding a failure away.** `buildLattice` never lets a nonzero
 *     `groups_failed` round down to zero cells. One failure in five hundred
 *     groups is exactly the case the headline exists to surface.
 *  4. **Chunk accumulation.** This is the model that emits `result` more than
 *     once per run, so a chunk that arrives twice under two seq numbers would
 *     otherwise double every group in it — see `accumulateGroups`.
 */

import { payloadOf } from "@/components/models/contract";
import type { ProgressMessage, ResultMessage } from "@/lib/envelope";
import {
  PANEL_FIT_FAILURE_REASONS,
  PANEL_FIT_GROUP_STATUSES,
  type PanelFitProgressPayload,
} from "@/lib/models";

/**
 * The provenance fields, which `PanelFitProgressPayload` does not declare.
 *
 * `job/models/panel_fit/model.py::_progress` spreads `**self._provenance` into
 * every payload, and `Dataset.describe()` (`job/models/_data/sample_data.py`) is
 * where these four keys come from. `@/lib/models` is hand-derived and missed
 * them; declaring the gap locally is cheaper than editing a file this track
 * does not own, and it is load-bearing rather than cosmetic —
 * `main.dbx_leaning.owid_country_year` has never been created, so the DEFAULT
 * run falls back to the synthetic panel. A view that does not surface that
 * shows a convincing chart of invented data.
 */
interface PanelProvenanceFields {
  data_source: string;
  data_synthetic: boolean;
  data_rows: number;
  data_fallback_reason: string | null;
}

type PanelPayload = PanelFitProgressPayload & PanelProvenanceFields;

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/* ================================================================== *
 * The split — the headline
 * ================================================================== */

/**
 * How much attention the failed groups deserve.
 *
 * Deliberately narrow, and `bad` is deliberately not a member: styling a
 * failed group as an error is then a type error rather than a judgement
 * someone re-makes in six months. A country with three observations cannot be
 * fitted, and that is a fact about the panel, not a fault in the run — the
 * model records it with a reason precisely so it is readable as information.
 */
export type FailureTone = "none" | "routine" | "notable";

export interface PanelCounts {
  done: number | null;
  total: number | null;
  fitted: number | null;
  failed: number | null;
  /** `total - done`. Null unless both are known. */
  remaining: number | null;
  /**
   * `groups_fitted + groups_failed === groups_done`. Null when the three are
   * not all present; false means the payload contradicts itself.
   */
  consistent: boolean | null;
  /** `failed / done`. Of the groups DONE, not of the panel — the share of the
   *  whole panel is unknowable mid-run and would drift downward as a run
   *  progressed even with a constant failure rate. */
  failedShare: number | null;
  tone: FailureTone;
  /** Every completed group failed, and at least one has completed. The model
   *  itself returns INFEASIBLE for this at the end of the run
   *  (`_terminal_status`) — "it ran, and the answer is that there isn't one" —
   *  so this flag is the mid-run view of a verdict the model will reach on its
   *  own, never a verdict this view invents. */
  allFailed: boolean;
}

export const NO_COUNTS: PanelCounts = {
  done: null,
  total: null,
  fitted: null,
  failed: null,
  remaining: null,
  consistent: null,
  failedShare: null,
  tone: "none",
  allFailed: false,
};

/**
 * Where "failures are normal" stops and "this run is going wrong" starts.
 *
 * The line is **half of the completed groups**, and it is drawn there because
 * it is the only threshold available that is not an arbitrary percentage.
 * `primary_metric` is the median R-squared *across fitted groups*: once the
 * fitted groups stop being the majority of the completed ones, the run's
 * headline number has stopped describing the panel and started describing a
 * minority of it. That is a statement about what the numbers on screen mean,
 * not a taste about how many failures are too many.
 *
 * It also degenerates correctly. At `failed === done` — every group failed —
 * the model's own `_terminal_status` returns INFEASIBLE rather than SUCCEEDED,
 * so the extreme end of `notable` is a line the model already draws itself and
 * this one is its continuation rather than a second opinion.
 *
 * `notable` maps to `warn` in the component, never `bad`. "Worth looking at"
 * is as far as this view is entitled to go: a panel where most groups are too
 * short to fit is a correct run over thin data, and the reason breakdown is
 * what tells the two apart.
 */
function toneFor(done: number | null, failed: number | null): FailureTone {
  if (failed === null || failed <= 0) return "none";
  if (done === null || done <= 0) return "routine";
  return failed * 2 >= done ? "notable" : "routine";
}

export function readCounts(latest: ProgressMessage | null): PanelCounts {
  const payload = payloadOf<PanelPayload>(latest);
  const done = finite(payload.groups_done);
  const total = finite(payload.groups_total);
  const fitted = finite(payload.groups_fitted);
  const failed = finite(payload.groups_failed);

  return {
    done,
    total,
    fitted,
    failed,
    remaining: done === null || total === null ? null : Math.max(0, total - done),
    consistent:
      done === null || fitted === null || failed === null ? null : fitted + failed === done,
    failedShare: failed === null || done === null || done <= 0 ? null : failed / done,
    tone: toneFor(done, failed),
    allFailed: done !== null && done > 0 && failed === done,
  };
}

/* ================================================================== *
 * Provenance — is any of this real data?
 * ================================================================== */

export interface PanelProvenance {
  source: string | null;
  /** Null means the run has not said. Do NOT default this to false: "we do
   *  not know whether this is real" and "this is real" are different claims,
   *  and only one of them justifies reading the chart. */
  synthetic: boolean | null;
  rows: number | null;
  /** Why the loader fell back. Null on a successful read — `describe()` always
   *  emits the key, so its absence means no provenance at all rather than a
   *  clean read. */
  fallbackReason: string | null;
}

export const NO_PROVENANCE: PanelProvenance = {
  source: null,
  synthetic: null,
  rows: null,
  fallbackReason: null,
};

export function readProvenance(latest: ProgressMessage | null): PanelProvenance {
  const payload = payloadOf<PanelPayload>(latest);
  const synthetic = payload.data_synthetic;
  return {
    source: text(payload.data_source),
    synthetic: typeof synthetic === "boolean" ? synthetic : null,
    rows: finite(payload.data_rows),
    fallbackReason: text(payload.data_fallback_reason),
  };
}

/* ================================================================== *
 * The lattice — one cell per group, or a proportion of them
 * ================================================================== */

export const LATTICE_COLUMNS = 24;
/** Above this many groups the lattice stops being one cell per group and
 *  becomes a proportion. 96 covers the default synthetic panel's 48 entities
 *  with a cell each, and the OWID-sized ~180 without becoming pixel dust. */
export const LATTICE_MAX_CELLS = 96;

export type CellKind = "fitted" | "failed" | "pending";

export interface Lattice {
  cells: readonly CellKind[];
  /** True when every cell is exactly one group. "One cell per group" is only
   *  literally true for the smaller panels and a viewer is owed which one they
   *  have. */
  oneCellPerGroup: boolean;
  /** Groups behind each cell. 1 when `oneCellPerGroup`. */
  groupsPerCell: number;
  /** First pending cell — the group being fitted right now. Null once nothing
   *  is pending. Falls out of the invariant: the fitted and failed blocks
   *  together are `groups_done` cells, so the first pending cell IS the
   *  frontier without needing `groups_done` a second time. */
  frontier: number | null;
}

export const NO_LATTICE: Lattice = {
  cells: [],
  oneCellPerGroup: false,
  groupsPerCell: 1,
  frontier: null,
};

/**
 * Lay the split out as a waffle.
 *
 * Contiguous blocks — fitted, then failed, then pending — rather than failures
 * sprinkled through the grid. Sprinkling would read as *which* units failed,
 * which the lattice does not know and must not imply; contiguous blocks read
 * as a proportion, which is exactly what is real here.
 *
 * The rounding rule that matters: a nonzero `groups_failed` never rounds down
 * to zero cells. One failure in five hundred groups is under half a cell at
 * this scale, and silently dropping it would delete the one thing this view
 * exists to show. Overflow from that minimum comes off the fitted block, which
 * has hundreds of cells to spare.
 */
export function buildLattice(counts: PanelCounts): Lattice {
  const { total, fitted, failed } = counts;
  if (total === null || total <= 0 || fitted === null || failed === null) return NO_LATTICE;

  const cellCount = Math.min(Math.round(total), LATTICE_MAX_CELLS);
  const scale = cellCount / total;

  let fittedCells = fitted > 0 ? Math.max(1, Math.round(fitted * scale)) : 0;
  let failedCells = failed > 0 ? Math.max(1, Math.round(failed * scale)) : 0;
  const overflow = fittedCells + failedCells - cellCount;
  if (overflow > 0) {
    fittedCells = Math.max(0, fittedCells - overflow);
    failedCells = Math.min(failedCells, cellCount - fittedCells);
  }

  const cells: CellKind[] = [];
  for (let i = 0; i < cellCount; i += 1) {
    cells.push(i < fittedCells ? "fitted" : i < fittedCells + failedCells ? "failed" : "pending");
  }

  const firstPending = fittedCells + failedCells;
  return {
    cells,
    oneCellPerGroup: cellCount === Math.round(total),
    groupsPerCell: total / cellCount,
    frontier: firstPending < cellCount ? firstPending : null,
  };
}

/* ================================================================== *
 * Failure reasons — a closed set, so a UI can pre-declare them
 * ================================================================== */

export interface ReasonInfo {
  reason: string;
  /** Whether it is one of the four `FAILURE_REASONS` the model declares. */
  known: boolean;
  label: string;
  /** What the data did. */
  meaning: string;
  /** What, if anything, is worth doing about it — hedged where the honest
   *  answer is "nothing, and that is fine". */
  note: string;
}

const REASON_INFO: Record<string, Omit<ReasonInfo, "reason" | "known">> = {
  too_few_observations: {
    label: "Too few observations",
    meaning:
      "Fewer usable rows than a fit of this degree needs. Reached two ways — a group that is simply short, and one that looked long enough until its null responses were dropped.",
    note: "Usually a fact about the panel rather than the run. The usable-of-seen counts below are what tell the two routes apart.",
  },
  zero_predictor_variance: {
    label: "No predictor variance",
    meaning:
      "Every observation in the group carries the same predictor value — one reporting year repeated across export revisions, typically. There is no slope to estimate.",
    note: "Nothing the degree can fix while the predictor is that column.",
  },
  singular_design: {
    label: "Singular design",
    meaning:
      "The design matrix is rank-deficient: fewer distinct predictor values than the fit has coefficients, or a system whose SVD comes back deficient.",
    note: "The one reason that is often about the configuration rather than the panel. A Vandermonde over calendar years is badly conditioned above the quadratic, so degree 3 reports this for most groups by design — a recorded failure instead of a quietly wrong fit.",
  },
  non_finite_result: {
    label: "Non-finite result",
    meaning:
      "The arithmetic ran to completion and produced NaN or infinity — overflow in the residual sum of squares on extreme values, most likely.",
    note: "Recorded rather than returned: a fit nobody should be handed as if it were a number.",
  },
};

const UNKNOWN_REASON: Omit<ReasonInfo, "reason" | "known"> = {
  label: "Unrecognised reason",
  meaning:
    "Not one of the four reasons this model declares. `FAILURE_REASONS` is closed on purpose, so a fifth value is a bug in the model rather than a new category.",
  note: "Shown rather than dropped — a reason this view cannot name is still a failure that happened.",
};

export function describeReason(reason: string): ReasonInfo {
  const known = (PANEL_FIT_FAILURE_REASONS as readonly string[]).includes(reason);
  const info = REASON_INFO[reason] ?? UNKNOWN_REASON;
  return { reason, known, ...info };
}

export interface ReasonCount extends ReasonInfo {
  count: number;
}

export interface FailureBreakdown {
  /** Every declared reason, in declaration order, INCLUDING the zeroes —
   *  then any undeclared reason the run produced. Declaration order rather
   *  than sorted-by-count so the bars do not reorder themselves as a run
   *  progresses, which reads as churn the run did not cause. Zeroes are kept
   *  because "no group failed this way" is an answer. */
  reasons: readonly ReasonCount[];
  /** Reasons with a nonzero count, most first. */
  present: readonly ReasonCount[];
  total: number;
  /** The reason accounting for the most failures. `too_few_observations`
   *  dominating says something quite different about the data than
   *  `singular_design` dominating says about the run. */
  dominant: ReasonCount | null;
  /** Largest single count, for scaling a bar. */
  peak: number;
}

export const NO_FAILURES: FailureBreakdown = {
  reasons: [],
  present: [],
  total: 0,
  dominant: null,
  peak: 0,
};

export function failureBreakdown(latest: ProgressMessage | null): FailureBreakdown {
  const payload = payloadOf<PanelPayload>(latest);
  const raw = payload.failure_counts;
  if (raw === undefined || raw === null || typeof raw !== "object") return NO_FAILURES;

  const counts = raw as Record<string, unknown>;
  const seen = new Map<string, number>();
  for (const reason of PANEL_FIT_FAILURE_REASONS) {
    seen.set(reason, finite(counts[reason]) ?? 0);
  }
  for (const [reason, value] of Object.entries(counts)) {
    if (seen.has(reason)) continue;
    seen.set(reason, finite(value) ?? 0);
  }

  const reasons: ReasonCount[] = [...seen.entries()].map(([reason, count]) => ({
    ...describeReason(reason),
    count,
  }));
  const present = reasons.filter((r) => r.count > 0).sort((a, b) => b.count - a.count);

  return {
    reasons,
    present,
    total: reasons.reduce((sum, r) => sum + r.count, 0),
    dominant: present[0] ?? null,
    peak: present[0]?.count ?? 0,
  };
}

/* ================================================================== *
 * Per-group points, off the progress stream
 * ================================================================== */

export type GroupStatus = (typeof PANEL_FIT_GROUP_STATUSES)[number];

export interface GroupPoint {
  /** `groups_done` — the group's 1-based position in the panel, and the x
   *  axis for everything derived from progress. */
  done: number;
  key: string | null;
  label: string | null;
  status: GroupStatus | null;
  reason: string | null;
  rSquared: number | null;
  /** Rows that survived the null and non-finite drop. */
  nObservations: number | null;
  /** Rows the group HAD. The difference between "this unit is small" and
   *  "this unit did not report", which is the first question about any
   *  failure. */
  rowsSeen: number | null;
  /** `primary_metric` at this point: the running MEDIAN R-squared across the
   *  groups fitted so far. Higher is better here — see `metricHigherIsBetter`,
   *  which is read off the payload rather than assumed, because the direction
   *  is the opposite of `forecasting`'s and importing one model's is how a
   *  chart ends up drawing "better" downward. */
  median: number | null;
}

function statusOf(value: unknown): GroupStatus | null {
  const raw = text(value);
  return raw !== null && (PANEL_FIT_GROUP_STATUSES as readonly string[]).includes(raw)
    ? (raw as GroupStatus)
    : null;
}

/**
 * One point per progress message, deduplicated on `groups_done` and sorted.
 *
 * Deduplicated because `groups_done` is the x axis: two messages claiming the
 * same position would put two dots on one group. The store already drops a
 * repeated `seq`, so this only fires for a genuine re-emission, and the first
 * copy wins to match `accumulateGroups`.
 */
export function buildGroupPoints(progress: readonly ProgressMessage[]): GroupPoint[] {
  const byDone = new Map<number, GroupPoint>();

  for (const message of progress) {
    const payload = payloadOf<PanelPayload>(message);
    const done = finite(payload.groups_done);
    if (done === null || byDone.has(done)) continue;
    byDone.set(done, {
      done,
      key: text(payload.group_key),
      label: text(payload.group_label),
      status: statusOf(payload.group_status),
      reason: text(payload.group_failure_reason),
      rSquared: finite(payload.group_r_squared),
      nObservations: finite(payload.n_observations),
      rowsSeen: finite(payload.rows_seen),
      median: message.primary_metric,
    });
  }

  return [...byDone.values()].sort((a, b) => a.done - b.done);
}

/**
 * The metric's direction, read from the run rather than hardcoded.
 *
 * Defaults to true when a run has not said, which matches the model — but the
 * point of reading it is that `forecasting`'s metric improves DOWNWARD, and a
 * direction copied between model directories is a bug nothing catches.
 */
export function metricHigherIsBetter(latest: ProgressMessage | null): boolean {
  const value = payloadOf<PanelPayload>(latest).metric_higher_is_better;
  return typeof value === "boolean" ? value : true;
}

export function failuresOf(points: readonly GroupPoint[]): GroupPoint[] {
  return points.filter((point) => point.status === "failed");
}

/* ================================================================== *
 * Durability — the chunks, and the model's central promise
 * ================================================================== */

export interface GroupRow {
  key: string | null;
  label: string | null;
  status: GroupStatus | null;
  reason: string | null;
  rSquared: number | null;
  nObservations: number | null;
  firstPeriod: number | null;
  lastPeriod: number | null;
}

export interface DurableView {
  /** Deduplicated on `chunk_index`, in chunk order. */
  chunks: readonly ResultMessage[];
  /** Rows written durably, summed across chunks. Here a row is a GROUP, not an
   *  observation, so this number is directly comparable to `groups_done`, and
   *  it trails it by up to `chunk_size` by design. */
  rowsWritten: number;
  /** A `final: true` chunk has been seen. Only then are results complete, and
   *  the model emits exactly one on every path — completed, cancelled, or a
   *  panel with no groups at all. */
  complete: boolean;
  missing: readonly number[];
  /** Preview rows, in chunk order. Failed groups are IN here with null
   *  coefficients: the model records them rather than dropping them, which is
   *  the claim this whole view exists to make visible. */
  rows: readonly GroupRow[];
  fittedRows: number;
  failedRows: number;
}

export const NO_DURABLE: DurableView = {
  chunks: [],
  rowsWritten: 0,
  complete: false,
  missing: [],
  rows: [],
  fittedRows: 0,
  failedRows: 0,
};

/**
 * Accumulate every `result` chunk this run produced.
 *
 * The dedupe is on `chunk_index`, not on `seq`. The store already drops a
 * repeated seq, which covers the live/backfill overlap; this covers the other
 * one — the same chunk re-emitted under a new seq, which a job retry produces
 * and which would otherwise double every group in it. First copy wins; `final`
 * is true if ANY copy carried it.
 *
 * Chunks APPEND. The last does not supersede the ones before it.
 */
export function accumulateGroups(results: readonly ResultMessage[]): DurableView {
  const byIndex = new Map<number, ResultMessage>();
  let complete = false;

  for (const message of results) {
    if (message.final) complete = true;
    if (!byIndex.has(message.chunk_index)) byIndex.set(message.chunk_index, message);
  }

  const chunks = [...byIndex.values()].sort((a, b) => a.chunk_index - b.chunk_index);
  const highest = chunks.at(-1)?.chunk_index ?? -1;
  const missing: number[] = [];
  for (let i = 0; i < highest; i += 1) {
    if (!byIndex.has(i)) missing.push(i);
  }

  const rows: GroupRow[] = [];
  for (const chunk of chunks) {
    for (const row of chunk.preview) {
      rows.push({
        key: text(row["group_key"]),
        label: text(row["group_label"]),
        status: statusOf(row["status"]),
        reason: text(row["failure_reason"]),
        rSquared: finite(row["r_squared"]),
        nObservations: finite(row["n_observations"]),
        firstPeriod: finite(row["first_period"]),
        lastPeriod: finite(row["last_period"]),
      });
    }
  }

  return {
    chunks,
    rowsWritten: chunks.reduce((sum, chunk) => sum + chunk.row_count, 0),
    complete,
    missing,
    rows,
    fittedRows: rows.filter((row) => row.status === "fitted").length,
    failedRows: rows.filter((row) => row.status === "failed").length,
  };
}

export type ArrivalState = "none" | "arriving" | "complete" | "stopped";

/**
 * How to describe the durable record, given the run is or is not over.
 *
 * `stopped` is the case worth separating: a cancelled or failed run keeps
 * every chunk it emitted, but no `final: true` will ever arrive, so "still
 * arriving" would be waiting for something that is not coming.
 */
export function arrivalState(view: DurableView, settled: boolean): ArrivalState {
  if (view.complete) return "complete";
  if (view.chunks.length === 0) return settled ? "stopped" : "none";
  return settled ? "stopped" : "arriving";
}

/* ================================================================== *
 * Formatting
 * ================================================================== */

const R2_FMT = new Intl.NumberFormat(undefined, {
  minimumFractionDigits: 3,
  maximumFractionDigits: 3,
});

/** R-squared is bounded above by 1 and unbounded below — a fit worse than the
 *  group's own mean is legitimately negative — so this never clamps. */
export function formatRSquared(value: number | null | undefined): string | null {
  return value === null || value === undefined || !Number.isFinite(value)
    ? null
    : R2_FMT.format(value);
}

export function formatShare(value: number | null): string | null {
  if (value === null || !Number.isFinite(value)) return null;
  const percent = value * 100;
  // Below 1% a rounded integer reads as zero failures, which is the one thing
  // this number must never say when there is at least one.
  return percent > 0 && percent < 1 ? "<1%" : `${Math.round(percent)}%`;
}
