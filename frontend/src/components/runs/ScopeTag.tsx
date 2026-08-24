/**
 * The `server` / `client` badge on every filter control.
 *
 * Not decoration. A client-side filter is a pass over the rows that happen to
 * be in the fetched window, and it is correct only while the window is big
 * enough to hold everything relevant — after that it goes wrong silently,
 * showing fewer rows than exist with no indication anything was missed. The
 * badge is what makes that visible to whoever eventually hits it, and the
 * `title` is what tells them what to do about it.
 */

const STYLE = {
  server: "text-good",
  client: "text-accent",
} as const;

const EXPLANATION = {
  server: "applied by the SQL query — narrows the whole table, not just this window",
  client: "applied in the browser over the rows already fetched — widen the window if a match may be older than it",
} as const;

export function ScopeTag({ scope }: { scope: "server" | "client" }) {
  return (
    <span
      title={EXPLANATION[scope]}
      className={`rounded-[3px] border border-current px-1 py-px text-[0.52rem] font-bold tracking-[0.06em] ${STYLE[scope]}`}
    >
      {scope}
    </span>
  );
}
