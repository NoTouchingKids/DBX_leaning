/**
 * `/runs/:runId`.
 *
 * Thin by design: the route param is the only thing this layer knows. All the
 * page's logic — seq paging, snapshot assembly, gap and completeness
 * reasoning — lives in `@/components/rundetail`, where it can be tested
 * without a router.
 */

import { useParams } from "react-router";

import { RunDetail } from "@/components/rundetail/RunDetail";
import { NotFound } from "./NotFound";

export function RunDetailPage() {
  const { runId } = useParams<{ runId: string }>();

  if (runId === undefined || runId === "") {
    return (
      <NotFound
        title="No run id in the URL"
        detail="This route needs a run id — open a row from the run history table."
      />
    );
  }

  return <RunDetail runId={runId} />;
}
