/**
 * The live "currently evaluating" readout.
 *
 * The one place in this view where the numbers on screen are the numbers off
 * the wire: `last_scenario` and `last_outcome` come straight out of the
 * progress payload. It is a diagnostics card rather than part of the
 * animation for exactly that reason — real telemetry has a home, and this is
 * it.
 *
 * What it is not is a per-scenario feed. `progress_every` (10) and
 * `progress_every_s` (1.0) mean each message summarises a batch, and the
 * scenario shown is the last member of that batch, not a live cursor over all
 * 72. At microseconds per scenario, one message each would flood the channel.
 */

import type { ModelViewProps } from "@/components/models/contract";
import { isSettled } from "@/components/models/contract";
import { EMPTY, formatCount, formatMetric } from "@/lib/format";

import { deriveSweep } from "./scenarioModel";

/** Emitted order is alphabetical (`sorted(self.grid)`); this is the order the
 *  grid axes are read in, which is what a reader is holding in their head. */
const MULTIPLIER_ORDER = ["demand", "capacity", "unit_cost"];
const OUTCOME_ORDER = ["served", "shortfall", "idle", "objective"];

function ordered(record: Record<string, number>, first: readonly string[]): [string, number][] {
  const keys = [...first.filter((k) => k in record), ...Object.keys(record).filter((k) => !first.includes(k))];
  return keys.flatMap((key) => {
    const value = record[key];
    return value === undefined ? [] : [[key, value] as [string, number]];
  });
}

export function CurrentlyEvaluating({ state, snapshot }: ModelViewProps) {
  const sweep = deriveSweep(snapshot.progress);
  const settled = isSettled(state);

  if (sweep.lastScenario === null && sweep.lastOutcome === null) {
    return (
      <div className="flex h-full min-h-[7.5rem] flex-col justify-center gap-1 text-[0.74rem] text-dim">
        <p className="font-semibold text-ink">No scenario reported yet.</p>
        <p>
          Progress is batched — by default every 10 scenarios or every second, whichever comes
          first. A sweep this cheap can finish inside one batch, so a run may report once, or not
          at all before it is over.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div>
        <div className="mb-1 text-[0.68rem] tracking-wide text-faint uppercase">
          {settled ? "last scenario reported" : "currently evaluating"}
          <span className="ml-2 font-mono normal-case">last_scenario</span>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {sweep.lastScenario === null ? (
            <span className="text-[0.74rem] text-dim">{EMPTY}</span>
          ) : (
            ordered(sweep.lastScenario, MULTIPLIER_ORDER).map(([key, value]) => (
              <span
                key={key}
                className="rounded-full border border-line bg-paper px-2 py-0.5 font-mono text-[0.7rem]"
              >
                {key} <span className="font-semibold">{formatMetric(value)}x</span>
              </span>
            ))
          )}
        </div>
      </div>

      <div>
        <div className="mb-1 text-[0.68rem] tracking-wide text-faint uppercase">
          outcome <span className="ml-1 font-mono normal-case">last_outcome</span>
        </div>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-0.5 font-mono text-[0.72rem]">
          {sweep.lastOutcome === null ? (
            <span className="text-dim">{EMPTY}</span>
          ) : (
            ordered(sweep.lastOutcome, OUTCOME_ORDER).map(([key, value]) => (
              <div key={key} className="flex justify-between gap-2">
                <dt className="truncate text-dim">{key}</dt>
                <dd className="font-semibold">{formatMetric(value)}</dd>
              </div>
            ))
          )}
        </dl>
      </div>

      <div className="flex flex-wrap justify-between gap-x-4 gap-y-1 border-t border-line pt-2 font-mono text-[0.72rem]">
        <span className="text-dim">
          scenarios{" "}
          <span className="font-semibold text-ink">
            {sweep.scenariosDone === null ? EMPTY : formatCount(sweep.scenariosDone)}
            {sweep.scenariosTotal === null ? "" : ` / ${formatCount(sweep.scenariosTotal)}`}
          </span>
        </span>
        <span className="text-dim">
          best_objective{" "}
          <span className="font-semibold text-accent">{formatMetric(sweep.bestObjective)}</span>
        </span>
      </div>
    </div>
  );
}
