/**
 * The paging hook, against a counted fake server.
 *
 * Every assertion here is about *requests*, not about pixels. The two claims
 * this page makes — "a finished run is fetched once and cached" and "a long
 * run is not pulled in its entirety up front" — are both claims about network
 * traffic, and only a request count can falsify them.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useRunHistory } from "./useRunHistory";

const RUN_ID = "run-paged123456";

function respond(body: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    statusText: "OK",
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  } as unknown as Response);
}

function logAt(seq: number) {
  return {
    type: "log",
    seq,
    ts: 1_700_000_000_000 + seq,
    message: `line ${seq}`,
    level: "INFO",
    source: "model",
    phase: "run",
    client_visible: true,
  };
}

/** A server holding `total` messages, handing out `pageSize` at a time. */
function pagedServer(total: number, pageSize: number) {
  return vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    const after = Number(url.searchParams.get("after_seq") ?? "-1");
    const messages = [];
    for (let seq = after + 1; seq < total && messages.length < pageSize; seq += 1) {
      messages.push(logAt(seq));
    }
    const last = messages.at(-1);
    return respond({
      run_id: RUN_ID,
      after_seq: after,
      count: messages.length,
      messages,
      more: messages.length >= pageSize,
      next_after_seq: last?.seq ?? after,
    });
  });
}

function wrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

function newClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useRunHistory", () => {
  it("fetches only the first page up front, however long the run is", async () => {
    // 5,000 messages at 100 a page is 50 requests if you walk the whole run
    // before rendering. An MCMC run is bigger than that. One request.
    const fetchMock = pagedServer(5_000, 100);
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(
      () => useRunHistory(RUN_ID, { enabled: true, rowStatus: "SUCCEEDED" }),
      { wrapper: wrapper(newClient()) },
    );

    await waitFor(() => expect(result.current.pagesLoaded).toBe(1));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current.snapshot.logs).toHaveLength(100);
    expect(result.current.fullyLoaded).toBe(false);
  });

  it("pages by seq on demand and stops when the server runs out", async () => {
    const fetchMock = pagedServer(250, 100);
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(
      () => useRunHistory(RUN_ID, { enabled: true, rowStatus: "SUCCEEDED" }),
      { wrapper: wrapper(newClient()) },
    );
    await waitFor(() => expect(result.current.pagesLoaded).toBe(1));

    // Each request carries the previous page's last seq as an exclusive lower
    // bound — the cursor is a seq, not an offset.
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.pagesLoaded).toBe(2));
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain("after_seq=99");

    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.pagesLoaded).toBe(3));
    expect(String(fetchMock.mock.calls[2]?.[0])).toContain("after_seq=199");

    // 250 of 250: the third page was short, so the server said there is no
    // more and the control turns itself off.
    await waitFor(() => expect(result.current.fullyLoaded).toBe(true));
    expect(result.current.snapshot.logs).toHaveLength(250);

    // And a further nudge is a no-op rather than another request.
    act(() => result.current.loadMore());
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("terminates on a server that says 'more' without advancing the cursor", async () => {
    // Empty page, `more: true`, `next_after_seq` echoing `after_seq` back.
    // Without the cursor-movement guard this is an infinite request loop.
    const fetchMock = vi.fn(() =>
      respond({
        run_id: RUN_ID,
        after_seq: -1,
        count: 0,
        messages: [],
        more: true,
        next_after_seq: -1,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(
      () => useRunHistory(RUN_ID, { enabled: true, rowStatus: "SUCCEEDED" }),
      { wrapper: wrapper(newClient()) },
    );

    await waitFor(() => expect(result.current.fullyLoaded).toBe(true));
    act(() => result.current.loadMore());
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("costs no request on a second view of the same finished run", async () => {
    // A terminal run is immutable, so a re-mount — navigating away and back —
    // must re-read the cache and touch the network zero times. This is the
    // whole reason the caching policy is "check the status", not a TTL.
    const fetchMock = pagedServer(150, 100);
    vi.stubGlobal("fetch", fetchMock);
    const client = newClient();

    const first = renderHook(
      () => useRunHistory(RUN_ID, { enabled: true, rowStatus: "SUCCEEDED" }),
      { wrapper: wrapper(client) },
    );
    await waitFor(() => expect(first.result.current.pagesLoaded).toBe(1));
    act(() => first.result.current.loadMore());
    await waitFor(() => expect(first.result.current.pagesLoaded).toBe(2));
    expect(fetchMock).toHaveBeenCalledTimes(2);

    first.unmount();

    const second = renderHook(
      () => useRunHistory(RUN_ID, { enabled: true, rowStatus: "SUCCEEDED" }),
      { wrapper: wrapper(client) },
    );
    // Both pages are still there, and nothing was asked for again.
    await waitFor(() => expect(second.result.current.pagesLoaded).toBe(2));
    expect(second.result.current.snapshot.logs).toHaveLength(150);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("fetches nothing until the run is known to exist", async () => {
    const fetchMock = pagedServer(10, 100);
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(
      () => useRunHistory(RUN_ID, { enabled: false, rowStatus: null }),
      { wrapper: wrapper(newClient()) },
    );

    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.snapshot.logs).toHaveLength(0);
    // Empty AND not hydrated: "not read yet", which a model view must be able
    // to tell apart from "this run genuinely emitted nothing".
    expect(result.current.snapshot.hydrated).toBe(false);
  });
});
