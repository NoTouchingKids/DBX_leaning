import type { ReactNode } from "react";

/** The one panel shape the whole app uses. Header is optional; a card with no
 *  header is still a card, which is why the divider is conditional. */
export function Card({
  title,
  hint,
  actions,
  bodyClassName = "p-4",
  className = "",
  children,
}: {
  title?: ReactNode;
  hint?: ReactNode;
  actions?: ReactNode;
  bodyClassName?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section
      className={`overflow-hidden rounded-[10px] border border-edge bg-raised ${className}`}
    >
      {title !== undefined && (
        <header className="flex items-baseline justify-between gap-4 border-b border-dashed border-edge px-4 py-3">
          <h2 className="text-[0.86rem] font-bold">{title}</h2>
          {hint !== undefined && <span className="text-[0.68rem] text-faint">{hint}</span>}
          {actions}
        </header>
      )}
      <div className={bodyClassName}>{children}</div>
    </section>
  );
}
