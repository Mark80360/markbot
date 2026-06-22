import { useState } from "react";
import { Plus, Trash2, Play, Power, Edit3, X, Terminal, Globe, ChevronDown, ChevronRight, Cable, CheckCircle2, XCircle } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { StatusBadge } from "@/components/StatusBadge";
import { EmptyState, Feedback, Button } from "@/components/ui";
import { useApi, useFeedback } from "@/hooks/useApi";
import { api } from "@/lib/api";

export default function McpPage() {
  const { data, loading, refetch } = useApi(() => api.getMcpServers(), []);
  const [showAdd, setShowAdd] = useState(false);
  const [editingServer, setEditingServer] = useState<any | null>(null);
  const [expandedServer, setExpandedServer] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, any>>({});
  const [testing, setTesting] = useState<string | null>(null);
  const { feedback, show } = useFeedback();

  const emptyForm = {
    name: "", type: "", command: "", argsText: "", envText: "",
    url: "", headersText: "", tool_timeout: 30, enabled_tools_text: "*",
  };
  const [formData, setFormData] = useState(emptyForm);

  const parseArgs = (args: string[]) => args.join("\n");
  const parseEnv = (env: Record<string, string>) => Object.entries(env).map(([k, v]) => `${k}=${v}`).join("\n");
  const parseHeaders = (headers: Record<string, string>) => Object.entries(headers).map(([k, v]) => `${k}: ${v}`).join("\n");

  const buildArgs = (text: string): string[] => text.split("\n").map(s => s.trim()).filter(Boolean);
  const buildEnv = (text: string): Record<string, string> => {
    const env: Record<string, string> = {};
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const idx = trimmed.indexOf("=");
      if (idx > 0) env[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim();
    }
    return env;
  };
  const buildHeaders = (text: string): Record<string, string> => {
    const headers: Record<string, string> = {};
    for (const line of text.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const idx = trimmed.indexOf(":");
      if (idx > 0) headers[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim();
    }
    return headers;
  };

  const handleAdd = async () => {
    if (!formData.name) return;
    try {
      await api.addMcpServer({
        name: formData.name,
        type: formData.type || undefined,
        command: formData.command,
        args: buildArgs(formData.argsText),
        env: buildEnv(formData.envText),
        url: formData.url,
        headers: buildHeaders(formData.headersText),
        tool_timeout: formData.tool_timeout,
        enabled_tools: formData.enabled_tools_text.split(",").map(s => s.trim()).filter(Boolean),
      });
      setFormData(emptyForm);
      setShowAdd(false);
      show(`已添加 MCP 服务器 "${formData.name}"`);
      refetch();
    } catch (e: any) {
      show(`添加失败: ${e.message}`, "error");
    }
  };

  const handleEdit = (s: any) => {
    setEditingServer(s);
    setFormData({
      name: s.name,
      type: s.type || "",
      command: s.command || "",
      argsText: parseArgs(s.args || []),
      envText: parseEnv(s.env || {}),
      url: s.url || "",
      headersText: parseHeaders(s.headers || {}),
      tool_timeout: s.tool_timeout || 30,
      enabled_tools_text: (s.enabled_tools || ["*"]).join(", "),
    });
  };

  const handleUpdate = async () => {
    if (!editingServer) return;
    try {
      await api.updateMcpServer(editingServer.name, {
        type: formData.type || undefined,
        command: formData.command,
        args: buildArgs(formData.argsText),
        env: buildEnv(formData.envText),
        url: formData.url,
        headers: buildHeaders(formData.headersText),
        tool_timeout: formData.tool_timeout,
        enabled_tools: formData.enabled_tools_text.split(",").map(s => s.trim()).filter(Boolean),
      });
      show(`已更新 "${editingServer.name}"`);
      setEditingServer(null);
      setFormData(emptyForm);
      refetch();
    } catch (e: any) {
      show(`更新失败: ${e.message}`, "error");
    }
  };

  const handleRemove = async (n: string) => {
    if (!confirm(`确定删除 MCP 服务器 "${n}"？`)) return;
    try {
      await api.removeMcpServer(n);
      show(`已删除 "${n}"`);
      refetch();
    } catch (e: any) {
      show(`删除失败: ${e.message}`, "error");
    }
  };

  const handleToggle = async (n: string, enabled: boolean) => {
    try {
      await api.toggleMcpServer(n, !enabled);
      show(`"${n}" 已${enabled ? "禁用" : "启用"}`);
      refetch();
    } catch (e: any) {
      show(`操作失败: ${e.message}`, "error");
    }
  };

  const handleTest = async (n: string) => {
    setTesting(n);
    setTestResult((prev) => ({ ...prev, [n]: null }));
    try {
      const result = await api.testMcpServer(n);
      setTestResult((prev) => ({ ...prev, [n]: result }));
      if (result.ok) show(`"${n}" 连接成功`);
      else show(`"${n}" 连接失败: ${result.error}`, "error");
    } catch (e: any) {
      setTestResult((prev) => ({ ...prev, [n]: { ok: false, error: e.message } }));
      show(`测试失败: ${e.message}`, "error");
    }
    setTesting(null);
  };

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;
  const servers = data?.servers || [];
  const enabledCount = servers.filter((s: any) => s.enabled).length;

  // Determine which form sections to show based on transport type
  const showStdioFields = formData.type === "stdio" || (!formData.type && !formData.url);
  const showHttpFields = formData.type === "sse" || formData.type === "streamableHttp" || (!formData.type && !!formData.url);

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader
        title="MCP Servers"
        description={`Model Context Protocol 服务器 · ${enabledCount}/${servers.length} 启用`}
        actions={
          <Button size="sm" onClick={() => { setShowAdd(!showAdd); setEditingServer(null); setFormData(emptyForm); }}>
            <Plus size={14} /> 添加
          </Button>
        }
      />

      <Feedback message={feedback} />

      <div className="max-w-3xl">
        {(showAdd || editingServer) && (
          <div className="mb-4 p-4 rounded-lg border border-border space-y-3 bg-background-secondary">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium">{editingServer ? "编辑服务器" : "新建服务器"}</span>
              <button onClick={() => { setShowAdd(false); setEditingServer(null); setFormData(emptyForm); }}
                className="p-1 rounded hover:bg-background-hover text-text-tertiary"><X size={14} /></button>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-xs text-text-tertiary block mb-1.5">名称</label>
                <input value={formData.name} onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  placeholder="my-server" disabled={!!editingServer}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary disabled:opacity-50 focus:outline-none focus:border-accent-teal transition-colors" />
              </div>
              <div>
                <label className="text-xs text-text-tertiary block mb-1.5">传输类型</label>
                <select value={formData.type} onChange={(e) => setFormData({ ...formData, type: e.target.value })}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary focus:outline-none focus:border-accent-teal transition-colors">
                  <option value="">自动检测</option>
                  <option value="stdio">stdio</option>
                  <option value="sse">SSE</option>
                  <option value="streamableHttp">Streamable HTTP</option>
                </select>
              </div>
            </div>

            {/* Stdio fields - show when type is stdio, or auto with no URL */}
            {showStdioFields && (
              <div className="space-y-3 p-3 rounded-lg border border-border/50 bg-background/50">
                <div className="text-xs text-text-muted flex items-center gap-1">
                  <Terminal size={10} /> Stdio 配置
                </div>
                <div>
                  <label className="text-xs text-text-tertiary block mb-1.5">启动命令</label>
                  <input value={formData.command} onChange={(e) => setFormData({ ...formData, command: e.target.value })}
                    placeholder="如 npx"
                    className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary focus:outline-none focus:border-accent-teal transition-colors" />
                </div>
                <div>
                  <label className="text-xs text-text-tertiary block mb-1.5">参数 (每行一个)</label>
                  <textarea value={formData.argsText} onChange={(e) => setFormData({ ...formData, argsText: e.target.value })}
                    rows={3}
                    className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary font-mono focus:outline-none focus:border-accent-teal transition-colors" />
                </div>
                <div>
                  <label className="text-xs text-text-tertiary block mb-1.5">环境变量 (每行 KEY=VALUE)</label>
                  <textarea value={formData.envText} onChange={(e) => setFormData({ ...formData, envText: e.target.value })}
                    rows={3}
                    className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary font-mono focus:outline-none focus:border-accent-teal transition-colors" />
                </div>
              </div>
            )}

            {/* HTTP fields - show when type is sse/streamableHttp, or auto with URL */}
            {showHttpFields && (
              <div className="space-y-3 p-3 rounded-lg border border-border/50 bg-background/50">
                <div className="text-xs text-text-muted flex items-center gap-1">
                  <Globe size={10} /> HTTP/SSE 配置
                </div>
                <div>
                  <label className="text-xs text-text-tertiary block mb-1.5">URL</label>
                  <input value={formData.url} onChange={(e) => setFormData({ ...formData, url: e.target.value })}
                    placeholder="如 http://localhost:3000/sse"
                    className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary focus:outline-none focus:border-accent-teal transition-colors" />
                </div>
                <div>
                  <label className="text-xs text-text-tertiary block mb-1.5">自定义 Headers (每行 Key: Value)</label>
                  <textarea value={formData.headersText} onChange={(e) => setFormData({ ...formData, headersText: e.target.value })}
                    rows={2}
                    className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary font-mono focus:outline-none focus:border-accent-teal transition-colors" />
                </div>
              </div>
            )}

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-xs text-text-tertiary block mb-1.5">工具超时 (秒)</label>
                <input type="number" value={formData.tool_timeout}
                  onChange={(e) => setFormData({ ...formData, tool_timeout: parseInt(e.target.value) || 30 })}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary focus:outline-none focus:border-accent-teal transition-colors" />
              </div>
              <div>
                <label className="text-xs text-text-tertiary block mb-1.5">启用工具 (逗号分隔, * = 全部)</label>
                <input value={formData.enabled_tools_text}
                  onChange={(e) => setFormData({ ...formData, enabled_tools_text: e.target.value })}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary focus:outline-none focus:border-accent-teal transition-colors" />
              </div>
            </div>
            <Button onClick={editingServer ? handleUpdate : handleAdd}>
              {editingServer ? "更新" : "保存"}
            </Button>
          </div>
        )}

        {servers.length === 0 ? (
          <EmptyState
            icon={<Cable size={40} />}
            title="暂无 MCP 服务器"
            description="添加 MCP 服务器来扩展 Bot 的工具能力"
          />
        ) : (
          <div className="space-y-2">
            {servers.map((s: any) => {
              const expanded = expandedServer === s.name;
              const result = testResult[s.name];
              const isHttp = s.type === "sse" || s.type === "streamableHttp" || (!s.type && s.url);
              return (
                <div key={s.name} className="rounded-lg border border-border overflow-hidden">
                  <div className="flex items-center gap-3 px-4 py-3 hover:bg-background-hover/50 transition-colors">
                    <button onClick={() => setExpandedServer(expanded ? null : s.name)}
                      className="p-0.5 text-text-tertiary hover:text-text-primary transition-colors">
                      {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </button>
                    <div className={`p-1.5 rounded ${s.enabled ? "bg-accent-teal-dim text-accent-teal" : "bg-background-tertiary text-text-muted"}`}>
                      {isHttp ? <Globe size={14} /> : <Terminal size={14} />}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium">{s.name}</span>
                        <span className="text-xs text-text-muted px-1.5 py-0.5 rounded bg-background-tertiary">{s.type || "auto"}</span>
                        <StatusBadge status={s.enabled ? "online" : "disabled"} />
                      </div>
                      <div className="text-xs text-text-tertiary mt-0.5 font-mono truncate">
                        {isHttp ? s.url : `${s.command} ${(s.args || []).join(" ")}`}
                      </div>
                    </div>
                    <button onClick={() => handleTest(s.name)} disabled={testing === s.name}
                      title="测试连接"
                      className="flex items-center gap-1 px-2.5 py-1 text-xs rounded-lg border border-border hover:bg-background-hover text-text-tertiary hover:text-text-primary disabled:opacity-50 transition-all">
                      {testing === s.name ? <Play size={12} className="animate-pulse" /> : <Play size={12} />}
                      测试
                    </button>
                    <button onClick={() => handleEdit(s)} title="编辑"
                      className="p-1.5 rounded-lg hover:bg-background-hover text-text-tertiary hover:text-text-primary transition-colors">
                      <Edit3 size={14} />
                    </button>
                    <button onClick={() => handleToggle(s.name, s.enabled)} title={s.enabled ? "禁用" : "启用"}
                      className={`p-1.5 rounded-lg hover:bg-background-hover transition-colors ${s.enabled ? "text-accent-teal" : "text-text-muted"}`}>
                      <Power size={14} />
                    </button>
                    <button onClick={() => handleRemove(s.name)} title="删除"
                      className="p-1.5 rounded-lg hover:bg-background-hover text-text-muted hover:text-destructive transition-colors">
                      <Trash2 size={14} />
                    </button>
                  </div>

                  {expanded && (
                    <div className="px-4 pb-3 border-t border-border pt-2 bg-background-secondary/50 space-y-0">
                      <div className="flex justify-between items-center py-1.5 text-xs">
                        <span className="text-text-muted">类型</span>
                        <span className="text-text-primary font-mono">{s.type || "auto"}</span>
                      </div>
                      {s.command && (
                        <div className="flex justify-between items-center py-1.5 text-xs">
                          <span className="text-text-muted">命令</span>
                          <span className="text-text-primary font-mono">{s.command}</span>
                        </div>
                      )}
                      {s.args && s.args.length > 0 && (
                        <div className="flex justify-between items-center py-1.5 text-xs">
                          <span className="text-text-muted">参数</span>
                          <span className="text-text-primary font-mono">{s.args.join(" ")}</span>
                        </div>
                      )}
                      {s.url && (
                        <div className="flex justify-between items-center py-1.5 text-xs">
                          <span className="text-text-muted">URL</span>
                          <span className="text-text-primary font-mono">{s.url}</span>
                        </div>
                      )}
                      {s.env && Object.keys(s.env).length > 0 && (
                        <div className="py-1.5">
                          <div className="text-xs text-text-muted mb-1">环境变量</div>
                          <pre className="p-2 rounded bg-background text-text-secondary text-xs overflow-x-auto">{Object.entries(s.env).map(([k, v]) => `${k}=${v}`).join("\n")}</pre>
                        </div>
                      )}
                      {s.headers && Object.keys(s.headers).length > 0 && (
                        <div className="py-1.5">
                          <div className="text-xs text-text-muted mb-1">Headers</div>
                          <pre className="p-2 rounded bg-background text-text-secondary text-xs overflow-x-auto">{Object.entries(s.headers).map(([k, v]) => `${k}: ${v}`).join("\n")}</pre>
                        </div>
                      )}
                      <div className="flex gap-4 pt-2">
                        <div className="flex justify-between items-center py-1.5 text-xs">
                          <span className="text-text-muted">超时</span>
                          <span className="text-text-primary font-mono">{s.tool_timeout}s</span>
                        </div>
                        <div className="flex justify-between items-center py-1.5 text-xs">
                          <span className="text-text-muted">启用工具</span>
                          <span className="text-text-primary font-mono">{(s.enabled_tools || ["*"]).join(", ")}</span>
                        </div>
                      </div>
                    </div>
                  )}

                  {result && (
                    <div className={`px-4 py-2 text-xs border-t border-border flex items-start gap-2 ${result.ok ? "text-success bg-success/5" : "text-destructive bg-destructive/5"}`}>
                      {result.ok ? <CheckCircle2 size={12} className="mt-0.5 flex-shrink-0" /> : <XCircle size={12} className="mt-0.5 flex-shrink-0" />}
                      <div className="flex-1">
                        <div>{result.ok ? result.message : result.error}</div>
                        {result.tools && result.tools.length > 0 && (
                          <div className="mt-1 text-text-muted">工具: {result.tools.join(", ")}</div>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </main>
  );
}
