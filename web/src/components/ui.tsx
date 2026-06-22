import type { ReactNode } from "react";
import { Inbox } from "lucide-react";

/** A labeled card displaying a single value. */
export function Card({
  label, value, sub, mono, icon, actions,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  mono?: boolean;
  icon?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="p-3 rounded-lg border border-border bg-background-secondary">
      <div className="flex items-center justify-between mb-1.5">
        <div className="flex items-center gap-1.5 text-xs text-text-tertiary">
          {icon}
          <span>{label}</span>
        </div>
        {actions}
      </div>
      <div className={`text-sm font-medium text-text-primary break-all ${mono ? "font-mono" : ""}`}>
        {value}
      </div>
      {sub && <div className="text-xs text-text-muted mt-1">{sub}</div>}
    </div>
  );
}

/** A section with a title and icon, used to group related content. */
export function Section({
  title, icon, description, actions, children, defaultCollapsed,
}: {
  title: string;
  icon?: ReactNode;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
  defaultCollapsed?: boolean;
}) {
  return (
    <section>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          {icon && <span className="text-accent-teal">{icon}</span>}
          <h2 className="text-sm font-semibold text-text-primary">{title}</h2>
          {description && <span className="text-xs text-text-muted ml-1">{description}</span>}
        </div>
        {actions}
      </div>
      {children}
    </section>
  );
}

/** Empty state placeholder with optional icon and action. */
export function EmptyState({
  icon, title, description, action,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      {icon && <div className="mb-3 text-text-muted opacity-50">{icon}</div>}
      <p className="text-sm text-text-secondary">{title}</p>
      {description && <p className="text-xs text-text-muted mt-1 max-w-sm">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

/** Inline feedback banner that auto-dismisses. Accepts a string or feedback object from useFeedback. */
export function Feedback({
  message,
  type = "info",
}: {
  message: string | { message: string; type?: "info" | "success" | "error" } | null;
  type?: "info" | "success" | "error";
}) {
  if (!message) return null;
  const msg = typeof message === "string" ? { message, type } : message;
  const styles = {
    info: "border-accent-teal/30 bg-accent-teal-dim text-accent-teal",
    success: "border-success/30 bg-success/10 text-success",
    error: "border-destructive/30 bg-destructive/10 text-destructive",
  };
  return (
    <div className={`mb-4 px-4 py-2 rounded-lg border text-sm ${styles[msg.type || type]}`}>
      {msg.message}
    </div>
  );
}

/** Key-value row for displaying settings. */
export function KVRow({ label, value, mono }: { label: string; value: ReactNode; mono?: boolean }) {
  return (
    <div className="flex justify-between items-center py-1.5 text-xs">
      <span className="text-text-muted">{label}</span>
      <span className={`text-text-primary ${mono ? "font-mono" : ""}`}>{value}</span>
    </div>
  );
}

/** Standard primary button. */
export function Button({
  children, onClick, disabled, variant = "primary", size = "md", className = "",
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  className?: string;
}) {
  const variants = {
    primary: "bg-accent-teal text-white hover:bg-accent-teal-hover",
    secondary: "border border-border bg-background-secondary text-text-primary hover:bg-background-hover",
    ghost: "text-text-tertiary hover:bg-background-hover hover:text-text-primary",
    danger: "text-destructive hover:bg-destructive/10",
  };
  const sizes = {
    sm: "px-2 py-1 text-xs",
    md: "px-3 py-1.5 text-sm",
  };
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`rounded-lg transition-all disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1.5 ${variants[variant]} ${sizes[size]} ${className}`}
    >
      {children}
    </button>
  );
}
