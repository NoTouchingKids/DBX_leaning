/**
 * `/runs` — a minimal cross-model list.
 *
 * Deliberately thin: M3 owns the real run-history page (capacity meter,
 * server/client-labelled filters, the stale-`RUNNING` treatment). This exists
 * because the model pages link here and a link to nothing is worse than a
 * link to a plain table.
 *
 * What is already correct and should survive M3:
 *
 *  - the columns are exactly what `list_runs` SELECTs, plus the injected
 *    `live`. There is no metric column because the endpoint returns none;
 *    adding one means an N+1 fetch of `/api/runs/{id}`, which is a decision,
 *    not a freebie.
 *  - `live` is not the status. `RUNNING` with no socket is a dead run nothing
 *    will ever finish, and it is styled as a warning.
 *  - ordering is `updated_ts DESC` and there is no cursor, so there are no
 *    sort controls and "load more" is a refetch with a bigger limit.
 */

import { Link, useSearchParams } from "react-router";

import { PageHead } from "@/components/layout/PageHead";
import { Button } from "@/components/ui/Button";
import { CopyButton } from "@/components/ui/CopyButton";
import { StatusPill } from "@/components/ui/StatusPill";
import { useRunList } from "@/hooks/useApi";
import { EMPTY, formatDateTime, formatDuration, truncateId } from "@/lib/format";
import { isTerminal } from "@/lib/envelope";
import { useState } from "react";

export function RunsPage() {
  const [params] = useSearchParams();
  const model = params.get("model") ?? undefined;
  const [limit, setLimit] = useState(50);
  const runs = useRunList({ model, limit });

  const rows = runs.data?.runs ?? [];
  const active = rows.filter((row) => !isTerminal(row.status)).length;

  return (
    <>
      <PageHead eyebrow="All models · GET /api/runs" title="Run history">
        A top-N window ordered by <code>updated_ts DESC</code> — not
        pagination, so a long-running old run reappears at the top every time
        it emits.
      </PageHead>

      <div className="mb-3 flex flex-wrap items-center gap-3 text-[0.76rem] text-dim">
        <span>
          {model === undefined ? "all models" : <code>{model}</code>} · {rows.length} rows
        </span>
        <span className="text-faint">
          {active} not yet terminal in this window (counted client-side, with the
          same predicate the server uses for the 5-task ceiling)
        </span>
        <span className="ml-auto flex gap-2">
          <Button onClick={() => setLimit((n) => Math.min(500, n * 2))} disabled={limit >= 500}>
            Load more ({limit})
          </Button>
          <Button onClick={() => void runs.refetch()}>↻ Refresh</Button>
        </span>
      </div>

      <div className="overflow-x-auto rounded-[10px] border border-line bg-raised">
        <table className="w-full min-w-[760px] border-collapse text-[0.78rem]">
          <thead>
            <tr>
              {["Run", "Model", "Status", "Job channel", "Started", "Duration", "Requested by"].map(
                (heading) => (
                  <th
                    key={heading}
                    className="border-b border-edge bg-paper px-3 py-2 text-left text-[0.6rem] font-bold tracking-wider text-faint uppercase"
                  >
                    {heading}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const stranded = row.status === "RUNNING" && !row.live;
              return (
                <tr key={row.run_id} className="border-b border-line last:border-b-0">
                  <td className="px-3 py-2 align-top">
                    <Link
                      to={`/models/${row.model}?run=${encodeURIComponent(row.run_id)}`}
                      className="font-mono text-[0.74rem] text-accent no-underline hover:underline"
                    >
                      {truncateId(row.run_id, 12, 4)}
                    </Link>
                    <CopyButton value={row.run_id} label="copy run id" />
                    {row.detail != null && row.detail !== "" && (
                      <div className="mt-0.5 max-w-[38ch] text-[0.68rem] leading-snug text-dim">
                        {row.detail}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2 align-top">
                    <code className="rounded border border-edge px-1 py-px text-[0.66rem] text-dim">
                      {row.model}
                    </code>
                  </td>
                  <td className="px-3 py-2 align-top">
                    <StatusPill state={row.status} />
                  </td>
                  <td
                    className={`px-3 py-2 align-top text-[0.7rem] ${stranded ? "font-semibold text-warn" : "text-dim"}`}
                  >
                    {row.live ? "connected" : stranded ? "no socket · stranded" : EMPTY}
                  </td>
                  <td className="px-3 py-2 align-top font-mono text-[0.72rem] whitespace-nowrap">
                    {formatDateTime(row.started_ts)}
                  </td>
                  <td className="px-3 py-2 align-top font-mono text-[0.72rem] whitespace-nowrap">
                    {formatDuration((row.updated_ts - row.started_ts) / 1000)}
                    {!isTerminal(row.status) && <span className="text-faint">+</span>}
                  </td>
                  <td className="px-3 py-2 align-top font-mono text-[0.72rem]">
                    {row.requested_by ?? EMPTY}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {rows.length === 0 && (
          <p className="px-4 py-10 text-center text-[0.8rem] text-faint">
            {runs.isPending ? "loading…" : "No runs in this window."}
          </p>
        )}
      </div>

      <p className="mt-3 text-[0.68rem] text-faint">
        Duration is <code>updated_ts − started_ts</code>: exact once terminal,
        and for a running row it measures to the last heartbeat rather than to
        now — hence the <code>+</code>.
      </p>
    </>
  );
}
