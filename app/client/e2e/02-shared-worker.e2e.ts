/**
 * Does the SharedWorker actually collapse N tabs onto one connection?
 *
 * The design rests on it — "five tabs on the same run is one connection, and
 * on Free Edition connections are the scarce thing" (`transport/client.ts`) —
 * and it is unprovable in jsdom, which has no SharedWorker at all: the unit
 * tests exercise the in-page tier through `forceTier`.
 *
 * What is asserted here, precisely:
 *
 *   - two tabs in ONE browser profile, both watching the same live run, cause
 *     exactly ONE `GET .../stream` at the server;
 *   - a third tab in a SECOND profile — which cannot share the first's
 *     SharedWorker — causes a second one.
 *
 * The second half is the control, and it is what makes the first half mean
 * something: it shows the counting method does detect an extra connection
 * when one exists, so "still 1" is evidence of sharing rather than evidence
 * of a blind instrument. Without it, a test that observed nothing would look
 * identical to a test that observed sharing.
 *
 * What is NOT asserted: which tier the client actually chose. There is no
 * externally visible signal for it, and `TransportClient.tier` is not exposed
 * on `window`. The connection count is a stronger claim than the tier name
 * anyway — a silent fall back to the per-tab worker tier would show up here
 * as two connections from one profile.
 */

import { expect, test } from "@playwright/test";

import { WATCHABLE_ITERATIONS, chip, percentValue, startRun, waitForLiveProgress } from "./app";
import { streamConnectionCount } from "./stack";

test("two tabs in one profile share a single SSE connection; a second profile opens its own", async ({
  browser,
}) => {
  // One profile = one SharedWorker. Two pages in it are two tabs of the same
  // browser, which is the situation the design is about.
  const profile = await browser.newContext();
  const first = await profile.newPage();

  // A longer run than the other tests use: three browser profiles have to
  // attach to it *while it is still live*, and they compete for the same CPU
  // the model is solving on. If the premise assertions below start failing,
  // the run is outliving the test by too little — raise this, do not weaken
  // them.
  const { runId } = await startRun(first, {
    iterations: String(Number(WATCHABLE_ITERATIONS) * 2),
  });
  await waitForLiveProgress(first);
  expect(
    streamConnectionCount(runId),
    "the tab that started the run should hold exactly one channel",
  ).toBe(1);

  const second = await profile.newPage();
  await second.goto(first.url());
  // Not "the page rendered" — that would pass with no telemetry at all. This
  // waits until the second tab is showing progress for the run, which it can
  // only have got through the shared worker, since it opened no connection of
  // its own.
  await waitForLiveProgress(second);
  expect(
    streamConnectionCount(runId),
    "a second tab in the same profile must not open a second SSE connection",
  ).toBe(1);
  await expect(chip(second, "Stream")).toContainText("live");

  const otherProfile = await browser.newContext();
  const third = await otherProfile.newPage();
  await third.goto(first.url());
  await waitForLiveProgress(third);
  // The premise of the control: this tab holds a channel to a run that is
  // still going. A finished run would also add a connection here — for an
  // unrelated reason (see 03-terminal-run) — and that would make the count
  // below prove nothing about sharing.
  await expect(chip(third, "Stream")).toContainText("live");
  expect(
    streamConnectionCount(runId),
    "a separate browser profile has its own SharedWorker, so it must open its own connection",
  ).toBe(2);

  // Both tabs of the first profile are still following the same run, not just
  // one of them; the shared connection feeds every subscriber.
  const [a, b] = [await percentValue(first), await percentValue(second)];
  expect(a).not.toBeNull();
  expect(b).not.toBeNull();

  await profile.close();
  await otherProfile.close();
});
