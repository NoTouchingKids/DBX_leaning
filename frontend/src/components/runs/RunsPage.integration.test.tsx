/**
 * Wiring tests for the run-history page.
 *
 * Lives here rather than beside the page because this directory owns every
 * piece of this feature. What is asserted is behaviour, not markup: which
 * requests go out (and how many), and that the two rows the endpoint cannot
 * distinguish for you — a healthy `RUNNING` and a stranded one — do not read
 * the same on screen.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RunsPage } from "@/pages/RunsPage";
import type { Run } from "@/lib/apiClient";

function row(over: Partial<Run> = {}): Run {
  return {
    run_id: "run-000000000001",
    job_run_id: "9001",
    model: "mcmc",
    status: "SUCCEEDED",
    detail: null,
    started_ts: Date.now() - 120_000,
    updated_ts: Date.now() - 60_000,
    requested_by: "someone@example.com",
    live: false,
    ...over,
  };
}

/** Answers `GET /api/runs`, honouring the `model` filter server-side the way
 *  `repo.list_runs` does, and recording every URL it was asked for. */
function fakeServer(rows: readonly Run[]) {
  const urls: string[] = [];
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    urls.push(url);
    const params = new URLSearchParams(url.split("?")[1] ?? "");
    const model = params.get("model");
    const status = params.get("status");
    const matched = rows.filter(
      (r) => (model === null || r.model === model) && (status === null || r.status === status),
    );
    const body = {
      count: matched.length,
      filters: { status, model },
      runs: matched,
    };
    return Promise.resolve({
      ok: true,
      status: 200,
      statusText: "OK",
      text: () => Promise.resolve(JSON.stringify(body)),
      json: () => Promise.resolve(body),
    } as unknown as Response);
  });
  vi.stubGlobal("fetch", fetchMock);
  return urls;
}

function mount(url: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <RunsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the model-page link", () => {
  it("pre-filters server-side from the URL, so it survives a refresh or a paste", async () => {
    // This is the exact link the five model pages build:
    // `/runs?model=<name>`. If it stopped reaching the query param the page
    // would show every model and look like it had worked.
    const urls = fakeServer([
      row({ run_id: "run-mcmc", model: "mcmc" }),
      row({ run_id: "run-scen", model: "scenario" }),
    ]);
    mount("/runs?model=mcmc");

    expect(await screen.findByText("run-mcmc")).toBeInTheDocument();
    expect(screen.queryByText("run-scen")).not.toBeInTheDocument();
    expect(urls).toContain("/api/runs?limit=50&model=mcmc");
  });
});

describe("the capacity read", () => {
  it("is a separate, unfiltered request when a filter is applied", async () => {
    // The ceiling is account-wide. Counting the model-filtered table would
    // answer "active runs of mcmc" and label it as the account's.
    const urls = fakeServer([
      row({ run_id: "run-mcmc", model: "mcmc", status: "RUNNING", live: true }),
      row({ run_id: "run-scen", model: "scenario", status: "RUNNING", live: true }),
    ]);
    mount("/runs?model=mcmc");

    await screen.findByText("run-mcmc");
    await waitFor(() => expect(urls).toContain("/api/runs?limit=50"));

    // Both runs are counted even though only one is on screen.
    expect(
      await screen.findByRole("img", { name: /2 of 5 concurrent job-task slots/i }),
    ).toBeInTheDocument();
  });

  it("costs no extra request when the page is unfiltered", async () => {
    // Unfiltered, `serverQuery` produces exactly `{limit: 50}` — the same
    // React Query key as the capacity read — so one request serves both.
    const urls = fakeServer([row({ status: "RUNNING", live: true })]);
    mount("/runs");

    await screen.findByRole("img", { name: /1 of 5 concurrent job-task slots/i });
    expect(urls).toEqual(["/api/runs?limit=50"]);
  });
});

describe("the live column", () => {
  it("distinguishes a stranded RUNNING run from a healthy one, and offers it no action", async () => {
    fakeServer([
      row({ run_id: "run-healthy", status: "RUNNING", live: true, job_run_id: "111" }),
      row({ run_id: "run-dead", status: "RUNNING", live: false, job_run_id: "222" }),
    ]);
    mount("/runs");

    const healthy = (await screen.findByText("run-healthy")).closest("tr");
    const dead = (await screen.findByText("run-dead")).closest("tr");
    expect(healthy).not.toBeNull();
    expect(dead).not.toBeNull();

    expect(within(healthy as HTMLElement).getByText(/connected/i)).toBeInTheDocument();
    expect(within(dead as HTMLElement).getByText(/stranded/i)).toBeInTheDocument();

    // The banner explains it; nothing anywhere offers to fix it, because
    // this API has no endpoint that could.
    expect(await screen.findByText(/1 run stranded/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /cancel|force|clear|reap/i })).toBeNull();
  });

  it("says nothing about `live` on a terminal row", async () => {
    // `live: true` on a SUCCEEDED row is a socket that has not closed yet,
    // not a running job.
    fakeServer([row({ run_id: "run-done", status: "SUCCEEDED", live: true })]);
    mount("/runs");

    const done = (await screen.findByText("run-done")).closest("tr");
    expect(within(done as HTMLElement).queryByText(/connected/i)).toBeNull();
  });
});

describe("the columns", () => {
  it("are exactly what the endpoint selects — no metric column", async () => {
    // A metric column means an N+1 fetch of `/api/runs/{id}` per row. This
    // test is the tripwire for adding one by accident.
    fakeServer([row()]);
    mount("/runs");

    await screen.findByText("run-000000000001");
    const headers = screen.getAllByRole("columnheader").map((th) => th.textContent);
    expect(headers).toEqual([
      "Run",
      "Model",
      "Status",
      "Job channel",
      "Started",
      "Duration",
      "Requested by",
    ]);
  });
});
