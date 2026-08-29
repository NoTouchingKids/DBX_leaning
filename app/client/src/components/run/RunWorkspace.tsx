/**
 * The run page. One component, every model, no model-specific code.
 *
 * This is the "generic progress view first" rule: it renders only fields
 * every model populates on every message — `percent_complete`,
 * `primary_metric`, `primary_metric_label`, the log stream, the run row — so
 * it is correct for a model nobody has written a page for yet, including the
 * ones being added to `MODEL_SPECS` on another track right now.
 *
 * Per-model pages do not replace it. There are two extension points, and
 * they are for different things:
 *
 *  - `view`, a `ModelView` from `@/components/models/contract` — the frozen
 *    per-model plug: one signature animation, up to two diagnostics charts,
 *    and the honesty note. That contract deliberately gives a model view no
 *    say over layout, so the layout lives here: the note is rendered BY this
 *    file, next to the animation, precisely so an animation cannot style its
 *    own disclaimer into invisibility.
 *  - `slots`, render functions for the handful of places the model contract
 *    does not reach — an extra identity chip, extra rail rows, or suppressing
 *    the generic results disclosure (gurobi_scheduling does: its signature IS
 *    its results view).
 *
 * Everything in this file keeps working underneath both, so a model page is
 * additive and an unfinished one degrades to exactly this view.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router";

import type { ModelView } from "@/components/models/contract";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Callout } from "@/components/ui/Callout";
import { HonestyNote } from "@/components/ui/HonestyNote";
import { PageHead } from "@/components/layout/PageHead";
import {
  useCancelRun,
  useFetchGap,
  useModels,
  useRefreshRunOnEvent,
  useRunDetail,
  useRunList,
  useTerminalHistory,
  useTriggerRun,
} from "@/hooks/useApi";
import { useElapsedSeconds } from "@/hooks/useElapsed";
import { useReconnectableRunStream } from "@/hooks/useReconnectableRunStream";
import { isApiError } from "@/lib/apiClient";
import { isTerminal, type UiRunState } from "@/lib/envelope";
import type { ModelSpec } from "@/lib/models";
import type { RunSnapshot } from "@/transport/runStore";

import { HealthNotices } from "./HealthNotices";
import { LatestRunCard } from "./LatestRunCard";
import { LogPane } from "./LogPane";
import { ProgressStrip } from "./ProgressStrip";
import { ResultsCard } from "./ResultsCard";
import { RunIdentityBar } from "./RunIdentityBar";
import { TriggerForm } from "./TriggerForm";
import { canCancel, deriveUiState, isStrandedRun } from "./runState";

/** What every slot is handed. Extended, not replaced, as model pages land. */
export interface RunViewContext {
  spec: ModelSpec;
  runId: string | null;
  state: UiRunState | null;
  snapshot: RunSnapshot;
  elapsedSeconds: number | null;
  /** True while the run can still produce messages. */
  active: boolean;
}

export interface RunViewSlots {
  subtitle?: (ctx: RunViewContext) => ReactNode;
  /** Extra chips in the identity bar — one headline number, per the design. */
  chips?: (ctx: RunViewContext) => ReactNode;
  /** Escape hatches for anything the `ModelView` contract does not cover.
   *  Prefer `view` for the signature animation and the charts. */
  signature?: (ctx: RunViewContext) => ReactNode;
  diagnostics?: (ctx: RunViewContext) => ReactNode;
  /** Return `null` to suppress the generic results disclosure entirely —
   *  gurobi_scheduling does, because its signature already is that view. */
  results?: (ctx: RunViewContext) => ReactNode;
  railExtras?: (ctx: RunViewContext) => ReactNode;
  statusRows?: (ctx: RunViewContext) => ReactNode;
}

