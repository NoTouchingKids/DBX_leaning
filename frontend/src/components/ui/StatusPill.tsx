import type { UiRunState } from "@/lib/envelope";

/**
 * The run's state as a pill.
 *
 * Covers `UiRunState`, which is `RunStatus` plus the client-only `STARTING`.
 * `STARTING` never came off the wire — it is the optimistic frame between a
 * 202 and the first real message — so it is styled as provisional rather than
 * as another status.
 */
const STYLES: Record<UiRunState, string> = {
  STARTING: "bg-accent-soft text-accent-ink border-accent",
  QUEUED: "bg-info-soft text-info border-info",
  RUNNING: "bg-info-soft text-info border-info",
  SUCCEEDED: "bg-good-soft text-good border-good",
  FAILED: "bg-bad-soft text-bad border-bad",
  CANCELLED: "bg-idle-soft text-idle border-idle",
  INFEASIBLE: "bg-warn-soft text-warn border-warn",
};

export const DOT_COLOR: Record<UiRunState, string> = {
  STARTING: "text-accent",
  QUEUED: "text-info",
  RUNNING: "text-info",
  SUCCEEDED: "text-good",
  FAILED: "text-bad",
  CANCELLED: "text-idle",
  INFEASIBLE: "text-warn",
};

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
      className={`inline-block rounded-full border px-2 py-[.16rem] text-[0.62rem] font-bold tracking-wider uppercase ${STYLES[state]}`}
    >
      {state.toLowerCase()}
    </span>
  );
}
