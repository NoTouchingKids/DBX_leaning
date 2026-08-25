/** Display formatting. Nothing here decides anything; it only renders. */

/** The em dash used everywhere a value is genuinely absent. One constant so
 *  "missing" looks the same in a chip, a table cell and a stat. */
export const EMPTY = "—";

const METRIC_FMT = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 6 });
const INT_FMT = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });

/**
 * A `primary_metric` value. Null is a real value the server produces (it
 * sanitises NaN and +/-Infinity to null), so it gets the empty marker rather
 * than "0" or a spinner.
 */
export function formatMetric(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EMPTY;
  return METRIC_FMT.format(value);
}

export function formatCount(value: number): string {
  return INT_FMT.format(value);
}

/** Seconds -> `mm:ss` under an hour, `h:mm:ss` over. Monospace-friendly. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return EMPTY;
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** Epoch ms -> local clock time. `started_ts` and `updated_ts` are BIGINT
 *  epoch-millisecond columns, not ISO strings — see `app/server/store.py`. */
export function formatClock(epochMs: number | null | undefined): string {
  if (!epochMs) return EMPTY;
  return new Date(epochMs).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatDateTime(epochMs: number | null | undefined): string {
  if (!epochMs) return EMPTY;
  const d = new Date(epochMs);
  const today = new Date();
  const sameDay =
    d.getFullYear() === today.getFullYear() &&
    d.getMonth() === today.getMonth() &&
    d.getDate() === today.getDate();
  return sameDay
    ? formatClock(epochMs)
    : d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}

/** A run id is `run-<12 hex>`; the middle carries no information a human
 *  reads, so the head and tail are what get shown next to a copy button. */
export function truncateId(runId: string, head = 10, tail = 4): string {
  if (runId.length <= head + tail + 1) return runId;
  return `${runId.slice(0, head)}…${runId.slice(-tail)}`;
}

/** Initials for the sidebar's icon rail. Derived from the label so the nav
 *  scales to however many models `MODEL_SPECS` grows to without a per-model
 *  icon table to keep in step. */
export function initials(label: string): string {
  const words = label.split(/[\s_/-]+/u).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return (words[0] ?? "").slice(0, 2).toUpperCase();
  return words
    .slice(0, 2)
    .map((w) => (w[0] ?? "").toUpperCase())
    .join("");
}
