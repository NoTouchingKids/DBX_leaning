/**
 * The one normalisation function, for live SSE frames and backfilled rows
 * alike.
 *
 * `app/repository.py::_rehydrate()` already unpacks `payload_json` /
 * `fetch_hint_json` / `preview_json` and coerces the numerics server-side, so
 * both paths arrive in the same flat shape. Writing two normalisers is the
 * documented mistake here; this file exists so there is nowhere to put the
 * second one.
 *
 * It never throws. A worker that dies on one malformed frame takes the whole
 * run's stream with it, so an unusable frame becomes `null` and is counted,
 * not raised.
 */

import {
  LOG_LEVELS,
  MESSAGE_TYPES,
  RUN_STATUSES,
  type LogLevel,
  type Message,
  type MessageType,
  type RunStatus,
} from "@/lib/envelope";

function asNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  // Delta/JSON round-trips can hand back a numeric string; the server coerces,
  // but this is the last line before the data reaches React.
  if (typeof value === "string" && value.trim() !== "") {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/** `null` is a real value for these — never a "not loaded yet" sentinel. */
function asNullableNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  return asNumber(value);
}

function asString(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function asNullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asBool(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function isMessageType(value: unknown): value is MessageType {
  return (MESSAGE_TYPES as readonly string[]).includes(value as string);
}

function isLogLevel(value: unknown): value is LogLevel {
  return (LOG_LEVELS as readonly string[]).includes(value as string);
}

/**
 * Exported because the UI needs the same question answered about a status it
 * read from `GET /api/runs`. An unknown status is not coerced to something
 * plausible — a server that grows a seventh status should show as unknown,
 * not silently render as FAILED.
 */
export function isKnownStatus(value: unknown): value is RunStatus {
  return (RUN_STATUSES as readonly string[]).includes(value as string);
}

/**
 * @param raw a parsed SSE `data` payload, or one element of a backfill
 *            response's `messages`.
 * @returns the message, or `null` if it is missing something without which it
 *          cannot be stored or ordered (`run_id`, `seq`, a known `type`).
 */
export function normalizeMessage(raw: unknown): Message | null {
  if (typeof raw !== "object" || raw === null) return null;
  const r = raw as Record<string, unknown>;

  const type = r.type;
  if (!isMessageType(type)) return null;

  const run_id = typeof r.run_id === "string" ? r.run_id : null;
  const seq = asNumber(r.seq);
  if (!run_id || seq === null) return null;

  // Missing ts is survivable in a way missing seq is not: ordering comes from
  // seq, ts only drives the display clock.
  const common = { run_id, seq, ts: asNumber(r.ts) ?? 0 };

  switch (type) {
    case "log":
      return {
        ...common,
        type: "log",
        message: asString(r.message, ""),
        // Defaults mirror the Pydantic model's, so a frame that omitted a
        // defaulted field lands where the server would have put it.
        level: isLogLevel(r.level) ? r.level : "INFO",
        source: asString(r.source, "model"),
        phase: asString(r.phase, "run"),
        client_visible: asBool(r.client_visible, true),
      };

    case "progress":
      return {
        ...common,
        type: "progress",
        elapsed_seconds: asNumber(r.elapsed_seconds) ?? 0,
        percent_complete: asNullableNumber(r.percent_complete),
        primary_metric: asNullableNumber(r.primary_metric),
        primary_metric_label: asNullableString(r.primary_metric_label),
        payload: asRecord(r.payload),
      };

    case "status": {
      // A status whose status is unrecognisable cannot drive the state
      // machine, and guessing would be worse than dropping it.
      if (!isKnownStatus(r.status)) return null;
      return {
        ...common,
        type: "status",
        status: r.status,
        detail: asNullableString(r.detail),
      };
    }

    case "result":
      return {
        ...common,
        type: "result",
        preview: Array.isArray(r.preview) ? r.preview.map(asRecord) : [],
        // 0 is meaningful ("wrote nothing"); the fallback is for a frame that
        // omitted the field entirely, which is a different thing again.
        row_count: asNumber(r.row_count) ?? 0,
        fetch_hint: asRecord(r.fetch_hint),
        chunk_index: asNumber(r.chunk_index) ?? 0,
        final: asBool(r.final, true),
      };
  }
}

/** Parses an SSE frame's `data` string. Same no-throw contract. */
export function parseFrame(data: string): Message | null {
  try {
    return normalizeMessage(JSON.parse(data));
  } catch {
    return null;
  }
}
