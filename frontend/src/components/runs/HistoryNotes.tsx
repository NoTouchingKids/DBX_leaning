/**
 * What this table cannot tell you.
 *
 * Kept on the page rather than in a doc, because every one of these is a
 * property of the endpoint that a reader will otherwise infer wrongly from
 * what the table shows: that the absence of a metric column means the runs
 * have no metrics, that a client-side filter is a filter, that a wider window
 * is a next page, or that a duration on a running row is current.
 */

import type { ReactNode } from "react";

import { Card } from "@/components/ui/Card";

function Note({ heading, children }: { heading: string; children: ReactNode }) {
  return (
    <li className="border-b border-dashed border-line pb-2.5 last:border-b-0 last:pb-0">
      <b className="text-ink">{heading}</b>{" "}
      <span className="text-dim">{children}</span>
    </li>
  );
}

export function HistoryNotes() {
  return (
    <Card title="Reading this table" hint="properties of GET /api/runs" className="mt-5">
      <ul className="m-0 flex list-none flex-col gap-2.5 p-0 text-[0.74rem] leading-relaxed">
        <Note heading="There is no result column, and there cannot be one from this endpoint.">
          <code>list_runs</code> selects <code>run_id, job_run_id, model, status, detail,
          started_ts, updated_ts, requested_by</code>. No <code>mip_gap</code>, no{" "}
          <code>val_mae</code>, no <code>max_rhat</code>. The <em>detail</em> sub-line is the only
          result-ish text available and it is free-form text the job last wrote, not a parsed
          metric. A real per-row metric means an N+1 fetch of <code>/api/runs/&#123;id&#125;</code>{" "}
          — a decision to take deliberately, not a freebie.
        </Note>
        <Note heading="“Job channel” is the most valuable column, and it is not the status.">
          The endpoint injects <code>live = run_id in hub.job_sockets.run_ids</code> — a live
          WebSocket check, independent of the stored row. <code>RUNNING</code> with no socket means
          the job died or the app restarted. There is no reaper: nothing will move that row to a
          terminal status, and no endpoint here can. The page surfaces it and offers no action,
          because there is no action to offer.
        </Note>
        <Note heading="Model and status filter server-side; the id search does not.">
          Both are real <code>WHERE</code> clauses, but <code>status</code> is one exact value —{" "}
          <code>status = :status</code>, no <code>IN</code> — so there is no multi-select. The id
          search is a pass over the fetched window, which is honest only while the window holds
          everything relevant. Every control says which it is.
        </Note>
        <Note heading="This is a top-N window, not pagination.">
          <code>limit</code> is 1–500 with no offset and no cursor, so “load more” is a refetch
          with a bigger limit and the rows you already have come back again. Ordering is{" "}
          <code>updated_ts DESC</code> — last update, not start time — so a long-running old run
          jumps back to the top every time it emits. The header offers no sort controls, because
          the server can honour none of them.
        </Note>
        <Note heading="Duration is derived, and stale by definition while a run is unfinished.">
          <code>updated_ts − started_ts</code>, both epoch milliseconds. Exact once terminal; for a
          running row it measures to the last heartbeat rather than to now, hence the{" "}
          <code>+</code>. <code>QUEUED</code> shows <code>—</code> rather than <code>00:00</code>.{" "}
          <code>requested_by</code> comes from the <code>x-forwarded-email</code> header and is
          nullable — the app says out loud that it is cosmetic identity, so this page displays it
          and never filters by it.
        </Note>
      </ul>
    </Card>
  );
}
