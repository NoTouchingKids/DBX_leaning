import type { UiRunState } from "@/lib/envelope";

/** Pill styling per state. `STARTING` is client-only and reads as
 *  provisional rather than as a seventh status. */
export const PILL_STYLE: Record<UiRunState, string> = {
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
