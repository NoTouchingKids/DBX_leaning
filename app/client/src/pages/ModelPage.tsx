/**
 * `/models/:model`.
 *
 * Resolves the route param against `MODEL_SPECS`, looks up the model's view
 * in the registry, and hands both to the generic workspace. A model with no
 * view registered still gets a correct page — that is what building the
 * generic one first bought, and it is why `viewFor` returns undefined rather
 * than throwing.
 */

import { useParams } from "react-router";

import { viewFor } from "@/components/models/registry";
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
        detail="The models this client knows about come from MODEL_SPECS, which is hand-derived from job/models/<name>/model.py. A model added to the repo needs an entry there before it gets a page."
      />
    );
  }

  // The per-model plug is looked up here and nowhere else.
  const view = viewFor(spec.name);

  return (
    <RunWorkspace
      key={spec.name}
      spec={spec}
      view={view}
      description={
        view ? undefined : (
          <>
            Generic run view — it renders only the fields every model
            populates, so it is correct for this model whether or not anyone
            has written a bespoke page for it yet.
          </>
        )
      }
    />
  );
}
