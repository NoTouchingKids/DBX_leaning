/**
 * What the `live` field means for one row — the most valuable column on this
 * page, and the one that is not the status.
 *
 * `live` is not stored. `app/server/routes/runs.py::list_runs` injects it per request
 * as `run_id in hub.job_sockets.run_ids`: a check against the set of job
 * WebSockets currently connected to *this* app process. So the pair
 * (`status`, `live`) says something neither field says alone, and the four
 * combinations are genuinely four different things — which is why this is a
 * classification and not a boolean rendered twice.
 */

import { isStrandedRun } from "@/components/run/runState";
import type { Run } from "@/lib/apiClient";
import { isTerminal } from "@/lib/envelope";

export type Liveness =
  /** A job WebSocket is attached right now: progress is arriving. */
  | "connected"
  /** QUEUED with no socket. Normal — the job has not started attaching yet. */
  | "awaiting"
  /**
   * RUNNING with no socket. The job died, or the app restarted while it was
   * running. There is NO reaper: nothing will ever move this row to a
   * terminal status, and no endpoint in this API can. Surfaced as a warning;
   * deliberately given no action, because there is no action to give.
   */
  | "stranded"
  /** Terminal. `live` carries no information here and is not rendered. */
  | "finished";

export function runLiveness(row: Pick<Run, "status" | "live">): Liveness {
  // Terminal first: a row can be SUCCEEDED with its socket still open for the
  // moment, and "connected" would be a distinction without a difference.
  if (isTerminal(row.status)) return "finished";
  // `isStrandedRun` is the shared predicate — same one the model pages use to
  // decide a run can no longer be cancelled. One definition, two surfaces.
  if (isStrandedRun(row.status, row.live)) return "stranded";
  return row.live ? "connected" : "awaiting";
}

export function countStranded(rows: readonly Run[]): number {
  return rows.filter((row) => runLiveness(row) === "stranded").length;
}

/**
 * Duration, in seconds, or null where there is nothing honest to show.
 *
 * `updated_ts - started_ts`, both epoch MILLISECONDS (BIGINT columns written
 * with `now_ms()` — not ISO strings, whatever an older contract said). Exact
 * for a terminal row. For a running one it measures to the last heartbeat and
 * not to now, which is why the page marks those with a trailing `+` rather
 * than ticking a number the data does not support.
 *
 * QUEUED is null, not zero: `updated_ts == started_ts` there, and `00:00`
 * would read as "finished instantly".
 */
export function rowDurationSeconds(row: Pick<Run, "status" | "started_ts" | "updated_ts">): number | null {
  if (row.status === "QUEUED") return null;
  const seconds = (row.updated_ts - row.started_ts) / 1000;
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}
