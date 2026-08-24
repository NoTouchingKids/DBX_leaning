/**
 * React Query bindings for the HTTP API.
 *
 * Two policies are load-bearing here, and both come from the platform rather
 * than from taste:
 *
 * **Nothing polls.** On Databricks Free Edition the SQL warehouse is billed
 * by uptime, not by statement count, and its auto-stop floor is minutes. A
 * 10-second refresh on a page someone leaves open all day keeps the warehouse
 * awake for the whole working day for no benefit. So these queries refetch on
 * mount, on window focus, and when something we already know about changes —
 * a trigger, a cancel, a terminal status off the live stream. Live progress
 * arrives over SSE, which costs nothing extra; the HTTP layer is for the
 * things SSE does not carry (`live`, the run list).
 *
 * **A terminal run is immutable.** Once finished, its row and its message
 * history can never change, so they are cached with no expiry rather than
 * through a TTL scheme that would re-fetch a fact that cannot have moved.
 */

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import type { TriggerRequest } from "@/lib/api";
import {
  cancelRun,
  fetchBackfill,
  getHealthz,
  getModels,
  getRun,
  getWhoami,
  listRuns,
  triggerRun,
  type ListRunsParams,
  type RunDetail,
  type TriggerOutcome,
} from "@/lib/apiClient";
import { isTerminal, type Message } from "@/lib/envelope";
import { getTransport } from "@/transport/client";

export const qk = {
  runs: (params: ListRunsParams) => ["runs", params] as const,
  run: (runId: string) => ["run", runId] as const,
  history: (runId: string) => ["run-history", runId] as const,
  models: () => ["models"] as const,
  whoami: () => ["whoami"] as const,
  healthz: () => ["healthz"] as const,
};

/* ------------------------------------------------------------------ *
 * Reads
 * ------------------------------------------------------------------ */

export function useRunList(params: ListRunsParams = {}, options: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: qk.runs(params),
    queryFn: ({ signal }) => listRuns(params, signal),
    enabled: options.enabled ?? true,
    // Ordering is `updated_ts DESC`, so any row can move. Nothing in a list
    // response is safe to treat as immutable, terminal rows included.
    staleTime: 5_000,
  });
}

export function useRunDetail(runId: string | null | undefined) {
  return useQuery({
    queryKey: qk.run(runId ?? ""),
    queryFn: ({ signal }) => getRun(runId as string, signal),
    enabled: typeof runId === "string" && runId.length > 0,
    // Function form so the policy can depend on the answer: a terminal run's
    // row is frozen, an active one's `live` flag can flip at any moment.
    staleTime: (query) => {
      const data = query.state.data as RunDetail | undefined;
      return data && isTerminal(data.run.status) ? Infinity : 3_000;
    },
    retry: (count, error) =>
      // A 404 here means the registry has no such run — retrying cannot
      // change that, and it is the expected answer for a `registered: false`
      // trigger, which is a run that exists but was never written down.
      error instanceof Error && "status" in error && error.status === 404 ? false : count < 2,
  });
}

export function useModels() {
  return useQuery({
    queryKey: qk.models(),
    queryFn: ({ signal }) => getModels(signal),
    staleTime: 5 * 60_000,
  });
}

export function useWhoami() {
  return useQuery({
    queryKey: qk.whoami(),
    queryFn: ({ signal }) => getWhoami(signal),
    staleTime: Infinity,
  });
}

export function useHealthz() {
  return useQuery({
    queryKey: qk.healthz(),
    queryFn: ({ signal }) => getHealthz(signal),
    staleTime: 30_000,
  });
}

/* ------------------------------------------------------------------ *
 * Writes
 * ------------------------------------------------------------------ */

export function useTriggerRun() {
  const client = useQueryClient();
  return useMutation<TriggerOutcome, Error, TriggerRequest>({
    mutationFn: (body) => triggerRun(body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["runs"] });
    },
  });
}

export function useCancelRun(runId: string | null | undefined) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => cancelRun(runId as string),
    onSuccess: () => {
      // The 200 only means the cancel frame reached the job over its
      // WebSocket. The run is cancelled when a `status` message says so, and
      // that arrives on the stream — this just refreshes `live`.
      if (runId) void client.invalidateQueries({ queryKey: qk.run(runId) });
    },
  });
}

