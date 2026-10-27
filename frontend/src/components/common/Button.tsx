import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "secondary" | "ghost" | "danger";

export function Button({
  variant = "primary",
  size = "md",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md" }) {
  const base =
    "inline-flex items-center justify-center gap-2 rounded border font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-45";
  const sizes = size === "sm" ? "h-8 px-3 text-[13px]" : "h-10 px-4 text-[14px]";
  const variants: Record<Variant, string> = {
    primary: "border-ink bg-ink text-white hover:bg-brand-dark",
    secondary: "border-paper-line bg-white text-ink hover:border-ink-muted",
    ghost: "border-transparent text-ink-soft hover:text-ink hover:underline",
    danger: "border-bad bg-bad text-white hover:bg-bad/90",
  };
  return <button className={`${base} ${sizes} ${variants[variant]} ${className}`} {...props} />;
}

export function Spinner({ className = "" }: { className?: string }) {
  return <span aria-hidden className={`inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-r-transparent ${className}`} />;
}
