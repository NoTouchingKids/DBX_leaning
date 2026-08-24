/**
 * The one warning this page raises, and the one it deliberately cannot fix.
 *
 * Shown only when the visible rows actually contain a stranded run, so it
 * stays a signal rather than a permanent disclaimer. It offers no button: the
 * app has no reaper, no force-terminal endpoint, and no socket to the job —
 * `POST /api/runs/{id}/cancel` answers 409 for exactly these runs. The one
 * real remedy lives outside this app, and the app's own escape-hatch text is
 * server-supplied on that 409, so it is not copied here where it would drift.
 */

import { Callout } from "@/components/ui/Callout";

export function StrandedBanner({ count }: { count: number }) {
  if (count === 0) return null;
  return (
    <div className="mb-3">
      <Callout
        tone="warn"
        title={`${count} run${count === 1 ? "" : "s"} stranded — RUNNING with no job channel`}
      >
        {`The job died, or the app restarted while it was running. There is no reaper: nothing will move ${
          count === 1 ? "this row" : "these rows"
        } to a terminal status, and no endpoint in this API can. They go on counting against the account-wide ceiling of 5 concurrent job tasks forever — and because they have stopped emitting, they sink down an ${"`updated_ts DESC`"} ordering until they fall out of the window entirely, which is how a trigger 429s against slots nothing on screen appears to be holding. Resolving one means acting on the Databricks job directly, outside this app — its job run id is on the row.`}
      </Callout>
    </div>
  );
}
