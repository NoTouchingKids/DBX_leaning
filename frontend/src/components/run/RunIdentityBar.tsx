/**
 * Title, subtitle, and the stat chips: state, elapsed, connection, run id.
 *
 * The connection chip is deliberately separate from the state chip. One is
 * the run; the other is our view of it. A dropped SSE connection says nothing
 * about whether the job is still solving, and conflating the two turns every
 * ingress hiccup into an apparent failure.
 */

import type { ReactNode } from "react";

import type { UiRunState } from "@/lib/envelope";
import { EMPTY, formatDuration, truncateId } from "@/lib/format";
import type { ConnectionState } from "@/transport/protocol";
import { CopyButton } from "@/components/ui/CopyButton";
import { DOT_COLOR } from "@/components/ui/StatusPill";

function Chip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-16 rounded-lg border border-edge bg-raised px-2.5 py-1.5">
      <div className="text-[0.6rem] tracking-wide text-faint uppercase">{label}</div>
      <div className="mt-0.5 flex items-center gap-1.5 font-mono text-[0.8rem] font-semibold">
        {children}
      </div>
    </div>
  );
}

const CONNECTION_TEXT: Record<ConnectionState, string> = {
  idle: "no channel",
  connecting: "connecting",
  open: "live",
  failed: "gave up",
};

const CONNECTION_TONE: Record<ConnectionState, string> = {
  idle: "text-faint",
  connecting: "text-warn",
  open: "text-good",
  failed: "text-bad",
};

export function RunIdentityBar({
  title,
  subtitle,
  state,
  elapsedSeconds,
  connection,
  runId,
  extraChips,
}: {
  title: string;
  subtitle: ReactNode;
  state: UiRunState | null;
  elapsedSeconds: number | null;
  connection: ConnectionState;
  runId: string | null;
  /** Where a per-model page hangs its one headline number. */
  extraChips?: ReactNode;
}) {
  return (
    <div className="mb-4 flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0">
        <h2 className="m-0 mb-0.5 text-[1.1rem] tracking-tight">{title}</h2>
        <div className="text-[0.78rem] text-dim">{subtitle}</div>
      </div>
      <div className="flex flex-wrap gap-2">
        <Chip label="Run state">
          <span
            aria-hidden
            className={`h-[7px] w-[7px] flex-none rounded-full bg-current ${state ? DOT_COLOR[state] : "text-faint"}`}
          />
          {state === null ? EMPTY : state.toLowerCase()}
        </Chip>
        <Chip label="Elapsed">{formatDuration(elapsedSeconds)}</Chip>
        <Chip label="Stream">
          <span className={CONNECTION_TONE[connection]}>{CONNECTION_TEXT[connection]}</span>
        </Chip>
        {extraChips}
        {runId !== null && (
          <Chip label="Run ID">
            <span className="font-normal" title={runId}>
              {truncateId(runId)}
            </span>
            <CopyButton value={runId} label="copy run id" />
          </Chip>
        )}
      </div>
    </div>
  );
}
