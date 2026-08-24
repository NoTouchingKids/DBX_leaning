/**
 * "A terminal run gets no live channel."
 *
 * It is behaviour #3 in `transport/hub.ts`'s header and one of the four
 * load-bearing properties named in `frontend/README.md`. Nothing further can
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
 * KNOWN DEFECT — this test is expected to FAIL, and `test.fail()` says so.
 *
 * Opening a finished run in a browser that has never seen it opens one SSE
 * connection to it. Measured, not inferred: the app serves one more
 * `GET /api/runs/{id}/stream` than before the page loaded.
 *
 * Why: `RunWorkspace` passes `terminal: rowTerminal` to
 * `useReconnectableRunStream`, and `rowTerminal` comes from `GET
 * /api/runs/{id}`. On the first render that query has not resolved, so it is
 * `false`; `useRunStream` reads `terminal` once, inside the `subscribe`
 * callback, and deliberately excludes it from the dependency list ("learning
 * mid-stream that a run finished must not tear the subscription down"). So
 * the subscription is taken out as non-terminal and the hub opens a channel.
 * The warm-cache case above escapes it only because `hub.ts` finds
 * `cached.terminal` in IndexedDB first.
 *
 * How bad: one connection per cold view of a finished run, closed as soon as
 * the server's connect-time snapshot delivers the terminal status — the UI
 * never even shows it as live. Small, but it is exactly the waste the rule
 * exists to prevent, and it is invisible to every offline test because the
 * fake EventSource in jsdom costs nothing to open.
 *
 * Removing `test.fail()` is the acceptance check for a fix. It is left
 * failing rather than deleted or renamed to something weaker, because a test
 * named after what the code does instead of what it should do stops being a
 * test.
 */
test("a finished run the client has never seen opens no live channel", async ({
  browser,
  page,
}) => {
  test.fail(
    true,
    "known defect: the subscription is taken out before GET /api/runs/{id} resolves, " +
      "so the hub opens a channel to a run it has not yet been told is finished",
  );

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
