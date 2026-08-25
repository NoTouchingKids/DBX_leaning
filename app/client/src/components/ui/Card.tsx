import type { ReactNode } from "react";

/**
 * The one panel shape the whole app uses. Header is optional; a card with no
 * header is still a card, which is why the divider is conditional.
 *
 * A HAIRLINE plus a barely-there shadow, not a heavy border: the border says
 * where the card ends and the shadow says it sits above the page, and doing
 * both jobs with a strong border is what makes an interface look drawn rather
 * than designed. `border-line`, not `border-edge` — `edge` is for controls a
 * user can act on, and a panel is not one.
 */
export function Card({
  title,
  hint,
  actions,
  collapsible = false,
  defaultOpen = false,
  bodyClassName = "p-5",
  className = "",
  children,
}: {
  title?: ReactNode;
  hint?: ReactNode;
  actions?: ReactNode;
  /**
   * Render the body behind the header, which becomes the toggle.
   *
   * For reference material — the notes explaining what an endpoint cannot
   * tell you. That text is worth keeping and worth reading once; leaving five
   * paragraphs of it permanently open under the thing it describes is what
   * buries the thing it describes.
   *
   * Native `<details>`, so it is keyboard reachable and findable by the
   * browser's own in-page search even while closed.
   */
  collapsible?: boolean;
  defaultOpen?: boolean;
  bodyClassName?: string;
  className?: string;
  children: ReactNode;
}) {
  const shell =
    `overflow-hidden rounded-xl border border-line bg-raised ` +
    `shadow-[var(--shadow-card)] ${className}`;

  const heading = (
    <>
      <h2 className="text-[0.875rem] font-semibold text-ink">{title}</h2>
      {hint !== undefined && <span className="text-[0.75rem] text-faint tabular-nums">{hint}</span>}
      {actions}
    </>
  );

  if (collapsible && title !== undefined) {
    return (
      <details className={`group ${shell}`} open={defaultOpen}>
        <summary
          className={
            `flex cursor-pointer list-none items-baseline justify-between gap-4 px-5 py-3.5 ` +
            `hover:bg-paper [&::-webkit-details-marker]:hidden group-open:border-b ` +
            `group-open:border-line`
          }
        >
          {heading}
          <span
            aria-hidden
            className={
              `ml-auto self-center text-[0.65rem] text-faint transition-transform ` +
              `duration-150 group-open:rotate-90 motion-reduce:transition-none`
            }
          >
            ▶
          </span>
        </summary>
        <div className={bodyClassName}>{children}</div>
      </details>
    );
  }

  return (
    <section className={shell}>
      {title !== undefined && (
        <header className="flex items-baseline justify-between gap-4 border-b border-line px-5 py-3.5">
          {heading}
        </header>
      )}
      <div className={bodyClassName}>{children}</div>
    </section>
  );
}
