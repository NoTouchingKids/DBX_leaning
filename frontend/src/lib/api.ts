/**
 * HTTP contract.
 *
 * Hand-derived from `app/routes/runs.py`, `app/routes/stream.py`,
 * `app/routes/meta.py`, `app/repository.py` at commit 114f4bb.
 *
 * Every shape here was read out of the source. Where a capability does NOT
 * exist, it is called out as such rather than left for you to discover.
 */

import type { Message, RunStatus } from "./envelope";

/* ------------------------------------------------------------------ *
 * POST /api/runs — trigger
 * ------------------------------------------------------------------ */

export interface TriggerRequest {
  model: string;
  /**
   * Passed VERBATIM AND UNVALIDATED into the job's DBX_MODEL_CONFIG. The app
   * enforces nothing about its shape. Every guarantee about these fields is a
   * frontend invention — see `models.ts`, which is derived from each model's
   * own `cfg.get(...)` calls, not from a server-side schema.
   */
  config: Record<string, unknown>;
  /** Optional caller-supplied id. Omit and the server mints `run-<12 hex>`. */
  run_id?: string;
}

/** 202. The `registered: true` shape. */
export interface TriggerAccepted {
  run_id: string;
  job_run_id: string;
  model: string;
  status: "QUEUED";
  registered: true;
  /** `/api/runs/{run_id}/stream` — the server hands you the path. Use it. */
  stream: string;
}

/**
 * 202, degraded. The job IS RUNNING but the run_status row could not be
 * written. Note there is no `status` and no `stream` field here.
 *
 * Do NOT treat this as a failure — the run is live and the ids are valid, so
 * the user can still watch it. Do surface the warning: startup reconciliation
 * will never see this run, so it can strand in a non-terminal state forever.
 */
export interface TriggerUnregistered {
  run_id: string;
  job_run_id: string;
  model: string;
  registered: false;
  warning: string;
}

export type TriggerResponse = TriggerAccepted | TriggerUnregistered;

/**
 * Failure modes worth handling distinctly, all from source:
 *   400 — no job configured for that model (body names the triggerable ones)
 *   429 — `active_run_count() >= max_concurrent_runs` (default 5, Free
 *         Edition's account-wide ceiling). The body names the current count
 *         and the ceiling. This is the single most likely user-facing error
 *         on this platform; render the body text, not a generic message.
 *   502 — the Databricks Jobs API itself failed.
 */

/* ------------------------------------------------------------------ *
 * GET /api/runs — list
 * ------------------------------------------------------------------ */

/** Exactly the columns `repo.list_runs` SELECTs, plus the injected `live`. */
export interface RunRow {
  run_id: string;
  job_run_id: string | null;
  model: string;
  status: RunStatus;
  /** Free-form text the job last wrote. NOT a parsed metric. */
  detail: string | null;
  started_ts: string;
  updated_ts: string;
  /** From the `x-forwarded-email` header at trigger time. Nullable. */
  requested_by: string | null;
  /**
   * NOT a column. Injected per request as
   * `run_id in hub.job_sockets.run_ids` — a live WebSocket check.
   *
   * (status: "RUNNING", live: false) is the real failure mode on this
   * platform: the job died or the app restarted, and NOTHING will move that
   * row to a terminal state — there is no reaper. Surface it.
   */
  live: boolean;
}

export interface RunListResponse {
  count: number;
  runs: RunRow[];
}

export interface RunListQuery {
  /** 1..500, default 50. There is NO offset or cursor — this is a top-N
   *  window, not pagination. "Load more" is a refetch with a bigger limit. */
  limit?: number;
  /** Server-side, but ONE exact value only (`WHERE status = :status`).
   *  No `IN`, no multi-select. */
  status?: RunStatus;
  /** DOES NOT EXIST. There is no `model` query param on this endpoint.
   *  Filtering to one model is a client-side pass over the fetched window.
   *  Adding it server-side is a one-line WHERE extension and is the
   *  recommended change — see notes/gaps-and-corrections.md. */
  model?: never;
}

