/**
 * Integration smoke: the generic page against a fake server and a fake
 * stream. Its job is to prove the wiring — that the cancel button's enabled
 * state really is driven by `GET /api/runs/{id}`'s `live` field and not by the
 * status, and that the page renders for a model with no bespoke view.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MCMC } from "@/lib/models";
import { FakeEventSource } from "@/transport/__fixtures__/fakeEventSource";
import { TransportClient, __setTransportForTests, resetTransportForTests } from "@/transport/client";
import { closeDb } from "@/transport/db";

import { RunWorkspace } from "./RunWorkspace";

const RUN_ID = "run-abc123456789";

function row(over: Record<string, unknown> = {}) {
  return {
    run_id: RUN_ID,
    job_run_id: "9001",
    model: "mcmc",
    status: "RUNNING",
    detail: "drawing 1200/3000",
    started_ts: Date.now() - 60_000,
    updated_ts: Date.now(),
    requested_by: "someone@example.com",
    ...over,
  };
}

function fakeServer({ live }: { live: boolean }) {
  return vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.startsWith("/api/models")
      ? { models: [{ name: "mcmc", job_id: "1" }], default_job_id: null }
      : url.startsWith(`/api/runs/${RUN_ID}/messages`)
        ? { run_id: RUN_ID, after_seq: -1, count: 0, messages: [], more: false, next_after_seq: -1 }
        : url.startsWith(`/api/runs/${RUN_ID}`)
          ? { run: row(), live, last_seq_seen: 3 }
          : { count: 1, runs: [{ ...row(), live }] };

    return Promise.resolve({
      ok: true,
      status: 200,
      statusText: "OK",
      text: () => Promise.resolve(JSON.stringify(body)),
      json: () => Promise.resolve(body),
    } as unknown as Response);
  });
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/models/mcmc?run=${RUN_ID}`]}>
        <RunWorkspace spec={MCMC} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(async () => {
  FakeEventSource.reset();
  await closeDb();
  __setTransportForTests(
    new TransportClient({
      forceTier: "in-page",
      createEventSource: (url) => new FakeEventSource(url),
    }),
  );
});

afterEach(async () => {
  resetTransportForTests();
  vi.unstubAllGlobals();
  await closeDb();
});

describe("RunWorkspace", () => {
  it("enables cancel for a RUNNING run that has a live job socket", async () => {
    vi.stubGlobal("fetch", fakeServer({ live: true }));
    mount();

    const cancel = await screen.findByRole("button", { name: /cancel run/i });
    await waitFor(() => expect(cancel).toBeEnabled());
  });

  it("disables cancel for a RUNNING run with no socket, and says why", async () => {
    // No live WebSocket means the cancel frame has nowhere to go: the endpoint
    // would 409 every time. The run is also stranded — nothing will ever move
    // it to a terminal status — which the page has to say without offering a
    // fix, because there is not one.
    vi.stubGlobal("fetch", fakeServer({ live: false }));
    mount();

    const cancel = await screen.findByRole("button", { name: /cancel run/i });
    await waitFor(() => expect(cancel).toBeDisabled());
    expect(await screen.findByText(/stranded/i)).toBeInTheDocument();
  });

  it("renders the generic progress view with no model-specific code", async () => {
    vi.stubGlobal("fetch", fakeServer({ live: true }));
    mount();

    // Indeterminate until a progress message arrives — not a 0% bar.
    const bar = await screen.findByRole("progressbar");
    expect(bar).not.toHaveAttribute("aria-valuenow");
    expect(screen.getByRole("log", { name: /run log/i })).toBeInTheDocument();
    // Advanced config fields are behind a disclosure, not on the main form.
    expect(screen.getByText(/^Advanced —/)).toBeInTheDocument();
  });
});
