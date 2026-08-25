/**
 * Everything that is wrong, separated into "the run" and "our view of it".
 *
 * These are visually distinct on purpose. A failed SSE connection or a hole
 * in the seq stream is the browser's problem — the job carries on solving and
 * the Delta writer carries on persisting either way. Styling those the same
 * as a failed run would train someone to read a network hiccup as a lost run.
 */

import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { formatCount } from "@/lib/format";
import type { Gap } from "@/transport/runStore";
import type { ConnectionState } from "@/transport/protocol";

export function HealthNotices({
  stranded,
  jobRunId,
  connection,
  consecutiveFailures,
  onReconnect,
  gaps,
  onFetchGap,
  fetchingGap,
  droppedProgress,
}: {
  stranded: boolean;
  jobRunId: string | null;
  connection: ConnectionState;
  consecutiveFailures: number;
  onReconnect: () => void;
  gaps: readonly Gap[];
  onFetchGap: (gap: Gap) => void;
  fetchingGap: boolean;
  droppedProgress: number;
}) {
  const missing = gaps.reduce((sum, gap) => sum + (gap.to - gap.from + 1), 0);

  return (
    <div className="mb-4 flex flex-col gap-2 empty:mb-0">
      {stranded && (
        <Callout tone="warn" title="This run is stranded, and nothing can un-strand it">
          {`The registry says RUNNING but there is no live WebSocket to the job — it died, or the app restarted while it was going. There is no reaper: this row will never reach a terminal status on its own.` +
            (jobRunId
              ? `\nIts telemetry up to the last flush is still durable in Delta. Job run ${jobRunId} can be checked or killed from the Databricks CLI.`
              : "")}
        </Callout>
      )}

      {connection === "failed" && (
        <Callout
          tone="bad"
          title="Live updates stopped — this is our connection, not the run"
          actions={
            <Button variant="ghost" onClick={onReconnect}>
              ↻ Reconnect
            </Button>
          }
        >
          {`Gave up after ${formatCount(consecutiveFailures)} consecutive failures to open the stream. The job is unaffected and keeps writing to Delta; reconnecting resumes from the last sequence number this browser saw.`}
        </Callout>
      )}

      {gaps.length > 0 && (
        <Callout
          tone="info"
          title={`${formatCount(gaps.length)} gap${gaps.length === 1 ? "" : "s"} in the message sequence`}
          actions={gaps.map((gap) => (
            <Button
              key={`${gap.from}-${gap.to}`}
              variant="ghost"
              disabled={fetchingGap}
              onClick={() => onFetchGap(gap)}
            >
              {fetchingGap ? "fetching…" : `fetch seq ${gap.from}–${gap.to}`}
            </Button>
          ))}
        >
          {`About ${formatCount(missing)} messages were skipped on the live path. Fetching is manual and may not close the gap: the live path drops logs marked client_visible=false and the backfill endpoint filters them out too, so some holes are permanent by design.`}
        </Callout>
      )}

      {droppedProgress > 0 && (
        <Callout tone="info" title="Older progress points were discarded">
          {`${formatCount(droppedProgress)} progress messages fell off this browser's in-memory cap. Charts start from what is still held; the full history is in Delta.`}
        </Callout>
      )}
    </div>
  );
}
