/**
 * The overview.
 *
 * Cross-references two lists that can legitimately disagree: `MODEL_SPECS`
 * (what this client knows how to build a form for, hand-derived from
 * `job/models/<name>/model.py`) and `GET /api/models` (what has a Databricks job
 * configured behind it). A model in the first and not the second exists but
 * cannot be run, and that difference is rendered rather than hidden — hiding
 * it turns a configuration gap into a mysterious 404 at trigger time.
 */

import { Link } from "react-router";

import { PageHead } from "@/components/layout/PageHead";
import { Card } from "@/components/ui/Card";
import { useHealthz, useModels, useWhoami } from "@/hooks/useApi";
import { MODEL_SPECS } from "@/lib/models";

export function HomePage() {
  const models = useModels();
  const health = useHealthz();
  const whoami = useWhoami();

  const triggerable = new Set(models.data?.models.map((m) => m.name) ?? []);
  const degraded = Object.entries(health.data?.degraded ?? {});

  return (
    <>
      <PageHead eyebrow="Reference" title="Modelling platform">
        Trigger a model as a Databricks Job and watch it stream progress back.
        The job is autonomous; this app is an optional observer of it — closing
        the tab does not stop a run, and a run that started while the app was
        down is still fully recorded.
      </PageHead>

      <div className="mb-5 grid gap-4 sm:grid-cols-2">
        <Card title="This app">
          <dl className="text-[0.8rem]">
            <div className="flex justify-between border-b border-dashed border-line py-1.5">
              <dt className="text-dim">Signed in as</dt>
              <dd className="font-mono">{whoami.data?.email ?? "—"}</dd>
            </div>
            <div className="flex justify-between border-b border-dashed border-line py-1.5">
              <dt className="text-dim">Health</dt>
              <dd className="font-mono">{health.data?.status ?? "…"}</dd>
            </div>
            <div className="flex justify-between border-b border-dashed border-line py-1.5">
              <dt className="text-dim">Live job sockets</dt>
              <dd className="font-mono">{health.data?.live_jobs ?? "—"}</dd>
            </div>
            <div className="flex justify-between py-1.5">
              <dt className="text-dim">Protocol schema</dt>
              <dd className="font-mono">v{health.data?.protocol_schema_version ?? "—"}</dd>
            </div>
          </dl>
          {degraded.length > 0 && (
            <ul className="mt-3 list-disc pl-4 text-[0.72rem] text-warn">
              {degraded.map(([service, reason]) => (
                <li key={service}>
                  <code>{service}</code>: {reason}
                </li>
              ))}
            </ul>
          )}
          <p className="mt-3 text-[0.7rem] leading-relaxed text-faint">
            Identity here is cosmetic — it comes from the platform proxy and is
            not an authorization boundary. Access is decided by Unity Catalog
            grants, not by this page.
          </p>
        </Card>

        <Card title="Concurrency">
          <p className="text-[0.8rem] leading-relaxed text-dim">
            Free Edition allows <strong>5 concurrent job tasks per account</strong>,
            across every model combined. The sixth trigger is refused with a 429
            whose body names the current count — that is the error you are most
            likely to see, and it is not a bug.
          </p>
          <Link
            to="/runs"
            className="mt-3 inline-block text-[0.78rem] font-semibold text-accent"
          >
            See what is running →
          </Link>
        </Card>
      </div>

      <Card title="Models" hint={`${MODEL_SPECS.length} known to this client`}>
        <ul className="grid gap-2 sm:grid-cols-2">
          {MODEL_SPECS.map((spec) => {
            const runnable = models.data ? triggerable.has(spec.name) : undefined;
            return (
              <li key={spec.name}>
                <Link
                  to={`/models/${spec.name}`}
                  className="flex items-baseline justify-between gap-3 rounded-lg border border-edge bg-paper px-3 py-2 no-underline hover:border-accent"
                >
                  <span>
                    <span className="block text-[0.84rem] font-semibold">{spec.label}</span>
                    <code className="text-[0.68rem] text-faint">{spec.name}</code>
                  </span>
                  <span
                    className={`text-[0.66rem] ${runnable === false ? "text-warn" : "text-faint"}`}
                  >
                    {runnable === undefined
                      ? ""
                      : runnable
                        ? `${spec.fields.length} fields`
                        : "no job configured"}
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      </Card>
    </>
  );
}
