/**
 * `useRunStream(runId)` — the only thing a component should need to watch a
 * run.
 *
 * `useSyncExternalStore`, not `useState` + effect: the store is mutated from
 * a worker message callback, outside React's batching, and the tearing that
 * produces is intermittent and miserable to debug. This is the hook that API
 * exists for.
 *
 * The subscription is acquired inside the `subscribe` callback rather than in
 * render or a memo. React calls that callback's cleanup and re-invokes it on
 * StrictMode's mount/unmount/mount, so ref-counting stays balanced; a memo
 * that subscribed during render would increment twice and release once, and
 * leak a live EventSource per mount.
 *
 * There is no global store here, by design (ADR-001). Server state belongs to
 * React Query; run telemetry belongs to this; nothing else is shared.
 */

import { useCallback, useSyncExternalStore } from "react";

import { getTransport } from "./client";
import { RunStore, type RunSnapshot } from "./runStore";

export interface UseRunStreamOptions {
  /** Pass `false` while the run id is not known yet. */
  enabled?: boolean;
  /** From `GET /api/runs/{id}`. A terminal run is served from cache with no
   *  live channel — passing `true` is what avoids opening one. */
  terminal?: boolean;
}

/** Stands in for a run that is not being watched: permanently empty, never
 *  notifies. A shared instance so its snapshot identity is stable. */
const IDLE_STORE = new RunStore("");
const NEVER = () => () => {};

export function useRunStream(
  runId: string | null | undefined,
  options: UseRunStreamOptions = {},
): RunSnapshot {
  const { enabled = true, terminal = false } = options;
  const active = enabled && typeof runId === "string" && runId.length > 0;
  const key = active ? runId : null;

  const store = key === null ? IDLE_STORE : getTransport().getStore(key);

  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      if (key === null) return NEVER();
      const transport = getTransport();
      // `terminal` is read once, here. Learning mid-stream that a run
      // finished must not tear the subscription down and rebuild it — the
      // worker closes the channel itself on a terminal status.
      const release = transport.acquire(key, terminal);
      const unsubscribe = transport.getStore(key).subscribe(onStoreChange);
      return () => {
        unsubscribe();
        release();
      };
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- see above
    [key],
  );

  return useSyncExternalStore(subscribe, store.getSnapshot, store.getSnapshot);
}
