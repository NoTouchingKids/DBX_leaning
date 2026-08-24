/**
 * The page, against a fake server.
 *
 * The per-model registry is replaced with a probe view. That is not a
 * shortcut around rendering the real ones — those have their own suites — it
 * is the only way to assert the thing this page is actually responsible for:
 * that a `ModelView` is handed a terminal `state` and a snapshot rebuilt from
 * backfilled pages, with no live stream anywhere in the picture.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { isSettled, type ModelViewProps } from "@/components/models/contract";
import { makeMessages } from "@/dev/fixtures";
import { isLog, isResult, type Message } from "@/lib/envelope";

import { RunDetail } from "./RunDetail";

/* ------------------------------------------------------------------ *
 * A probe standing in for the nine real views
 * ------------------------------------------------------------------ */

const seen: ModelViewProps[] = [];

function Probe(props: ModelViewProps) {
  seen.push(props);
  return <div data-testid="signature">signature</div>;
}

vi.mock("@/components/models/registry", () => ({
  viewFor: () => ({
    model: "probe",
    Signature: Probe,
    charts: [],
    honesty: "the bars are decorative; the numbers under them are not.",
  }),
}));

/* ------------------------------------------------------------------ *
 * Fake server
 * ------------------------------------------------------------------ */

const RUN_ID = "run-abc123456789";

function respond(status: number, body: unknown) {
  return Promise.resolve({
    ok: status < 400,
    status,
    statusText: status === 404 ? "Not Found" : "OK",
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  } as unknown as Response);
}

/** Backfill drops `run_id` and every `client_visible: false` log — see
 *  `app/repository.py::messages_since`. */
function asServerRows(messages: readonly Message[]) {
  return messages
    .filter((message) => !isLog(message) || message.client_visible)
    .map((message) => {
      const { run_id: _dropped, ...rest } = message;
      return rest as unknown;
    });
}

