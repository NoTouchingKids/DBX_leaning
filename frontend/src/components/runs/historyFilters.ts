/**
 * Filter state for the run-history page, and the URL it round-trips through.
 *
 * The URL is the state. Not because deep-linking is a nice touch, but because
 * five model pages link here with `?model=<name>` already set — that link has
 * to survive a refresh and a paste into someone else's tab, and a `useState`
 * initialised from the URL once would silently stop tracking it the moment
 * the user navigated between two model pages' history links.
 *
 * WHERE EACH FILTER RUNS, and why it is labelled in the UI:
 *
 *  - `model`  — SERVER (`WHERE model = :model`, `app/repository.py::list_runs`).
 *  - `status` — SERVER, but ONE exact value (`WHERE status = :status`). There
 *               is no `IN`, so there is no multi-select to offer.
 *  - `limit`  — SERVER, 1..500. Not a page size: there is no offset and no
 *               cursor, so a bigger limit is a wider top-N window, not page 2.
 *  - `query`  — CLIENT. A substring pass over the rows already fetched. It is
 *               honest only while the window holds everything relevant, which
 *               is exactly why the control carries a `client` tag: when the
 *               window outgrows it the label is what makes the trade-off
 *               visible, rather than the filter quietly starting to lie.
 */

import type { ListRunsParams, Run } from "@/lib/apiClient";
import { RUN_STATUSES, type RunStatus } from "@/lib/envelope";

export interface HistoryFilters {
  /** server-side */
  model: string | null;
  /** server-side, one exact value */
  status: RunStatus | null;
  /** client-side substring over `run_id` and `job_run_id` */
  query: string;
  /** server-side window size, not a page size */
  limit: number;
}

/** The `limit` values the window control offers. `limit` accepts anything in
 *  1..500; these are just the rungs, and "load more" climbs them. */
export const LIMIT_STEPS = [50, 100, 250, 500] as const;

export const MIN_LIMIT = 1;
export const MAX_LIMIT = 500;
/** `Query(50, ge=1, le=500)` in `app/routes/runs.py::list_runs`. */
export const DEFAULT_LIMIT = 50;

export const DEFAULT_FILTERS: HistoryFilters = {
  model: null,
  status: null,
  query: "",
  limit: DEFAULT_LIMIT,
};

function nonEmpty(value: string | null): string | null {
  const trimmed = value?.trim() ?? "";
  return trimmed === "" ? null : trimmed;
}

/**
 * A hand-edited or stale URL is a real input here — the model pages build
 * these links by hand. An unrecognised status is dropped rather than sent on:
 * the server would accept it happily (`status` is an unvalidated string in the
 * query) and answer with zero rows, which reads as "no runs" instead of "that
 * is not a status".
 */
function asStatus(value: string | null): RunStatus | null {
  const upper = value?.trim().toUpperCase() ?? "";
  return (RUN_STATUSES as readonly string[]).includes(upper) ? (upper as RunStatus) : null;
}

/** Out-of-range is clamped, not passed through: FastAPI answers 422 for
 *  `limit=0` or `limit=9000`, and a 422 from a URL someone typed is a worse
 *  outcome than the nearest window that works. */
export function clampLimit(value: number | null | undefined): number {
  if (value === null || value === undefined || !Number.isFinite(value)) return DEFAULT_LIMIT;
  return Math.min(MAX_LIMIT, Math.max(MIN_LIMIT, Math.floor(value)));
}

export function parseHistoryFilters(params: URLSearchParams): HistoryFilters {
  const rawLimit = params.get("limit");
  return {
    model: nonEmpty(params.get("model")),
    status: asStatus(params.get("status")),
    // Deliberately untrimmed: this is a controlled input's value, and
    // trimming on every keystroke fights whoever is typing.
    query: params.get("q") ?? "",
    limit: rawLimit === null ? DEFAULT_LIMIT : clampLimit(Number(rawLimit)),
  };
}

/**
 * Defaults are omitted rather than written out, so `/runs` stays `/runs` and
 * the link a model page produces stays the one-parameter link it wrote.
 */
export function historyFiltersToParams(filters: HistoryFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.model !== null) params.set("model", filters.model);
  if (filters.status !== null) params.set("status", filters.status);
  if (filters.query.trim() !== "") params.set("q", filters.query);
  if (filters.limit !== DEFAULT_LIMIT) params.set("limit", String(filters.limit));
  return params;
}

/** What actually goes on the wire. `query` is absent by construction — there
 *  is no server-side run-id search, and inventing a parameter name for one
 *  would just be ignored. */
export function serverQuery(filters: HistoryFilters): ListRunsParams {
  return {
    limit: filters.limit,
    ...(filters.status === null ? {} : { status: filters.status }),
    ...(filters.model === null ? {} : { model: filters.model }),
  };
}

export function hasServerFilter(filters: HistoryFilters): boolean {
  return filters.model !== null || filters.status !== null;
}

export function hasAnyFilter(filters: HistoryFilters): boolean {
  return hasServerFilter(filters) || filters.query.trim() !== "";
}

/** Matches on `job_run_id` too: when a run has stranded, the Databricks job
 *  run id is the identifier an operator is holding, not the app's `run_id`. */
export function matchesQuery(row: Pick<Run, "run_id" | "job_run_id">, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (needle === "") return true;
  if (row.run_id.toLowerCase().includes(needle)) return true;
  return (row.job_run_id ?? "").toLowerCase().includes(needle);
}

/** The client-side pass, kept as one function so the page never filters
 *  inline and quietly grows a second, unlabelled client-side filter. */
export function applyClientFilters(rows: readonly Run[], filters: HistoryFilters): Run[] {
  return rows.filter((row) => matchesQuery(row, filters.query));
}

/**
 * "Load more" — the next rung up, because there is nothing else it can be.
 *
 * With no offset and no cursor, fetching more means asking for a wider window
 * and receiving the rows already on screen a second time. Escalating through
 * fixed steps keeps the query key stable enough for React Query to reuse a
 * window that was already fetched, which a `limit + 25` would not.
 */
export function nextLimit(current: number): number {
  const here = clampLimit(current);
  return LIMIT_STEPS.find((step) => step > here) ?? MAX_LIMIT;
}

export function canLoadMore(current: number): boolean {
  return clampLimit(current) < MAX_LIMIT;
}

/**
 * Did the server apply what we asked for?
 *
 * `GET /api/runs` echoes `filters: {status, model}` — it reports back what it
 * filtered on. Comparing is nearly free and catches the one failure this page
 * cannot otherwise see: a filter that was dropped somewhere between here and
 * the `WHERE` clause, which looks exactly like "there are no such runs".
 */
export function serverFilterMismatches(
  requested: HistoryFilters,
  echoed: { status: RunStatus | null; model: string | null } | undefined,
): string[] {
  if (echoed === undefined) return [];
  const out: string[] = [];
  if ((echoed.model ?? null) !== requested.model) out.push("model");
  if ((echoed.status ?? null) !== requested.status) out.push("status");
  return out;
}

/**
 * Options for the model select.
 *
 * The union of the models this frontend knows about, the models present in the
 * fetched window, and whatever is currently selected. The last two matter: the
 * filter is server-side, so a model that exists only in old history — or a
 * `?model=` someone pasted — must still be selectable, otherwise the control
 * silently resets a filter that is genuinely applied.
 */
export function modelOptions(
  known: readonly string[],
  rows: readonly Run[],
  selected: string | null,
): string[] {
  const names = new Set(known);
  for (const row of rows) names.add(row.model);
  if (selected !== null) names.add(selected);
  return [...names].sort();
}
