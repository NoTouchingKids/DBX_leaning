import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "ghost" | "danger";

/**
 * Three, and no more. `primary` is the one action a screen is for — never two
 * on the same screen, which is how a user stops being able to tell which one
 * matters. `ghost` is everything else. `danger` is destructive only.
 *
 * Primary is the accent filled, not ink filled: a black button is the safe
 * choice that makes a product look unbranded, and the accent already carries
 * enough contrast to be legible white-on-blue.
 */
const VARIANTS: Record<Variant, string> = {
  primary:
    "border-accent bg-accent text-white hover:bg-accent-ink hover:border-accent-ink " +
    "shadow-[var(--shadow-card)]",
  ghost: "border-edge bg-raised text-dim hover:bg-accent-soft hover:border-accent hover:text-accent",
  danger: "border-bad/40 bg-raised text-bad hover:bg-bad-soft hover:border-bad",
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
        `inline-flex cursor-pointer items-center justify-center gap-1.5 rounded-lg border ` +
        `px-3.5 py-2 text-[0.8125rem] leading-none font-semibold ` +
        `transition-colors duration-150 motion-reduce:transition-none ` +
        `disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:bg-raised ` +
        `${VARIANTS[variant]} ${className}`
      }
    />
  );
}
