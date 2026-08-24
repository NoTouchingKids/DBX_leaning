/**
 * What a chart shows when it has nothing to draw.
 *
 * A deliberate frame, not an axis with no data on it. `snapshot.progress` can
 * be empty for a long time — the job has to spin up before it emits anything —
 * or forever, if the run failed inside its first batch of iterations. Recharts
 * given an empty array renders a pair of axes over blank space, which looks
 * like a chart that is broken rather than a run that has not reported yet.
 */

import { CHART_HEIGHT } from "./chartTokens";

export function EmptyPlot({ children }: { children: string }) {
  return (
    <div
      className="flex items-center justify-center rounded-[8px] border border-dashed border-edge px-8 text-center text-[0.72rem] leading-relaxed text-faint"
      style={{ height: CHART_HEIGHT }}
    >
      <p className="max-w-[42ch]">{children}</p>
    </div>
  );
}
