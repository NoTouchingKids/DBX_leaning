/**
 * Reload while a run is in flight.
 *
 * The transport persists every message to IndexedDB keyed `[run_id, seq]` and
 * hydrates a new subscriber from it before any live batch is delivered. Both
 * halves have unit tests; what has never been exercised is the two of them
 * meeting in a real browser, across a real navigation, with a real
 * `EventSource` reconnecting behind them. A duplication bug lived exactly
 * there once, and the offline tests did not see it — a fake EventSource does
 * not reconnect, and a fake IndexedDB does not survive a page load because
 * there is no page load.
 *
 * Note what is deliberately NOT asserted: that the reloaded tab still has
 * every message the run has emitted. It legitimately may not. The app's
 * connect-time snapshot carries only the latest status and progress point,
 * the explicit backfill needs a warehouse (503 here), and the seq gap that
 * leaves is reported rather than silently closed. Asserting "no loss" would
 * be asserting something the design does not promise.
 *
 * What it does promise, and what is asserted:
 *   - history the tab had before the reload is still there afterwards;
 *   - nothing is rendered twice;
 *   - the tab keeps following the run to completion;
 *   - the client's final line count never exceeds what the durable writer
 *     recorded — the arithmetic a duplication bug fails.
 */

import { expect, test } from "@playwright/test";

import { chip, logRows, logTotals, startRun } from "./app";
import { durableLogs } from "./stack";

test("reloading mid-run keeps the run's history and does not duplicate it", async ({ page }) => {
  const { runId } = await startRun(page);

  // Wait for real history to exist before reloading — a reload that happens
  // before anything arrived would prove nothing about hydration.
  await expect
    .poll(async () => (await logTotals(page)).total, {
      message: "no log lines arrived before the reload",
      timeout: 60_000,
    })
    // Two, not more: logs emitted before the browser's channel opened are not
    // replayed by the connect-time snapshot, so how much early history a tab
    // catches depends on how fast the job process starts.
    .toBeGreaterThanOrEqual(2);
  const before = await logRows(page).allInnerTexts();

  await page.reload();

  await expect
    .poll(async () => logRows(page).count(), {
      message: "the reloaded tab never hydrated any history from IndexedDB",
      timeout: 60_000,
    })
    .toBeGreaterThanOrEqual(before.length);

  const after = await logRows(page).allInnerTexts();
  expect(
    before.filter((line) => !after.includes(line)),
    "lines the tab had before the reload went missing after it",
  ).toEqual([]);
  expect(
    after.length - new Set(after).size,
    "the same log line was rendered more than once after hydrating",
  ).toBe(0);

  // Still following: the reloaded tab must pick the live run back up, not sit
  // on a hydrated snapshot.
  await expect(chip(page, "Run state")).toContainText("succeeded", { timeout: 120_000 });

  const final = await logRows(page).allInnerTexts();
  expect(final.length - new Set(final).size, "duplicates after the run finished").toBe(0);
  const durable = durableLogs(runId).filter((row) => row.client_visible);
  expect((await logTotals(page)).total).toBeLessThanOrEqual(durable.length);
});
