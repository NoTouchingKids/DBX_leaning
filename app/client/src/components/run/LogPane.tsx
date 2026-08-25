/**
 * The log pane: filters, windowed rendering, auto-follow.
 *
 * **Virtualised by hand, deliberately.** The store caps a run at 5,000 log
 * lines and every line is a single row of fixed height, which is the one case
 * windowing is trivial for: `startIndex = floor(scrollTop / ROW)` and two
 * spacer divs. That is about forty lines of code with no dependency, no
 * measurement cache and no resize observer. Pulling in a virtual-list library
 * for it would add a package to a bundle that has to ship inside a Databricks
 * App, to solve a problem this shape does not have.
 *
 * **Auto-follow stops the moment the user scrolls up.** It resumes only when
 * they scroll back to the bottom or press "jump to latest" — no timeout, no
 * heuristic. The bottom-detection doubles as the resume signal, so a
 * programmatic scroll-to-bottom naturally re-arms following without needing a
 * flag to distinguish it from a user gesture.
 */

import { useLayoutEffect, useMemo, useRef, useState } from "react";

import { LOG_LEVELS, type LogLevel, type LogMessage } from "@/lib/envelope";
import { formatCount } from "@/lib/format";
import { deriveLogFacets, filterLogs, type LogFilterState } from "./logFilters";

const ROW_HEIGHT = 22;
const VIEWPORT_HEIGHT = 380;
const OVERSCAN = 8;

const LEVEL_TONE: Record<LogLevel, string> = {
  DEBUG: "text-faint",
  INFO: "text-dim",
  WARNING: "text-warn",
  ERROR: "text-bad",
};

