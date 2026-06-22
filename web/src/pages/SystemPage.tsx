import { useState, useEffect, useRef, useCallback } from "react";
import { RefreshCw, Pause, Play, Cpu, MemoryStick, HardDrive, Network, Activity, Server, Clock, Layers } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { StatusBadge } from "@/components/StatusBadge";
import { api } from "@/lib/api";

function formatBytes(bytes: number): string {
  if (!bytes) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

function formatUptime(seconds: number): string {
  if (!seconds) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  return `${m}m ${s}s`;
}

function formatTime(ts: number | string): string {
  if (!ts) return "—";
  const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  return d.toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function ProgressBar({ percent, color = "bg-accent-teal" }: { percent: number; color?: string }) {
  const pct = Math.min(100, Math.max(0, percent));
  const tone = pct >= 90 ? "bg-destructive" : pct >= 70 ? "bg-warning" : color;
  return (
    <div className="h-1.5 w-full rounded-full bg-background-tertiary overflow-hidden">
      <div className={`h-full ${tone} transition-all duration-300`} style={{ width: `${pct}%` }} />
    </div>
  );
}

function StatCard({
  icon, label, value, sub, percent, color,
}: {
  icon: React.ReactNode; label: string; value: string; sub?: string;
  percent?: number; color?: string;
}) {
  return (
    <div className="p-4 rounded-lg border border-border bg-background-secondary">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2 text-text-tertiary">
          {icon}
          <span className="text-xs">{label}</span>
        </div>
        {percent !== undefined && (
          <span className={`text-xs font-mono ${percent >= 90 ? "text-destructive" : percent >= 70 ? "text-warning" : "text-text-secondary"}`}>
            {percent.toFixed(1)}%
          </span>
        )}
      </div>
      <div className="text-base font-semibold text-text-primary break-all">{value}</div>
      {sub && <div className="text-xs text-text-muted mt-1">{sub}</div>}
      {percent !== undefined && <div className="mt-2"><ProgressBar percent={percent} color={color} /></div>}
    </div>
  );
}

function ProcessRow({ proc, isCurrent }: { proc: any; isCurrent?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="border-b border-border last:border-0">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-3 px-3 py-2 hover:bg-background-hover text-left text-xs"
      >
        <span className="font-mono text-text-muted w-12">{proc.pid}</span>
        <span className="flex-1 truncate">
          {proc.name}
          {isCurrent && <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-accent-teal/20 text-accent-teal">main</span>}
        </span>
        <span className="font-mono text-text-secondary w-16 text-right">{proc.cpu_percent?.toFixed(1)}%</span>
        <span className="font-mono text-text-secondary w-20 text-right">{formatBytes(proc.memory_rss || 0)}</span>
        <StatusBadge status={proc.status === "running" ? "online" : "disabled"} label={proc.status} />
      </button>
      {expanded && (
        <div className="px-3 pb-3 pt-1 text-xs text-text-muted space-y-1 bg-background">
          <div><span className="text-text-tertiary">命令行:</span> <code className="break-all">{proc.cmdline || "—"}</code></div>
          <div className="flex gap-6">
            <span><span className="text-text-tertiary">RSS:</span> {formatBytes(proc.memory_rss || 0)}</span>
            <span><span className="text-text-tertiary">VMS:</span> {formatBytes(proc.memory_vms || 0)}</span>
            <span><span className="text-text-tertiary">运行时长:</span> {formatUptime(proc.uptime || 0)}</span>
            <span><span className="text-text-tertiary">启动时间:</span> {formatTime(proc.create_time || 0)}</span>
          </div>
        </div>
      )}
    </div>
  );
}

export default function SystemPage() {
  const [stats, setStats] = useState<any>(null);
  const [process, setProcess] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [interval, setIntervalMs] = useState(3000);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchAll = useCallback(async () => {
    try {
      const [s, p] = await Promise.all([api.getSystemStats(), api.getSystemProcess()]);
      setStats(s);
      setProcess(p.process);
      setLastUpdated(new Date());
      setError(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial fetch
  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  // Auto refresh
  useEffect(() => {
    if (!autoRefresh) return;
    timerRef.current = setTimeout(() => { fetchAll(); }, interval);
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [autoRefresh, interval, lastUpdated, fetchAll]);

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;
  if (error && !stats) return <main className="flex-1 p-6"><p className="text-sm text-destructive">{error}</p></main>;
  if (!stats) return <main className="flex-1 p-6"><p className="text-sm text-text-tertiary">无法加载系统信息</p></main>;

  const cpu = stats.cpu || {};
  const mem = stats.memory || {};
  const swap = stats.swap || {};
  const disk = stats.disk || {};
  const net = stats.network || {};
  const la = stats.load_average;
  const proc = stats.process || {};
  const currentProc = process?.current || proc.current;
  const childProcs = process?.children || proc.children || [];

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader
        title="System"
        description="系统状态和诊断信息"
        actions={
          <div className="flex items-center gap-2">
            <select
              value={interval}
              onChange={(e) => setIntervalMs(Number(e.target.value))}
              className="text-xs px-2 py-1.5 rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none"
            >
              <option value={2000}>2s</option>
              <option value={3000}>3s</option>
              <option value={5000}>5s</option>
              <option value={10000}>10s</option>
              <option value={30000}>30s</option>
            </select>
            <button
              onClick={() => setAutoRefresh(!autoRefresh)}
              className={`flex items-center gap-1 px-2 py-1.5 text-xs rounded-lg border border-border hover:bg-background-hover ${autoRefresh ? "text-accent-teal" : "text-text-tertiary"}`}
            >
              {autoRefresh ? <Pause size={12} /> : <Play size={12} />}
              {autoRefresh ? "暂停" : "自动"}
            </button>
            <button
              onClick={fetchAll}
              className="flex items-center gap-1 px-2 py-1.5 text-xs rounded-lg border border-border hover:bg-background-hover text-text-tertiary"
            >
              <RefreshCw size={12} className={loading ? "animate-spin" : ""} /> 刷新
            </button>
          </div>
        }
      />

      {/* Status bar */}
      <div className="flex items-center gap-4 mb-4 text-xs text-text-muted">
        <span className="flex items-center gap-1">
          <Activity size={12} className={autoRefresh ? "text-success animate-pulse" : "text-text-muted"} />
          {autoRefresh ? "实时监控中" : "已暂停"}
        </span>
        {lastUpdated && <span>最后更新: {lastUpdated.toLocaleTimeString("zh-CN")}</span>}
        {error && <span className="text-destructive">{error}</span>}
      </div>

      {/* Overview cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3 mb-6">
        <StatCard
          icon={<Cpu size={14} />}
          label="CPU"
          value={`${(cpu.percent ?? stats.cpu_percent ?? 0).toFixed(1)}%`}
          sub={`${cpu.count_physical || 0} 物理 / ${cpu.count_logical || 0} 逻辑核心${cpu.frequency_mhz ? ` · ${cpu.frequency_mhz}MHz` : ""}`}
          percent={cpu.percent ?? stats.cpu_percent}
        />
        <StatCard
          icon={<MemoryStick size={14} />}
          label="内存"
          value={`${formatBytes(mem.used || 0)} / ${formatBytes(mem.total || 0)}`}
          sub={`可用 ${formatBytes(mem.available || 0)}`}
          percent={mem.percent}
        />
        <StatCard
          icon={<HardDrive size={14} />}
          label="磁盘"
          value={`${formatBytes(disk.used || 0)} / ${formatBytes(disk.total || 0)}`}
          sub={`可用 ${formatBytes(disk.free || 0)}`}
          percent={disk.percent}
          color="bg-accent-purple"
        />
        <StatCard
          icon={<Layers size={14} />}
          label="Swap"
          value={`${formatBytes(swap.used || 0)} / ${formatBytes(swap.total || 0)}`}
          sub={swap.total ? undefined : "未启用"}
          percent={swap.percent}
          color="bg-warning"
        />
      </div>

      {/* Per-CPU usage */}
      {cpu.percent_per_cpu && cpu.percent_per_cpu.length > 1 && (
        <div className="mb-6 p-4 rounded-lg border border-border bg-background-secondary">
          <div className="text-xs text-text-tertiary mb-3 flex items-center gap-1">
            <Cpu size={12} /> 各核心使用率
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-2">
            {cpu.percent_per_cpu.map((p: number, i: number) => (
              <div key={i} className="text-center">
                <div className="text-[10px] text-text-muted mb-1">#{i}</div>
                <div className="h-16 w-full rounded bg-background-tertiary relative overflow-hidden flex items-end">
                  <div
                    className={`w-full transition-all duration-300 ${p >= 90 ? "bg-destructive" : p >= 70 ? "bg-warning" : "bg-accent-teal"}`}
                    style={{ height: `${Math.min(100, p)}%` }}
                  />
                </div>
                <div className="text-[10px] font-mono mt-1">{p.toFixed(0)}%</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* System info + Network */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
        <div className="p-4 rounded-lg border border-border bg-background-secondary">
          <div className="text-xs text-text-tertiary mb-3 flex items-center gap-1">
            <Server size={12} /> 系统信息
          </div>
          <div className="space-y-2 text-xs">
            <div className="flex justify-between">
              <span className="text-text-muted">版本</span>
              <span className="font-mono text-text-primary">MarkBot {stats.version}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">Python</span>
              <span className="font-mono text-text-primary">{stats.python_version}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">主机名</span>
              <span className="font-mono text-text-primary">{stats.hostname}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">系统</span>
              <span className="font-mono text-text-primary">{stats.system} {stats.release}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">架构</span>
              <span className="font-mono text-text-primary">{stats.architecture}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">平台</span>
              <span className="font-mono text-text-primary text-right text-[10px]">{stats.platform}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">启动时间</span>
              <span className="font-mono text-text-primary">{formatTime(stats.boot_time || 0)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-muted">运行时长</span>
              <span className="font-mono text-text-primary flex items-center gap-1">
                <Clock size={10} /> {formatUptime(stats.uptime || 0)}
              </span>
            </div>
            {la && (
              <div className="flex justify-between">
                <span className="text-text-muted">负载 (1/5/15m)</span>
                <span className="font-mono text-text-primary">{la.map((x: number) => x.toFixed(2)).join(" / ")}</span>
              </div>
            )}
          </div>
        </div>

        <div className="p-4 rounded-lg border border-border bg-background-secondary">
          <div className="text-xs text-text-tertiary mb-3 flex items-center gap-1">
            <Network size={12} /> 网络流量
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="p-3 rounded bg-background">
              <div className="text-[10px] text-text-muted mb-1">发送</div>
              <div className="text-sm font-mono text-accent-teal">{formatBytes(net.bytes_sent || 0)}</div>
              <div className="text-[10px] text-text-muted mt-1">{net.packets_sent || 0} 包</div>
            </div>
            <div className="p-3 rounded bg-background">
              <div className="text-[10px] text-text-muted mb-1">接收</div>
              <div className="text-sm font-mono text-accent-purple">{formatBytes(net.bytes_recv || 0)}</div>
              <div className="text-[10px] text-text-muted mt-1">{net.packets_recv || 0} 包</div>
            </div>
          </div>
          <div className="mt-3 pt-3 border-t border-border text-[10px] text-text-muted">
            累计数据自系统启动起
          </div>
        </div>
      </div>

      {/* Process info */}
      <div className="rounded-lg border border-border bg-background-secondary overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          <div className="text-xs text-text-tertiary flex items-center gap-1">
            <Activity size={12} /> 进程信息
            <span className="ml-2 px-1.5 py-0.5 rounded bg-background text-text-muted">
              共 {process?.count || proc.count || 0} 个
            </span>
          </div>
        </div>
        <div className="flex items-center gap-3 px-3 py-2 bg-background text-[10px] text-text-muted border-b border-border">
          <span className="w-12">PID</span>
          <span className="flex-1">名称</span>
          <span className="w-16 text-right">CPU</span>
          <span className="w-20 text-right">内存</span>
          <span className="w-16">状态</span>
        </div>
        {currentProc && <ProcessRow proc={currentProc} isCurrent />}
        {childProcs.length > 0 ? (
          childProcs.map((p: any, i: number) => <ProcessRow key={p.pid || i} proc={p} />)
        ) : (
          !currentProc && <div className="px-3 py-6 text-center text-xs text-text-muted">无子进程</div>
        )}
      </div>
    </main>
  );
}
