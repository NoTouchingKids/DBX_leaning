/**
 * Generic results view: a bounded preview per `result` message.
 *
 * Collapsed by default. Every model writes its real `results()` rows to its
 * own Delta table, and a dashboarding tool against those tables is the better
 * exploration surface — what a `result` message carries is an LTTB-downsampled
 * preview and a pointer, never the full set.
 *
 * `row_count: 0` gets said out loud rather than rendered as an empty state.
 * It is the difference between "succeeded, wrote 8,760 rows" and "succeeded,
 * wrote nothing — possibly because the write itself failed", and those look
 * identical if a zero is treated as "nothing to show".
 *
 * Results APPEND across chunks; `panel_fit` emits many of them, each with its
 * own `chunk_index` and `final: false` until the last.
 */

import type { ResultMessage } from "@/lib/envelope";
import { formatCount } from "@/lib/format";

function PreviewTable({ rows }: { rows: Array<Record<string, unknown>> }) {
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  const shown = rows.slice(0, 20);

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse font-mono text-[0.72rem]">
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column}
                className="border border-line bg-paper px-2 py-1 text-right font-semibold text-dim"
              >
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td key={column} className="border border-line px-2 py-1 text-right">
                  {row[column] === null || row[column] === undefined
                    ? "—"
                    : typeof row[column] === "object"
                      ? JSON.stringify(row[column])
                      : String(row[column])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > shown.length && (
        <p className="mt-1 text-[0.66rem] text-faint">
          showing {shown.length} of {rows.length} preview rows
        </p>
      )}
    </div>
  );
}

export function ResultsCard({ results }: { results: readonly ResultMessage[] }) {
  if (results.length === 0) return null;

  const complete = results.some((r) => r.final);
  const totalRows = results.reduce((sum, r) => sum + r.row_count, 0);

  return (
    <details className="mb-5 overflow-hidden rounded-[10px] border border-dashed border-edge">
      <summary className="cursor-pointer bg-paper px-4 py-3 text-[0.84rem] font-bold marker:text-accent">
        Results preview
        <span className="ml-2 font-mono text-[0.66rem] font-normal text-faint">
          {formatCount(results.length)} chunk{results.length === 1 ? "" : "s"} ·{" "}
          {formatCount(totalRows)} rows written · {complete ? "complete" : "more expected"}
        </span>
      </summary>
      <div className="flex flex-col gap-4 px-4 pb-4">
        {results.map((result) => (
          <div key={result.seq}>
            <div className="mb-1 flex flex-wrap items-baseline gap-3 font-mono text-[0.68rem] text-dim">
              <span>chunk {result.chunk_index}</span>
              <span className={result.row_count === 0 ? "font-bold text-warn" : ""}>
                row_count {formatCount(result.row_count)}
              </span>
              <span>{result.final ? "final" : "not final"}</span>
              <span className="text-faint">seq {result.seq}</span>
            </div>
            {result.row_count === 0 && (
              <p className="mb-2 text-[0.7rem] text-warn">
                Zero rows were written durably. That is reported, not inferred —
                it distinguishes a run that wrote nothing from one that never
                reached its result write.
              </p>
            )}
            {result.preview.length > 0 ? (
              <PreviewTable rows={result.preview} />
            ) : (
              <p className="text-[0.7rem] text-faint">no preview rows in this chunk</p>
            )}
            {Object.keys(result.fetch_hint).length > 0 && (
              <p className="mt-1 font-mono text-[0.64rem] text-faint">
                fetch_hint {JSON.stringify(result.fetch_hint)}
              </p>
            )}
          </div>
        ))}
      </div>
    </details>
  );
}