function stamp(ts: number): string {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(
    d.getSeconds(),
  ).padStart(2, "0")}`;
}

export function LogPane({
  logs,
  droppedLogs,
}: {
  logs: readonly LogMessage[];
  /** Logs the store evicted at its cap. Shown rather than hidden: a partial
   *  history that does not say it is partial is worse than none. */
  droppedLogs: number;
}) {
  const [filters, setFilters] = useState<LogFilterState>(() => ({
    levels: new Set<LogLevel>(),
    source: "",
    phase: "",
    search: "",
  }));
  const [following, setFollowing] = useState(true);
  const [scrollTop, setScrollTop] = useState(0);
  const viewport = useRef<HTMLDivElement>(null);

  const facets = useMemo(() => deriveLogFacets(logs), [logs]);

  // A selected source or phase that this run has not (yet) emitted falls back
  // to "all" rather than filtering everything away. Both lists are derived
  // from the messages, and messages arrive over time — a value selected while
  // watching one run must not blank the pane when the page switches to
  // another that has not produced it. Resolved during render rather than by
  // resetting state from an effect, which would flash the unfiltered list.
  const effective = useMemo<LogFilterState>(
    () => ({
      ...filters,
      source: facets.sources.includes(filters.source) ? filters.source : "",
      phase: facets.phases.includes(filters.phase) ? filters.phase : "",
    }),
    [filters, facets],
  );
  const visible = useMemo(() => filterLogs(logs, effective), [logs, effective]);

  useLayoutEffect(() => {
    if (!following) return;
    const node = viewport.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [visible.length, following]);

  const total = visible.length * ROW_HEIGHT;
  const start = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const end = Math.min(
    visible.length,
    Math.ceil((scrollTop + VIEWPORT_HEIGHT) / ROW_HEIGHT) + OVERSCAN,
  );
  const window_ = visible.slice(start, end);

  function toggleLevel(level: LogLevel) {
    setFilters((f) => {
      const levels = new Set(f.levels);
      if (levels.has(level)) levels.delete(level);
      else levels.add(level);
      return { ...f, levels };
    });
  }

  return (
    <div className="rounded-[10px] border border-edge bg-raised p-4">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <input
          type="search"
          value={filters.search}
          onChange={(event) => setFilters((f) => ({ ...f, search: event.target.value }))}
          placeholder="search logs…"
          aria-label="Search log messages"
          className="min-w-32 flex-1 rounded-md border border-edge bg-paper px-2 py-1 font-mono text-[0.72rem]"
        />
        {/* source and phase options come from the run, never a hardcoded list */}
        <label className="flex items-center gap-1 text-[0.68rem] text-dim">
          <span className="sr-only">Source</span>
          <select
            value={effective.source}
            onChange={(event) => setFilters((f) => ({ ...f, source: event.target.value }))}
            aria-label="Filter by source"
            className="rounded-md border border-edge bg-paper px-1.5 py-1 font-mono text-[0.68rem]"
          >
            <option value="">source: all</option>
            {facets.sources.map((source) => (
              <option key={source} value={source}>
                {source}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1 text-[0.68rem] text-dim">
          <span className="sr-only">Phase</span>
          <select
            value={effective.phase}
            onChange={(event) => setFilters((f) => ({ ...f, phase: event.target.value }))}
            aria-label="Filter by phase"
            className="rounded-md border border-edge bg-paper px-1.5 py-1 font-mono text-[0.68rem]"
          >
            <option value="">phase: all</option>
            {facets.phases.map((phase) => (
              <option key={phase} value={phase}>
                {phase}
              </option>
            ))}
          </select>
        </label>
        <label className="ml-auto flex cursor-pointer items-center gap-1.5 text-[0.68rem] text-dim">
          <input
            type="checkbox"
            checked={following}
            onChange={(event) => {
              setFollowing(event.target.checked);
              if (event.target.checked) {
                const node = viewport.current;
                if (node) node.scrollTop = node.scrollHeight;
              }
            }}
            className="h-3.5 w-3.5 accent-[var(--c-accent)]"
          />
          Follow live
        </label>
      </div>

      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        {/* level IS a closed enum in the envelope, so this list is safe to fix */}
        {LOG_LEVELS.map((level) => {
          const on = filters.levels.has(level);
          return (
            <button
              key={level}
              type="button"
              aria-pressed={on}
              onClick={() => toggleLevel(level)}
              className={
                `cursor-pointer rounded-full border px-2 py-[.1rem] font-mono text-[0.65rem] ` +
                (on ? "border-accent text-accent" : "border-edge text-dim")
              }
            >
              {level}
              <span className="ml-1 opacity-60">{facets.levelCounts[level]}</span>
            </button>
          );
        })}
        <span className="ml-auto font-mono text-[0.66rem] text-faint">
          {formatCount(visible.length)} of {formatCount(logs.length)} lines
          {droppedLogs > 0 && ` · ${formatCount(droppedLogs)} dropped at the client cap`}
        </span>
      </div>

      <div className="relative">
        <div
          ref={viewport}
          onScroll={(event) => {
            const node = event.currentTarget;
            setScrollTop(node.scrollTop);
            // 4px of slack: sub-pixel layout means scrollTop rarely lands
            // exactly on scrollHeight - clientHeight.
            setFollowing(node.scrollHeight - node.scrollTop - node.clientHeight < 4);
          }}
          style={{ height: VIEWPORT_HEIGHT }}
          className="overflow-auto rounded-md border border-edge bg-paper px-2 py-1 font-mono text-[0.72rem] leading-[22px]"
          role="log"
          aria-label="Run log"
        >
          {visible.length === 0 ? (
            <div className="py-6 text-center text-[0.72rem] text-faint">
              {logs.length === 0 ? "no log lines yet" : "no lines match these filters"}
            </div>
          ) : (
            <div style={{ height: total }} className="relative">
              <div style={{ transform: `translateY(${start * ROW_HEIGHT}px)` }}>
                {window_.map((log) => (
                  <div
                    key={log.seq}
                    style={{ height: ROW_HEIGHT }}
                    className="flex items-center gap-2 overflow-hidden whitespace-nowrap"
                    title={log.message}
                  >
                    <span className="flex-none text-faint">{stamp(log.ts)}</span>
                    <span className={`w-[4.2em] flex-none ${LEVEL_TONE[log.level]}`}>
                      {log.level}
                    </span>
                    <span className="flex-none text-faint">
                      [{log.source}/{log.phase}]
                    </span>
                    <span className="truncate">{log.message}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {!following && visible.length > 0 && (
          <button
            type="button"
            onClick={() => {
              const node = viewport.current;
              if (node) node.scrollTop = node.scrollHeight;
              setFollowing(true);
            }}
            className="absolute right-4 bottom-3 cursor-pointer rounded-full border border-accent bg-accent-soft px-3 py-1 text-[0.68rem] font-semibold text-accent-ink"
          >
            ↓ jump to latest
          </button>
        )}
      </div>
    </div>
  );
}
