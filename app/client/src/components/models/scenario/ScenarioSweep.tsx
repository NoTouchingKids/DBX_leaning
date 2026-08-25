/**
 * The `scenario` signature: a raster scan across the demand x capacity grid.
 *
 * Deliberately a scan and not a flicker. `gurobi_scheduling` searches — its
 * cells have no order and its animation says so. This model enumerates, in a
 * fixed order it cannot deviate from, and the one thing the animation exists
 * to communicate is that difference. Someone who has seen both should be able
 * to tell which is running from the far side of a desk.
 *
 * The scan head's position is derived (see `scenarioModel.ts`), not timed:
 * nothing here runs on an interval. Progress is batched model-side, so the
 * head jumps several cells at a time; the per-column transition delay turns
 * each jump into a short left-to-right cascade so it still reads as a sweep
 * rather than a blink. That stagger is the only decorative thing in here.
 */

import { isSettled, type ModelViewProps } from "@/components/models/contract";
import { usePrefersReducedMotion } from "@/components/models/useReducedMotion";
import type { UiRunState } from "@/lib/envelope";
import { DOT_COLOR } from "@/components/ui/runStateStyles";

import { deriveSweep, SCAN_CAPACITY, SCAN_CELLS, SCAN_COLS, SCAN_DEMAND } from "./scenarioModel";

/** One flat frame per terminal state, applied to every cell. Nothing
 *  per-cell survives the end of a run — see the note in `contract.ts`. */
const TERMINAL_CELL: Partial<Record<UiRunState, string>> = {
  SUCCEEDED: "bg-good-soft border-good",
  FAILED: "bg-bad-soft border-bad",
  CANCELLED: "bg-idle-soft border-idle",
  INFEASIBLE: "bg-warn-soft border-warn",
};

const CAPTION: Record<string, [string, string]> = {
  none: ["No run selected", "Trigger a sweep to watch it scan"],
  QUEUED: ["Queued", "Waiting for compute"],
  STARTING: ["Starting sweep", "Preparing the scenario grid"],
  RUNNING: ["Sweeping scenarios", "Evaluating grid cells in order"],
  SUCCEEDED: ["Sweep complete", "The whole grid was enumerated"],
  FAILED: ["Sweep failed", "The run did not complete"],
  CANCELLED: ["Sweep cancelled", "Stopped part-way through the grid"],
  INFEASIBLE: ["Sweep reported infeasible", "No scenario produced a usable outcome"],
};

function captionFor(state: UiRunState | null): [string, string] {
  return CAPTION[state ?? "none"] ?? CAPTION["none"] ?? ["", ""];
}

export function ScenarioSweep({ state, snapshot }: ModelViewProps) {
  const reduced = usePrefersReducedMotion();
  const sweep = deriveSweep(snapshot.progress);
  const settled = isSettled(state);
  const terminalCell = settled ? TERMINAL_CELL[state ?? "SUCCEEDED"] : undefined;
  const [line1, line2] = captionFor(state);

  // STARTING is the client-only frame between the 202 and the first message;
  // parking the head on the first cell is what makes it read as "about to
  // sweep" rather than as a dead grid.
  const head = settled ? null : (sweep.head ?? (state === "STARTING" ? 0 : null));
  const best = settled ? null : sweep.bestCell;

  const label = settled
    ? `Scenario sweep ${state}`
    : head === null
      ? "Scenario sweep, no scenarios reported yet"
      : `Scenario sweep, evaluating cell ${head + 1} of ${SCAN_CELLS}` +
        (sweep.scenariosTotal !== null
          ? ` (${sweep.scenariosDone ?? 0} of ${sweep.scenariosTotal} scenarios)`
          : "");

  return (
    <div className="p-4">
      <div className="mb-3 flex items-center gap-2.5">
        <span
          className={
            `inline-block h-2 w-2 shrink-0 rounded-full bg-current ${DOT_COLOR[state ?? "QUEUED"]} ` +
            (state === "RUNNING" ? "live-dot" : "")
          }
        />
        <div>
          <div className="text-[0.82rem] font-semibold">{line1}</div>
          <div className="text-[0.72rem] text-dim">{line2}</div>
        </div>
      </div>

      <div className="overflow-x-auto">
        <div
          role="img"
          aria-label={label}
          className="inline-grid gap-[3px]"
          style={{ gridTemplateColumns: `2.9rem repeat(${SCAN_COLS}, minmax(2rem, 1fr))` }}
        >
          <div />
          {SCAN_DEMAND.map((value) => (
            <div key={`d${value}`} className="text-center font-mono text-[0.6rem] text-faint">
              d{value}
            </div>
          ))}

          {SCAN_CAPACITY.map((capacity, row) => (
            <Row
              key={`c${capacity}`}
              capacity={capacity}
              row={row}
              head={head}
              best={best}
              terminalCell={terminalCell}
              reduced={reduced}
            />
          ))}
        </div>
      </div>

      {/* The legend is load-bearing: four discrete cell states with no
          magnitude anywhere means the colours have to be named. */}
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[0.68rem] text-dim">
        <Key className="border-edge bg-paper" text="not yet reached" />
        <Key className="border-info bg-info" text="evaluating" />
        <Key className="border-info bg-info-soft" text="evaluated" />
        <Key className="border-accent bg-accent" text="best objective so far" />
      </div>
    </div>
  );
}

function Row({
  capacity,
  row,
  head,
  best,
  terminalCell,
  reduced,
}: {
  capacity: number;
  row: number;
  head: number | null;
  best: number | null;
  terminalCell: string | undefined;
  reduced: boolean;
}) {
  return (
    <>
      <div className="flex items-center justify-end pr-1 font-mono text-[0.6rem] text-faint">
        c{capacity}
      </div>
      {SCAN_DEMAND.map((demand, col) => {
        const index = row * SCAN_COLS + col;
        return (
          <div
            key={`${capacity}-${demand}`}
            title={`demand ${demand} x capacity ${capacity}`}
            className={
              "h-[1.35rem] rounded-[3px] border transition-[background-color,border-color,transform] duration-300 motion-reduce:transition-none " +
              cellTone(index, head, best, terminalCell) +
              // Scale is pure emphasis, so it is the first thing reduced
              // motion drops; the fill still says where the head is.
              (index === head && !reduced && terminalCell === undefined ? " scale-[1.12]" : "")
            }
            style={reduced ? undefined : { transitionDelay: `${col * 35}ms` }}
          />
        );
      })}
    </>
  );
}

function cellTone(
  index: number,
  head: number | null,
  best: number | null,
  terminalCell: string | undefined,
): string {
  if (terminalCell !== undefined) return terminalCell;
  if (head !== null && index === head) return "bg-info border-info";
  if (best !== null && index === best) return "bg-accent border-accent";
  if (head !== null && index < head) return "bg-info-soft border-info";
  return "bg-paper border-edge";
}

function Key({ className, text }: { className: string; text: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className={`inline-block h-3 w-5 rounded-[3px] border ${className}`} />
      {text}
    </span>
  );
}
