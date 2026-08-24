/**
 * Deriving what the page shows from the two sources that know anything.
 *
 * There are two, and they answer different questions:
 *
 *  - the live stream (`useRunStream`) — the freshest status, but only exists
 *    while the app has a channel to the job;
 *  - `GET /api/runs/{id}` — the registry row plus `live`, a WebSocket check
 *    the stream cannot tell you about.
 *
 * Neither subsumes the other, and the interesting states are exactly the ones
 * where they disagree.
 */

import { isTerminal, type RunStatus, type UiRunState } from "@/lib/envelope";

export interface UiStateInput {
  /** Latest status seen on the SSE stream. */
  streamStatus: RunStatus | null;
  /** `run.status` from the registry row. */
  rowStatus: RunStatus | null;
  /** The run id a trigger just returned a 202 for, if any. */
  optimisticStartingFor: string | null;
  runId: string | null;
  /** Whether anything at all has arrived on the stream for this run. */
  sawAnyMessage: boolean;
}

/**
 * `STARTING` is client-only. It is not a `RunStatus`, the server has never
 * heard of it, and it must never be compared against a wire value — it exists
 * purely to fill the gap between a 202 and the first real message, which on a
 * cold Databricks job can be tens of seconds of otherwise blank page.
 */
export function deriveUiState(input: UiStateInput): UiRunState | null {
  if (input.streamStatus !== null) return input.streamStatus;
  if (
    input.optimisticStartingFor !== null &&
    input.optimisticStartingFor === input.runId &&
    !input.sawAnyMessage &&
    (input.rowStatus === null || input.rowStatus === "QUEUED")
  ) {
    return "STARTING";
  }
  return input.rowStatus;
}

export interface CancelInput {
  /** `live` from `GET /api/runs/{id}`. Undefined while unknown. */
  live: boolean | undefined;
  state: UiRunState | null;
  /** Set optimistically once a cancel has been accepted. */
  cancelRequested: boolean;
}

/**
 * Cancel is enabled exactly when there is a live WebSocket to the job and the
 * run has not finished.
 *
 * `live` — not the status — is the deciding field: cancel is forwarded over
 * that socket and nothing else, so a `RUNNING` run with no socket cannot be
 * cancelled through this app at all. Offering the button anyway would produce
 * a 409 every time.
 */
export function canCancel({ live, state, cancelRequested }: CancelInput): boolean {
  if (cancelRequested) return false;
  if (live !== true) return false;
  if (state === null) return false;
  if (state === "STARTING") return true;
  return !isTerminal(state);
}

/**
 * `RUNNING` with no live socket: the job died, or the app restarted while it
 * was running. There is no reaper — nothing will ever move this row to a
 * terminal status, and no endpoint in this API can fix it. Worth showing;
 * not worth offering an action for, because there isn't one.
 *
 * `QUEUED` with no socket is normal — the job has not attached yet.
 */
export function isStrandedRun(state: UiRunState | null, live: boolean | undefined): boolean {
  return state === "RUNNING" && live === false;
}
