/**
 * The one that covers the most untested surface: a real model, triggered from
 * the real form, watched live in a real browser.
 *
 * Nothing in this file is simulated. The button posts to the real
 * `POST /api/runs`, which claims a slot in a real Postgres, which launches the
 * real `job/` harness in its own process, which opens a real WebSocket to the
 * app and emits real envelope messages, which the app fans out over real SSE
 * to a real `SharedWorker` in Chromium, which parses them and posts them to
 * the page. Not one of those hops is exercised by the unit tests under
 * `src/`, every one of which runs against a fake `EventSource` in jsdom.
 */

import { expect, test } from "@playwright/test";

import { chip, logRows, logTotals, percentValue, startRun, waitForLiveProgress } from "./app";
import { BASE_URL, durableLogs, streamConnectionCount } from "./stack";

test("a live run's telemetry reaches the DOM over SSE, and lands durably too", async ({
  page,
  request,
}) => {
  /*
   * The premise of every assertion below: there is no warehouse behind this
   * stack, so the HTTP read paths that could otherwise explain telemetry on
   * the page — the explicit backfill, and the results endpoint behind the
   * same dependency — answer 503. Anything rendered therefore arrived over
   * the live path. If this ever fails because someone pointed the suite at a
   * stack with a warehouse, the test has not broken; its claim has weakened,
   * and it needs re-thinking.
   */
  const backfill = await request.get(`${BASE_URL}/api/runs/does-not-matter/messages`);
  expect(
    backfill.status(),
    "the local stack has no warehouse, so backfill must be unavailable — " +
      "otherwise this test cannot claim the telemetry came over the live path",
  ).toBe(503);

  const { runId } = await startRun(page);

  // The client's own view of the transport. Separate from the run's state on
  // purpose (see RunIdentityBar): this chip is the browser saying it holds an
  // open channel, which is the thing no jsdom test can assert.
  await expect(chip(page, "Stream")).toContainText("live", { timeout: 30_000 });
  await expect(chip(page, "Run state")).toContainText("running", { timeout: 30_000 });

  // Streaming, not a single snapshot: the server hands a fresh viewer the
  // latest progress point on connect, so one non-zero reading proves nothing.
  // Two increasing ones can only come from messages pushed after the open.
  const first = await waitForLiveProgress(page);
  await expect
    .poll(async () => (await percentValue(page)) ?? -1, {
      message: "percent_complete never advanced after the first reading",
      timeout: 60_000,
    })
    .toBeGreaterThan(first);

  await expect(logRows(page).first()).toBeVisible();

  await expect(chip(page, "Run state")).toContainText("succeeded", { timeout: 120_000 });
  // A result is not best-effort — a SUCCEEDED run must be able to say how many
  // rows it wrote, which is what tells "wrote nothing" apart from "never got
  // that far".
  await expect(page.getByText(/rows written/)).toBeVisible();

  // Exactly one connection for the whole run. Not a nicety: an EventSource
  // that reconnects on its own would show up here as a rising count, and on
  // Free Edition connections are the scarce resource.
  expect(streamConnectionCount(runId)).toBe(1);

  /*
   * Delta is the floor, not a fallback tier: it runs in parallel with the
   * live path regardless of whether anyone is watching. So the durable copy
   * must be a superset of what the browser saw — the live path is allowed to
   * drop logs under pressure, the durable one never is.
   */
  const rendered = await logTotals(page);
  const durable = durableLogs(runId).filter((row) => row.client_visible);
  expect(rendered.total).toBeGreaterThan(0);
  expect(durable.length).toBeGreaterThanOrEqual(rendered.total);
});
