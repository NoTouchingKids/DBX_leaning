/**
 * Results, for a run that has stopped producing them.
 *
 * Distinct from `run/ResultsCard` on exactly one point, and it is the point
 * that matters here. That card says "more expected" when no chunk carries
 * `final: true`, which is right while a stream is open and wrong the moment
 * the run is over: nothing more is expected, because nothing is running. The
 * honest word for a finished run missing its final chunk is *incomplete*.
 *
 * Two ways to reach that state, and the client cannot tell them apart: a
 * chunking model stopped between chunks returns cleanly, having written every
 * chunk it finished and never the final one; or the final write failed.
 * Saying so is better than picking one.
 *
 * `row_count: 0` is never rendered as an empty state. "Succeeded, wrote
 * nothing" and "succeeded, wrote 8,760 rows" are different outcomes, and the
 * envelope carries `row_count` precisely so they can be told apart —
 * collapsing a zero into "no data" throws away the only signal that a result
 * write may have failed.
 */

import { Callout } from "@/components/ui/Callout";
import { Card } from "@/components/ui/Card";
import type { ResultMessage } from "@/lib/envelope";
import { formatCount } from "@/lib/format";

import { totalRowCount, type ResultsCompleteness } from "./history";

function PreviewTable({ rows }: { rows: ReadonlyArray<Record<string, unknown>> }) {
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
              {columns.map((column) => {
                const cell = row[column];
                return (
                  <td key={column} className="border border-line px-2 py-1 text-right">
                    {cell === null || cell === undefined
                      ? "—"
                      : typeof cell === "object"
                        ? JSON.stringify(cell)
                        : String(cell)}
                  </td>
                );
              })}
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

const COMPLETENESS_HINT: Record<ResultsCompleteness, string> = {
  complete: "final chunk present",
  incomplete: "no final chunk",
  none: "nothing written",
  unknown: "not fully read",
};

export function TerminalResults({
  results,
  completeness,
}: {
  results: readonly ResultMessage[];
  completeness: ResultsCompleteness;
}) {
  const rows = totalRowCount(results);

  return (
    <Card
      title="Results"
      hint={
        <span className="font-mono">
          {formatCount(results.length)} chunk{results.length === 1 ? "" : "s"} ·{" "}
          {formatCount(rows)} rows written · {COMPLETENESS_HINT[completeness]}
        </span>
      }
    >
      {completeness === "incomplete" && (
        <Callout tone="warn" title="Incomplete — not still arriving">
          The run has stopped and no chunk is marked <code>final</code>. Either
          it ended between chunks (a cancelled run of a chunking model returns
          cleanly, keeping every chunk it finished), or the final result write
          did not land. Those are indistinguishable from here; what is below is
          everything Delta holds.
        </Callout>
      )}

      {completeness === "none" && (
        <p className="text-[0.78rem] leading-relaxed text-dim">
          This run recorded no <code>result</code> message at all — it never
          reached its result write. That is not the same as writing zero rows,
          which would have been reported as a chunk with{" "}
          <code>row_count 0</code>.
        </p>
      )}

      {completeness === "unknown" && results.length === 0 && (
        <p className="text-[0.78rem] text-dim">
          Not every page has been read yet, so whether this run produced
          results is not yet known.
        </p>
      )}

      <div className="mt-3 flex flex-col gap-4">
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
              <p className="mb-2 text-[0.7rem] leading-relaxed text-warn">
                Zero rows were written durably. Reported, not inferred: this is
                what distinguishes a run that wrote nothing — possibly because
                the write itself failed — from one that never got that far.
              </p>
            )}
            {result.preview.length > 0 ? (
              <PreviewTable rows={result.preview} />
            ) : (
              <p className="text-[0.7rem] text-faint">
                no preview rows in this chunk
                {result.row_count > 0 && " (the rows are in the model's own table)"}
              </p>
            )}
            {Object.keys(result.fetch_hint).length > 0 && (
              <p className="mt-1 font-mono text-[0.64rem] break-all text-faint">
                fetch_hint {JSON.stringify(result.fetch_hint)}
              </p>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}
