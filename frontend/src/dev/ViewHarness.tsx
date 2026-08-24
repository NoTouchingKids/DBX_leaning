/**
 * One `ModelView`, rendered through every lifecycle state at once.
 *
 * The states that matter most are the ones that are hardest to catch against
 * a real workspace: a run that finishes before any progress arrives, a run
 * stuck in QUEUED, INFEASIBLE. Waiting to see those by accident is how they
 * ship broken. Here they are all on screen together, from the same fixture,
 * so the only difference between two columns is the state.
 *
 * Three deliberate choices:
 *
 *  - **Columns, scrolling horizontally.** Comparison is the point, and a
 *    vertical stack makes two states impossible to hold in the eye at once.
 *  - **`honesty` under every column, not once at the top.** The contract
 *    says a view without an honesty note is incomplete; repeating it in each
 *    cell is what makes a missing or vague one impossible to skim past.
 *  - **A separate error boundary per signature and per chart.** A signature
 *    whose Three.js scene fails must still leave its charts standing, and
 *    that is only visible if they cannot take each other down.
 */

import { useMemo } from "react";

import type { ModelView, ModelViewProps } from "@/components/models/contract";
import type { UiRunState } from "@/lib/envelope";
import { EMPTY, formatCount, formatMetric } from "@/lib/format";
import { StatusPill } from "@/components/ui/StatusPill";
import type { RunSnapshot } from "@/transport/runStore";
import { ErrorBoundary } from "./ErrorBoundary";
import { auditHonesty, HARNESS_STATES, VERDICT_STYLE } from "./harness";
import { hasScript, makeSnapshot, type FixtureName } from "./fixtures";

/* ------------------------------------------------------------------ *
 * Snapshot facts
 * ------------------------------------------------------------------ */

function Fact({ label, value, tone = "" }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-faint">{label}</span>
      <span className={`font-mono ${tone === "" ? "text-dim" : tone}`}>{value}</span>
    </div>
  );
}

/**
 * What the view was handed, in numbers.
 *
 * Shown next to the rendering so a column that looks wrong can be checked
 * against its input without a debugger — "no bar" is correct when
 * percent_complete is null and a bug when it is 40.
 */
