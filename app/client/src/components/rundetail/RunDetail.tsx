/**
 * A finished run, hydrated from Delta.
 *
 * The whole page hangs off one fact: **a finished run is immutable**. There
 * is no SSE connection, no `useRunStream`, no cancel button, no poll and no
 * invalidation — fetch once, cache forever, render. That is not a
 * simplification of the live page; it is what the live page's caching policy
 * already assumes about terminal runs, taken to its conclusion.
 *
 * It is deliberately the *same* page at rest rather than a different product:
 * the same identity bar, the same signature animation frozen in its terminal
 * frame, the same charts, the same honesty note rendered by the page rather
 * than by the animation, the same log pane. What differs is only what a
 * finished run can be asked that a live one cannot — whether its results are
 * complete, and whether its history has holes that will never close.
 *
 * Composition follows `run/RunWorkspace.tsx` on purpose. If that layout
 * changes, this one should change with it.
 */

import type { ReactNode } from "react";
import { Link } from "react-router";

import { PageHead } from "@/components/layout/PageHead";
import { viewFor } from "@/components/models/registry";
import { LogPane } from "@/components/run/LogPane";
import { RunIdentityBar } from "@/components/run/RunIdentityBar";
import { isStrandedRun } from "@/components/run/runState";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { Card } from "@/components/ui/Card";
import { HonestyNote } from "@/components/ui/HonestyNote";
import { useRunDetail } from "@/hooks/useApi";
import { isApiError } from "@/lib/apiClient";
import { isTerminal } from "@/lib/envelope";
import { EMPTY, formatCount, formatDateTime } from "@/lib/format";
import { MODEL_SPECS } from "@/lib/models";

import { lockedViewState } from "./history";
import {
  GapNotice,
  LiveNotice,
  StrandedNotice,
  UnusableNotice,
} from "./HistoryNotices";
import { TerminalResults } from "./TerminalResults";
import { useRunHistory, type RunHistory } from "./useRunHistory";

/** Matches `RunIdentityBar`'s own chip, which is not exported. */
function Chip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-16 rounded-lg border border-edge bg-raised px-2.5 py-1.5">
      <div className="text-[0.6rem] tracking-wide text-faint uppercase">{label}</div>
      <div className="mt-0.5 flex items-center gap-1.5 font-mono text-[0.8rem] font-semibold">
        {children}
      </div>
    </div>
  );
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <>
      <PageHead eyebrow="Past run · GET /api/runs/{id}" title="Run detail">
        A finished run is immutable, so this page reads it once and keeps it.
        No stream is opened and nothing polls.
      </PageHead>
      {children}
    </>
  );
}

