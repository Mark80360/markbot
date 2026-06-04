import { useState } from "react";
import { Plus, Trash2, Play, Power } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { StatusBadge } from "@/components/StatusBadge";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";

export default function McpPage() {
  const { data, loading, refetch } = useApi(() => api.getMcpServers(), []);
  const [showAdd, setShowAdd] = useState(false);
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");

  const handleAdd = async () => {
    if (!name || !command) return;
    await api.addMcpServer({ name, command, args: [], env: {} });
    setName(""); setCommand(""); setShowAdd(false); refetch();
  };

  const handleRemove = async (n: string) => { await api.removeMcpServer(n); refetch() };
  const handleToggle = async (n: string, enabled: boolean) => { await api.toggleMcpServer(n, !enabled); refetch() };
  const handleTest = async (n: string) => { await api.testMcpServer(n) };

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;
  const servers = data?.servers || [];

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader title="MCP Servers" description="Model Context Protocol 服务器管理"
        actions={<button onClick={() => setShowAdd(!showAdd)}
          className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg bg-accent-teal text-white hover:bg-accent-teal-hover"><Plus size={14} /> 添加</button>}
      />

      {showAdd && (
        <div className="mb-4 p-4 rounded-lg border border-border space-y-2 max-w-lg">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="名称"
            className="w-full px-3 py-1.5 text-sm rounded border border-border bg-background-secondary text-text-primary" />
          <input value={command} onChange={(e) => setCommand(e.target.value)} placeholder="启动命令"
            className="w-full px-3 py-1.5 text-sm rounded border border-border bg-background-secondary text-text-primary" />
          <button onClick={handleAdd}
            className="px-3 py-1.5 text-sm rounded bg-accent-teal text-white">保存</button>
        </div>
      )}

      <div className="space-y-1 max-w-2xl">
        {servers.map((s: any) => (
          <div key={s.name} className="flex items-center gap-3 px-4 py-3 rounded-lg border border-border">
            <div className="flex-1 min-w-0">
              <div className="text-sm font-medium">{s.name}</div>
              <div className="text-xs text-text-tertiary mt-0.5 font-mono">{s.command} {s.args?.join(" ")}</div>
            </div>
            <StatusBadge status={s.enabled ? "online" : "disabled"} />
            <button onClick={() => handleTest(s.name)}
              className="p-1.5 rounded hover:bg-background-hover text-text-tertiary"><Play size={12} /></button>
            <button onClick={() => handleToggle(s.name, s.enabled)}
              className="p-1.5 rounded hover:bg-background-hover text-text-tertiary"><Power size={12} /></button>
            <button onClick={() => handleRemove(s.name)}
              className="p-1.5 rounded hover:bg-background-hover text-text-muted hover:text-destructive"><Trash2 size={12} /></button>
          </div>
        ))}
      </div>
    </main>
  );
}
