/**
 * Failed how, how many of each, and where those rows went.
 *
 * `FAILURE_REASONS` is a closed set of four in the model, exported and
 * commented as closed precisely so a UI can group by it and pre-declare a
 * label for each. That is what this card is: the answer to the first question
 * anyone asks about a run with failures, which is not "how many" — the
 * signature already said that — but "failed *how*". `too_few_observations`
 * dominating says something quite different about the panel than
 * `singular_design` dominating says about the degree the run was given.
 *
 * Nothing in here is styled as an error. A country with three observations
 * cannot be fitted, and that is a fact about the data rather than a fault in
 * the run; the tone comes from `FailureTone`, which has no `bad` member.
 *
 * The strip at the bottom is the durable record, and it belongs on this card
 * rather than anywhere else because it is the evidence for the model's central
 * promise: unfittable groups are *recorded*, with a reason and null
 * coefficients, never dropped. Results are chunked — `chunk_size` groups per
 * emission — so that count trails `groups_done` by design, and saying so is
 * what stops the lag reading as loss.
 */

import { isSettled, type ModelViewProps } from "@/components/models/contract";
import { EMPTY, formatCount } from "@/lib/format";

import { TONE_CLASS } from "./frames";
import {
  accumulateGroups,
  arrivalState,
  buildGroupPoints,
  describeReason,
  failureBreakdown,
  failuresOf,
  formatShare,
  readCounts,
  type ArrivalState,
} from "./panelModel";

/** How many individual failures to name. The model itself stops logging them
 *  after `failure_log_limit` (12 by default) for the same reason: past a
 *  couple of dozen the list is chatter, and every one of them is in the
 *  results table regardless. */
const NAMED_FAILURES = 6;

const ARRIVAL_TEXT: Record<ArrivalState, string> = {
  none: "no chunks yet",
  arriving: "still arriving",
  complete: "complete — final chunk seen",
  stopped: "incomplete — the run ended before a final chunk",
};

const ARRIVAL_TONE: Record<ArrivalState, string> = {
  none: "border-line text-faint",
  arriving: "border-info text-info",
  complete: "border-good text-good",
  stopped: "border-warn text-warn",
};

