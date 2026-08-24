/**
 * `/models/:model`.
 *
 * Resolves the route param against `MODEL_SPECS` and hands the whole page to
 * the generic workspace. When a per-model view lands (M4) it replaces the
 * `slots` argument here and nothing else — which is the point of building the
 * generic one first.
 */

import { useParams } from "react-router";

import { RunWorkspace } from "@/components/run/RunWorkspace";
import { MODEL_SPECS } from "@/lib/models";
import { NotFound } from "./NotFound";

export function ModelPage() {
  const { model } = useParams<{ model: string }>();
  const spec = MODEL_SPECS.find((candidate) => candidate.name === model);

  if (!spec) {
    return (
      <NotFound
        title={`No model named ${model ?? "—"}`}
        detail="The models this client knows about come from MODEL_SPECS, which is hand-derived from models/<name>/model.py. A model added to the repo needs an entry there before it gets a page."
      />
    );
  }

  // The per-model plug is looked up here and nowhere else. When
  // `@/components/models/<name>` lands, it is registered in that directory's
  // index and resolved by name — the page below does not change, which is the
  // point of building the generic view first.
  return (
    <RunWorkspace
      key={spec.name}
      spec={spec}
      description={
        <>
          Generic run view — it renders only the fields every model populates,
          so it is correct for this model whether or not anyone has written a
          bespoke page for it yet.
        </>
      }
    />
  );
}
