import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "ghost" | "danger";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-ink text-paper border-ink hover:opacity-90",
  ghost: "bg-transparent text-dim border-edge hover:border-accent hover:text-accent",
  danger: "bg-transparent text-bad border-bad/50 hover:bg-bad-soft",
};

export function Button({
  variant = "ghost",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      {...props}
      className={
        `cursor-pointer rounded-md border px-3 py-1.5 text-[0.78rem] font-semibold ` +
        `disabled:cursor-not-allowed disabled:opacity-45 ${VARIANTS[variant]} ${className}`
      }
    />
  );
}
