/**
 * The generic progress strip: percent bar + primary metric.
 *
 * These are the two fields every model populates on every `progress` message,
 * which is why this component needs no model-specific code at all.
 *
 * `percent_complete: null` is a REAL value, not a loading state. It is null
 * for the entire life of a `gurobi_scheduling` run — branch-and-bound has no
 * meaningful completion fraction — and transiently null for the others. A 0%
 * bar would be a lie, and a spinner would imply something is about to arrive.
 * An indeterminate bar says the true thing: running, no fraction available.
 */

import type { ProgressMessage } from "@/lib/envelope";
import { EMPTY, formatMetric } from "@/lib/format";

export function ProgressStrip({
  progress,
  running,
}: {
  progress: ProgressMessage | null;
  /** Drives whether the indeterminate bar animates; a stopped run's unknown
   *  percentage is unknown forever, and should not look busy. */
  running: boolean;
}) {
  const percent = progress?.percent_complete ?? null;
  const determinate = percent !== null && Number.isFinite(percent);
  const clamped = determinate ? Math.min(100, Math.max(0, percent)) : 0;
  const label = progress?.primary_metric_label ?? "primary_metric";

  return (
    <div className="flex flex-wrap items-center gap-6 rounded-[10px] border border-edge bg-raised px-4 py-3.5">
      <div className="min-w-[220px] flex-1">
        <div className="mb-1.5 flex justify-between text-[0.7rem] text-dim">
          <span className="font-mono">percent_complete</span>
          <span className="font-mono" title={determinate ? undefined : "null — the model does not report a completion fraction"}>
            {determinate ? `${clamped.toFixed(1)}%` : EMPTY}
          </span>
        </div>
        <div
          role="progressbar"
          aria-label="percent complete"
          // No aria-valuenow when indeterminate: that is precisely how the
          // accessibility tree spells "in progress, amount unknown".
          aria-valuemin={determinate ? 0 : undefined}
          aria-valuemax={determinate ? 100 : undefined}
          aria-valuenow={determinate ? clamped : undefined}
          aria-valuetext={determinate ? undefined : "unknown"}
          data-indeterminate={determinate ? undefined : "true"}
          className={
            `relative h-2 overflow-hidden rounded-full bg-idle-soft ` +
            (determinate ? "" : running ? "bar-indeterminate" : "opacity-60")
          }
        >
          {determinate && (
            <span
              className="block h-full rounded-full bg-accent transition-[width] duration-500 motion-reduce:transition-none"
              style={{ width: `${clamped}%` }}
            />
          )}
        </div>
      </div>

      {/* Fixed minimum width: a null metric must not collapse the layout and
          shove the bar sideways on every message that happens to lack one. */}
      <div className="min-w-[9rem] text-right">
        <div className="font-mono text-[1.05rem] leading-tight font-semibold">
          {formatMetric(progress?.primary_metric)}
        </div>
        <div className="mt-0.5 truncate text-[0.68rem] text-dim" title={label}>
          {label}
        </div>
      </div>
    </div>
  );
}
