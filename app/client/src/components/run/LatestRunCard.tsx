/**
 * The rail's status card: what state the watched run is in, plus a way to
 * look at an earlier one.
 *
 * The run picker is deliberately a plain select over `GET /api/runs?model=…`.
 * That filter is applied SERVER-side (`repo.list_runs` takes `model`), so it
 * is not a client-side sieve over a top-N window that quietly goes wrong once
 * the window fills.
 */

import { Link } from "react-router";

import { Card } from "@/components/ui/Card";
import { StatusPill } from "@/components/ui/StatusPill";
import type { Run } from "@/lib/apiClient";
import type { UiRunState } from "@/lib/envelope";
import { EMPTY, formatClock, formatDateTime, formatDuration, truncateId } from "@/lib/format";
import type { ReactNode } from "react";

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-4 border-b border-dashed border-line py-1.5 text-[0.78rem] last:border-b-0">
      <span className="text-dim">{label}</span>
      <span className="text-right font-mono font-semibold break-all">{children}</span>
    </div>
  );
}

export function LatestRunCard({
  modelName,
  state,
  live,
  run,
  elapsedSeconds,
  recent,
  selectedRunId,
  onSelectRun,
  extraRows,
}: {
  modelName: string;
  state: UiRunState | null;
  live: boolean | undefined;
  run: Omit<Run, "live"> | undefined;
  elapsedSeconds: number | null;
  recent: Run[];
  selectedRunId: string | null;
  onSelectRun: (runId: string) => void;
  /** Per-model headline numbers slot in here. */
  extraRows?: ReactNode;
}) {
  return (
    <Card title="Latest run" bodyClassName="px-4 pt-2 pb-4">
      <Row label="Status">
        <StatusPill state={state} />
      </Row>
      <Row label="Job channel">
        {live === undefined ? EMPTY : live ? "connected" : "no socket"}
      </Row>
      {extraRows}
      <Row label="Started">{run ? formatClock(run.started_ts) : EMPTY}</Row>
      <Row label="Elapsed">{formatDuration(elapsedSeconds)}</Row>
      {run?.detail != null && run.detail !== "" && (
        <div className="border-b border-dashed border-line py-1.5 text-[0.72rem] leading-relaxed text-dim last:border-b-0">
          {/* Free-form text the job last wrote. Not a parsed metric. */}
          {run.detail}
        </div>
      )}
      <Row label="Requested by">{run?.requested_by ?? EMPTY}</Row>

      {recent.length > 0 && (
        <label className="mt-3 block text-[0.68rem] text-dim">
          Recent runs for this model
          <select
            value={selectedRunId ?? ""}
            onChange={(event) => onSelectRun(event.target.value)}
            className="mt-1 w-full rounded-md border border-edge bg-paper px-2 py-1 font-mono text-[0.7rem]"
          >
            {selectedRunId === null && <option value="">select a run…</option>}
            {recent.map((option) => (
              <option key={option.run_id} value={option.run_id}>
                {truncateId(option.run_id, 8, 4)} · {option.status.toLowerCase()} ·{" "}
                {formatDateTime(option.updated_ts)}
              </option>
            ))}
          </select>
        </label>
      )}

      <Link
        to={`/runs?model=${encodeURIComponent(modelName)}`}
        className="mt-3 block text-right text-[0.76rem] font-semibold text-accent no-underline hover:underline"
      >
        View previous runs →
      </Link>
    </Card>
  );
}