function server(options: {
  status?: string;
  live?: boolean;
  model?: string;
  rows?: readonly unknown[];
  detail404?: boolean;
}) {
  const rows = options.rows ?? [];
  return vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/messages")) {
      return respond(200, {
        run_id: RUN_ID,
        after_seq: -1,
        count: rows.length,
        messages: rows,
        more: false,
        next_after_seq: -1,
      });
    }
    if (options.detail404 === true) {
      return respond(404, { detail: `no such run ${RUN_ID}` });
    }
    return respond(200, {
      run: {
        run_id: RUN_ID,
        job_run_id: "9001",
        model: options.model ?? "mcmc",
        status: options.status ?? "SUCCEEDED",
        detail: "done",
        started_ts: 1_700_000_000_000,
        updated_ts: 1_700_000_060_000,
        requested_by: "someone@example.com",
      },
      live: options.live ?? false,
      last_seq_seen: null,
    });
  });
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/runs/${RUN_ID}`]}>
        <RunDetail runId={RUN_ID} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  seen.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/* ------------------------------------------------------------------ *
 * Tests
 * ------------------------------------------------------------------ */

describe("RunDetail", () => {
  it("renders a 404 as a real answer, not a spinner", async () => {
    const fetchMock = server({ detail404: true });
    vi.stubGlobal("fetch", fetchMock);
    mount();

    expect(await screen.findByText("No such run")).toBeInTheDocument();
    // The server's own words, not a client-side copy of them.
    expect(screen.getByText(`no such run ${RUN_ID}`)).toBeInTheDocument();
    // And nothing was asked of the SQL warehouse for a run that does not exist.
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]).includes("/messages")),
    ).toHaveLength(0);
  });

  it("hands the model view a settled state and a backfilled snapshot", async () => {
    const source = makeMessages("mcmc", "typical", "SUCCEEDED");
    vi.stubGlobal("fetch", server({ rows: asServerRows(source) }));
    mount();

    await waitFor(() => expect(screen.getByTestId("signature")).toBeInTheDocument());
    await waitFor(() => {
      const last = seen.at(-1);
      expect(last?.snapshot.progress.length).toBeGreaterThan(0);
    });

    const last = seen.at(-1);
    // Locked to the registry row's terminal status, so every view freezes in
    // one flat frame instead of animating a run that ended.
    expect(last?.state).toBe("SUCCEEDED");
    expect(isSettled(last?.state ?? null)).toBe(true);
    // Rebuilt from Delta, with no connection ever opened.
    expect(last?.snapshot.connection).toBe("idle");
    expect(last?.snapshot.hydrated).toBe(true);
    expect(last?.snapshot.terminal).toBe(true);
    // run_id was put back on every message despite the server omitting it.
    expect(last?.snapshot.progress.every((p) => p.run_id === RUN_ID)).toBe(true);

    // The honesty note is rendered by the page, next to the animation.
    expect(screen.getByText(/the bars are decorative/)).toBeInTheDocument();
    // The stream chip says "no channel" without dressing it as a failure.
    expect(screen.getByText("no channel")).toBeInTheDocument();
  });

  it("says plainly that a stranded RUNNING run will never finish, and offers nothing", async () => {
    vi.stubGlobal("fetch", server({ status: "RUNNING", live: false, rows: [] }));
    mount();

    expect(
      await screen.findByText(/Stranded: RUNNING, with no channel to the job/),
    ).toBeInTheDocument();
    expect(screen.getByText(/nothing will ever move this row to a terminal status/)).toBeInTheDocument();
    // No action is offered, because none exists.
    expect(screen.queryByRole("button", { name: /cancel/i })).not.toBeInTheDocument();
  });

  it("never renders row_count 0 as an empty state", async () => {
    const zeroed = makeMessages("scenario", "typical", "SUCCEEDED").map((message) =>
      isResult(message) ? { ...message, row_count: 0, preview: [] } : message,
    );
    vi.stubGlobal("fetch", server({ model: "scenario", rows: asServerRows(zeroed) }));
    mount();

    expect(await screen.findByText(/Zero rows were written durably/)).toBeInTheDocument();
    expect(screen.getByText(/row_count 0/)).toBeInTheDocument();
  });

  it("calls a finished run with no final chunk incomplete, not 'still arriving'", async () => {
    // The chunked fixture's last chunk carries `final: true`; dropping it is
    // exactly what a run that stopped between windows — or whose final result
    // write did not land — leaves behind in Delta.
    const chunks = makeMessages("streaming_results", "chunked", "SUCCEEDED").filter(
      (message) => !isResult(message) || !message.final,
    );
    vi.stubGlobal(
      "fetch",
      server({ model: "streaming_results", rows: asServerRows(chunks) }),
    );
    mount();

    expect(await screen.findByText(/Incomplete — not still arriving/)).toBeInTheDocument();
    expect(screen.queryByText(/more expected/)).not.toBeInTheDocument();
  });

  it("surfaces a permanent seq gap as permanent, with no way to close it", async () => {
    const gappy = makeMessages("mcmc", "gappy", "SUCCEEDED");
    vi.stubGlobal("fetch", server({ rows: asServerRows(gappy) }));
    mount();

    expect(
      await screen.findByText(/Gaps in the seq stream — permanent/),
    ).toBeInTheDocument();
    expect(screen.getByText(/paging further cannot close them/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /load missing/i })).not.toBeInTheDocument();
  });

  it("does not offer to re-read a terminal run, and does offer it for one that is not", async () => {
    vi.stubGlobal("fetch", server({ status: "SUCCEEDED", rows: [] }));
    const terminal = mount();
    await waitFor(() => expect(screen.getByTestId("signature")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Re-read/ })).not.toBeInTheDocument();
    terminal.unmount();

    vi.unstubAllGlobals();
    vi.stubGlobal("fetch", server({ status: "RUNNING", live: true, rows: [] }));
    mount();
    // A live run belongs on the model page; this one links there rather than
    // pretending to stream.
    expect(await screen.findByText("Watch it live →")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Re-read/ })).toBeInTheDocument();
  });

  it("pages the log on demand rather than pulling the whole run", async () => {
    // Two full pages then a short one, so "load more" is real.
    const rows = asServerRows(makeMessages("mcmc", "dense", "SUCCEEDED"));
    const pageSize = 100;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (!url.pathname.endsWith("/messages")) {
        return respond(200, {
          run: {
            run_id: RUN_ID,
            job_run_id: null,
            model: "mcmc",
            status: "SUCCEEDED",
            detail: null,
            started_ts: 1_700_000_000_000,
            updated_ts: 1_700_000_060_000,
            requested_by: null,
          },
          live: false,
          last_seq_seen: null,
        });
      }
      const after = Number(url.searchParams.get("after_seq") ?? "-1");
      const slice = rows
        .filter((row) => (row as { seq: number }).seq > after)
        .slice(0, pageSize);
      return respond(200, {
        run_id: RUN_ID,
        after_seq: after,
        count: slice.length,
        messages: slice,
        more: slice.length >= pageSize,
        next_after_seq: (slice.at(-1) as { seq: number } | undefined)?.seq ?? after,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    mount();

    const loadMore = await screen.findByRole("button", { name: /Load more from Delta/ });
    const before = fetchMock.mock.calls.filter((call) =>
      String(call[0]).includes("/messages"),
    ).length;
    expect(before).toBe(1);

    await userEvent.click(loadMore);
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter((call) => String(call[0]).includes("/messages")).length,
      ).toBe(2),
    );
    // Paged by seq, not by offset.
    expect(String(fetchMock.mock.calls.at(-1)?.[0])).toMatch(/after_seq=\d+/);
  });
});
