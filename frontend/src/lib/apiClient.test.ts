import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, cancelRun, listRuns, triggerRun } from "./apiClient";

function respond(status: number, body: unknown) {
  const text = JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `status ${status}`,
    text: () => Promise.resolve(text),
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

function mockFetch(response: Response) {
  const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
    Promise.resolve(response),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("error bodies", () => {
  it("keeps the 429's own text, which names the count and the ceiling", async () => {
    // Free Edition allows 5 concurrent job tasks account-wide. This is the
    // error a user is most likely to hit, and the numbers in it are the whole
    // point — a generic message would throw them away.
    const detail = "5 runs already active; the ceiling is 5";
    mockFetch(respond(429, { detail }));

    await expect(triggerRun({ model: "mcmc", config: {} })).rejects.toMatchObject({
      status: 429,
      detail,
    });
  });

  it("keeps the 409 cancel escape hatch verbatim, command and all", async () => {
    const detail =
      "no live channel to this run; cancel it with `databricks jobs cancel-run " +
      "--run-id <job_run_id>` (a hard kill: the job gets SIGTERM and keeps " +
      "whatever results it already wrote)";
    mockFetch(respond(409, { detail }));

    const error = await cancelRun("run-a").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).detail).toBe(detail);
  });

  it("falls back to the raw body when the error is not FastAPI-shaped", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve({
        ok: false,
        status: 502,
        statusText: "Bad Gateway",
        text: () => Promise.resolve("upstream exploded"),
      } as unknown as Response),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(listRuns()).rejects.toMatchObject({ detail: "upstream exploded" });
  });
});

describe("triggerRun", () => {
  it("reports a clean 202 with no warning", async () => {
    mockFetch(
      respond(202, {
        run_id: "run-a",
        job_run_id: "912",
        model: "mcmc",
        status: "QUEUED",
        registered: true,
        job_run_id_stored: true,
        stream: "/api/runs/run-a/stream",
      }),
    );

    await expect(triggerRun({ model: "mcmc", config: {} })).resolves.toEqual({
      run_id: "run-a",
      job_run_id: "912",
      model: "mcmc",
      warning: null,
    });
  });

  it("treats `registered: false` as a success carrying a warning", async () => {
    // The job IS running; only the registry row failed. Rendering this as a
    // failure would tell someone nothing started when something did — and
    // that invisible run is still holding one of five account-wide slots.
    mockFetch(
      respond(202, {
        run_id: "run-b",
        job_run_id: "913",
        model: "mcmc",
        registered: false,
        warning: "run_status row could not be written",
      }),
    );

    const outcome = await triggerRun({ model: "mcmc", config: {} });
    expect(outcome.run_id).toBe("run-b");
    expect(outcome.warning).toBe("run_status row could not be written");
  });

  it("warns when the job launched but its job_run_id was not stored", async () => {
    mockFetch(
      respond(202, {
        run_id: "run-c",
        job_run_id: "914",
        model: "mcmc",
        status: "QUEUED",
        registered: true,
        job_run_id_stored: false,
        stream: "/api/runs/run-c/stream",
      }),
    );

    const outcome = await triggerRun({ model: "mcmc", config: {} });
    expect(outcome.warning).toContain("job-run id could not be stored");
  });
});

describe("listRuns", () => {
  it("sends `model` as a server-side query param, not a client-side sieve", async () => {
    // `repo.list_runs` takes it. Filtering a top-N window in the browser is
    // only correct while the window happens to hold everything relevant.
    const fetchMock = mockFetch(respond(200, { count: 0, runs: [] }));
    await listRuns({ model: "mcmc", limit: 25, status: "RUNNING" });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/runs?limit=25&status=RUNNING&model=mcmc");
  });

  it("omits absent params rather than sending empty ones", async () => {
    const fetchMock = mockFetch(respond(200, { count: 0, runs: [] }));
    await listRuns();
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/runs");
  });
});
