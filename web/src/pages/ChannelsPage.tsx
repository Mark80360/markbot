import { useState } from "react";
import { Play, Power, ChevronDown, ChevronRight, Wifi, WifiOff, Radio, Globe } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { StatusBadge } from "@/components/StatusBadge";
import { EmptyState, Feedback } from "@/components/ui";
import { useApi, useFeedback } from "@/hooks/useApi";
import { api } from "@/lib/api";

export default function ChannelsPage() {
  const { data, loading, refetch } = useApi(() => api.getChannels(), []);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, any>>({});
  const [testing, setTesting] = useState<string | null>(null);
  const { feedback, show } = useFeedback();

  const handleTest = async (id: string) => {
    setTesting(id);
    setTestResults((prev) => ({ ...prev, [id]: null }));
    try {
      const result = await api.testChannel(id);
      setTestResults((prev) => ({ ...prev, [id]: result }));
      if (result.ok) show(`${id} 连接成功`);
      else show(`${id} 连接失败: ${result.error}`, "error");
    } catch (e: any) {
      setTestResults((prev) => ({ ...prev, [id]: { ok: false, error: e.message } }));
      show(`测试失败: ${e.message}`, "error");
    }
    setTesting(null);
  };

  const handleToggle = async (id: string, enabled: boolean) => {
    try {
      await api.toggleChannel(id, !enabled);
      show(`${id} 已${enabled ? "禁用" : "启用"}`);
      refetch();
    } catch (e: any) {
      show(`操作失败: ${e.message}`, "error");
    }
  };

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;

  const channels = data?.channels || [];
  const enabledCount = channels.filter((c: any) => c.enabled).length;

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader title="Channels" description={`消息通道状态与配置 · ${enabledCount}/${channels.length} 启用`} />

      <Feedback message={feedback} />

      <div className="max-w-3xl">
        {channels.length === 0 ? (
          <EmptyState
            icon={<Radio size={40} />}
            title="暂无通道配置"
            description="在配置文件中添加通道（如 slack、telegram、discord 等）后将显示在此处"
          />
        ) : (
          <div className="space-y-2">
            {channels.map((ch: any) => {
              const isExpanded = expanded === ch.id;
              const result = testResults[ch.id];
              const configEntries = ch.config ? Object.entries(ch.config).filter(([k]: [string, any]) => k !== "enabled") : [];
              return (
                <div key={ch.id} className="rounded-lg border border-border overflow-hidden">
                  <div className="flex items-center gap-3 px-4 py-3 hover:bg-background-hover/50 transition-colors">
                    <button onClick={() => setExpanded(isExpanded ? null : ch.id)}
                      className="p-0.5 text-text-tertiary hover:text-text-primary transition-colors">
                      {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </button>
                    <div className={`p-1.5 rounded ${ch.enabled ? "bg-accent-teal-dim text-accent-teal" : "bg-background-tertiary text-text-muted"}`}>
                      {ch.host ? <Globe size={14} /> : <Radio size={14} />}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium capitalize">{ch.name}</span>
                        <StatusBadge status={ch.enabled ? "online" : "disabled"} />
                      </div>
                      <div className="text-xs text-text-tertiary mt-0.5 font-mono">
                        {ch.host && ch.port ? `${ch.host}:${ch.port}` : "未配置连接地址"}
                      </div>
                    </div>
                    <button onClick={() => handleTest(ch.id)} disabled={testing === ch.id}
                      className="flex items-center gap-1 px-2.5 py-1 text-xs rounded-lg border border-border hover:bg-background-hover text-text-tertiary hover:text-text-primary disabled:opacity-50 transition-all">
                      {testing === ch.id ? <WifiOff size={12} className="animate-pulse" /> : <Play size={12} />}
                      测试
                    </button>
                    <button onClick={() => handleToggle(ch.id, ch.enabled)}
                      title={ch.enabled ? "禁用" : "启用"}
                      className={`p-1.5 rounded-lg hover:bg-background-hover transition-colors ${ch.enabled ? "text-accent-teal" : "text-text-muted"}`}>
                      <Power size={14} />
                    </button>
                  </div>

                  {isExpanded && configEntries.length > 0 && (
                    <div className="px-4 pb-3 border-t border-border pt-2 bg-background-secondary/50">
                      <div className="text-xs text-text-muted mb-2">配置详情</div>
                      <div className="space-y-0">
                        {configEntries.map(([k, v]: [string, any]) => (
                          <div key={k} className="flex justify-between items-center py-1.5 text-xs">
                            <span className="text-text-muted">{k}</span>
                            <span className={`text-text-primary ${typeof v !== "object" ? "font-mono" : ""}`}>
                              {typeof v === "object" ? JSON.stringify(v) : String(v)}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {result && (
                    <div className={`px-4 py-2 text-xs border-t border-border flex items-center gap-2 ${result.ok ? "text-success bg-success/5" : "text-destructive bg-destructive/5"}`}>
                      {result.ok ? <Wifi size={12} /> : <WifiOff size={12} />}
                      {result.ok ? result.message : result.error}
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