export function RunWorkspace({
  spec,
  description,
  view,
  slots = {},
}: {
  spec: ModelSpec;
  description?: ReactNode;
  /** `view.model` must equal `spec.name`; they are looked up separately. */
  view?: ModelView;
  slots?: RunViewSlots;
}) {
  const [params, setParams] = useSearchParams();
  const urlRunId = params.get("run");

  /* --- which run are we looking at ------------------------------------- */

  const recent = useRunList({ model: spec.name, limit: 10 });
  const runs = useMemo(() => recent.data?.runs ?? [], [recent.data]);
  const runId = urlRunId ?? runs[0]?.run_id ?? null;

  const selectRun = useCallback(
    (next: string) => {
      setParams(
        (prev) => {
          const copy = new URLSearchParams(prev);
          copy.set("run", next);
          return copy;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  /* --- the two sources of truth ---------------------------------------- */

  const detail = useRunDetail(runId);
  const rowStatus = detail.data?.run.status ?? null;
  const rowTerminal = rowStatus !== null && isTerminal(rowStatus);

  // Declared before the stream hook because `knownTerminal` depends on it: a
  // run this page just triggered is provably not terminal, and should not
  // wait on a fetch to say so.
  const [startingFor, setStartingFor] = useState<string | null>(null);

  /**
   * Undefined until the run row lands, and that distinction is load-bearing.
   *
   * Collapsing "not loaded" into `false` tells the transport a run is live
   * before anything has said so, and it opens a channel to runs that are
   * already over. Passing undefined makes it hydrate from cache and wait —
   * costing a live run one API round trip before its stream opens, which is
   * nothing next to a run measured in minutes.
   *
   * The one case that must not wait is a run this page just triggered: its
   * 202 is proof it is not terminal, so `startingFor` answers immediately.
   */
  const knownTerminal =
    rowStatus !== null ? rowTerminal : startingFor === runId ? false : undefined;

  const { snapshot, reconnect } = useReconnectableRunStream(runId, {
    terminal: knownTerminal,
  });

  // A finished run has no live tail, so a first view has to fetch or show
  // nothing at all. Only once: the result is cached with no expiry, because a
  // terminal run's history is immutable.
  useTerminalHistory(runId, rowTerminal && snapshot.hydrated && snapshot.lastSeq === null);

  /* --- STARTING: client-only, between the 202 and the first message ----- */

  const state = deriveUiState({
    streamStatus: snapshot.status,
    rowStatus,
    optimisticStartingFor: startingFor,
    runId,
    sawAnyMessage: snapshot.lastSeq !== null,
  });
  const active = state !== null && state !== "STARTING" ? !isTerminal(state) : true;

  /* --- the HTTP row refreshes on stream events, never on a timer -------- */

  const refresh = useRefreshRunOnEvent(runId);
  // Keyed by run as well as status: two consecutive runs can both end
  // SUCCEEDED, and the second one still needs its row re-read.
  const lastSeenStatus = useRef<string | null>(null);
  useEffect(() => {
    if (runId === null || snapshot.status === null) return;
    const key = `${runId}:${snapshot.status}`;
    if (lastSeenStatus.current === key) return;
    lastSeenStatus.current = key;
    refresh();
  }, [runId, snapshot.status, refresh]);

  /* --- elapsed ---------------------------------------------------------- */

  const terminalMessage = useMemo(
    () => [...snapshot.statuses].reverse().find((s) => isTerminal(s.status)) ?? null,
    [snapshot.statuses],
  );
  const elapsedSeconds = useElapsedSeconds({
    startedTs: detail.data?.run.started_ts,
    anchor: snapshot.latestProgress
      ? {
          elapsedSeconds: snapshot.latestProgress.elapsed_seconds,
          ts: snapshot.latestProgress.ts,
        }
      : null,
    frozenAt: terminalMessage?.ts ?? (rowTerminal ? detail.data?.run.updated_ts : null) ?? null,
  });

  /* --- actions ---------------------------------------------------------- */

  const models = useModels();
  const triggerable = models.data
    ? models.data.models.some((m) => m.name === spec.name)
    : undefined;

  const trigger = useTriggerRun();
  const cancel = useCancelRun(runId);
  const gapFetch = useFetchGap(runId);
  // The cancel latch is stored as the run it belongs to, not as a boolean, so
  // switching runs cannot leave a previous run's optimistic disable behind.
  const [cancelRequestedFor, setCancelRequestedFor] = useState<string | null>(null);
  const cancelRequested = runId !== null && cancelRequestedFor === runId;

  const ctx: RunViewContext = { spec, runId, state, snapshot, elapsedSeconds, active };

  const cancelEnabled = canCancel({ live: detail.data?.live, state, cancelRequested });

  return (
    <>
      <PageHead eyebrow={`Model · ${spec.name}`} title={spec.label}>
        {description}
      </PageHead>

      <div className="grid items-start gap-5 lg:grid-cols-[300px_minmax(0,1fr)]">
        <div className="flex flex-col gap-5 lg:sticky lg:top-6">
          <TriggerForm
            key={spec.name}
            spec={spec}
            triggerable={triggerable}
            pending={trigger.isPending}
            error={trigger.error}
            onSubmit={(config) => {
              trigger.mutate(
                { model: spec.name, config },
                {
                  onSuccess: (outcome) => {
                    setStartingFor(outcome.run_id);
                    selectRun(outcome.run_id);
                  },
                },
              );
            }}
            cancelSlot={
              <>
                <Button
                  variant="danger"
                  disabled={!cancelEnabled}
                  title={
                    cancelEnabled
                      ? undefined
                      : "cancel is forwarded over the job's WebSocket; there is no live one"
                  }
                  onClick={() => {
                    cancel.mutate(undefined, { onSuccess: () => setCancelRequestedFor(runId) });
                  }}
                >
                  {cancelRequested ? "Cancel requested…" : "■ Cancel run"}
                </Button>
                {cancel.error != null && (
                  /*
                   * A 409 body is CANCEL_ESCAPE_HATCH — a real
                   * `databricks jobs cancel-run` command. Rendered from the
                   * response, never from a copy kept here, because a copy
                   * drifts from the command that actually works.
                   */
                  <Callout tone="warn" title="Cancel was not delivered">
                    {isApiError(cancel.error) ? cancel.error.detail : String(cancel.error)}
                    {detail.data?.run.job_run_id != null &&
                      `\n\njob_run_id for that command: ${detail.data.run.job_run_id}`}
                  </Callout>
                )}
              </>
            }
          />

          {trigger.data?.warning != null && (
            <Callout tone="warn" title="Started, but only partly recorded">
              {`${trigger.data.warning}\n\nThe run is live — watch it here — but startup reconciliation may never see it.`}
            </Callout>
          )}

          <LatestRunCard
            modelName={spec.name}
            state={state}
            live={detail.data?.live}
            run={detail.data?.run}
            elapsedSeconds={elapsedSeconds}
            recent={runs}
            selectedRunId={runId}
            onSelectRun={selectRun}
            extraRows={slots.statusRows?.(ctx)}
          />

          {slots.railExtras?.(ctx)}
        </div>

        <div className="min-w-0">
          <RunIdentityBar
            title={
              runId === null ? "No run selected" : active ? "Live telemetry" : "Completed run"
            }
            subtitle={
              slots.subtitle?.(ctx) ?? (
                <>
                  <code>{spec.name}</code>
                  {detail.data?.run.requested_by != null && ` · ${detail.data.run.requested_by}`}
                </>
              )
            }
            state={state}
            elapsedSeconds={elapsedSeconds}
            connection={snapshot.connection}
            runId={runId}
            extraChips={slots.chips?.(ctx)}
          />

          <HealthNotices
            stranded={isStrandedRun(state, detail.data?.live)}
            jobRunId={detail.data?.run.job_run_id ?? null}
            connection={snapshot.connection}
            consecutiveFailures={snapshot.consecutiveFailures}
            onReconnect={reconnect}
            gaps={snapshot.gaps}
            onFetchGap={(gap) => gapFetch.mutate(gap)}
            fetchingGap={gapFetch.isPending}
            droppedProgress={snapshot.droppedProgress}
          />

          {runId === null ? (
            <div className="rounded-[10px] border border-dashed border-edge px-4 py-10 text-center text-[0.8rem] text-faint">
              {recent.isPending
                ? "looking for recent runs…"
                : `No runs of ${spec.name} yet. Start one from the rail.`}
            </div>
          ) : (
            <div className="flex flex-col gap-5">
              <ProgressStrip progress={snapshot.latestProgress} running={active} />

              {view !== undefined && (
                <Card bodyClassName="p-4">
                  <view.Signature state={state} snapshot={snapshot} />
                  {/*
                    Rendered here, not by the animation. A decorative visual
                    that is read as data is the failure mode this note exists
                    to prevent, so it cannot be the animation's job to show it.
                  */}
                  <HonestyNote>{view.honesty}</HonestyNote>
                </Card>
              )}

              {view !== undefined && view.charts.length > 0 && (
                // Two charts side by side above ~640px; one takes the full
                // width rather than sitting in a half-empty row.
                <div
                  className={`grid gap-4 ${view.charts.length > 1 ? "min-[640px]:grid-cols-2" : ""}`}
                >
                  {view.charts.map((chart) => (
                    <Card key={chart.id} title={chart.title} hint={chart.caption}>
                      <chart.Chart state={state} snapshot={snapshot} />
                    </Card>
                  ))}
                </div>
              )}

              {slots.signature?.(ctx)}
              {slots.diagnostics?.(ctx)}
              {slots.results === undefined ? (
                <ResultsCard results={snapshot.results} />
              ) : (
                slots.results(ctx)
              )}
              <LogPane logs={snapshot.logs} droppedLogs={snapshot.droppedLogs} />
            </div>
          )}
        </div>
      </div>
    </>
  );
}
