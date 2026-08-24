/**
 * Typed fetch over the app's HTTP API.
 *
 * The shapes live in `api.ts`; this file is the transport plus the three
 * places where `api.ts` and `app/routes/runs.py` genuinely disagree. Where
 * they do, the server wins and the divergence is declared here rather than
 * papered over — `api.ts` is not editable from this track.
 *
 *  1. `started_ts` / `updated_ts` are BIGINT epoch MILLISECONDS, not ISO
 *     strings (`app/store.py::RunRecord`, written with `now_ms()`). Treating
 *     them as strings gives `Invalid Date` on every row.
 *  2. `GET /api/runs` DOES take `model` now — `repo.list_runs(limit, status,
 *     model)`, exposed as a `Query` param. `api.ts` marks it `never` and the
 *     gaps note lists it as missing; both predate the change. Filtering
 *     client-side over a top-N window is silently wrong once the window is
 *     full, so the server param is the one to use.
 *  3. `/healthz` returns `degraded` as a MAP of service -> reason (it is
 *     `hub.degraded`), not a boolean. An empty map is healthy. It also
 *     returns `protocol_schema_version`, which `api.ts` does not mention.
 *
 * Every error path here preserves the server's own message. On this platform
 * the two errors a user will actually hit — 429 at the concurrency ceiling
 * and 409 on cancel — both carry text that names real numbers and a real
 * command. A generic "something went wrong" throws that away.
 */

import type {
  BackfillResponse,
  CancelResponse,
  HealthzResponse,
  ModelsResponse,
  RunListResponse,
  RunRow,
  TriggerRequest,
  TriggerResponse,
  WhoamiResponse,
} from "./api";
import type { RunStatus } from "./envelope";

/* ------------------------------------------------------------------ *
 * Corrected response shapes
 * ------------------------------------------------------------------ */

/** `RunRow` with the timestamp columns typed as what the server sends. */
export type Run = Omit<RunRow, "started_ts" | "updated_ts"> & {
  started_ts: number;
  updated_ts: number;
};

export interface RunList extends Omit<RunListResponse, "runs"> {
  runs: Run[];
  /** Echoed back by the endpoint so a client can tell which filters the
   *  server actually applied, versus which it applied itself. */
  filters?: { status: string | null; model: string | null };
}

export interface RunDetail {
  run: Omit<Run, "live">;
  /** A live WebSocket check against the job, not a stored column. This — not
   *  the status — is what the cancel button's enabled state depends on. */
  live: boolean;
  last_seq_seen: number | null;
}

export interface Healthz extends Omit<HealthzResponse, "degraded"> {
  degraded: Record<string, string>;
  protocol_schema_version: number;
}

export interface ListRunsParams {
  /** 1..500, default 50. No offset, no cursor: this is a top-N window, so
   *  "load more" is a refetch with a bigger limit. */
  limit?: number;
  status?: RunStatus;
  model?: string;
}

/**
 * `POST /api/runs` normalised across its two documented 202 shapes.
 *
 * The registered/unregistered split matters because the degraded one is a
 * SUCCESS: the Databricks job is running, only the registry row is missing.
 * Rendering it as a failure would tell the user nothing started when
 * something did — and the run they cannot see is still holding one of five
 * account-wide task slots.
 */
export interface TriggerOutcome {
  run_id: string;
  job_run_id: string;
  model: string;
  /** Present when the run started but something about recording it did not.
   *  Never a reason to hide the run. */
  warning: string | null;
}

/* ------------------------------------------------------------------ *
 * Errors
 * ------------------------------------------------------------------ */

export class ApiError extends Error {
  readonly status: number;
  /** The server's own words, unedited. FastAPI puts `HTTPException`'s detail
   *  in `{"detail": ...}`; anything else falls back to the raw body. */
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`HTTP ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

async function readError(response: Response): Promise<ApiError> {
  const text = await response.text().catch(() => "");
  let detail = text;
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed !== null && typeof parsed === "object" && "detail" in parsed) {
      const raw = (parsed as { detail: unknown }).detail;
      detail = typeof raw === "string" ? raw : JSON.stringify(raw);
    }
  } catch {
    // Not JSON. The raw body is still the most informative thing available.
  }
  return new ApiError(response.status, detail || response.statusText);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body === undefined ? {} : { "Content-Type": "application/json" }),
      ...init?.headers,
    },
  });
  if (!response.ok) throw await readError(response);
  return (await response.json()) as T;
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

/* ------------------------------------------------------------------ *
 * Endpoints
 * ------------------------------------------------------------------ */

export function listRuns(params: ListRunsParams = {}, signal?: AbortSignal): Promise<RunList> {
  return request<RunList>(
    `/api/runs${query({ limit: params.limit, status: params.status, model: params.model })}`,
    { signal },
  );
}

export function getRun(runId: string, signal?: AbortSignal): Promise<RunDetail> {
  return request<RunDetail>(`/api/runs/${encodeURIComponent(runId)}`, { signal });
}

export async function triggerRun(body: TriggerRequest): Promise<TriggerOutcome> {
  const raw = await request<TriggerResponse & { job_run_id_stored?: boolean }>("/api/runs", {
    method: "POST",
    body: JSON.stringify(body),
  });

  // Two distinct partial failures, and the server signals them differently.
  // `registered: false` is the documented degraded 202. `job_run_id_stored:
  // false` is what the current handler actually emits when the launch worked
  // but the id could not be written back; there is no server-side message for
  // it, so this is the one place a warning string is written client-side.
  const warning =
    raw.registered === false
      ? raw.warning
      : raw.job_run_id_stored === false
        ? "the job launched, but its Databricks job-run id could not be stored — " +
          "cancel and reconciliation may not be able to find it"
        : null;

  return { run_id: raw.run_id, job_run_id: raw.job_run_id, model: raw.model, warning };
}

/** 409 carries `CANCEL_ESCAPE_HATCH` — a real `databricks jobs cancel-run`
 *  instruction. It arrives on `ApiError.detail`; render that, never a copy. */
export function cancelRun(runId: string): Promise<CancelResponse> {
  return request<CancelResponse>(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
  });
}

export function fetchBackfill(
  runId: string,
  params: { after_seq?: number; limit?: number } = {},
  signal?: AbortSignal,
): Promise<BackfillResponse> {
  return request<BackfillResponse>(
    `/api/runs/${encodeURIComponent(runId)}/messages${query({
      after_seq: params.after_seq,
      limit: params.limit,
    })}`,
    { signal },
  );
}

export function getModels(signal?: AbortSignal): Promise<ModelsResponse> {
  return request<ModelsResponse>("/api/models", { signal });
}

export function getWhoami(signal?: AbortSignal): Promise<WhoamiResponse> {
  return request<WhoamiResponse>("/api/whoami", { signal });
}

export function getHealthz(signal?: AbortSignal): Promise<Healthz> {
  return request<Healthz>("/healthz", { signal });
}
