import type { ReactNode } from "react";

export function PageHead({
  eyebrow,
  title,
  children,
}: {
  eyebrow?: string;
  title: string;
  children?: ReactNode;
}) {
  return (
    <header className="mb-5">
      {eyebrow !== undefined && (
        <div className="mb-1 text-[0.68rem] font-semibold tracking-[0.1em] text-accent uppercase">
          {eyebrow}
        </div>
      )}
      <h1 className="m-0 mb-1.5 text-[1.55rem] tracking-tight text-balance">{title}</h1>
      {children !== undefined && (
        <p className="m-0 max-w-[62ch] text-[0.92rem] leading-relaxed text-dim">{children}</p>
      )}
    </header>
  );
}
