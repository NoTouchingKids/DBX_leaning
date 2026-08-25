/**
 * Five slots and a sentence about where the number came from.
 *
 * The sentence is the point. This count is inferred client-side from the run
 * list — no endpoint reports it — and a bare "3 / 5" would read as something
 * the server said. See `capacity.ts` for the derivation and its two failure
 * modes; both are rendered here rather than hidden, because the case where
 * the count is a lower bound is exactly the case that explains a 429 the
 * meter otherwise says is impossible.
 */

import type { Capacity } from "./capacity";
import { filledSlots, isExact } from "./capacity";

export function CapacityMeter({ capacity, pending }: { capacity: Capacity; pending: boolean }) {
  const filled = filledSlots(capacity);
  const exact = isExact(capacity);
  const tone = capacity.atCeiling ? "warn" : "info";

  return (
    <div
      className={`mb-4 flex flex-wrap items-center gap-3 rounded-[10px] border border-line bg-raised px-3.5 py-2.5 ${
        capacity.atCeiling ? "border-l-[3px] border-l-warn" : "border-l-[3px] border-l-info"
      }`}
    >
      <span className="text-[0.72rem] text-dim">
        Active runs{" "}
        <b className={`font-mono ${capacity.atCeiling ? "text-warn" : "text-ink"}`}>
          {pending ? "…" : `${exact ? "" : "≥"}${capacity.active}`}
        </b>{" "}
        / {capacity.ceiling}
      </span>

      <div
        className="flex gap-[3px]"
        role="img"
        aria-label={`${capacity.active} of ${capacity.ceiling} concurrent job-task slots in use`}
      >
        {Array.from({ length: capacity.ceiling }, (_, index) => (
          <div
            key={index}
            className={`h-[9px] w-[18px] rounded-[2px] border ${
              index < filled
                ? tone === "warn"
                  ? "border-warn bg-warn"
                  : "border-info bg-info"
                : "border-edge bg-paper"
            }`}
          />
        ))}
      </div>

      {capacity.atCeiling && (
        <span className="text-[0.7rem] font-semibold text-warn">
          at the ceiling — the next trigger is expected to 429
        </span>
      )}

      <span className="ml-auto max-w-[52ch] text-right text-[0.66rem] leading-relaxed text-faint">
        Derived client-side by counting rows whose status is not{" "}
        <code>SUCCEEDED/FAILED/CANCELLED/INFEASIBLE</code> — the same predicate{" "}
        <code>active_run_count()</code> uses. Free Edition allows 5 concurrent job tasks per
        account, across all models.
        {!capacity.unfiltered && " Counted from an unfiltered window, not the filtered table below."}
        {!capacity.windowComplete && (
          <>
            {" "}
            <span className="text-warn">
              The window filled, so older non-terminal rows may sit below the cut — read this as a
              lower bound.
            </span>
          </>
        )}
      </span>
    </div>
  );
}
