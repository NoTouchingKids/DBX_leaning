/**
 * Pure logic for the past-run page: paging, assembly, and the three questions
 * a finished run can be asked that a live one cannot.
 *
 * Everything here is a plain function over data. The React layer above it
 * (`useRunHistory`, `RunDetail`) does no arithmetic — this is where the parts
 * that can be *wrong* live, so they can be tested without a DOM, a fetch or a
 * transport.
 *
 * The governing fact: **a finished run is immutable**. Its message history
 * cannot change, so it is fetched once and kept, and none of the loops here
 * need a retry, a poll or an invalidation. The governing hazard is the mirror
 * image: **a gap in a finished run's seq stream is permanent**. Backfill
 * filters `client_visible = false` server-side (`app/server/repository.py::
 * messages_since`, `AND client_visible` on the log branch), exactly as the
 * live path does, so those seqs exist and will never be returned by any
 * request. A loop that pages until the seq range is contiguous does not
 * terminate. That is why `nextCursor` below is written against the server's
 * own `more` flag and against cursor movement — never against seq contiguity.
 */

import {
  isTerminal,
  type Message,
  type ResultMessage,
  type RunStatus,
  type UiRunState,
} from "@/lib/envelope";
import { normalizeMessage } from "@/transport/normalize";
import { RunStore, type Gap, type RunSnapshot } from "@/transport/runStore";

/** `after_seq` is an exclusive lower bound and the route's default is -1, so
 *  this is "from the beginning" and not a sentinel of our own invention. */
export const FIRST_CURSOR = -1;

/* ------------------------------------------------------------------ *
 * Paging
 * ------------------------------------------------------------------ */

/** The two fields of a backfill response that decide whether to ask again. */
export interface PageCursorInput {
  more: boolean;
  next_after_seq: number;
}

/**
 * The next `after_seq` to request, or `null` when there is nothing more.
 *
 * Three termination conditions, and all three are needed:
 *
 *  1. `more === false` — the server filled less than a page, which is its own
 *     statement that it has run out. This is the normal exit.
 *  2. the cursor did not advance — `next_after_seq` is `messages[-1].seq`, or
 *     an echo of `after_seq` when the page was empty. A server that returns
 *     `more: true` with a non-advancing cursor (empty page at a full-page
 *     limit, or a bug) would otherwise be requested forever at the same
 *     offset. This is the guard the whole "no unbounded loop" claim rests on.
 *  3. it is never asked whether the seqs are contiguous. They may not be, and
 *     no further request can make them so.
 */
export function nextCursor(page: PageCursorInput, cursor: number): number | null {
  if (!page.more) return null;
  if (page.next_after_seq <= cursor) return null;
  return page.next_after_seq;
}

/* ------------------------------------------------------------------ *
 * Normalisation
 * ------------------------------------------------------------------ */

export interface NormalizedPages {
  messages: Message[];
  /** Rows that could not be turned into a message at all. Counted rather than
   *  thrown, and surfaced, so a partial history says it is partial. */
  unusable: number;
}

/**
 * Backfilled rows through the one normalisation function — with `run_id` put
 * back.
 *
 * `app/server/repository.py::_rehydrate` builds `{"type", "seq", "ts", **fields}`,
 * and `run_id` is neither selected by `messages_since` nor packed into
 * `body`. So a backfilled message arrives WITHOUT the field, even though
 * `BackfillResponse.messages` is typed `Message[]` — and `normalizeMessage`
 * rejects a message with no `run_id`, since on the live path a missing one is
 * a frame that cannot be filed. Injecting it here is safe precisely because
 * the request was per-run: every row in the response is that run's by
 * construction. Doing it at the seam keeps `normalizeMessage` the single
 * normaliser rather than growing a backfill-shaped second one.
 */
export function normalizeBackfilled(
  runId: string,
  rows: readonly unknown[],
): NormalizedPages {
  const messages: Message[] = [];
  let unusable = 0;
  for (const row of rows) {
    const raw =
      typeof row === "object" && row !== null
        ? { run_id: runId, ...(row as Record<string, unknown>) }
        : row;
    const message = normalizeMessage(raw);
    if (message === null) unusable += 1;
    else messages.push(message);
  }
  return { messages, unusable };
}

/* ------------------------------------------------------------------ *
 * Assembly
 * ------------------------------------------------------------------ */

export interface AssembledHistory {
  /** The same shape the live path produces, so a `ModelView` cannot tell the
   *  difference between this and a stream it watched. */
  snapshot: RunSnapshot;
  unusable: number;
  /** Highest seq actually loaded, or null if nothing was. */
  lastSeq: number | null;
}

/**
 * Backfilled pages -> a `RunSnapshot`.
 *
 * Built by feeding the real `RunStore`, not by hand-assembling the arrays.
 * Every derived field — `latestProgress`, `status`, `terminal`, the caps and
 * their drop counters — is then computed by the same code that computes it
 * for a live run, which is the only way "the finished page is the live page
 * at rest" is true rather than merely intended. A hand-built snapshot is free
 * to be subtly impossible; this one is not.
 *
 * `hydrate` is set from whether Delta has ANSWERED, not from whether it
 * answered with anything. An empty store with `hydrated: true` means "this
 * run really emitted nothing", which is a real answer — bayesian_ab is closed
 * form and can finish before it emits a single progress message. An empty
 * store with `hydrated: false` means "not read yet". A view is entitled to
 * tell those apart, so a page that has not fetched yet must not claim the
 * first one.
 */
