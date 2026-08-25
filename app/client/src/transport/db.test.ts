import { afterEach, describe, expect, it } from "vitest";

import type { Message } from "@/lib/envelope";
import { closeDb, forgetRun, getRun, putMessages, putRun, readMessages } from "./db";

function progress(runId: string, seq: number, metric: number | null = null): Message {
  return {
    type: "progress", run_id: runId, seq, ts: seq, elapsed_seconds: seq,
    percent_complete: null, primary_metric: metric, primary_metric_label: null, payload: {},
  };
}

afterEach(async () => {
  // An open handle blocks deleteDatabase, and the next test's open() then
  // waits on the delete. Close first or the suite hangs.
  await closeDb();
  await new Promise<void>((resolve) => {
    const req = indexedDB.deleteDatabase("dbx-leaning");
    req.onsuccess = () => resolve();
    req.onerror = () => resolve();
    req.onblocked = () => resolve();
  });
});

describe("IndexedDB cache", () => {
  it("round-trips messages in seq order", async () => {
    await putMessages([progress("a", 2), progress("a", 0), progress("a", 1)]);
    expect((await readMessages("a")).map((m) => m.seq)).toEqual([0, 1, 2]);
  });

  it("collapses a live message and its backfilled twin", async () => {
    // The whole reason `seq` is assigned by the job rather than by a UC
    // identity column: [run_id, seq] is the natural key, so `put` dedupes.
    await putMessages([progress("a", 4, 1)]);
    await putMessages([progress("a", 4, 2)]);
    const rows = await readMessages("a");
    expect(rows).toHaveLength(1);
    expect((rows[0] as { primary_metric: number }).primary_metric).toBe(2);
  });

  it("keeps runs apart", async () => {
    await putMessages([progress("a", 0), progress("b", 0)]);
    expect(await readMessages("a")).toHaveLength(1);
    expect(await readMessages("b")).toHaveLength(1);
  });

  it("returns nothing for a run it has never seen", async () => {
    expect(await readMessages("nope")).toEqual([]);
    expect(await getRun("nope")).toBeUndefined();
  });

  it("round-trips run metadata", async () => {
    await putRun({
      run_id: "a", model: "mcmc", status: "RUNNING",
      terminal: false, last_seq: 12, updated_ts: 5,
    });
    expect(await getRun("a")).toMatchObject({ model: "mcmc", last_seq: 12, terminal: false });
  });

  it("forgets a run completely, and only that run", async () => {
    await putMessages([progress("a", 0), progress("a", 1), progress("b", 0)]);
    await putRun({ run_id: "a", model: null, status: null, terminal: false, last_seq: 1, updated_ts: 0 });

    await forgetRun("a");

    expect(await readMessages("a")).toEqual([]);
    expect(await getRun("a")).toBeUndefined();
    expect(await readMessages("b")).toHaveLength(1);
  });
});
