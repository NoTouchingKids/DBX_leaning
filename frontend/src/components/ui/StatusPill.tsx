import type { UiRunState } from "@/lib/envelope";
import { PILL_STYLE } from "./runStateStyles";

/**
 * The run's state as a pill. Covers `UiRunState` — `RunStatus` plus the
 * client-only `STARTING`, which never came off the wire and must never be
 * compared against something that did.
 */
export function StatusPill({ state }: { state: UiRunState | null }) {
  if (state === null) {
    return (
      <span className="inline-block rounded-full border border-line px-2 py-[.16rem] text-[0.62rem] font-bold tracking-wider text-faint uppercase">
        unknown
      </span>
    );
  }
  return (
    <span
      className={`inline-block rounded-full border px-2 py-[.16rem] text-[0.62rem] font-bold tracking-wider uppercase ${PILL_STYLE[state]}`}
    >
      {state.toLowerCase()}
    </span>
  );
}