/* ------------------------------------------------------------------ *
 * Backfill
 * ------------------------------------------------------------------ */

const MAX_BACKFILL_PAGES = 40;

/** Walk `next_after_seq` until the server stops saying there is more. Capped:
 *  a paging loop with no bound is one server bug away from spinning. */
async function fetchRange(
  runId: string,
  afterSeq: number,
  signal?: AbortSignal,
): Promise<Message[]> {
  const out: Message[] = [];
  let cursor = afterSeq;
  for (let page = 0; page < MAX_BACKFILL_PAGES; page += 1) {
    const response = await fetchBackfill(runId, { after_seq: cursor }, signal);
    out.push(...response.messages);
    if (!response.more || response.next_after_seq <= cursor) break;
    cursor = response.next_after_seq;
  }
  return out;
}

/**
 * Push fetched messages into the run's live store.
 *
 * Filtered against what is already there, by `seq`. The store appends without
 * deduping — it is fed by one ordered source in normal operation — and a
 * backfill range routinely overlaps messages we already have.
 */
function ingest(runId: string, messages: readonly Message[]): number {
  const store = getTransport().getStore(runId);
  const snapshot = store.getSnapshot();
  const known = new Set<number>();
  for (const list of [snapshot.logs, snapshot.progress, snapshot.statuses, snapshot.results]) {
    for (const message of list) known.add(message.seq);
  }
  const fresh = messages.filter((message) => !known.has(message.seq));
  if (fresh.length > 0) store.ingest(fresh);
  return fresh.length;
}

/**
 * The whole history of a finished run, fetched once and kept.
 *
 * Only for terminal runs, and only when the local cache is empty: a finished
 * run has no live tail to wait for, so a first view has to fetch or show
 * nothing. An active run does NOT get this — backfilling on every reconnect
 * is the warehouse-uptime mistake this rewrite exists to avoid.
 */
export function useTerminalHistory(runId: string | null | undefined, enabled: boolean) {
  const query = useQuery({
    queryKey: qk.history(runId ?? ""),
    queryFn: async ({ signal }) => {
      const messages = await fetchRange(runId as string, -1, signal);
      return { messages, ingested: ingest(runId as string, messages) };
    },
    enabled: enabled && typeof runId === "string" && runId.length > 0,
    // Immutable by definition. Never refetched, never garbage collected
    // during the session — a second visit to this run costs no request.
    staleTime: Infinity,
    gcTime: Infinity,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  return query;
}

/**
 * User-triggered "fetch what's missing" for one observed gap.
 *
 * Deliberately manual. A gap can be permanent: the live path drops
 * `client_visible=false` logs and the backfill endpoint filters them out too,
 * so an automatic "backfill until contiguous" loop never terminates.
 */
export function useFetchGap(runId: string | null | undefined) {
  return useMutation({
    mutationFn: async (gap: { from: number; to: number }) => {
      if (!runId) return 0;
      const messages = await fetchRange(runId, gap.from - 1);
      const inRange = messages.filter((m) => m.seq >= gap.from && m.seq <= gap.to);
      return ingest(runId, inRange);
    },
  });
}

/* ------------------------------------------------------------------ *
 * Invalidation helper
 * ------------------------------------------------------------------ */

/**
 * Refresh a run's row because the live stream said something changed.
 *
 * This is what stands in for polling: the SSE channel already knows when a
 * status moves, so the one HTTP read that matters (`live`, which SSE does not
 * carry) is refreshed at exactly those moments and at no other time.
 */
export function useRefreshRunOnEvent(runId: string | null | undefined): () => void {
  const client = useQueryClient();
  return useCallback(() => {
    if (!runId) return;
    void client.invalidateQueries({ queryKey: qk.run(runId) });
    void client.invalidateQueries({ queryKey: ["runs"] });
  }, [client, runId]);
}

/** Exported for tests that need to seed or inspect the cache. */
export type { QueryClient };
