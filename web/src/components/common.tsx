import type { ReactNode } from "react";

export function PositionBadge({ position }: { position: string }) {
  return <span className={`pos pos-${position}`}>{position}</span>;
}

export function Panel({
  title,
  action,
  children,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="panel">
      <h3 className="panel-title">
        <span>{title}</span>
        {action}
      </h3>
      {children}
    </section>
  );
}

export function Banner({
  kind = "info",
  title,
  children,
}: {
  kind?: "info" | "warn" | "error";
  title?: string;
  children: ReactNode;
}) {
  return (
    <div className={`banner banner-${kind}`} role={kind === "error" ? "alert" : undefined}>
      {title ? <strong>{title}</strong> : null}
      {children}
    </div>
  );
}

export function Spinner() {
  return <span className="spinner" aria-label="Loading" role="status" />;
}

/** Signed number with colour, e.g. `+38.4`. */
export function Signed({ value, digits = 1 }: { value: number | null; digits?: number }) {
  if (value === null || value === undefined) return <span className="faint">–</span>;
  const cls = value > 0 ? "positive" : value < 0 ? "negative" : "";
  return (
    <span className={cls}>
      {value > 0 ? "+" : ""}
      {value.toFixed(digits)}
    </span>
  );
}

/** Availability as a percentage, coloured by urgency. */
export function Availability({ value }: { value: number | null }) {
  if (value === null || value === undefined) return <span className="faint">–</span>;
  const pct = Math.round(value * 100);
  const cls = value < 0.25 ? "avail-low" : value < 0.6 ? "avail-mid" : "avail-high";
  return <span className={cls}>{pct}%</span>;
}

export function num(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "–";
  return value.toFixed(digits);
}
