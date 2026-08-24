import type { ReactNode } from "react";

export type CalloutTone = "info" | "warn" | "bad" | "accent";

const TONES: Record<CalloutTone, string> = {
  info: "bg-info-soft border-info text-info",
  warn: "bg-warn-soft border-warn text-warn",
  bad: "bg-bad-soft border-bad text-bad",
  accent: "bg-accent-soft border-accent text-accent-ink",
};

/**
 * A short, bordered message.
 *
 * `children` is usually server text rendered verbatim — the 429 body naming
 * the concurrency count and ceiling, or the 409 body carrying a real
 * `databricks jobs cancel-run` command. Hence `whitespace-pre-wrap` and
 * `break-words`: that text contains backticks, flags and long ids, and
 * reflowing it into an unreadable smear defeats the point of showing it.
 */
export function Callout({
  tone = "info",
  title,
  actions,
  children,
}: {
  tone?: CalloutTone;
  title?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className={`rounded-lg border px-3 py-2.5 text-[0.78rem] ${TONES[tone]}`}>
      {title !== undefined && <div className="font-bold">{title}</div>}
      {children !== undefined && (
        <div className="mt-0.5 leading-relaxed break-words whitespace-pre-wrap">{children}</div>
      )}
      {actions !== undefined && <div className="mt-2 flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}
