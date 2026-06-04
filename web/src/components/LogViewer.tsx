import { useRef, useEffect } from "react";

interface LogViewerProps {
  lines: string[];
  height?: string;
}

export function LogViewer({ lines, height = "500px" }: LogViewerProps) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [lines.length]);

  return (
    <div
      className="w-full font-mono text-xs rounded-lg border border-border bg-background-secondary p-4 overflow-y-auto"
      style={{ height }}
    >
      {lines.length === 0 ? (
        <span className="text-text-muted">暂无日志</span>
      ) : (
        lines.map((line, i) => (
          <div key={i} className="whitespace-pre-wrap break-all hover:bg-background-hover px-1 rounded">
            {line}
          </div>
        ))
      )}
      <div ref={endRef} />
    </div>
  );
}
