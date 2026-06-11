import { cn } from "@/lib/utils";

interface SegmentedOption {
  value: string;
  label: string;
}

interface SegmentedProps {
  value: string;
  onChange: (value: string) => void;
  options: SegmentedOption[];
  className?: string;
}

export function Segmented({ value, onChange, options, className }: SegmentedProps) {
  return (
    <div
      className={cn(
        "inline-flex items-center gap-0 rounded-lg border border-border bg-background-secondary p-0.5",
        className,
      )}
    >
      {options.map((opt) => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          className={cn(
            "px-3 py-1 text-xs rounded-md transition-all whitespace-nowrap",
            value === opt.value
              ? "bg-accent-teal text-white shadow-sm"
              : "text-text-secondary hover:text-text-primary hover:bg-background-hover",
          )}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}
