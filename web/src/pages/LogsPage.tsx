import { useState, useEffect, useCallback, useRef } from "react";
import { FileText, RefreshCw } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { Segmented } from "@/components/Segmented";
import { api } from "@/lib/api";

const LEVELS = ["ALL", "ERROR", "WARNING", "INFO", "DEBUG"] as const;
const LINE_COUNTS = ["100", "200", "500", "1000"] as const;

function classifyLine(line: string): "error" | "warning" | "info" | "debug" {
  const upper = line.toUpperCase();
  if (upper.includes("ERROR") || upper.includes("CRITICAL") || upper.includes("FATAL")) return "error";
  if (upper.includes("WARNING") || upper.includes("WARN")) return "warning";
  if (upper.includes("DEBUG")) return "debug";
  return "info";
}

const LINE_COLORS: Record<string, string> = {
  error: "text-destructive",
  warning: "text-warning",
  info: "text-text-primary",
  debug: "text-text-muted",
};

export default function LogsPage() {
  const [files, setFiles] = useState<string[]>([]);
  const [selectedFile, setSelectedFile] = useState("markbot.log");
  const [lines, setLines] = useState("200");
  const [level, setLevel] = useState("ALL");
  const [logs, setLogs] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const fetchLogs = useCallback(() => {
    setLoading(true);
    api.getLogs({
      file: selectedFile,
      lines: Number(lines),
      level: level === "ALL" ? undefined : level,
    })
      .then((d) => {
        setLogs(d.logs || []);
        setTimeout(() => {
          if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
        }, 50);
      })
      .catch(() => setLogs([]))
      .finally(() => setLoading(false));
  }, [selectedFile, lines, level]);

  useEffect(() => {
    api.getLogFiles().then((d) => {
      if (d.files.length) {
        setFiles(d.files);
        if (!d.files.includes(selectedFile)) setSelectedFile(d.files[0]);
      }
    }).catch(() => {});
  }, []);

  useEffect(() => { fetchLogs(); }, [fetchLogs]);

  useEffect(() => {
    if (!autoRefresh) return;
    const interval = setInterval(fetchLogs, 5000);
    return () => clearInterval(interval);
  }, [autoRefresh, fetchLogs]);

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader
        title="Logs"
        description="查看运行日志"
        actions={
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 text-xs text-text-secondary cursor-pointer">
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
                className="w-3.5 h-3.5 rounded border-border accent-accent-teal"
              />
              自动刷新
            </label>
            {autoRefresh && (
              <span className="flex items-center gap-1 text-xs text-success">
                <span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />
                LIVE
              </span>
            )}
            <button
              onClick={fetchLogs}
              disabled={loading}
              className="p-1.5 rounded-lg border border-border hover:bg-background-hover transition-all disabled:opacity-50"
              title="刷新"
            >
              <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
            </button>
          </div>
        }
      />

      <div className="flex items-center gap-4 mb-4 flex-wrap">
        <div className="flex flex-col gap-1">
          <span className="text-xs text-text-muted">文件</span>
          <Segmented
            value={selectedFile}
            onChange={setSelectedFile}
            options={files.map((f) => ({ value: f, label: f.replace(".log", "") }))}
          />
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-xs text-text-muted">级别</span>
          <Segmented
            value={level}
            onChange={setLevel}
            options={LEVELS.map((l) => ({ value: l, label: l }))}
          />
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-xs text-text-muted">行数</span>
          <Segmented
            value={lines}
            onChange={setLines}
            options={LINE_COUNTS.map((n) => ({ value: n, label: n }))}
          />
        </div>
      </div>

      <div className="rounded-lg border border-border bg-background-secondary overflow-hidden">
        <div className="flex items-center gap-2 px-4 py-2 border-b border-border">
          <FileText size={14} className="text-text-muted" />
          <span className="text-xs text-text-secondary">{selectedFile}</span>
          <span className="text-xs text-text-muted ml-auto">{logs.length} 行</span>
        </div>
        <div
          ref={scrollRef}
          className="font-mono text-xs leading-5 overflow-y-auto"
          style={{ height: "calc(100vh - 280px)", minHeight: "400px" }}
        >
          {loading && logs.length === 0 ? (
            <div className="flex items-center justify-center py-12">
              <Loading />
            </div>
          ) : logs.length === 0 ? (
            <p className="text-text-muted text-center py-12">暂无日志</p>
          ) : (
            logs.map((line, i) => {
              const cls = classifyLine(line);
              return (
                <div
                  key={i}
                  className={`${LINE_COLORS[cls]} hover:bg-background-hover px-4 py-0.5 -mx-4`}
                >
                  {line}
                </div>
              );
            })
          )}
        </div>
      </div>
    </main>
  );
}
