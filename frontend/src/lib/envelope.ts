/**
 * Message envelope — the wire contract between job, app, and browser.
 *
 * Hand-derived from `shared/envelope.py` at commit 114f4bb (main, 2026-08-24).
 * `shared/` is frozen by `docs/parallelization-plan.md`; if it ever changes,
 * this file is the thing that must change with it.
 *
 * Two facts that shape everything downstream:
 *
 *  1. `app/repository.py::_rehydrate()` already flattens backfilled rows —
 *     `payload_json` / `fetch_hint_json` / `preview_json` are unpacked and
 *     numerics coerced SERVER-SIDE. A backfilled message and a live SSE
 *     message arrive in the same flat shape. Write ONE normalisation
 *     function, not two.
 *
 *  2. `app/routes/stream.py` already sets `event: <type>` on every SSE frame
 *     (verified in source — this is NOT an outstanding action item). Use
 *     `addEventListener('progress', …)` per type. Do not write a single
 *     `onmessage` with a type switch.
 */

export type MessageType = "log" | "progress" | "status" | "result";

export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR";

/**
 * The complete server-side status set. Note what is NOT here: `STARTING`.
 * See `UiRunState` below.
 */
export type RunStatus =
  | "QUEUED"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED"
  | "INFEASIBLE";

/**
 * Runtime companions to the unions above.
 *
 * TypeScript unions vanish at compile time, which makes them impossible to
 * check against the server's own JSON Schema. These arrays exist so
 * `envelope.contract.test.ts` can compare them to `schema/envelope.schema.json`
 * — generated from `shared/envelope.py` — and fail if the server gains a
 * status, level or message type this file has not been told about.
 *
 * `satisfies` keeps them honest in the other direction too: a member here
 * that is not in the union is a compile error.
 */
export const MESSAGE_TYPES = [
  "log",
  "progress",
  "status",
  "result",
] as const satisfies readonly MessageType[];

export const LOG_LEVELS = [
  "DEBUG",
  "INFO",
  "WARNING",
  "ERROR",
] as const satisfies readonly LogLevel[];

export const RUN_STATUSES = [
  "QUEUED",
  "RUNNING",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "INFEASIBLE",
] as const satisfies readonly RunStatus[];

/** Mirrors `TERMINAL_STATUSES` in envelope.py. Nothing further arrives for a run in one of these. */
export const TERMINAL_STATUSES = [
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "INFEASIBLE",
] as const satisfies readonly RunStatus[];

export function isTerminal(status: RunStatus): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status);
}

/**
 * The state the UI renders, which is NOT the same as `RunStatus`.
 *
 * `STARTING` is a CLIENT-ONLY optimistic sub-state: entered when
 * `POST /api/runs` returns 202, exited on the first real progress/status
 * message. It exists so the signature animations have a "spinning up" frame
 * to show instead of sitting on QUEUED. Never compare it against a value
 * that came off the wire, and never send it anywhere.
 */
export type UiRunState = RunStatus | "STARTING";

/** Fields every message carries. Frozen server-side — treat as immutable here too. */
export interface CommonFields {
  run_id: string;
  /** Monotonic per run, shared across ALL message types. Assigned by the job,
   *  known before the durable write — which is what lets live and backfilled
   *  records be reconciled. Also the SSE `id:`, hence `Last-Event-ID` resume. */
  seq: number;
  /** Epoch milliseconds. */
  ts: number;
}

export interface LogMessage extends CommonFields {
  type: "log";
  message: string;
  level: LogLevel;
  /** Open set in practice, NOT an enum: "model" (default), "job"
   *  (job/runner.py), "gurobi" (job/drivers/gurobi.py). Populate any filter
   *  UI from what a run actually emits — never hardcode the list. */
  source: string;
  /** Genuinely free text chosen per model: "input" / "build" / "solve" /
   *  "run" today. A new model may introduce new values without violating any
   *  contract. Populate filters dynamically. */
  phase: string;
  /** Already filtered server-side on the live path. A `false` value only
   *  reaches you via backfill. */
  client_visible: boolean;
}

export interface ProgressMessage extends CommonFields {
  type: "progress";
  elapsed_seconds: number;
  /** `null` is a REAL, CORRECT value — not a loading state. Always null for
   *  gurobi_scheduling; transiently null for mcmc / scenario / streaming.
   *  Render an indeterminate treatment, never a 0% bar. */
  percent_complete: number | null;
  /** Sanitised server-side: NaN and ±Infinity become null. */
  primary_metric: number | null;
  primary_metric_label: string | null;
  /** Model-specific extras. A generic view ignores it; a model page grows
   *  into it. See `models.ts` for the per-model shapes. */
  payload: Record<string, unknown>;
}

export interface StatusMessage extends CommonFields {
  type: "status";
  status: RunStatus;
  detail: string | null;
}

export interface ResultMessage extends CommonFields {
  type: "result";
  /** LTTB-downsampled, bounded at PREVIEW_MAX_POINTS server-side. A pointer
   *  and a preview — never the full result set, which lives in the model's
   *  own UC table under its own grants. */
  preview: Array<Record<string, unknown>>;
  /** Rows actually written durably. Always populated, INCLUDING 0 — which is
   *  how "succeeded, wrote 8,760 rows" is told apart from "succeeded, wrote
   *  nothing because the write failed". Surface a 0 rather than hiding it. */
  row_count: number;
  /** Enough to pull the full set on demand (table, keys). */
  fetch_hint: Record<string, unknown>;
  /** Which chunk of a multi-emission run. 0 for the common
   *  once-at-the-end case. DISTINCT from `seq`: two chunks may be
   *  chunk 0 and 1 but seq 40 and 91. */
  chunk_index: number;
  /** False while more chunks are coming. Results are complete only once a
   *  message with `final: true` has been seen. streaming_results is the model
   *  that actually exercises this. */
  final: boolean;
}

export type Message =
  | LogMessage
  | ProgressMessage
  | StatusMessage
  | ResultMessage;

/** Discriminated-union narrowing helpers — cheaper than repeating the check. */
export const isLog = (m: Message): m is LogMessage => m.type === "log";
export const isProgress = (m: Message): m is ProgressMessage => m.type === "progress";
export const isStatus = (m: Message): m is StatusMessage => m.type === "status";
export const isResult = (m: Message): m is ResultMessage => m.type === "result";