function SnapshotFacts({ snapshot }: { snapshot: RunSnapshot }) {
  const latest = snapshot.latestProgress;
  return (
    <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 rounded-md border border-line bg-paper px-2 py-1.5 text-[0.6rem]">
      <Fact label="progress" value={formatCount(snapshot.progress.length)} />
      <Fact label="logs" value={formatCount(snapshot.logs.length)} />
      <Fact label="results" value={formatCount(snapshot.results.length)} />
      <Fact label="statuses" value={formatCount(snapshot.statuses.length)} />
      <Fact
        label="percent"
        value={latest?.percent_complete === null || latest === null ? "null" : `${latest.percent_complete}%`}
        tone={latest?.percent_complete === null ? "text-warn" : ""}
      />
      <Fact
        label="metric"
        value={latest === null ? EMPTY : formatMetric(latest.primary_metric)}
        tone={latest !== null && latest.primary_metric === null ? "text-warn" : ""}
      />
      <Fact label="lastSeq" value={snapshot.lastSeq === null ? "null" : String(snapshot.lastSeq)} />
      <Fact
        label="gaps"
        value={String(snapshot.gaps.length)}
        tone={snapshot.gaps.length > 0 ? "text-warn" : ""}
      />
      <Fact label="hydrated" value={String(snapshot.hydrated)} />
      <Fact label="terminal" value={String(snapshot.terminal)} />
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * One state column
 * ------------------------------------------------------------------ */

function StateColumn({
  view,
  state,
  snapshot,
  showCharts,
}: {
  view: ModelView;
  state: UiRunState | null;
  snapshot: RunSnapshot;
  showCharts: boolean;
}) {
  const props: ModelViewProps = { state, snapshot };
  const { Signature } = view;
  const verdict = auditHonesty(view.honesty);

  return (
    <div className="flex w-[340px] shrink-0 flex-col gap-2">
      <div className="flex items-center gap-2">
        <StatusPill state={state} />
        {state === null && (
          <span className="text-[0.6rem] text-faint">no run selected — not the same as an empty one</span>
        )}
      </div>

      {/* On `paper` rather than `raised`: a signature that draws white on
          white is a real bug and this is where it shows. */}
      <ErrorBoundary label={`${view.model} signature (${state ?? "null"})`}>
        <div className="overflow-hidden rounded-md border border-line bg-paper">
          <Signature {...props} />
        </div>
      </ErrorBoundary>

      {/* Beside every state, not once per view: an absent note has to be
          impossible to skim past. */}
      <div className={`rounded-md border px-2 py-1.5 text-[0.62rem] leading-snug ${VERDICT_STYLE[verdict.level]}`}>
        <span className="font-bold tracking-wide uppercase">honesty</span>{" "}
        {view.honesty?.trim() === "" || view.honesty === undefined ? (
          <span className="font-bold">{verdict.note}</span>
        ) : (
          view.honesty
        )}
      </div>

      <SnapshotFacts snapshot={snapshot} />

      {showCharts &&
        view.charts.map((chart) => {
          const { Chart } = chart;
          return (
            <div key={chart.id} className="rounded-md border border-line bg-raised">
              <div className="border-b border-dashed border-line px-2 py-1">
                <div className="text-[0.68rem] font-bold">{chart.title}</div>
                {chart.caption !== undefined && (
                  <div className="text-[0.6rem] text-faint">{chart.caption}</div>
                )}
              </div>
              <div className="p-1.5">
                <ErrorBoundary label={`${view.model} chart "${chart.id}" (${state ?? "null"})`}>
                  <Chart {...props} />
                </ErrorBoundary>
              </div>
            </div>
          );
        })}
    </div>
  );
}

/* ------------------------------------------------------------------ *
 * The harness
 * ------------------------------------------------------------------ */

export interface ViewHarnessProps {
  view: ModelView;
  fixture: FixtureName;
  /** Off when the point of the pass is the signature, or when the fixture is
   *  dense enough that sixteen chart mounts would drown it. */
  showCharts?: boolean;
  /** Restrict the columns. Defaults to all eight. */
  states?: readonly (UiRunState | null)[];
}

export function ViewHarness({
  view,
  fixture,
  showCharts = true,
  states = HARNESS_STATES,
}: ViewHarnessProps) {
  // Fixtures are memoised inside `fixtures.ts` too; this keeps the array
  // identity stable across re-renders so a chart's data prop does not look
  // new on every keystroke elsewhere on the page.
  const snapshots = useMemo(
    () => states.map((state) => [state, makeSnapshot(view.model, fixture, state)] as const),
    [states, view.model, fixture],
  );
  const verdict = auditHonesty(view.honesty);
  const chartCount = view.charts.length;

  return (
    <section className="rounded-[10px] border border-edge bg-raised">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-dashed border-edge px-4 py-3">
        <h2 className="font-mono text-[0.9rem] font-bold">{view.model}</h2>
        <span className="text-[0.66rem] text-faint">
          {chartCount} chart{chartCount === 1 ? "" : "s"}
          {chartCount > 2 && " — the contract caps this at two"}
        </span>
        {!hasScript(view.model) && (
          <span className="rounded border border-warn bg-warn-soft px-1.5 py-0.5 text-[0.6rem] text-warn">
            no fixture script for this model name — rendering common-fields-only traffic
          </span>
        )}
        {verdict.level !== "ok" && (
          <span className={`rounded border px-1.5 py-0.5 text-[0.6rem] ${VERDICT_STYLE[verdict.level]}`}>
            honesty: {verdict.note}
          </span>
        )}
      </header>

      {/* Horizontal scroll rather than wrapping: the states are a sequence and
          reading them left to right is the whole affordance. */}
      <div className="flex gap-3 overflow-x-auto p-4">
        {snapshots.map(([state, snapshot]) => (
          <StateColumn
            key={state ?? "null"}
            view={view}
            state={state}
            snapshot={snapshot}
            showCharts={showCharts}
          />
        ))}
      </div>
    </section>
  );
}
