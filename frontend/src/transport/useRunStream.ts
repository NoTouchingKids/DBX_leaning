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

import { useCallback, useEffect, useSyncExternalStore } from "react";

import { getTransport } from "./client";
import { RunStore, type RunSnapshot } from "./runStore";

export interface UseRunStreamOptions {
  /** Pass `false` while the run id is not known yet. */
  enabled?: boolean;
  /**
   * Whether the run has already finished, from `GET /api/runs/{id}`.
   *
   * **`undefined` is a third state and not a synonym for `false`.** It means
   * "not known yet": the run hydrates from IndexedDB and NO live channel is
   * opened until the answer arrives. A caller that genuinely knows — one that
   * just triggered a run, say — should pass `false` explicitly and get a
   * channel immediately.
   *
   * Guessing `false` while a fetch is in flight is what opened a live channel
   * to runs that were already over. See the effect below.
   */
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
      // `terminal` is read once, here, and may be undefined — which the
      // worker treats as "hydrate, but open nothing yet". Learning mid-stream
      // that a run finished must not tear the subscription down and rebuild
      // it, so the answer arrives separately, in the effect below.
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

  // The answer to the question `subscribe` could not answer.
  //
  // `terminal` is read once inside `subscribe` and is deliberately not a
  // dependency of it — re-subscribing would tear a live run's stream down and
  // rebuild it every time it finished. So the answer is forwarded here
  // instead, and until it arrives the worker holds off on opening anything.
  //
  // An earlier version of this only forwarded `true`, to *close* a channel
  // that had already opened. It does not work, and a real browser showed why:
  // `GET /api/runs/{id}` is a network round trip while the IndexedDB hydrate
  // is sub-millisecond, so the channel was always already open by the time
  // the answer landed. Gating beats chasing.
  //
  // Invisible to every offline test, because a fake EventSource costs nothing
  // to open and so a wasted one looks exactly like no connection at all.
  useEffect(() => {
    if (key === null || terminal === undefined) return;
    getTransport().setTerminality(key, terminal);
  }, [key, terminal]);

  return useSyncExternalStore(subscribe, store.getSnapshot, store.getSnapshot);
}
