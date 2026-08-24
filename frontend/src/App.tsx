/**
 * Router and server-state provider.
 *
 * React Query owns everything fetched over HTTP; `useRunStream` owns live
 * telemetry. There is no third store, and deliberately so — the two have
 * different invalidation rules (one is request/response, the other is an
 * append-only stream with its own IndexedDB cache), and a global store that
 * tried to hold both would need to reimplement one of them.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router";

import { AppShell } from "@/components/layout/AppShell";
import StreamProbe from "@/dev/StreamProbe";
import { HomePage } from "@/pages/HomePage";
import { ModelPage } from "@/pages/ModelPage";
import { NotFound } from "@/pages/NotFound";
import { RunsPage } from "@/pages/RunsPage";

const client = new QueryClient({
  defaultOptions: {
    queries: {
      // Nothing polls. The warehouse behind these reads is billed by uptime,
      // so a background interval on an open tab costs money all day for
      // information the SSE stream already delivers. Refetch on focus is the
      // compromise: it is bounded by human attention.
      refetchOnWindowFocus: true,
      refetchInterval: false,
      retry: 1,
    },
  },
});

export default function App() {
  return (
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<HomePage />} />
            <Route path="runs" element={<RunsPage />} />
            <Route
              path="runs/:runId"
              element={
                <NotFound
                  title="Past-run detail is not built yet"
                  detail="A finished run is watchable from its model page — the same view, hydrated from Delta instead of a live stream. Open it from the run history table."
                />
              }
            />
            <Route path="models/:model" element={<ModelPage />} />
            <Route path="dev/probe" element={<StreamProbe />} />
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
