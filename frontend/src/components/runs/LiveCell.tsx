/**
 * The `live` column, which is the reason this page exists.
 *
 * Four states, not two — see `liveness.ts`. The one that matters is
 * `stranded`: `RUNNING` with no job socket. Nothing will move that row to a
 * terminal status, because there is no reaper, and this app has no endpoint
 * that could. So it is styled as a warning and given no button: an action
 * that cannot act is worse than no action, because it teaches people to click
 * it and wait.
 */

import { EMPTY } from "@/lib/format";

import type { Liveness } from "./liveness";

const DOT = "inline-block h-[7px] w-[7px] shrink-0 rounded-full";

export function LiveCell({ liveness }: { liveness: Liveness }) {
  if (liveness === "finished") {
    // `live` on a terminal row says nothing: the run is over either way.
    return <span className="text-faint">{EMPTY}</span>;
  }

  if (liveness === "stranded") {
    return (
      <span
        className="flex items-center gap-1.5 font-semibold text-warn"
        title="RUNNING with no job WebSocket — the job died or the app restarted. There is no reaper: nothing will move this row to a terminal status, and no endpoint in this API can."
      >
        <span className={`${DOT} bg-warn`} /> no socket · stranded
      </span>
    );
  }

  if (liveness === "connected") {
    return (
      <span className="flex items-center gap-1.5 text-info">
        <span className={`${DOT} live-dot bg-info text-info`} /> connected
      </span>
    );
  }

  // QUEUED with no socket. Normal: the job has not attached yet.
  return (
    <span className="flex items-center gap-1.5 text-dim">
      <span className={`${DOT} border border-edge`} /> not yet
    </span>
  );
}