export function FailureReasons({ state, snapshot }: ModelViewProps) {
  const counts = readCounts(snapshot.latestProgress);
  const breakdown = failureBreakdown(snapshot.latestProgress);
  const points = buildGroupPoints(snapshot.progress);
  const durable = accumulateGroups(snapshot.results);
  const settled = isSettled(state);
  const arrival = arrivalState(durable, settled);
  const tone = TONE_CLASS[counts.tone];

  const named = failuresOf(points).slice(-NAMED_FAILURES).reverse();
  const absent = breakdown.reasons.filter((reason) => reason.count === 0 && reason.known);

  return (
    <div className="flex flex-col gap-3">
      {breakdown.total === 0 ? (
        <div className="flex flex-col justify-center gap-1 text-[0.74rem] text-dim">
          <p className="font-semibold text-ink">
            {counts.done === null
              ? "No groups reported yet."
              : settled
                ? "Every group was fitted."
                : "No group has failed to fit so far."}
          </p>
          <p className="max-w-[46ch] leading-relaxed">
            {counts.done === null
              ? "The failure breakdown arrives with the first progress message — it is on every one of them, not only at the end."
              : "This card fills in if a group cannot be fitted. It is not an error state: a unit with too few observations is a fact about the panel, and the model records it with a reason rather than dropping it."}
          </p>
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 font-mono text-[0.7rem] text-dim">
            <span>
              failed <span className={`font-semibold ${tone.text}`}>{formatCount(breakdown.total)}</span>
            </span>
            <span>
              of <span className="font-semibold text-ink">{formatCount(counts.done ?? 0)}</span>{" "}
              processed
            </span>
            {counts.failedShare !== null && (
              <span className={tone.text}>{formatShare(counts.failedShare) ?? EMPTY}</span>
            )}
          </div>

          <ul className="flex flex-col gap-2.5">
            {breakdown.present.map((reason) => (
              <li key={reason.reason}>
                <div className="flex items-baseline justify-between gap-3 text-[0.74rem]">
                  <span className={`font-semibold ${reason.known ? "text-ink" : "text-warn"}`}>
                    {reason.label}
                  </span>
                  <span className={`font-mono text-[0.7rem] ${tone.text}`}>
                    {formatCount(reason.count)}
                  </span>
                </div>
                <div
                  className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-idle-soft"
                  role="img"
                  aria-label={`${reason.reason}: ${reason.count} of ${breakdown.total} failures`}
                >
                  <span
                    className={`block h-full ${tone.bar} transition-[width] duration-300 motion-reduce:transition-none`}
                    style={{
                      width: `${breakdown.peak > 0 ? (reason.count / breakdown.peak) * 100 : 0}%`,
                    }}
                  />
                </div>
                <p className="mt-1 text-[0.68rem] leading-relaxed text-dim">{reason.meaning}</p>
                <p className="mt-0.5 text-[0.68rem] leading-relaxed text-faint">{reason.note}</p>
                <p className="mt-0.5 font-mono text-[0.64rem] text-faint">{reason.reason}</p>
              </li>
            ))}
          </ul>

          {absent.length > 0 && (
            <p className="text-[0.68rem] leading-relaxed text-faint">
              Not seen in this run:{" "}
              <span className="font-mono">{absent.map((r) => r.reason).join(", ")}</span>. The four
              reasons are a closed set, so a zero here is an answer rather than a missing category.
            </p>
          )}

          {named.length > 0 && (
            <div>
              <p className="text-[0.7rem] font-semibold text-dim">Most recent failures</p>
              <ul className="mt-1 flex flex-col gap-0.5 font-mono text-[0.68rem] text-dim">
                {named.map((point) => (
                  <li key={point.done} className="flex flex-wrap gap-x-2">
                    <span className="text-ink">{point.label ?? point.key ?? EMPTY}</span>
                    <span className="text-faint">
                      {point.reason === null ? EMPTY : describeReason(point.reason).reason}
                    </span>
                    {/*
                      Usable rows against rows the group HAD. "This unit is
                      small" and "this unit did not report" are different
                      answers, and the pair is the only place the difference
                      shows: a group can look long enough right up until its
                      null responses are dropped.
                    */}
                    <span className="text-faint">
                      {formatCount(point.nObservations ?? 0)} usable of{" "}
                      {formatCount(point.rowsSeen ?? 0)} rows
                    </span>
                  </li>
                ))}
              </ul>
              {breakdown.total > named.length && (
                <p className="mt-1 text-[0.66rem] text-faint">
                  {formatCount(breakdown.total - named.length)} more, all of them in the results
                  table with their reason.
                </p>
              )}
            </div>
          )}
        </>
      )}

      <div className="mt-1 border-t border-line pt-2">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[0.7rem] text-dim">
          <span>
            groups written <span className="font-semibold text-ink">{formatCount(durable.rowsWritten)}</span>
          </span>
          <span>
            chunks <span className="font-semibold text-ink">{formatCount(durable.chunks.length)}</span>
          </span>
          <span
            className={`rounded-full border px-2 py-0.5 text-[0.66rem] ${ARRIVAL_TONE[arrival]}`}
          >
            {ARRIVAL_TEXT[arrival]}
          </span>
        </div>
        <p className="mt-1 text-[0.66rem] leading-relaxed text-faint">
          One row per group, failures included, with a reason and null coefficients. Results are
          chunked, so this trails the processed count by up to one chunk while a run is live.
        </p>
        {durable.missing.length > 0 && (
          <p className="mt-1 text-[0.68rem] text-warn">
            Chunk {durable.missing.join(", ")} never arrived, so the groups in it are missing from
            this tally — not from the table.
          </p>
        )}
      </div>
    </div>
  );
}
