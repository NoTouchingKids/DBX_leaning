/**
 * Log filtering, and where the filter options come from.
 *
 * `level` is a closed enum in the envelope and safe to hardcode. `source` and
 * `phase` are NOT: `source` is open in practice ("model", "job", "gurobi",
 * and whatever the next driver calls itself) and `phase` is free text each
 * model picks for itself. A hardcoded list of either would quietly hide log
 * lines from any model that chose a value nobody had written down yet — the
 * worst kind of filter bug, because the UI looks like it is working.
 *
 * So the options are derived from the messages this run actually emitted.
 */

import { LOG_LEVELS, type LogLevel, type LogMessage } from "@/lib/envelope";

export interface LogFilterState {
  /** Empty set means "no level filter", not "hide everything". */
  levels: ReadonlySet<LogLevel>;
  source: string;
  phase: string;
  search: string;
}

export const EMPTY_FILTERS: LogFilterState = {
  levels: new Set<LogLevel>(),
  source: "",
  phase: "",
  search: "",
};

export interface LogFacets {
  sources: string[];
  phases: string[];
  /** Which levels this run has actually produced, for counts on the chips. */
  levelCounts: Record<LogLevel, number>;
}

export function deriveLogFacets(logs: readonly LogMessage[]): LogFacets {
  const sources = new Set<string>();
  const phases = new Set<string>();
  const levelCounts: Record<LogLevel, number> = { DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0 };

  for (const log of logs) {
    if (log.source) sources.add(log.source);
    if (log.phase) phases.add(log.phase);
    if (LOG_LEVELS.includes(log.level)) levelCounts[log.level] += 1;
  }

  return {
    sources: [...sources].sort(),
    phases: [...phases].sort(),
    levelCounts,
  };
}

/**
 * Filter, and drop duplicate `seq` values on the way through.
 *
 * The dedupe is defensive, not decorative. `RunStore.ingest` appends without
 * checking, and the transport re-sends a run's whole IndexedDB history as a
 * `hydrate` batch every time the run is subscribed to — so navigating away
 * from a run page and back within the same session appends a second copy of
 * every line already on screen. `seq` is unique per run across all message
 * types, so keeping the first occurrence is exact rather than heuristic.
 */
export function filterLogs(
  logs: readonly LogMessage[],
  filters: LogFilterState,
): LogMessage[] {
  const needle = filters.search.trim().toLowerCase();
  const seen = new Set<number>();
  const out: LogMessage[] = [];

  for (const log of logs) {
    if (seen.has(log.seq)) continue;
    seen.add(log.seq);
    if (filters.levels.size > 0 && !filters.levels.has(log.level)) continue;
    if (filters.source !== "" && log.source !== filters.source) continue;
    if (filters.phase !== "" && log.phase !== filters.phase) continue;
    if (needle !== "" && !log.message.toLowerCase().includes(needle)) continue;
    out.push(log);
  }
  return out;
}
