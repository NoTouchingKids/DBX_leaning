/**
 * The decision itself: P(B>A), the two expected losses, and the lift interval.
 *
 * Not Recharts. The lift is one number with a credible interval around it,
 * read against zero — a single horizontal bar with a zero reference does that
 * exactly, and a charting library would add a legend, an axis component and an
 * animation to draw four coordinates.
 *
 * Three deliberate choices here, all from the model's own documentation:
 *
 * - `prob_b_beats_a` is a probability, not an error or a gap. It is rendered
 *   on a fixed 0-1 track, not scaled to look large, and nothing colours it
 *   good or bad — "higher is better" is not true of it.
 * - Both the probability and the expected loss are shown, because the model's
 *   decision rule consults both. Probability alone says which arm is ahead;
 *   expected loss says whether being ahead matters. With thousands of
 *   observations P(B>A) saturates at 0 or 1 long before the effect is
 *   interesting, and the loss is what stays readable there.
 * - Each of the five decision keys is ABSENT from the payload until its stage
 *   completes. Each cell handles its own absence; none of them assumes an
 *   earlier one populated.
 */

import type { ModelViewProps } from "@/components/models/contract";
import { EMPTY } from "@/lib/format";

import { decisionFromSnapshot } from "./derive";

function fmt(value: number | null, places = 4): string {
  return value === null ? EMPTY : value.toFixed(places);
}

function Cell({
  label,
  value,
  note,
}: {
  label: string;
  value: string;
  note?: string;
}) {
  return (
    <div className="rounded-md border border-line bg-paper px-3 py-2">
      <div className="font-mono text-[0.6rem] text-faint">{label}</div>
      <div className="font-mono text-[0.95rem]">{value}</div>
      {note !== undefined && <div className="text-[0.62rem] text-faint">{note}</div>}
    </div>
  );
}

export function DecisionChart({ snapshot }: ModelViewProps) {
  const d = decisionFromSnapshot(snapshot);

  if (d.source === "none") {
    return (
      <p className="px-1 py-10 text-center text-[0.75rem] text-faint">
        Nothing to decide from yet. This model is closed-form and finishes in
        milliseconds, so this panel usually fills in one step — or straight from
        the result rows, if the run beat the stream.
      </p>
    );
  }

  const prob = d.probBBeatsA;
  const lift = d.lift;

  // The interval's own span, padded, and always containing zero — zero is the
  // reference the interval is read against, so an axis that excludes it makes
  // the one visual comparison this bar exists for impossible.
  const bounds = [lift?.ciLow ?? null, lift?.ciHigh ?? null, lift?.mean ?? null, 0].filter(
    (v): v is number => v !== null,
  );
  const rawLow = Math.min(...bounds);
  const rawHigh = Math.max(...bounds);
  const pad = Math.max((rawHigh - rawLow) * 0.15, 1e-6);
  const low = rawLow - pad;
  const high = rawHigh + pad;
  const at = (value: number) => ((value - low) / (high - low)) * 100;

  return (
    <div>
      <div className="grid grid-cols-3 gap-2">
        <Cell
          label="prob_b_beats_a"
          value={fmt(prob)}
          note={prob === null ? "absent until stage 2" : "a probability, not a score"}
        />
        <Cell label="expected_loss A" value={fmt(d.expectedLossA, 5)} />
        <Cell label="expected_loss B" value={fmt(d.expectedLossB, 5)} />
      </div>

      {prob !== null && (
        <div className="mt-3">
          <div className="mb-1 flex justify-between font-mono text-[0.6rem] text-faint">
            <span>P(B&gt;A) 0</span>
            <span>1</span>
          </div>
          <div className="relative h-2 overflow-hidden rounded-full bg-idle-soft">
            <span
              className="absolute inset-y-0 left-0 rounded-full bg-info transition-[width] duration-500 motion-reduce:transition-none"
              style={{ width: `${Math.min(100, Math.max(0, prob * 100))}%` }}
            />
          </div>
        </div>
      )}

      <div className="mt-4">
        <div className="mb-1.5 flex items-baseline justify-between">
          <span className="font-mono text-[0.62rem] text-faint">
            lift (B − A){d.credibleMass !== null && `, ${(d.credibleMass * 100).toFixed(0)}% CI`}
          </span>
          <span className="font-mono text-[0.68rem] text-dim">
            {lift === null
              ? EMPTY
              : `${fmt(lift.mean)}  [${fmt(lift.ciLow)}, ${fmt(lift.ciHigh)}]`}
          </span>
        </div>
        {lift === null ? (
          <p className="text-[0.68rem] text-faint">
            The lift interval is stage 4; it is absent from the payload until then.
          </p>
        ) : (
          <div className="relative h-9 rounded-md border border-line bg-paper">
            {/* Zero. An interval that crosses it is a difference the data does
                not resolve the sign of. */}
            <span
              className="absolute inset-y-1 w-px bg-edge"
              style={{ left: `${at(0)}%` }}
              aria-hidden="true"
            />
            {lift.ciLow !== null && lift.ciHigh !== null && (
              <span
                className="absolute top-1/2 h-1.5 -translate-y-1/2 rounded-full bg-accent-soft"
                style={{
                  left: `${at(lift.ciLow)}%`,
                  width: `${Math.max(0.5, at(lift.ciHigh) - at(lift.ciLow))}%`,
                }}
              />
            )}
            {lift.mean !== null && (
              <span
                className="absolute top-1/2 h-3 w-[3px] -translate-x-1/2 -translate-y-1/2 rounded-sm bg-accent"
                style={{ left: `${at(lift.mean)}%` }}
              />
            )}
            <span className="absolute bottom-0.5 font-mono text-[0.55rem] text-faint" style={{ left: `${at(0)}%` }}>
              0
            </span>
          </div>
        )}
      </div>

      <p className="mt-3 text-[0.66rem] leading-relaxed text-faint">
        {d.decision === null
          ? "The decision is stage 5."
          : d.conclusive === true
            ? `Conclusive: ${d.decision} clears both the probability threshold and the loss tolerance.`
            : "Inconclusive: an arm can lead on probability and still not clear the expected-loss tolerance, so both numbers are shown."}
        {" "}
        The arms were not randomised — they are defined by observing the data —
        and observations within an arm are not independent, so these intervals
        are narrower than the evidence really supports.
        {d.source === "results" && " Read from the result rows, not from a live message."}
      </p>
    </div>
  );
}
