/**
 * The table.
 *
 * The columns are exactly what `repo.list_runs` SELECTs, plus the injected
 * `live`. There is NO result-metric column and there cannot be one from this
 * endpoint: it returns `run_id, job_run_id, model, status, detail,
 * started_ts, updated_ts, requested_by` and nothing else. `detail` is
 * free-form text the job last wrote — a status string, not a parsed number —
 * so it renders as a sub-line, never as a value in a metric column. A real
 * per-row metric means an N+1 fetch of `/api/runs/{id}`; that is a decision
 * to take deliberately later, not a freebie now.
 *
 * No sortable headers, because `ORDER BY updated_ts DESC` is the only
 * ordering the server can produce.
 */

import { Link } from "react-router";

import { CopyButton } from "@/components/ui/CopyButton";
import { StatusPill } from "@/components/ui/StatusPill";
import type { Run } from "@/lib/apiClient";
import { EMPTY, formatDateTime, formatDuration, truncateId } from "@/lib/format";
import { isTerminal } from "@/lib/envelope";

import { LiveCell } from "./LiveCell";
import { rowDurationSeconds, runLiveness } from "./liveness";

const HEADINGS = [
  "Run",
  "Model",
  "Status",
  "Job channel",
  "Started",
  "Duration",
  "Requested by",
] as const;

const CELL = "px-3 py-2 align-top";
const NUM = `${CELL} font-mono text-[0.72rem] whitespace-nowrap tabular-nums`;

export function RunsTable({ rows }: { rows: readonly Run[] }) {
  return (
    <div className="overflow-x-auto rounded-[10px] border border-line bg-raised">
      <table className="w-full min-w-[820px] border-collapse text-[0.78rem]">
        <thead>
          <tr>
            {HEADINGS.map((heading) => (
              <th
                key={heading}
                scope="col"
                className="border-b border-edge bg-paper px-3 py-2 text-left text-[0.6rem] font-bold tracking-wider whitespace-nowrap text-faint uppercase"
              >
                {heading}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const liveness = runLiveness(row);
            const stranded = liveness === "stranded";
            const duration = rowDurationSeconds(row);
            return (
              <tr
                key={row.run_id}
                className={`border-b border-line last:border-b-0 ${
                  stranded ? "border-l-[3px] border-l-warn bg-warn-soft/40" : ""
                }`}
              >
                <td className={CELL}>
                  {/* Where a row leads depends on whether the run is over.
                      A finished run goes to `/runs/:runId`, which is immutable
                      and served from cache with no live channel. A run that is
                      still going goes to its model page, where it can actually
                      be watched — and cancelled, which the detail page cannot
                      do. Sending a live run to the past-run view would show it
                      frozen at whatever Delta happened to hold. */}
                  <Link
                    to={
                      isTerminal(row.status)
                        ? `/runs/${encodeURIComponent(row.run_id)}`
                        : `/models/${row.model}?run=${encodeURIComponent(row.run_id)}`
                    }
                    className="font-mono text-[0.74rem] text-accent no-underline hover:underline"
                  >
                    {truncateId(row.run_id, 12, 4)}
                  </Link>
                  <CopyButton value={row.run_id} label="copy run id" />
                  {row.detail !== null && row.detail !== "" && (
                    <div className="mt-0.5 max-w-[38ch] text-[0.68rem] leading-snug text-dim">
                      {row.detail}
                    </div>
                  )}
                  {stranded && row.job_run_id !== null && (
                    // The Databricks job run id, shown only where it is
                    // actionable outside this app. Not a button: the app
                    // cannot reach a job it has no socket to.
                    <div className="mt-0.5 font-mono text-[0.66rem] text-warn">
                      job run {row.job_run_id}
                      <CopyButton value={row.job_run_id} label="copy job run id" />
                    </div>
                  )}
                </td>

                <td className={CELL}>
                  <code className="rounded border border-edge px-1 py-px text-[0.66rem] whitespace-nowrap text-dim">
                    {row.model}
                  </code>
                </td>

                <td className={CELL}>
                  <StatusPill state={row.status} />
                </td>

                <td className={`${CELL} text-[0.7rem]`}>
                  <LiveCell liveness={liveness} />
                </td>

                <td className={NUM}>{formatDateTime(row.started_ts)}</td>

                <td className={NUM}>
                  {duration === null ? (
                    <span className="text-faint">{EMPTY}</span>
                  ) : (
                    <>
                      {formatDuration(duration)}
                      {/* Measured to the last heartbeat, not to now. */}
                      {!isTerminal(row.status) && <span className="text-faint">+</span>}
                    </>
                  )}
                </td>

                <td className={NUM}>{row.requested_by ?? <span className="text-faint">{EMPTY}</span>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
