/**
 * "No rows" has several causes and only one of them is "there are no runs".
 *
 * Getting this wrong is how a server-side filter becomes invisible: a status
 * filter narrows the whole history, not just the window, so an empty table
 * under `status=INFEASIBLE` means "none, ever", while an empty table under a
 * client-side id search means "none in the top N you fetched" — which is a
 * completely different instruction to the person reading it.
 */

import type { HistoryFilters } from "./historyFilters";

export interface EmptyStateCopy {
  title: string;
  body: string;
}

export function describeEmptyState(
  filters: HistoryFilters,
  /** Rows the server returned, before the client-side pass. Non-zero here
   *  with zero on screen means the id search did the hiding. */
  serverRowCount: number,
): EmptyStateCopy {
  if (serverRowCount > 0 && filters.query.trim() !== "") {
    return {
      title: "No match in this window",
      body:
        `No run id or job run id in the top ${filters.limit} rows contains ` +
        `"${filters.query.trim()}". This search runs in the browser over the rows already ` +
        `fetched, so a match older than this window will not be found — widen the window, or ` +
        `narrow by model, which filters server-side.`,
    };
  }

  const applied: string[] = [];
  if (filters.status !== null) applied.push(`status = ${filters.status}`);
  if (filters.model !== null) applied.push(`model = ${filters.model}`);

  if (applied.length > 0) {
    return {
      title: "No runs match",
      body:
        `${applied.join(" and ")} — applied server-side, so this is the whole history and not ` +
        `just this window. Clear the filter to see everything else.`,
    };
  }

  return {
    title: "No runs yet",
    body: "Trigger one from any model page. Runs started while this app was down are here too — the job writes its own registry row, the app is only an observer.",
  };
}
