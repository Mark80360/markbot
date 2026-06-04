import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

function formatUptime(seconds: number): string {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${d}d ${h}h ${m}m`;
}

export default function SystemPage() {
  const { data, loading } = useApi(() => api.getSystemStats(), []);

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;
  if (!data) return <main className="flex-1 p-6"><p className="text-sm text-text-tertiary">无法加载系统信息</p></main>;

  const stats = [
    { label: "Version", value: data.version },
    { label: "Python", value: data.python_version },
    { label: "Platform", value: data.platform },
    { label: "CPU", value: `${data.cpu_percent}%` },
    { label: "Memory", value: `${formatBytes(data.memory?.used || 0)} / ${formatBytes(data.memory?.total || 0)} (${data.memory?.percent}%)` },
    { label: "Disk", value: `${formatBytes(data.disk?.used || 0)} / ${formatBytes(data.disk?.total || 0)} (${data.disk?.percent}%)` },
    { label: "Uptime", value: formatUptime(data.uptime || 0) },
  ];

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader title="System" description="系统状态和诊断信息" />
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 max-w-2xl">
        {stats.map((s) => (
          <div key={s.label} className="p-4 rounded-lg border border-border">
            <div className="text-xs text-text-tertiary mb-1">{s.label}</div>
            <div className="text-sm font-medium break-all">{s.value}</div>
          </div>
        ))}
      </div>
    </main>
  );
}
