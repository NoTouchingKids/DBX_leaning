/**
 * The two derivations `streaming_results` needs, as pure functions.
 *
 * Both are places the view can be quietly wrong, which is why they are here
 * and not inline in a component:
 *
 *  1. **Chunk accumulation.** This is the only model that emits `result` more
 *     than once per run. Chunks APPEND — the last one does not supersede the
 *     ones before it — and results are complete only once a message with
 *     `final: true` has been seen. A run cancelled between windows returns
 *     without emitting a final chunk at all, so "no final flag" and "still
 *     arriving" are not the same statement.
 *
 *  2. **Window placement.** The window's position on the timeline is the one
 *     thing in this model's signature that is state rather than decoration:
 *     it steps with `windows_done`. Deriving it from the counts, in one
 *     testable function, is what keeps that claim true.
 */

import type { ProgressMessage, ResultMessage } from "@/lib/envelope";
import { payloadOf } from "@/components/models/contract";
import type { StreamingProgressPayload } from "@/lib/models";

/** Segments on the track. A fixed count, because the real window count
 *  depends on the config (`range(window, len(series) - horizon + 1, step)`)
 *  and at the defaults it is in the thirties, not twelve. */
export const TIMELINE_SEGMENTS = 12;
/** How many segments the window covers. Purely how wide a window looks. */
export const WINDOW_SEGMENTS = 3;

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

export interface WindowPlacement {
  windowsDone: number | null;
  windowsTotal: number | null;
  /** Index into the series the last reported window forecast from. */
  origin: number | null;
  /** Segment the window's left edge sits on. Null when nothing is known. */
  start: number | null;
  /** Segments the run has finished, 0..TIMELINE_SEGMENTS. */
  segmentsDone: number;
  /** True when one window is exactly one step of the window's position — i.e.
   *  the run has few enough windows for the track to show each of them. */
  lockstep: boolean;
}

export const NO_WINDOW: WindowPlacement = {
  windowsDone: null,
  windowsTotal: null,
  origin: null,
  start: null,
  segmentsDone: 0,
  lockstep: false,
};

/**
 * Map `windows_done` onto the track.
 *
 * The window sits over the window that was just completed: the first report
 * puts it at the left edge, the last at the right edge, and the interval is
 * split evenly between them. A window three segments wide cannot start past
 * `TIMELINE_SEGMENTS - WINDOW_SEGMENTS`, so that is the number of places
 * there are to put it — up to that many windows each get their own step, and
 * beyond it several windows share one. `lockstep` says which of those two a
 * viewer is looking at, because "the window steps once per window" is only
 * literally true for the smaller runs.
 */
export function placeWindow(latest: ProgressMessage | null): WindowPlacement {
  const payload = payloadOf<StreamingProgressPayload>(latest);
  const done = finite(payload.windows_done);
  const total = finite(payload.windows_total);
  const origin = finite(payload.origin);

  if (done === null || total === null || total <= 0) {
    return { ...NO_WINDOW, windowsDone: done, windowsTotal: total, origin };
  }

  const lastStart = TIMELINE_SEGMENTS - WINDOW_SEGMENTS;
  return {
    windowsDone: done,
    windowsTotal: total,
    origin,
    start: clamp(Math.floor((lastStart * (done - 1)) / Math.max(1, total - 1)), 0, lastStart),
    segmentsDone: clamp(Math.floor((TIMELINE_SEGMENTS * done) / total), 0, TIMELINE_SEGMENTS),
    lockstep: total <= lastStart + 1,
  };
}

export interface WindowPoint {
  /** Absolute position in the series — `origin + step`. A chunk's rows all
   *  share an origin, so plotting `step` alone would stack every window on
   *  top of the last. */
  x: number;
  origin: number;
  predicted: number | null;
  actual: number | null;
}

export interface ChunkView {
  /** Deduplicated by `chunk_index`, in chunk order. */
  chunks: readonly ResultMessage[];
  /** Rows written durably, summed across chunks. A chunk reporting 0 is
   *  counted, not skipped: zero is a real answer. */
  totalRows: number;
  /** A `final: true` chunk has been seen. Only then are results complete. */
  complete: boolean;
  /** Chunk indices below the highest one seen that never arrived. */
  missing: readonly number[];
  /** Chunks whose `row_count` is 0 — reported, never rendered as emptiness. */
  emptyChunks: readonly number[];
  points: readonly WindowPoint[];
}

export const NO_CHUNKS: ChunkView = {
  chunks: [],
  totalRows: 0,
  complete: false,
  missing: [],
  emptyChunks: [],
  points: [],
};

/**
 * Accumulate every `result` message this run has produced.
 *
 * Deduplicated on `chunk_index`, not on `seq`. The store already drops a
 * message whose seq it has seen, which covers the live/backfill overlap; this
 * covers the other one — the same chunk re-emitted under a new seq, which a
 * job retry would produce and which would otherwise double every row in it.
 * The first copy seen wins, and `final` is true if ANY copy carried it.
 */
export function accumulateChunks(results: readonly ResultMessage[]): ChunkView {
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

  const points: WindowPoint[] = [];
  for (const chunk of chunks) {
    for (const row of chunk.preview) {
      const origin = finite(row["origin"]);
      const step = finite(row["step"]);
      if (origin === null || step === null) continue;
      points.push({
        x: origin + step,
        origin,
        predicted: finite(row["predicted"]),
        actual: finite(row["actual"]),
      });
    }
  }
  points.sort((a, b) => a.x - b.x);

  return {
    chunks,
    totalRows: chunks.reduce((sum, chunk) => sum + chunk.row_count, 0),
    complete,
    missing,
    emptyChunks: chunks.filter((c) => c.row_count === 0).map((c) => c.chunk_index),
    points,
  };
}

export type ArrivalState = "none" | "arriving" | "complete" | "stopped";

/**
 * How to describe the results, given the run is or is not over.
 *
 * `stopped` is the case worth separating: a cancelled or failed run keeps
 * every chunk it already emitted, but no `final: true` will ever arrive, so
 * saying "still arriving" would be waiting for something that is not coming.
 */
export function arrivalState(view: ChunkView, settled: boolean): ArrivalState {
  if (view.complete) return "complete";
  if (view.chunks.length === 0) return settled ? "stopped" : "none";
  return settled ? "stopped" : "arriving";
}