export function RunDetail({ runId }: { runId: string }) {
  const detail = useRunDetail(runId);

  const run = detail.data?.run ?? null;
  const rowStatus = run?.status ?? null;
  const live = detail.data?.live ?? false;
  const runTerminal = rowStatus !== null && isTerminal(rowStatus);

  // Enabled only once the registry has confirmed the run exists. The backfill
  // endpoint does not 404 for an unknown run — it queries Delta and returns
  // an empty page — so firing it for a mistyped id would spend warehouse
  // uptime to learn nothing.
  const history = useRunHistory(runId, { enabled: detail.isSuccess, rowStatus });

  /* --- a run id that is not in the registry is a real answer ----------- */

  if (isApiError(detail.error) && detail.error.status === 404) {
    return (
      <Shell>
        <Card title="No such run">
          <p className="max-w-[64ch] text-[0.84rem] leading-relaxed text-dim">
            <code className="break-all">{runId}</code> is not in{" "}
            <code>run_status</code>. Either the id is wrong, or the run was
            launched but its registry row was never written — a trigger that
            came back <code>registered: false</code> produces exactly that: a
            job which really ran, and which nothing here can find.
          </p>
          <p className="mt-3 text-[0.78rem] text-faint">{detail.error.detail}</p>
          <p className="mt-4">
            <Link to="/runs" className="text-[0.82rem] font-semibold text-accent">
              ← Back to run history
            </Link>
          </p>
        </Card>
      </Shell>
    );
  }

  if (detail.error !== null && detail.error !== undefined) {
    return (
      <Shell>
        <Callout tone="bad" title="Could not read this run">
          {isApiError(detail.error) ? detail.error.detail : detail.error.message}
        </Callout>
      </Shell>
    );
  }

  if (run === null) {
    return (
      <Shell>
        <p className="py-16 text-center text-[0.82rem] text-faint">reading the run row…</p>
      </Shell>
    );
  }

  /* --- the run exists -------------------------------------------------- */

  const spec = MODEL_SPECS.find((candidate) => candidate.name === run.model);
  const view = viewFor(run.model);
  const state = lockedViewState(rowStatus);
  const stranded = isStrandedRun(state, live);
  const snapshot = history.snapshot;

  return (
    <>
      <PageHead eyebrow={`Past run · ${run.model}`} title={spec?.label ?? run.model}>
        Read from Delta, not from a stream. A finished run cannot change, so
        this is fetched once and cached for the session.
      </PageHead>

      <RunIdentityBar
        title={spec?.label ?? run.model}
        subtitle={
          <>
            started {formatDateTime(run.started_ts)} · last update{" "}
            {formatDateTime(run.updated_ts)} · requested by{" "}
            {run.requested_by ?? EMPTY}
            {run.detail !== null && run.detail !== "" && <> · {run.detail}</>}
          </>
        }
        state={state}
        elapsedSeconds={(run.updated_ts - run.started_ts) / 1000}
        // Always `idle`: this page opens no SSE connection at all, and the
        // chip means "our view of the run", not "the run". Conflating it with
        // `live` — which is the app's WebSocket to the *job* — is the exact
        // mistake the chip was split out to avoid, so `live` gets its own.
        connection="idle"
        runId={run.run_id}
        extraChips={
          <Chip label="Job channel">
            <span className={live ? "text-good" : stranded ? "text-warn" : "text-faint"}>
              {live ? "connected" : stranded ? "none · stranded" : "closed"}
            </span>
          </Chip>
        }
      />

      <div className="mb-4 flex flex-col gap-2">
        {live && <LiveNotice model={run.model} runId={run.run_id} />}
        {stranded && <StrandedNotice jobRunId={run.job_run_id} />}
        {history.gapSummary.count > 0 && <GapNotice summary={history.gapSummary} />}
        {history.unusable > 0 && <UnusableNotice count={history.unusable} />}
        {history.error !== null && (
          <Callout tone="bad" title="Could not read this run's history">
            {isApiError(history.error) ? history.error.detail : history.error.message}
          </Callout>
        )}
      </div>

      <div className="flex flex-col gap-5">
        {view !== undefined && (
          <Card bodyClassName="p-4">
            {/* `state` is the registry row's status, so `isSettled()` is true
                for a terminal run and the animation renders one flat frozen
                frame rather than moving. A still animation is how a finished
                run reads as finished from across a room. */}
            <view.Signature state={state} snapshot={snapshot} />
            {/* Rendered by the page, never by the animation — an animation
                cannot be trusted to show its own disclaimer. */}
            <HonestyNote>{view.honesty}</HonestyNote>
          </Card>
        )}

        {view !== undefined && view.charts.length > 0 && (
          <div className={`grid gap-4 ${view.charts.length > 1 ? "min-[640px]:grid-cols-2" : ""}`}>
            {view.charts.map((chart) => (
              <Card key={chart.id} title={chart.title} hint={chart.caption}>
                <chart.Chart state={state} snapshot={snapshot} />
              </Card>
            ))}
          </div>
        )}

        <TerminalResults results={snapshot.results} completeness={history.results} />

        <div className="flex flex-col gap-2">
          <LogPane logs={snapshot.logs} droppedLogs={snapshot.droppedLogs} />
          <HistoryFooter history={history} runTerminal={runTerminal} />
        </div>
      </div>
    </>
  );
}

/**
 * How much of the run has been read, and the control that reads more.
 *
 * Paging is user-driven past the first page on purpose. The first page is
 * automatic because a finished run has no live tail — without it the page is
 * blank forever — but an MCMC run is tens of thousands of messages, and
 * pulling all of them before showing anything spends SQL-warehouse uptime on
 * lines nobody scrolled to.
 */
function HistoryFooter({
  history,
  runTerminal,
}: {
  history: RunHistory;
  runTerminal: boolean;
}) {
  const { snapshot } = history;
  const loadedTotal =
    snapshot.logs.length +
    snapshot.progress.length +
    snapshot.statuses.length +
    snapshot.results.length;

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-xl border border-line bg-raised px-4 py-2.5 text-[0.72rem] text-dim">
      <span className="font-mono">
        {history.loading
          ? "reading from Delta…"
          : `${formatCount(loadedTotal)} messages · ${formatCount(history.pagesLoaded)} page${
              history.pagesLoaded === 1 ? "" : "s"
            } · up to seq ${snapshot.lastSeq ?? EMPTY}`}
      </span>

      <span className="text-faint">
        {history.fullyLoaded
          ? runTerminal
            ? "the whole run — nothing further exists"
            : "everything Delta held at the time of this read"
          : "more pages available"}
      </span>

      <span className="ml-auto flex gap-2">
        {!history.fullyLoaded && !history.loading && (
          <Button onClick={history.loadMore} disabled={history.loadingMore}>
            {history.loadingMore ? "loading…" : "Load more from Delta"}
          </Button>
        )}
        {/* A terminal run's history cannot have changed, so re-reading it
            would be a request whose answer is known in advance. */}
        {!runTerminal && (
          <Button onClick={history.reread} title="re-read this run's messages from Delta">
            ↻ Re-read
          </Button>
        )}
      </span>
    </div>
  );
}
