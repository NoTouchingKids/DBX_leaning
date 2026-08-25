/**
 * "A terminal run gets no live channel."
 *
 * It is behaviour #3 in `transport/hub.ts`'s header and one of the four
 * load-bearing properties named in `app/client/README.md`. Nothing further can
 * ever arrive on a finished run, so an `EventSource` on one is a connection
 * that exists only to be cut and retried — and connections are the scarce
 * thing on Free Edition.
 *
 * Observing this from the browser side is not possible with
 * `page.on("request")`: the SPA opens its EventSource inside a SharedWorker,
 * whose traffic Playwright cannot see (see `stack.ts`). Both tests below
 * therefore count the app's own served requests, which is the same fact read
 * from the other end of the wire.
 *
 * The two cases are split because they do not behave the same, and the split
 * is the finding — see the second test.
 */

import { expect, test, type Page } from "@playwright/test";

import { chip, startRun } from "./app";
import { BASE_URL, streamConnectionCount } from "./stack";

/** Enough for a connection that was going to be opened to have been opened.
 *  Proving an absence has no event to wait on; this is bounded by the fact
 *  that the page has already finished rendering the finished run. */
const SETTLE_MS = 2000;

/** Runs the `annealing` model briefly and waits for it to finish, so the
 *  tests below have a genuinely terminal run to open. */
async function finishedRun(page: Page): Promise<string> {
  const { runId } = await startRun(page, { iterations: "20000" });
  await expect(chip(page, "Run state")).toContainText("succeeded", { timeout: 120_000 });
  return runId;
}

test("a finished run already in the client's cache opens no live channel", async ({ browser }) => {
  const profile = await browser.newContext();
  const page = await profile.newPage();
  const runId = await finishedRun(page);
  const afterTheRun = streamConnectionCount(runId);

  // Same profile, so the transport has this run cached in IndexedDB and knows
  // it is terminal before anything else resolves.
  const revisit = await profile.newPage();
  await revisit.goto(`${BASE_URL}/models/annealing?run=${runId}`);
  await expect(revisit.getByText("Completed run")).toBeVisible();
  await revisit.waitForTimeout(SETTLE_MS);

  expect(streamConnectionCount(runId)).toBe(afterTheRun);
  await expect(chip(revisit, "Stream")).toContainText("no channel");
  await profile.close();
});

/**
 * This test was written failing, and the defect it named is now fixed.
 *
 * What it caught: opening a finished run in a browser that had never seen it
 * opened one SSE connection to it. Measured, not inferred — the app served
 * one more `GET /api/runs/{id}/stream` than before the page loaded.
 *
 * Why it happened: `useRunStream` reads `terminal` once inside its subscribe
 * callback and deliberately keeps it out of the dependency list, because
 * learning mid-stream that a run finished must not tear a live subscription
 * down and rebuild it. Correct at that end, and it left a hole at the other:
 * on a cold page the run row has not loaded, so `terminal` is false on the
 * first render and the hub opens a channel to a run that will never send
 * anything. The warm-cache case above escaped it only because the hub finds
 * `cached.terminal` in IndexedDB first.
 *
 * The fix keeps the constraint that caused it: the answer is forwarded as a
 * one-way `terminal-hint` rather than as a resubscribe. If it lands while the
 * worker is still reading IndexedDB — the common case on a cold page — no
 * connection is opened at all, because the check that decides runs after that
 * read. If it lands later, the open channel is closed.
 *
 * Invisible to all 661 offline tests, because a fake EventSource in jsdom
 * costs nothing to open. That is what this suite is for.
 */
test("a finished run the client has never seen opens no live channel", async ({
  browser,
  page,
}) => {
  const runId = await finishedRun(page);
  const afterTheRun = streamConnectionCount(runId);

  // A fresh profile: empty IndexedDB, so the only thing that could tell the
  // transport this run is finished is GET /api/runs/{id}.
  const coldProfile = await browser.newContext();
  const cold = await coldProfile.newPage();
  await cold.goto(`${BASE_URL}/models/annealing?run=${runId}`);
  await expect(cold.getByText("Completed run")).toBeVisible();
  await cold.waitForTimeout(SETTLE_MS);

  expect(streamConnectionCount(runId)).toBe(afterTheRun);
  await coldProfile.close();
});
