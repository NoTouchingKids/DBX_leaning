/**
 * The 429 — the most likely user-facing error on this platform.
 *
 * Free Edition allows five concurrent job tasks per account, across every
 * model. `POST /api/runs` enforces that itself before triggering anything,
 * inside the same transaction that claims the run's row, so the count cannot
 * race. The local stack runs the real `PostgresRunStore` against a real
 * Postgres, which means the count-and-claim exercised here is the shipped one
 * — the substituted Jobs API sits behind it and is never reached.
 *
 * The assertion that matters is not "an error appeared". It is that the
 * server's own words reach the user: the body names the live count and the
 * ceiling, and `TriggerForm` renders `error.detail` verbatim rather than a
 * copy that will drift.
 *
 * This file runs last (its numeric prefix) because it deliberately fills the
 * account's ceiling; anything that wanted to start a run while it holds five
 * slots would get the same refusal.
 */

import { expect, test } from "@playwright/test";

import { WATCHABLE_ITERATIONS } from "./app";
import { BASE_URL, MAX_CONCURRENT_RUNS } from "./stack";

interface RunRow {
  run_id: string;
  status: string;
}

const TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"]);

test("filling the concurrency ceiling refuses the next run with the server's own 429", async ({
  page,
  request,
}) => {
  const active = async (): Promise<RunRow[]> => {
    const response = await request.get(`${BASE_URL}/api/runs?limit=500`);
    expect(response.ok()).toBeTruthy();
    const body = (await response.json()) as { runs: RunRow[] };
    return body.runs.filter((run) => !TERMINAL.has(run.status));
  };

  // Top up rather than firing five: an earlier test may still be holding a
  // slot, and the ceiling is account-wide, not per-test.
  const held = await active();
  for (let i = held.length; i < MAX_CONCURRENT_RUNS; i++) {
    const response = await request.post(`${BASE_URL}/api/runs`, {
      data: { model: "annealing", config: { iterations: Number(WATCHABLE_ITERATIONS) } },
    });
    expect(response.status(), await response.text()).toBe(202);
  }

  await expect
    .poll(async () => (await active()).length, {
      message: "the ceiling never filled — the runs finished faster than they could be started",
      timeout: 60_000,
    })
    .toBe(MAX_CONCURRENT_RUNS);

  // Now the real form, in the browser, with every slot taken.
  await page.goto(`${BASE_URL}/models/annealing`);
  await page.getByRole("button", { name: /Start run/ }).click();

  const refusal = page.getByText("Refused (HTTP 429)");
  await expect(refusal).toBeVisible();
  // The server's sentence, not the client's: "N runs already active and the
  // account ceiling is M concurrent job tasks; wait for one to finish".
  const callout = refusal.locator("..");
  await expect(callout).toContainText(
    new RegExp(`${MAX_CONCURRENT_RUNS} runs already active`),
  );
  await expect(callout).toContainText(
    new RegExp(`account ceiling is ${MAX_CONCURRENT_RUNS} concurrent job tasks`),
  );

  // Leave the stack as it was found: hold the suite here until the ceiling
  // clears, so a later file (or a rerun against a still-running stack) does
  // not inherit five held slots.
  await expect
    .poll(async () => (await active()).length, { timeout: 180_000 })
    .toBe(0);
});
