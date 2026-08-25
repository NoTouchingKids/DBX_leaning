import type { ReactNode } from "react";

/**
 * A label/value list — the shape half the panels in this app need.
 *
 * It exists because the same four-line `<div className="flex justify-between
 * border-b ...">` was written out by hand everywhere one was wanted, which is
 * how a border style ends up differing between two cards on the same screen.
 *
 * Values are `tabular-nums` and right-aligned, so a column of numbers lines up
 * on the decimal instead of shifting as digits change under a live run — the
 * single most useful thing this component does.
 */
export function DataList({ children }: { children: ReactNode }) {
  return <dl className="text-[0.8125rem]">{children}</dl>;
}

export function DataRow({
  label,
  children,
  mono = true,
}: {
  label: ReactNode;
  children: ReactNode;
  /** Off for prose values; on for ids, counts and versions. */
  mono?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-6 border-b border-line py-2 last:border-b-0">
      <dt className="text-dim">{label}</dt>
      <dd className={`text-right text-ink ${mono ? "font-mono tabular-nums" : ""}`}>{children}</dd>
    </div>
  );
}
