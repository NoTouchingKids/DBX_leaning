/**
 * `prefers-reduced-motion`, as a hook.
 *
 * Shared so the nine signature animations answer it the same way. The rule
 * throughout this app: reduced motion disables the TRANSITION, never the
 * information. An animation that conveys a state must still convey it — it
 * just arrives at each frame instantly instead of easing into it, and
 * anything purely ambient (drifting, pulsing, shimmer) stops entirely.
 */

import { useEffect, useState } from "react";

const QUERY = "(prefers-reduced-motion: reduce)";

export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() =>
    typeof matchMedia === "function" ? matchMedia(QUERY).matches : false,
  );

  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const mq = matchMedia(QUERY);
    const onChange = () => setReduced(mq.matches);
    mq.addEventListener("change", onChange);
    // The OS setting can change while the tab is open, and a user who turns
    // it on mid-run is asking for it to take effect now.
    onChange();
    return () => mq.removeEventListener("change", onChange);
  }, []);

  return reduced;
}
