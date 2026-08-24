/**
 * The things about a past run that are worth interrupting the page for.
 *
 * Every one of them is a statement, and only one of them comes with an
 * action. That asymmetry is deliberate: the two most alarming states this
 * page can show — a stranded `RUNNING` row and a permanent seq gap — have no
 * remedy in this API, and offering a button that cannot work is worse than
 * offering none.
 */

import { Link } from "react-router";

import { Callout } from "@/components/ui/Callout";
import { formatCount } from "@/lib/format";

import type { GapSummary } from "./history";

export function StrandedNotice({ jobRunId }: { jobRunId: string | null }) {
  return (
    <Callout tone="warn" title="Stranded: RUNNING, with no channel to the job">
      The registry still says this run is <code>RUNNING</code>, but the app has
      no WebSocket to it — the job died, or the app was redeployed while it was
      running. There is no reaper, so nothing will ever move this row to a
      terminal status, and no endpoint here can fix it. Whatever the job wrote
      before it stopped is below and is durable.
      {jobRunId !== null && (
        <>
          {" "}
          Its Databricks job run is <code>{jobRunId}</code>, if you want to
          check what became of it from the CLI.
        </>
      )}
    </Callout>
  );
}

export function LiveNotice({ model, runId }: { model: string; runId: string }) {
  return (
    <Callout tone="info" title="This run is still live">
      What is below was read from Delta once, and it stops where that read
      stopped. The model page holds the open stream.{" "}
      <Link
        to={`/models/${model}?run=${encodeURIComponent(runId)}`}
        className="font-semibold text-accent"
      >
        Watch it live →
      </Link>
    </Callout>
  );
}

/**
 * A gap here is not a loading state, and this is the page where that has to
 * be said out loud. On the live page a gap is usually a reconnect and a
 * backfill closes it. Here, everything has already been backfilled: the seqs
 * that are missing are missing because `messages_since` filters
 * `client_visible = false` out of the log branch, exactly as the live path
 * does. They are job-internal lines that were never meant for a browser, and
 * no request will ever return them.
 */
export function GapNotice({ summary }: { summary: GapSummary }) {
  return (
    <Callout tone="info" title="Gaps in the seq stream — permanent">
      {formatCount(summary.count)} break{summary.count === 1 ? "" : "s"} in the
      sequence, {formatCount(summary.missing)} seq value
      {summary.missing === 1 ? "" : "s"} unaccounted for. Backfill filters
      non-client-visible logs, so these are almost certainly job-internal lines
      that were never sent to a browser. There is no action, because there is
      nothing to fetch: paging further cannot close them.
    </Callout>
  );
}

export function UnusableNotice({ count }: { count: number }) {
  return (
    <Callout tone="warn" title="Some rows could not be read">
      {formatCount(count)} row{count === 1 ? "" : "s"} came back in a shape
      this client could not turn into a message and were dropped. The history
      below is therefore incomplete in a way the seq numbers will not show.
    </Callout>
  );
}
