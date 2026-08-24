/**
 * Locators and flows for the run page, in one place.
 *
 * These tests own no application code, so everything here addresses the DOM
 * the way a user does — accessible roles and visible labels — rather than
 * through test ids that would have to be added to `src/`. Where that is
 * fragile it is called out at the locator, because a browser test that
 * silently stops asserting anything when a label is reworded is worse than
 * one that breaks.
 */

import { expect, type Locator, type Page } from "@playwright/test";

import { BASE_URL } from "./stack";

/**
 * A run long enough to be *watched* rather than merely observed after the
 * fact. `annealing` is one of the three models that need no third-party
 * solver, and its `iterations` field is an honest duration knob: the model
 * plans the whole search up front, so 2.5M iterations is a run of roughly
 * ten to twenty seconds that reports a truthful `percent_complete` the whole
 * way through.
 *
 * A default `bayesian_ab` run finishes in under a second, which is the wrong
 * shape for every assertion here — by the time a browser has a channel open,
 * there is nothing left to stream.
 */
export const WATCHABLE_ITERATIONS = "2500000";

/** One of the `Chip`s in the run identity bar, addressed by its label.
 *  `exact` matters: "Stream" would otherwise also match the sidebar's
 *  "Streaming results". */
export function chip(page: Page, label: string): Locator {
  return page.getByText(label, { exact: true }).locator("..");
}

/** `percent_complete`, straight off the progress messages. Indeterminate runs
 *  carry no `aria-valuenow` at all — that is deliberate in `ProgressStrip`,
 *  and it is why this reads the attribute rather than trusting a number. */
export function percentComplete(page: Page): Locator {
  return page.getByRole("progressbar", { name: "percent complete" });
}

export async function percentValue(page: Page): Promise<number | null> {
  const raw = await percentComplete(page).getAttribute("aria-valuenow");
  return raw === null ? null : Number(raw);
}

/** The rendered log rows. `LogPane` windows its list, so this counts what is
 *  on screen, not what the store holds — hence `logTotals` for the totals. */
export function logRows(page: Page): Locator {
  return page.locator('[role="log"] [title]');
}

/**
 * `LogPane` prints "N of M lines"; M is the store's count for the run, which
 * is the number a duplication bug inflates. Both are run through
 * `formatCount`, i.e. `Intl.NumberFormat`, so the group separator is whatever
 * the browser's locale uses — strip everything that is not a digit rather
 * than assume a comma.
 */
export async function logTotals(page: Page): Promise<{ shown: number; total: number }> {
  const text = await page.getByText(/\d+ of \d+ lines/).innerText();
  const match = /([\d\u00a0\u202f,. ]*\d) of ([\d\u00a0\u202f,. ]*\d) lines/.exec(text);
  if (match === null) {
    throw new Error(`could not parse the log counter from ${JSON.stringify(text)}`);
  }
  const num = (value: string) => Number(value.replace(/[^0-9]/g, ""));
  return { shown: num(match[1]!), total: num(match[2]!) };
}

export interface StartedRun {
  runId: string;
}

/**
 * Fill the real trigger form and press the real button.
 *
 * Deliberately not `POST /api/runs` from the test: the point of this suite is
 * the browser, and the form is the only place the client's config building,
 * the 202 handling and the run-selection side effect are exercised together.
 *
 * `model` is a parameter but only `annealing` will work as written — the
 * duration knob is filled by field id, and `iterations` is that model's.
 * Another model means another field, not another argument here.
 */
export async function startRun(
  page: Page,
  { model = "annealing", iterations = WATCHABLE_ITERATIONS } = {},
): Promise<StartedRun> {
  // Absolute, not baseURL-relative: these tests hand-build browser contexts
  // for the multi-tab case, and a context made with `browser.newContext()`
  // does not reliably carry the config's baseURL.
  await page.goto(`${BASE_URL}/models/${model}`);
  await page.locator("#cfg-iterations").fill(iterations);
  await page.getByRole("button", { name: /Start run/ }).click();

  // The workspace writes the new run into the query string on the 202, which
  // is also how it stops showing whichever run was selected before. Waiting
  // for it is what makes every later assertion refer to *this* run.
  await page.waitForURL(/[?&]run=run-/, { timeout: 30_000 });
  const runId = new URL(page.url()).searchParams.get("run");
  expect(runId, "the 202 should have selected the new run in the URL").not.toBeNull();
  return { runId: runId! };
}

/** Wait until the run page is showing live progress for a run in flight. */
export async function waitForLiveProgress(page: Page): Promise<number> {
  await expect
    .poll(async () => (await percentValue(page)) ?? -1, {
      message: "percent_complete never arrived over the live path",
      timeout: 60_000,
    })
    .toBeGreaterThan(0);
  return (await percentValue(page)) ?? -1;
}
