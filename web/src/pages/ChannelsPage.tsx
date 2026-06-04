import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { StatusBadge } from "@/components/StatusBadge";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";

export default function ChannelsPage() {
  const { data, loading, refetch } = useApi(() => api.getChannels(), []);

  const handleTest = async (id: string) => {
    await api.testChannel(id);
  };

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;

  const channels = data?.channels || [];

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader title="Channels" description="消息通道状态" />

      {channels.length === 0 ? (
        <p className="text-sm text-text-tertiary">暂无通道配置</p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4 max-w-2xl">
          {channels.map((ch: any) => (
            <div key={ch.id} className="p-4 rounded-lg border border-border">
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-medium">{ch.name}</h3>
                <StatusBadge status={ch.status} />
              </div>
              <div className="text-xs text-text-tertiary mb-3">
                {ch.enabled ? "已启用" : "已禁用"}
              </div>
              <button onClick={() => handleTest(ch.id)}
                className="px-3 py-1 text-xs rounded-lg border border-border hover:bg-background-hover transition-all">
                测试连接
              </button>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
