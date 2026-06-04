import { cn } from "@/lib/utils";

interface StatusBadgeProps {
  status: "online" | "offline" | "error" | "disabled" | "running";
  label?: string;
}

const statusColors: Record<string, string> = {
  online: "bg-success",
  offline: "bg-text-muted",
  error: "bg-destructive",
  disabled: "bg-text-muted",
  running: "bg-warning",
};

export function StatusBadge({ status, label }: StatusBadgeProps) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-text-tertiary">
      <span className={cn("w-1.5 h-1.5 rounded-full", statusColors[status] || "bg-text-muted")} />
      {label || status}
    </span>
  );
}
