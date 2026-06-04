import { useState, useEffect } from "react";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { LogViewer } from "@/components/LogViewer";
import { api } from "@/lib/api";

export default function LogsPage() {
  const [files, setFiles] = useState<string[]>([]);
  const [selectedFile, setSelectedFile] = useState("markbot.log");
  const [lines, setLines] = useState(200);
  const [level, setLevel] = useState("");
  const [logs, setLogs] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchLogs = () => {
    setLoading(true);
    api.getLogs({ file: selectedFile, lines, level: level || undefined })
      .then((d) => setLogs(d.logs))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    api.getLogFiles().then((d) => { if (d.files.length) setFiles(d.files) }).catch(() => {});
  }, []);

  useEffect(() => { fetchLogs() }, [selectedFile, lines, level]);

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader title="Logs" description="查看运行日志" />

      <div className="flex gap-2 mb-4 flex-wrap">
        <select
          value={selectedFile}
          onChange={(e) => setSelectedFile(e.target.value)}
          className="px-3 py-1.5 text-sm rounded-lg border border-border bg-background-secondary text-text-primary"
        >
          {files.map((f) => <option key={f} value={f}>{f}</option>)}
        </select>
        <select value={String(lines)} onChange={(e) => setLines(Number(e.target.value))}
          className="px-3 py-1.5 text-sm rounded-lg border border-border bg-background-secondary text-text-primary">
          <option value="100">100 行</option>
          <option value="200">200 行</option>
          <option value="500">500 行</option>
          <option value="1000">1000 行</option>
        </select>
        <select value={level} onChange={(e) => setLevel(e.target.value)}
          className="px-3 py-1.5 text-sm rounded-lg border border-border bg-background-secondary text-text-primary">
          <option value="">全部级别</option>
          <option value="ERROR">ERROR</option>
          <option value="WARNING">WARNING</option>
          <option value="INFO">INFO</option>
          <option value="DEBUG">DEBUG</option>
        </select>
        <button onClick={fetchLogs}
          className="px-3 py-1.5 text-sm rounded-lg border border-border hover:bg-background-hover transition-all">
          刷新
        </button>
      </div>

      {loading ? <Loading /> : <LogViewer lines={logs} />}
    </main>
  );
}
