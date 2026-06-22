import { useState } from "react";
import { Plus, Play, Pause, Trash2, Edit3, Clock, History, X, Calendar, Zap } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { StatusBadge } from "@/components/StatusBadge";
import { EmptyState, Feedback, Button } from "@/components/ui";
import { useApi, useFeedback } from "@/hooks/useApi";
import { api } from "@/lib/api";

function formatTime(ms: number | null | undefined): string {
  if (!ms) return "—";
  return new Date(ms).toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function formatDuration(ms: number | null | undefined): string {
  if (!ms) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export default function CronPage() {
  const { data, loading, refetch } = useApi(() => api.getCronJobs(), []);
  const [showAdd, setShowAdd] = useState(false);
  const [editingJob, setEditingJob] = useState<any | null>(null);
  const [historyJob, setHistoryJob] = useState<any | null>(null);
  const [formData, setFormData] = useState({ name: "", schedule: "", command: "", tz: "" });
  const { feedback, show } = useFeedback();

  const handleAdd = async () => {
    if (!formData.name || !formData.schedule || !formData.command) return;
    try {
      await api.createCronJob({
        name: formData.name, schedule: formData.schedule,
        command: formData.command, tz: formData.tz || undefined,
      });
      setFormData({ name: "", schedule: "", command: "", tz: "" });
      setShowAdd(false);
      show(`已创建任务 "${formData.name}"`);
      refetch();
    } catch (e: any) {
      show(`创建失败: ${e.message}`, "error");
    }
  };

  const handleEdit = (job: any) => {
    setEditingJob(job);
    setFormData({
      name: job.name,
      schedule: job.schedule_expr || job.schedule,
      command: job.command,
      tz: job.schedule_tz || "",
    });
  };

  const handleUpdate = async () => {
    if (!editingJob) return;
    try {
      await api.updateCronJob(editingJob.id, {
        name: formData.name, schedule: formData.schedule,
        command: formData.command, tz: formData.tz || undefined,
      });
      show(`已更新任务 "${formData.name}"`);
      setEditingJob(null);
      setFormData({ name: "", schedule: "", command: "", tz: "" });
      refetch();
    } catch (e: any) {
      show(`更新失败: ${e.message}`, "error");
    }
  };

  const handleControl = async (id: string, action: "pause" | "resume" | "trigger") => {
    try {
      await api.controlCronJob(id, action);
      const labels = { pause: "已暂停", resume: "已恢复", trigger: "已触发执行" };
      show(labels[action]);
      refetch();
    } catch (e: any) {
      show(`操作失败: ${e.message}`, "error");
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm("确定删除此任务？")) return;
    try {
      await api.deleteCronJob(id);
      show("已删除任务");
      refetch();
    } catch (e: any) {
      show(`删除失败: ${e.message}`, "error");
    }
  };

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;

  const jobs = data?.jobs || [];
  const enabledCount = jobs.filter((j: any) => j.enabled).length;

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader
        title="Cron"
        description={`定时任务管理 · ${enabledCount}/${jobs.length} 启用`}
        actions={
          <Button size="sm" onClick={() => { setShowAdd(!showAdd); setEditingJob(null); }}>
            <Plus size={14} /> 添加任务
          </Button>
        }
      />

      <Feedback message={feedback} />

      <div className="max-w-3xl">
        {(showAdd || editingJob) && (
          <div className="mb-4 p-4 rounded-lg border border-border space-y-3 bg-background-secondary">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium">{editingJob ? "编辑任务" : "新建任务"}</span>
              <button onClick={() => { setShowAdd(false); setEditingJob(null); }}
                className="p-1 rounded hover:bg-background-hover text-text-tertiary"><X size={14} /></button>
            </div>
            <div>
              <label className="text-xs text-text-tertiary block mb-1.5">任务名称</label>
              <input value={formData.name} onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                placeholder="如 每日报告"
                className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary focus:outline-none focus:border-accent-teal transition-colors" />
            </div>
            <div>
              <label className="text-xs text-text-tertiary block mb-1.5">Cron 表达式</label>
              <input value={formData.schedule} onChange={(e) => setFormData({ ...formData, schedule: e.target.value })}
                placeholder="*/5 * * * * (分 时 日 月 周)"
                className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary font-mono focus:outline-none focus:border-accent-teal transition-colors" />
            </div>
            <div>
              <label className="text-xs text-text-tertiary block mb-1.5">执行命令 / 消息内容</label>
              <input value={formData.command} onChange={(e) => setFormData({ ...formData, command: e.target.value })}
                placeholder="如 发送每日报告"
                className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary focus:outline-none focus:border-accent-teal transition-colors" />
            </div>
            <div>
              <label className="text-xs text-text-tertiary block mb-1.5">时区 (可选)</label>
              <input value={formData.tz} onChange={(e) => setFormData({ ...formData, tz: e.target.value })}
                placeholder="如 Asia/Shanghai"
                className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary focus:outline-none focus:border-accent-teal transition-colors" />
            </div>
            <Button onClick={editingJob ? handleUpdate : handleAdd}>
              {editingJob ? "更新" : "保存"}
            </Button>
          </div>
        )}

        {historyJob && (
          <div className="mb-4 p-4 rounded-lg border border-border bg-background-secondary">
            <div className="flex items-center justify-between mb-3">
              <span className="text-sm font-medium flex items-center gap-2">
                <History size={14} className="text-accent-teal" /> 执行历史 — {historyJob.name}
              </span>
              <button onClick={() => setHistoryJob(null)}
                className="p-1 rounded hover:bg-background-hover text-text-tertiary"><X size={14} /></button>
            </div>
            {historyJob.run_history?.length > 0 ? (
              <div className="space-y-1 max-h-64 overflow-y-auto">
                {historyJob.run_history.map((rec: any, i: number) => (
                  <div key={i} className="flex items-center gap-3 px-3 py-2 rounded text-xs bg-background">
                    <StatusBadge status={rec.status === "ok" ? "online" : rec.status === "error" ? "error" : "disabled"} />
                    <span className="text-text-tertiary">{formatTime(rec.run_at_ms)}</span>
                    <span className="text-text-muted">耗时 {formatDuration(rec.duration_ms)}</span>
                    {rec.error && <span className="text-destructive truncate flex-1">{rec.error}</span>}
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-sm text-text-muted text-center py-4">暂无执行记录</div>
            )}
          </div>
        )}

        {jobs.length === 0 ? (
          <EmptyState
            icon={<Clock size={40} />}
            title="暂无定时任务"
            description="创建定时任务来自动执行周期性操作"
          />
        ) : (
          <div className="space-y-2">
            {jobs.map((job: any) => (
              <div key={job.id} className="rounded-lg border border-border overflow-hidden">
                <div className="flex items-center gap-3 px-4 py-3 hover:bg-background-hover/50 transition-colors">
                  <div className={`p-1.5 rounded ${job.enabled ? "bg-accent-teal-dim text-accent-teal" : "bg-background-tertiary text-text-muted"}`}>
                    <Calendar size={14} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium">{job.name}</span>
                      <StatusBadge status={job.enabled ? "online" : "disabled"} />
                      {job.last_status && (
                        <StatusBadge status={job.last_status === "ok" ? "online" : "error"} label={job.last_status} />
                      )}
                    </div>
                    <div className="text-xs text-text-tertiary mt-1 flex items-center gap-2">
                      <code className="text-accent-teal">{job.schedule}</code>
                      <span className="text-text-muted">·</span>
                      <span className="truncate">{job.command}</span>
                    </div>
                  </div>
                  <div className="flex items-center gap-1">
                    <button onClick={() => handleEdit(job)} title="编辑"
                      className="p-1.5 rounded-lg hover:bg-background-hover text-text-tertiary hover:text-text-primary transition-colors">
                      <Edit3 size={14} />
                    </button>
                    <button onClick={() => setHistoryJob(job)} title="执行历史"
                      className="p-1.5 rounded-lg hover:bg-background-hover text-text-tertiary hover:text-text-primary transition-colors">
                      <History size={14} />
                    </button>
                    <button onClick={() => handleControl(job.id, job.enabled ? "pause" : "resume")}
                      title={job.enabled ? "暂停" : "恢复"}
                      className={`p-1.5 rounded-lg hover:bg-background-hover transition-colors ${job.enabled ? "text-accent-teal" : "text-text-muted"}`}>
                      {job.enabled ? <Pause size={14} /> : <Play size={14} />}
                    </button>
                    <button onClick={() => handleControl(job.id, "trigger")} title="立即执行"
                      className="p-1.5 rounded-lg hover:bg-background-hover text-text-tertiary hover:text-text-primary transition-colors">
                      <Zap size={14} />
                    </button>
                    <button onClick={() => handleDelete(job.id)} title="删除"
                      className="p-1.5 rounded-lg hover:bg-background-hover text-text-muted hover:text-destructive transition-colors">
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
                <div className="px-4 pb-3 pt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-text-muted border-t border-border/50">
                  <span className="flex items-center gap-1">
                    <Clock size={10} /> 下次: {formatTime(job.next_run_at_ms)}
                  </span>
                  <span>上次: {formatTime(job.last_run_at_ms)}</span>
                  {job.schedule_tz && <span>时区: {job.schedule_tz}</span>}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </main>
  );
}
