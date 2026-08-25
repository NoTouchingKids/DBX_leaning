/**
 * Paged history for one past run.
 *
 * ## Why this is an infinite query and not `useTerminalHistory`
 *
 * `hooks/useApi.ts::useTerminalHistory` implements the caching policy this
 * page needs — `staleTime: Infinity`, `gcTime: Infinity`, no refetch on
 * mount/focus/reconnect, because a terminal run is immutable — but it walks
 * every page to the end of the run in one shot. That is right for the live
 * page, which reaches for it only when the local cache is *empty* and the run
 * is short enough to have been missed entirely. It is wrong here: this page
 * is opened from run history, where "a past run" routinely means an MCMC run
 * with tens of thousands of messages, and pulling all of them before showing
 * anything spends the SQL warehouse's uptime on lines nobody scrolled to.
 *
 * So the *policy* is reused verbatim and only the *granularity* changes: one
 * cache entry per run, holding the pages fetched so far, under a query key
 * that deliberately does not collide with `qk.history(runId)` — the live page
 * stores a different shape under that key for the same run, and two shapes
 * under one key is a bug waiting for a user who visits both pages.
 *
 * React Query owns the cache. There is no second one: no ref, no module map,
 * no IndexedDB write. A second mount of the same run re-reads the pages it
 * already has and issues no request, which is the same guarantee
 * `useTerminalHistory` gives, tested the same way.
 *
 * ## What triggers a fetch
 *
 * The first page, automatically, on first view — a finished run has no live
 * tail to wait for, so an unprompted first fetch is the only way the page is
 * ever non-empty. Every page after that is a user action. Nothing polls.
 */

import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";

import { fetchBackfill } from "@/lib/apiClient";
import type { RunStatus } from "@/lib/envelope";
import { isTerminal } from "@/lib/envelope";
import type { Gap, RunSnapshot } from "@/transport/runStore";

import {
  FIRST_CURSOR,
  assembleHistory,
  findGaps,
  nextCursor,
  resultsCompleteness,
  snapshotSeqs,
  summariseGaps,
  type GapSummary,
  type ResultsCompleteness,
} from "./history";

/**
 * Namespaced under `run-history` so it reads as the same concern in devtools,
 * but with a third segment so it is a different cache entry from
 * `qk.history(runId)`. See the note above.
 */
export function historyPagesKey(runId: string) {
  return ["run-history", runId, "paged"] as const;
}

export interface RunHistory {
  snapshot: RunSnapshot;
  /** Every page has been walked; the seq range shown reaches the run's end. */
  fullyLoaded: boolean;
  /** Pages fetched so far — what the "loaded N of ..." line counts. */
  pagesLoaded: number;
  gaps: readonly Gap[];
  gapSummary: GapSummary;
  results: ResultsCompleteness;
  /** Rows the server sent that could not be parsed into a message. */
  unusable: number;
  loading: boolean;
  loadingMore: boolean;
  error: Error | null;
  loadMore: () => void;
  /** Re-read from Delta. Only meaningful for a run that is not terminal — a
   *  terminal run's history cannot have changed since it was fetched. */
  reread: () => void;
}

export function useRunHistory(
  runId: string | null | undefined,
  options: { enabled: boolean; rowStatus: RunStatus | null },
): RunHistory {
  const client = useQueryClient();
  const id = typeof runId === "string" && runId.length > 0 ? runId : null;

  const query = useInfiniteQuery({
    queryKey: historyPagesKey(id ?? ""),
    initialPageParam: FIRST_CURSOR,
    queryFn: ({ pageParam, signal }) =>
      fetchBackfill(id as string, { after_seq: pageParam }, signal),
    // `undefined` is React Query's "there is no next page", which is what
    // disables `hasNextPage` and, with it, the load-more control. The
    // termination rules themselves live in `nextCursor` so they can be tested
    // without a query client.
    getNextPageParam: (last, _pages, lastParam) => nextCursor(last, lastParam) ?? undefined,
    enabled: options.enabled && id !== null,
    // Identical to `useTerminalHistory`'s policy, and for the identical
    // reason. A finished run's messages are immutable, so there is nothing a
    // refetch could discover.
    staleTime: Infinity,
    gcTime: Infinity,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: 1,
  });

  const pages = useMemo(() => query.data?.pages ?? [], [query.data]);

  const assembled = useMemo(
    () => assembleHistory({ runId: id ?? "", pages, rowStatus: options.rowStatus }),
    [id, pages, options.rowStatus],
  );

  const gaps = useMemo(
    () => findGaps(snapshotSeqs(assembled.snapshot)),
    [assembled.snapshot],
  );

  const fullyLoaded = query.isSuccess && !query.hasNextPage;
  const runTerminal = options.rowStatus !== null && isTerminal(options.rowStatus);

  const loadMore = useCallback(() => {
    if (query.hasNextPage && !query.isFetchingNextPage) void query.fetchNextPage();
  }, [query]);

  const reread = useCallback(() => {
    if (id === null) return;
    // Drops the cached pages outright rather than refetching them one by one:
    // an unfinished run has grown at the tail, and re-walking from -1 is both
    // simpler and the only way to pick up messages written since.
    void client.resetQueries({ queryKey: historyPagesKey(id) });
  }, [client, id]);

  return {
    snapshot: assembled.snapshot,
    fullyLoaded,
    pagesLoaded: pages.length,
    gaps,
    gapSummary: summariseGaps(gaps),
    results: resultsCompleteness(assembled.snapshot.results, { runTerminal, fullyLoaded }),
    unusable: assembled.unusable,
    loading: query.isPending && query.fetchStatus !== "idle",
    loadingMore: query.isFetchingNextPage,
    error: query.error,
    loadMore,
    reread,
  };
}
