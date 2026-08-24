import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import { useTerminalHistory } from "./useApi";

function respond(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

function page(messages: unknown[], more = false, next = -1) {
  return respond({
    run_id: "run-terminal",
    after_seq: -1,
    count: messages.length,
    messages,
    more,
    next_after_seq: next,
  });
}

function wrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useTerminalHistory", () => {
  it("fetches a finished run's history once and never again", async () => {
    // A terminal run is immutable: its full message history can never change,
    // so a second view of it must cost no request at all. This is the whole
    // reason the caching policy is "check the status" rather than a TTL.
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        page([
          {
            type: "status",
            run_id: "run-terminal",
            seq: 9,
            ts: 1,
            status: "SUCCEEDED",
            detail: null,
          },
        ]),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const first = renderHook(() => useTerminalHistory("run-terminal", true), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(first.result.current.isSuccess).toBe(true));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Navigate away and back.
    first.unmount();
    const second = renderHook(() => useTerminalHistory("run-terminal", true), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(second.result.current.isSuccess).toBe(true));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not fetch at all when the run is not terminal", async () => {
    // An active run has a live tail. Backfilling it on sight is the
    // warehouse-uptime mistake this rewrite exists to avoid.
    const fetchMock = vi.fn(() => Promise.resolve(page([])));
    vi.stubGlobal("fetch", fetchMock);

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useTerminalHistory("run-live", false), {
      wrapper: wrapper(client),
    });

    await waitFor(() => expect(result.current.fetchStatus).toBe("idle"));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("pages by seq until the server stops saying there is more", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        page([{ type: "log", run_id: "r", seq: 1, ts: 1, message: "a", level: "INFO", source: "model", phase: "run", client_visible: true }], true, 1),
      )
      .mockResolvedValueOnce(
        page([{ type: "log", run_id: "r", seq: 2, ts: 2, message: "b", level: "INFO", source: "model", phase: "run", client_visible: true }], false, 2),
      );
    vi.stubGlobal("fetch", fetchMock);

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useTerminalHistory("run-paged", true), {
      wrapper: wrapper(client),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current.data?.messages).toHaveLength(2);
  });
});