export function assembleHistory(input: {
  runId: string;
  /** Raw pages, in cursor order. `unknown` rather than `Message[]`, because
   *  that is what they are: `BackfillResponse` types them as messages, but a
   *  backfilled row is missing `run_id` until `normalizeBackfilled` puts it
   *  back. Typing the input honestly is what stops that being forgotten. */
  pages: readonly { messages: readonly unknown[] }[];
  /** The registry row's status — authoritative, and the reason a run with no
   *  `status` message at all still renders as finished. */
  rowStatus: RunStatus | null;
}): AssembledHistory {
  const rows: unknown[] = [];
  for (const page of input.pages) rows.push(...page.messages);

  const { messages, unusable } = normalizeBackfilled(input.runId, rows);

  const store = new RunStore(input.runId);
  store.ingest(messages, { hydrate: input.pages.length > 0 });

  // A terminal run whose terminal `status` message never reached Delta is
  // still terminal; the registry row is what says so. Only applied when the
  // history did not already carry it, so a real message always wins.
  const beforeMark = store.getSnapshot();
  if (
    input.rowStatus !== null &&
    isTerminal(input.rowStatus) &&
    !beforeMark.terminal
  ) {
    store.markTerminal(input.rowStatus);
  }

  const snapshot = store.getSnapshot();
  return { snapshot, unusable, lastSeq: snapshot.lastSeq };
}

/* ------------------------------------------------------------------ *
 * Gaps — permanent, on this page
 * ------------------------------------------------------------------ */

/**
 * Holes in the loaded seq range.
 *
 * Interior only. A run's first backfilled seq is routinely not the run's
 * first seq — job-internal `client_visible: false` logs are filtered out
 * server-side — so treating "the lowest seq I have" as a hole would report a
 * gap on almost every healthy run.
 */
export function findGaps(seqs: readonly number[]): Gap[] {
  const sorted = [...new Set(seqs)].sort((a, b) => a - b);
  const gaps: Gap[] = [];
  for (let i = 1; i < sorted.length; i += 1) {
    const prev = sorted[i - 1];
    const current = sorted[i];
    if (prev === undefined || current === undefined) continue;
    if (current > prev + 1) gaps.push({ from: prev + 1, to: current - 1 });
  }
  return gaps;
}

/** Every seq the snapshot holds, across all four message types. */
export function snapshotSeqs(snapshot: RunSnapshot): number[] {
  const seqs: number[] = [];
  for (const list of [
    snapshot.logs,
    snapshot.progress,
    snapshot.statuses,
    snapshot.results,
  ]) {
    for (const message of list) seqs.push(message.seq);
  }
  return seqs;
}

export interface GapSummary {
  count: number;
  /** How many seq values are missing in total, not how many runs of them. */
  missing: number;
}

export function summariseGaps(gaps: readonly Gap[]): GapSummary {
  return {
    count: gaps.length,
    missing: gaps.reduce((sum, gap) => sum + (gap.to - gap.from + 1), 0),
  };
}

/* ------------------------------------------------------------------ *
 * Results completeness
 * ------------------------------------------------------------------ */

/**
 * `none`       — Delta holds no result message for this run at all.
 * `complete`   — a chunk with `final: true` exists.
 * `incomplete` — the run is over, everything is loaded, and no final chunk
 *                arrived. NOT "still arriving": nothing is arriving, the run
 *                has stopped. `streaming_results` cancelled between windows
 *                ends exactly here, and so does a run whose final write
 *                failed. The two are indistinguishable from the client, which
 *                is itself worth saying.
 * `unknown`    — not enough has been loaded (or the run has not finished) to
 *                make either claim honestly.
 */
export type ResultsCompleteness = "none" | "complete" | "incomplete" | "unknown";

export function resultsCompleteness(
  results: readonly ResultMessage[],
  context: { runTerminal: boolean; fullyLoaded: boolean },
): ResultsCompleteness {
  if (results.some((result) => result.final)) return "complete";
  if (!context.fullyLoaded) return "unknown";
  if (!context.runTerminal) return "unknown";
  return results.length === 0 ? "none" : "incomplete";
}

/** Rows written durably across every chunk. 0 is a real, reportable total. */
export function totalRowCount(results: readonly ResultMessage[]): number {
  return results.reduce((sum, result) => sum + result.row_count, 0);
}

/* ------------------------------------------------------------------ *
 * The state handed to the per-model view
 * ------------------------------------------------------------------ */

/**
 * What `state` a `ModelView` is rendered with here.
 *
 * The registry row's status, unchanged, and nothing else. There is no live
 * stream to derive from and `STARTING` is meaningless for a run that has
 * already happened, so the derivation the live page performs
 * (`run/runState.ts::deriveUiState`) collapses to the identity — which is the
 * point. `isSettled()` is then true for every terminal run, and each view
 * freezes into one flat terminal frame instead of animating.
 *
 * A `RUNNING` row is passed through as `RUNNING` even when nothing will ever
 * finish it. Substituting a terminal state would be a lie about a
 * `RunStatus`; the page says the run is stranded in prose, next to the
 * animation, instead.
 */
export function lockedViewState(rowStatus: RunStatus | null): UiRunState | null {
  return rowStatus;
}
