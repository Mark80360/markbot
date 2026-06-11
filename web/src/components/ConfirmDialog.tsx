import { AlertTriangle, X } from "lucide-react";
import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  loading?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = "确认",
  cancelLabel = "取消",
  destructive = false,
  loading = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [open, onCancel]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[9998] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/50" onClick={onCancel} />
      <div
        ref={dialogRef}
        className="relative w-full max-w-sm mx-4 rounded-xl border border-border bg-background-base shadow-2xl dialog-enter"
      >
        <div className="flex items-start gap-3 p-5">
          <div className={cn(
            "w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0",
            destructive ? "bg-destructive/10" : "bg-accent-teal-dim",
          )}>
            <AlertTriangle size={16} className={destructive ? "text-destructive" : "text-accent-teal"} />
          </div>
          <div className="flex-1 min-w-0">
            <h3 className="text-sm font-semibold text-text-primary">{title}</h3>
            {description && (
              <p className="text-xs text-text-secondary mt-1">{description}</p>
            )}
          </div>
          <button onClick={onCancel} className="text-text-muted hover:text-text-primary p-1">
            <X size={14} />
          </button>
        </div>
        <div className="flex justify-end gap-2 px-5 pb-4">
          <button
            onClick={onCancel}
            disabled={loading}
            className="px-3 py-1.5 text-sm rounded-lg border border-border hover:bg-background-hover transition-all disabled:opacity-50"
          >
            {cancelLabel}
          </button>
          <button
            onClick={onConfirm}
            disabled={loading}
            className={cn(
              "px-3 py-1.5 text-sm rounded-lg transition-all disabled:opacity-50",
              destructive
                ? "bg-destructive text-destructive-foreground hover:opacity-90"
                : "bg-accent-teal text-white hover:bg-accent-teal-hover",
            )}
          >
            {loading ? "处理中..." : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
