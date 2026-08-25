/**
 * Chain health — `per_chain_acceptance` as bars, and `stuck_chains` beside it.
 *
 * Bars rather than a multi-series line, deliberately: the question this chart
 * answers is "is one of these chains unwell?", and a single short bar in a row
 * of tall ones answers it at a glance, where the same chain as one line among
 * eight is buried. There is no time axis here at all — this is the latest
 * snapshot, not a history, because acceptance is cumulative in emcee and its
 * history is a smoothly rising curve that says nothing the current value does
 * not.
 *
 * emcee has no divergences — that is an HMC/NUTS diagnostic — so `stuck_chains`
 * is the honest equivalent, and the model's docstring says as much. It is
 * shown as the model computed it, over every chain, even when the bars are a
 * truncated view of a very large ensemble.
 */

import { Bar, BarChart, Cell, ResponsiveContainer, XAxis, YAxis } from "recharts";

import type { ModelViewProps } from "@/components/models/contract";
import { EMPTY, formatCount, formatMetric } from "@/lib/format";

import { deriveChainHealth, mcmcPayload, type ChainTone } from "./payload";

/**
 * Bar colour by tone.
 *
 * `text-*` plus `fill="currentColor"`, not `fill-*`. Recharts puts its own
 * `fill` presentation attribute on every shape it draws, and a presentation
 * attribute beats an inherited CSS value — so a colour class on the wrapper
 * silently loses and the chart renders in Recharts' default blue. Routing
 * through `currentColor` is what makes the design tokens, and therefore the
 * dark palette, actually reach the SVG.
 */
const BAR_CLASS: Record<ChainTone, string> = {
  good: "text-good",
  warn: "text-warn",
  bad: "text-bad",
};

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div>
      <div className="font-mono text-[0.62rem] text-faint">{label}</div>
      <div className={`font-mono text-[0.9rem] ${tone ?? "text-ink"}`}>{value}</div>
    </div>
  );
}

export function ChainHealthChart({ snapshot }: ModelViewProps) {
  const health = deriveChainHealth(mcmcPayload(snapshot.latestProgress));

  if (health.bars.length === 0) {
    return (
      <p className="px-1 py-10 text-center text-[0.75rem] text-faint">
        No acceptance figures yet. The sampler emits its first progress message
        after {" "}
        <code className="font-mono">progress_every</code> draws — nothing is
        wrong with an empty chart on a run that has just started.
      </p>
    );
  }

  const stuck = health.stuck ?? 0;

  return (
    <div>
      <div className="mb-3 grid grid-cols-3 gap-3">
        <Stat
          label="stuck_chains"
          value={health.stuck === null ? EMPTY : formatCount(health.stuck)}
          tone={stuck > 0 ? "text-bad" : "text-good"}
        />
        <Stat label="mean_acceptance" value={formatMetric(health.mean)} />
        <Stat
          label="min_acceptance"
          value={formatMetric(health.min)}
          tone={health.min !== null && health.min < 0.1 ? "text-warn" : undefined}
        />
      </div>

      <div className="h-[180px] w-full text-faint">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={health.bars} margin={{ top: 4, right: 4, bottom: 0, left: -18 }}>
            <XAxis
              dataKey="label"
              tickLine={false}
              axisLine={false}
              interval="preserveStartEnd"
              tick={{ fill: "currentColor", fontSize: 10 }}
            />
            <YAxis
              domain={[0, 1]}
              tickLine={false}
              axisLine={false}
              width={44}
              tick={{ fill: "currentColor", fontSize: 10 }}
            />
            <Bar dataKey="acceptance" isAnimationActive={false} radius={[2, 2, 0, 0]}>
              {health.bars.map((bar) => (
                <Cell key={bar.chain} fill="currentColor" className={BAR_CLASS[bar.tone]} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      <p className="mt-2 text-[0.66rem] leading-relaxed text-faint">
        One bar per chain, acceptance fraction on 0–1.
        {health.hidden > 0 &&
          ` Showing ${formatCount(health.bars.length)} of ${formatCount(health.chainsTotal)} chains; stuck_chains counts all of them.`}
        {stuck > 0 &&
          " A chain at zero has accepted nothing since the run began — it is not exploring."}
      </p>
    </div>
  );
}
