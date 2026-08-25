import { useEffect, useState } from "react";

import type { RunSnapshot } from "@/transport/runStore";
import { useRunStream, type UseRunStreamOptions } from "@/transport/useRunStream";

/**
 * `useRunStream` plus a manual reconnect.
 *
 * The transport gives up after ten consecutive failures and reports
 * `connection: "failed"`. That is the right default — `EventSource` retries
 * forever and cannot be capped declaratively — but it leaves no way back, and
 * "the ingress was unhappy for a minute" is a perfectly recoverable situation
 * someone should be able to retry.
 *
 * There is no reconnect verb in the worker protocol, so this releases the
 * subscription for one tick and re-acquires it. Dropping the last subscriber
 * deletes the hub's connection record entirely, which is what resets the
 * failure counter and lets a fresh `EventSource` open.
 *
 * The snapshot in hand at the moment of the click is held across that tick.
 * Without it the page blinks through an empty store — a blank log pane — on
 * the way to reconnecting, which looks exactly like data loss.
 */
export function useReconnectableRunStream(
  runId: string | null | undefined,
  options: UseRunStreamOptions = {},
): { snapshot: RunSnapshot; reconnect: () => void } {
  const [held, setHeld] = useState<RunSnapshot | null>(null);
  const live = useRunStream(runId, {
    ...options,
    enabled: (options.enabled ?? true) && held === null,
  });

  useEffect(() => {
    if (held === null) return;
    const id = setTimeout(() => setHeld(null), 0);
    return () => clearTimeout(id);
  }, [held]);

  return {
    snapshot: held ?? live,
    // `live` is captured from this render, which is exactly the snapshot the
    // user is looking at when they press the button.
    reconnect: () => setHeld(live),
  };
}
