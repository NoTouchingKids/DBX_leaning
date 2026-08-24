/**
 * `/runs` — the run-history page.
 *
 * The one genuinely cross-model page in the app, and where every model page's
 * "View previous runs →" link lands. Built strictly from the eight fields
 * `repo.list_runs` selects plus the injected `live`; everything else on screen
 * is derived from those, and says so.
 *
 * What is load-bearing here, and survives from the thin version this replaced:
 *
 *  - the columns are exactly what `list_runs` SELECTs, plus `live`. There is
 *    no metric column because the endpoint returns none; adding one means an
 *    N+1 fetch of `/api/runs/{id}`, which is a decision, not a freebie.
 *  - `live` is not the status. `RUNNING` with no socket is a dead run nothing
 *    will ever finish, and it is styled as a warning — with no action, because
 *    this API has none to offer.
 *  - ordering is `updated_ts DESC` and there is no cursor, so there are no
 *    sort controls and "load more" is a refetch with a bigger limit.
 *
 * Filter state lives in the URL and nowhere else — five model pages link here
 * with `?model=` preset, and that link has to survive a refresh, a paste and
 * a back button. See `components/runs/historyFilters.ts` for which filters run
 * server-side and which do not; each one is labelled on the control itself.
 */

import { useSearchParams } from "react-router";

import { PageHead } from "@/components/layout/PageHead";
import { CapacityMeter } from "@/components/runs/CapacityMeter";
import { EmptyState } from "@/components/runs/EmptyState";
import { HistoryNotes } from "@/components/runs/HistoryNotes";
import { HistoryToolbar } from "@/components/runs/HistoryToolbar";
import { RunsTable } from "@/components/runs/RunsTable";
import { StrandedBanner } from "@/components/runs/StrandedBanner";
import { deriveCapacity } from "@/components/runs/capacity";
import { describeEmptyState } from "@/components/runs/emptyState";
import {
  DEFAULT_LIMIT,
  applyClientFilters,
  canLoadMore,
  historyFiltersToParams,
  modelOptions,
  nextLimit,
  parseHistoryFilters,
  serverFilterMismatches,
  serverQuery,
  type HistoryFilters,
} from "@/components/runs/historyFilters";
import { countStranded } from "@/components/runs/liveness";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { useRunList } from "@/hooks/useApi";
import { EMPTY, formatCount } from "@/lib/format";
import { MODEL_SPECS } from "@/lib/models";

const KNOWN_MODELS = MODEL_SPECS.map((spec) => spec.name);

export function RunsPage() {
  const [params, setParams] = useSearchParams();
  const filters = parseHistoryFilters(params);

  function update(patch: Partial<HistoryFilters>) {
    // `replace`, not push. Every keystroke in the id search is a filter
    // change, and a history stack full of them would bury the model page the
    // user arrived from behind twenty back-presses.
    setParams(historyFiltersToParams({ ...filters, ...patch }), { replace: true });
  }

  const list = useRunList(serverQuery(filters));

  /**
   * A second, deliberately unfiltered read, for the capacity meter only.
   *
   * The ceiling is account-wide, so counting the filtered table would answer
   * "active runs of this model" and label it as the account's. When no filter
   * is set and the window is the default, this resolves to the same query key
   * as the list above and React Query serves both from one request; with a
   * filter applied it is one extra statement, which on a warehouse billed by
   * uptime rather than statement count is the cheap half of the trade.
   */
  const capacityWindow = useRunList({ limit: DEFAULT_LIMIT });

  const serverRows = list.data?.runs ?? [];
  const rows = applyClientFilters(serverRows, filters);
  const capacity = deriveCapacity(capacityWindow.data?.runs ?? [], {
    windowLimit: DEFAULT_LIMIT,
    unfiltered: true,
  });
  const mismatches = serverFilterMismatches(filters, list.data?.filters);
  const hidden = serverRows.length - rows.length;

  return (
    <>
      <PageHead eyebrow="All models · GET /api/runs" title="Run history">
        A top-N window ordered by <code>updated_ts DESC</code> — not pagination, so a long-running
        old run reappears at the top every time it emits.
      </PageHead>

      <CapacityMeter capacity={capacity} pending={capacityWindow.data === undefined} />

      <HistoryToolbar
        filters={filters}
        models={modelOptions(KNOWN_MODELS, serverRows, filters.model)}
        onChange={update}
        onRefresh={() => {
          void list.refetch();
          void capacityWindow.refetch();
        }}
        isFetching={list.isFetching}
        updatedAt={list.dataUpdatedAt > 0 ? list.dataUpdatedAt : null}
      />

      {list.isError && (
        <div className="mb-3">
          {/* The server's own text: the errors that matter on this platform
              name real numbers and real commands, and a generic message
              throws that away. */}
          <Callout tone="bad" title="Could not read the run list">
            {list.error instanceof Error ? list.error.message : String(list.error)}
          </Callout>
        </div>
      )}

      {mismatches.length > 0 && (
        <div className="mb-3">
          {/* The response echoes the filters it applied. A disagreement is the
              one failure this page cannot otherwise see — a dropped filter
              looks exactly like "there are no such runs". */}
          <Callout tone="warn" title="The server applied different filters than were requested">
            {`Mismatch on ${mismatches.join(" and ")}. The rows below are not what this page asked for; treat the filter controls as unreliable until this clears.`}
          </Callout>
        </div>
      )}

      <StrandedBanner count={countStranded(rows)} />

      {rows.length > 0 ? (
        <RunsTable rows={rows} />
      ) : list.isPending ? (
        <p className="px-4 py-10 text-center text-[0.8rem] text-faint">loading…</p>
      ) : (
        <EmptyState copy={describeEmptyState(filters, serverRows.length)} />
      )}

      <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-2 text-[0.7rem] text-faint">
        <span>
          {formatCount(rows.length)} rows
          {hidden > 0 && ` (${formatCount(hidden)} hidden by the client-side id search)`} · ordered
          by <code>updated_ts DESC</code>, not by start time
        </span>
        <span>
          server applied: model <code>{list.data?.filters.model ?? EMPTY}</code> · status{" "}
          <code>{list.data?.filters.status ?? EMPTY}</code> · limit <code>{filters.limit}</code>
        </span>
        <span className="ml-auto flex items-center gap-2">
          {/* Not "next page". There is no offset and no cursor, so this
              refetches a wider window and every row already on screen comes
              back with it. */}
          <Button
            onClick={() => update({ limit: nextLimit(filters.limit) })}
            disabled={!canLoadMore(filters.limit) || list.isFetching}
            title="Refetches a wider top-N window — this endpoint has no offset or cursor"
          >
            {canLoadMore(filters.limit)
              ? `Widen to top ${nextLimit(filters.limit)}`
              : "Window at its 500-row maximum"}
          </Button>
        </span>
      </div>

      <HistoryNotes />
    </>
  );
}
