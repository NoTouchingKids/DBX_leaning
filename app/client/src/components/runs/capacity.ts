/**
 * The account-wide concurrency meter.
 *
 * Free Edition allows **5 concurrent job tasks per account, across all
 * models**. `POST /api/runs` returns 429 the moment `active_run_count() >= 5`
 * (`app/server/routes/runs.py`, `app/server/config.py::max_concurrent_runs`, env
 * `DBX_MAX_CONCURRENT_RUNS`) — the single most likely user-facing error on
 * this platform.
 *
 * No endpoint exposes that count: it is computed inside the trigger handler.
 * So this is DERIVED, client-side, from the run list, using the server's own
 * predicate:
 *
 *     WHERE status NOT IN ('SUCCEEDED','FAILED','CANCELLED','INFEASIBLE')
 *
 * which is exactly `!isTerminal(status)`. The meter says out loud on the page
 * that it is derived; it is an inference, not a reported number, and the two
 * ways it can be wrong are both encoded below rather than left implicit.
 *
 * WHY IT BELONGS ON THIS PAGE. The ceiling is account-wide. A model page can
 * only ever see its own model's runs, so it cannot explain why its trigger
 * just 429'd — the four runs holding the slots belong to other models.
 */

import type { Run } from "@/lib/apiClient";
import { isTerminal } from "@/lib/envelope";

/** `app/server/config.py`: `max_concurrent_runs: int = 5`. A deployment can raise it
 *  via `DBX_MAX_CONCURRENT_RUNS`, and nothing serves the value, so this is the
 *  documented default and the meter is labelled with it rather than pretending
 *  to have read it from the server. */
export const MAX_CONCURRENT_RUNS = 5;

export interface Capacity {
  /** Rows in the window whose status is not terminal. */
  active: number;
  ceiling: number;
  /** `active >= ceiling` — the next trigger is expected to 429. */
  atCeiling: boolean;
  /**
   * Whether the window is known to contain every run there is.
   *
   * True only when the response came back short of the limit it asked for.
   * A full window may have cut off older rows, and ordering is `updated_ts
   * DESC` — so the rows that fall off the bottom are the ones that have not
   * been updated in a while. A stranded `RUNNING` run is precisely that: it
   * stopped emitting, it sinks, and it still holds a slot. When this is
   * false, `active` is a LOWER BOUND, and that is the case that explains an
   * unexpected 429.
   */
  windowComplete: boolean;
  /**
   * Whether the rows counted came from an unfiltered window.
   *
   * `model` and `status` both filter server-side, so counting a filtered
   * response gives the active runs *of that model* — not the account. The
   * page therefore derives this from its own unfiltered query and never from
   * the filtered table it is displaying.
   */
  unfiltered: boolean;
}

export function isActiveRow(row: Pick<Run, "status">): boolean {
  return !isTerminal(row.status);
}

export function deriveCapacity(
  /** MUST be an unfiltered window; see `unfiltered` above. */
  rows: readonly Run[],
  options: { windowLimit: number; unfiltered?: boolean; ceiling?: number },
): Capacity {
  const ceiling = options.ceiling ?? MAX_CONCURRENT_RUNS;
  const active = rows.filter(isActiveRow).length;
  return {
    active,
    ceiling,
    atCeiling: active >= ceiling,
    windowComplete: rows.length < options.windowLimit,
    unfiltered: options.unfiltered ?? true,
  };
}

/** How many meter slots to draw as filled. Clamped, because `active` can
 *  exceed the ceiling: the ceiling is only enforced at trigger time, and a
 *  deployment that lowered `DBX_MAX_CONCURRENT_RUNS` can sit above it. */
export function filledSlots(capacity: Capacity): number {
  return Math.min(capacity.ceiling, Math.max(0, capacity.active));
}

/** Whether the derivation can be trusted as an exact count of the account's
 *  active runs, rather than a lower bound. */
export function isExact(capacity: Capacity): boolean {
  return capacity.unfiltered && capacity.windowComplete;
}
