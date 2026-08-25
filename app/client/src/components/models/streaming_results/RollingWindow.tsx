/**
 * The `streaming_results` signature: a window sliding across the series.
 *
 * The exception to this app's rule about signature animations. Everywhere
 * else, positions during a run are decorative and the honesty note says so.
 * Here the window's position is derived entirely from `windows_done` — it
 * moves when, and only when, a real window finished and its chunk of results
 * went out. Nothing in this component runs on a timer.
 *
 * The track is a fixed twelve segments while the real window count comes from
 * the config, so the mapping is a proportion rather than one segment per
 * window (see `placeWindow`). `lockstep` reports which of those a viewer is
 * getting, because "the window steps once per window" is only literally true
 * for the smaller runs.
 */

import { isSettled, type ModelViewProps } from "@/components/models/contract";
import { usePrefersReducedMotion } from "@/components/models/useReducedMotion";
import type { UiRunState } from "@/lib/envelope";
import { DOT_COLOR } from "@/components/ui/runStateStyles";
import { EMPTY, formatCount } from "@/lib/format";

import { placeWindow, TIMELINE_SEGMENTS, WINDOW_SEGMENTS } from "./streamingModel";

const TERMINAL_SEGMENT: Partial<Record<UiRunState, string>> = {
  SUCCEEDED: "bg-good-soft border-good",
  FAILED: "bg-bad-soft border-bad",
  CANCELLED: "bg-idle-soft border-idle",
  INFEASIBLE: "bg-warn-soft border-warn",
};

const CAPTION: Record<string, [string, string]> = {
  none: ["No run selected", "Trigger a backtest to watch it roll"],
  QUEUED: ["Queued", "Waiting for compute"],
  STARTING: ["Starting backtest", "Preparing the first window"],
  RUNNING: ["Backtesting windows", "Rolling the origin forward"],
  SUCCEEDED: ["Backtest complete", "Every window forecast and scored"],
  FAILED: ["Backtest failed", "The run did not complete"],
  CANCELLED: ["Backtest cancelled", "Stopped between windows; earlier chunks stand"],
  INFEASIBLE: ["Backtest reported infeasible", "No window could be scored"],
};

function captionFor(state: UiRunState | null): [string, string] {
  return CAPTION[state ?? "none"] ?? CAPTION["none"] ?? ["", ""];
}

export function RollingWindow({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const settled = isSettled(state);
  const place = placeWindow(snapshot.latestProgress);
  const [line1, line2] = captionFor(state);

  const terminalSegment = settled ? TERMINAL_SEGMENT[state ?? "SUCCEEDED"] : undefined;
  // STARTING parks the window at the left: a spin-up frame, not a report.
  const start = settled ? null : (place.start ?? (state === "STARTING" ? 0 : null));
  const span = Math.min(WINDOW_SEGMENTS, TIMELINE_SEGMENTS - (start ?? 0));

  const label = settled
    ? `Rolling backtest ${state}`
    : place.windowsDone === null
      ? "Rolling backtest, no windows reported yet"
      : `Rolling backtest, ${place.windowsDone} of ${place.windowsTotal ?? "?"} windows complete`;

  return (
    <div className="p-4">
      <div className="mb-3 flex items-center gap-2.5">
        <span
          className={
            `inline-block h-2 w-2 shrink-0 rounded-full bg-current ${DOT_COLOR[state ?? "QUEUED"]} ` +
            (state === "RUNNING" ? "live-dot" : "")
          }
        />
        <div>
          <div className="text-[0.82rem] font-semibold">{line1}</div>
          <div className="text-[0.72rem] text-dim">{line2}</div>
        </div>
      </div>

      <div role="img" aria-label={label} className="relative flex h-[2.1rem] gap-[3px] px-[2px]">
        {Array.from({ length: TIMELINE_SEGMENTS }, (_, index) => (
          <div
            key={index}
            className={
              "flex-1 rounded-[3px] border transition-colors duration-300 motion-reduce:transition-none " +
              (terminalSegment ??
                (index < place.segmentsDone ? "bg-info-soft border-info" : "bg-paper border-edge"))
            }
          />
        ))}
        {start !== null && (
          <div
            className={
              "pointer-events-none absolute -top-1 -bottom-1 rounded-[5px] border-2 border-accent " +
              (reduced ? "" : "transition-[left,width] duration-500 ease-out")
            }
            style={{
              left: `${(start / TIMELINE_SEGMENTS) * 100}%`,
              width: `${(span / TIMELINE_SEGMENTS) * 100}%`,
            }}
          />
        )}
      </div>

      <div className="mt-3 flex flex-wrap justify-between gap-x-5 gap-y-1 font-mono text-[0.7rem] text-dim">
        <span>
          windows{" "}
          <span className="font-semibold text-ink">
            {place.windowsDone === null ? EMPTY : formatCount(place.windowsDone)}
            {place.windowsTotal === null ? "" : ` / ${formatCount(place.windowsTotal)}`}
          </span>
        </span>
        <span>
          origin{" "}
          <span className="font-semibold text-ink">
            {place.origin === null ? EMPTY : formatCount(place.origin)}
          </span>
        </span>
        <span className="text-faint">
          {place.windowsTotal === null
            ? `${TIMELINE_SEGMENTS}-segment track`
            : place.lockstep
              ? "one step per window"
              : `${formatCount(place.windowsTotal)} windows across ${TIMELINE_SEGMENTS} segments`}
        </span>
      </div>
    </div>
  );
}
