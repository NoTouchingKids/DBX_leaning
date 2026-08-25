import { useEffect, useState } from "react";

import { computeElapsedSeconds, type ElapsedInput } from "@/lib/elapsed";

/**
 * A ticking elapsed clock, frozen when the run is over.
 *
 * The timer only exists while the clock is live — a finished run's page has
 * no interval running behind it, which matters when someone leaves five run
 * tabs open all afternoon.
 */
export function useElapsedSeconds(input: ElapsedInput): number | null {
  const frozen = input.frozenAt != null;
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (frozen) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [frozen]);

  return computeElapsedSeconds(input, now);
}