/** Ordering is `updated_ts DESC` — last-update, not start time. A
 *  long-running old run jumps back to the top every time it emits. Do not
 *  offer sort controls the server cannot honour. */

/* ------------------------------------------------------------------ *
 * GET /api/runs/{id} — detail snapshot
 * ------------------------------------------------------------------ */

export interface RunDetailResponse {
  /** `SELECT *` from run_status — the same columns as a list row, minus the
   *  injected `live`, which is a sibling field here rather than inline. */
  run: Omit<RunRow, "live">;
  /** Same live-WebSocket check as the list's `live`. This is exactly the
   *  signal the cancel button's enabled state depends on. */
  live: boolean;
  last_seq_seen: number | null;
}

/* ------------------------------------------------------------------ *
 * GET /api/runs/{id}/messages — explicit backfill
 * ------------------------------------------------------------------ */

export interface BackfillQuery {
  /** Exclusive lower bound. Default -1 (from the beginning). */
  after_seq?: number;
  /** 1..50_000. Defaults to the server's `backfill_page_size`. */
  limit?: number;
}

export interface BackfillResponse {
  run_id: string;
  after_seq: number;
  count: number;
  /** Already flattened by `_rehydrate()` — same shape as live SSE. */
  messages: Message[];
  /** A full page probably means more. Page by seq using `next_after_seq`. */
  more: boolean;
  next_after_seq: number;
}

/** Call this only when the client KNOWS it has a gap — not automatically on
 *  every reconnect. Reconnects are usually a gap of milliseconds; hitting the
 *  SQL warehouse on each one is the cost mistake this rewrite exists to
 *  avoid. A finished run is immutable: fetch once, cache forever. */

/* ------------------------------------------------------------------ *
 * POST /api/runs/{id}/cancel
 * ------------------------------------------------------------------ */

export interface CancelResponse {
  run_id: string;
  cancel_requested: true;
  requested_by: string;
}

/**
 * 409 when there is no live WebSocket to the job. The body carries
 * CANCEL_ESCAPE_HATCH:
 *
 *   "no live channel to this run; cancel it with
 *    `databricks jobs cancel-run --run-id <job_run_id>` (a hard kill: the job
 *    gets SIGTERM and keeps whatever results it already wrote)"
 *
 * Render the RESPONSE BODY, not a hardcoded client-side copy of that string —
 * it is a real instruction containing a real command, and it will drift.
 *
 * Enable the button exactly when `live === true` && !isTerminal(status).
 * Optimistically disable on success; a `status` message carrying CANCELLED
 * is the real confirmation.
 */

/* ------------------------------------------------------------------ *
 * GET /api/runs/{id}/stream — SSE
 * ------------------------------------------------------------------ */

/** Named events are ALREADY implemented server-side (`event: <type>` in
 *  `_event()`). `id:` is the message `seq`, so native `Last-Event-ID` resume
 *  works with no handshake — never hand-roll a `from_seq` opening message.
 *  The server also sends `retry: 2000` and `: keepalive` comment frames. */
export const SSE_EVENTS = ["log", "progress", "status", "result"] as const;

/**
 * THE KNOWN TRAP (from `frontend/README.md`, restated because it is the one
 * bug that will look like a healthy stream dying):
 *
 * The reconnect counter must count CONSECUTIVE failures and reset to zero on
 * every successful reconnect. A naive "give up after 3 tries" kills a
 * perfectly healthy stream a few minutes in, because the ingress cuts long
 * connections periodically (~120s per community reports; `/spike-sse` exists
 * to measure the real number).
 */

/* ------------------------------------------------------------------ *
 * Meta
 * ------------------------------------------------------------------ */

export interface ModelsResponse {
  models: Array<{ name: string; job_id: string }>;
  default_job_id: string | null;
}

export interface WhoamiResponse {
  email: string | null;
  user: string | null;
  user_id: string | null;
  authenticated: boolean;
  /** The server literally says: "cosmetic identity from the platform proxy;
   *  not an authorization boundary". Display it; never gate on it. */
  note: string;
}

export interface HealthzResponse {
  status: "ok" | "degraded";
  degraded: boolean;
  live_jobs: number;
  messages_ingested: number;
}
