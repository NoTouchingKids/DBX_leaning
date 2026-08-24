/**
 * The filter bar.
 *
 * Every control carries a `server` or `client` badge — see `ScopeTag`. Two
 * things are deliberately absent:
 *
 *  - **Column sorting.** Ordering is `ORDER BY updated_ts DESC` and there is
 *    no sort parameter. A client-side sort would only reorder the fetched
 *    window, which is worse than not offering it: it looks like a sort of the
 *    whole history and is not one.
 *  - **Auto-refresh.** The wireframe drew a 10-second poll. On Free Edition
 *    the SQL warehouse behind this read is billed by uptime with an auto-stop
 *    floor of minutes, so a tab left open on a 10s interval keeps it awake all
 *    day. Refresh is manual, and refetch-on-focus is bounded by human
 *    attention. That is a cost decision, so the toolbar states it.
 */

import type { ReactNode } from "react";

import { Button } from "@/components/ui/Button";
import { RUN_STATUSES } from "@/lib/envelope";
import { formatClock } from "@/lib/format";

import { LIMIT_STEPS, type HistoryFilters } from "./historyFilters";
import { ScopeTag } from "./ScopeTag";

const INPUT =
  "rounded-md border border-edge bg-raised px-2 py-1.5 text-[0.74rem] text-ink min-w-[9rem]";

function Field({
  label,
  scope,
  children,
}: {
  label: string;
  scope: "server" | "client";
  children: ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="flex items-center gap-1.5 text-[0.58rem] font-bold tracking-[0.08em] text-faint uppercase">
        {label} <ScopeTag scope={scope} />
      </span>
      {children}
    </label>
  );
}

export function HistoryToolbar({
  filters,
  models,
  onChange,
  onRefresh,
  isFetching,
  updatedAt,
}: {
  filters: HistoryFilters;
  models: readonly string[];
  onChange: (patch: Partial<HistoryFilters>) => void;
  onRefresh: () => void;
  isFetching: boolean;
  updatedAt: number | null;
}) {
  // A hand-edited `?limit=` that is not one of the rungs still has to be
  // selectable, or the control would silently snap the window to something
  // the user did not ask for.
  const windowSteps: number[] = LIMIT_STEPS.includes(filters.limit as (typeof LIMIT_STEPS)[number])
    ? [...LIMIT_STEPS]
    : [...LIMIT_STEPS, filters.limit].sort((a, b) => a - b);

  return (
    <div className="mb-3 flex flex-wrap items-end gap-2.5">
      <Field label="Run ID" scope="client">
        <input
          type="text"
          value={filters.query}
          placeholder="run-9c4e…"
          spellCheck={false}
          onChange={(event) => onChange({ query: event.target.value })}
          className={`${INPUT} font-mono`}
        />
      </Field>

      <Field label="Model" scope="server">
        <select
          value={filters.model ?? ""}
          onChange={(event) => onChange({ model: event.target.value || null })}
          className={INPUT}
        >
          <option value="">All models</option>
          {models.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </Field>

      <Field label="Status" scope="server">
        {/* One exact value: the query is `WHERE status = :status`, with no
            `IN`, so there is no multi-select to offer here. */}
        <select
          value={filters.status ?? ""}
          onChange={(event) =>
            onChange({
              status: RUN_STATUSES.find((status) => status === event.target.value) ?? null,
            })
          }
          className={INPUT}
        >
          <option value="">Any status</option>
          {RUN_STATUSES.map((status) => (
            <option key={status} value={status}>
              {status}
            </option>
          ))}
        </select>
      </Field>

      <Field label="Window" scope="server">
        <select
          value={filters.limit}
          onChange={(event) => onChange({ limit: Number(event.target.value) })}
          className={INPUT}
          title="Window size, not a page size — there is no offset or cursor"
        >
          {windowSteps.map((step) => (
            <option key={step} value={step}>
              top {step} rows
            </option>
          ))}
        </select>
      </Field>

      <div className="ml-auto flex items-center gap-2">
        <span className="max-w-[22ch] text-right text-[0.64rem] leading-snug text-faint">
          No auto-refresh — the warehouse behind this read is billed by uptime.
          {updatedAt !== null && ` Read at ${formatClock(updatedAt)}.`}
        </span>
        <Button onClick={onRefresh} disabled={isFetching}>
          {isFetching ? "…" : "↻"} Refresh
        </Button>
      </div>
    </div>
  );
}
