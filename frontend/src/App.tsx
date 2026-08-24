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
import { Suspense, lazy } from "react";
import { BrowserRouter, Route, Routes } from "react-router";

import { MODEL_VIEWS } from "@/components/models/registry";

/**
 * Lazy, unlike every other route here. The gallery pulls in `src/dev/fixtures`
 * — nine models' worth of synthetic run data, which exists to be looked at
 * during development and has no business in the bundle a user downloads.
 * Static-importing it put it in the main chunk.
 */
const ModelGallery = lazy(() =>
  import("@/dev/ModelGallery").then((m) => ({ default: m.ModelGallery })),
);

import { AppShell } from "@/components/layout/AppShell";
import StreamProbe from "@/dev/StreamProbe";
import { HomePage } from "@/pages/HomePage";
import { ModelPage } from "@/pages/ModelPage";
import { NotFound } from "@/pages/NotFound";
import { RunDetailPage } from "@/pages/RunDetailPage";
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
            <Route path="runs/:runId" element={<RunDetailPage />} />
            <Route path="models/:model" element={<ModelPage />} />
            <Route path="dev/probe" element={<StreamProbe />} />
            {/* The review surface for the nine signature animations. There is
                no workspace to see them in, so this is where they get looked
                at — every view, every lifecycle state, against fixtures that
                include the states a real run rarely sits in long enough to
                catch. */}
            <Route
              path="dev/gallery"
              element={
                <Suspense fallback={<p className="p-8 text-sm text-dim">Loading gallery…</p>}>
                  <ModelGallery views={MODEL_VIEWS} />
                </Suspense>
              }
            />
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
