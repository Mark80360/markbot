import { useState } from "react";
import { Plus, Play, Pause, Trash2 } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { StatusBadge } from "@/components/StatusBadge";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";

export default function CronPage() {
  const { data, loading, refetch } = useApi(() => api.getCronJobs(), []);
  const [showAdd, setShowAdd] = useState(false);
  const [name, setName] = useState("");
  const [schedule, setSchedule] = useState("");
  const [command, setCommand] = useState("");

  const handleAdd = async () => {
    if (!name || !schedule || !command) return;
    await api.createCronJob({ name, schedule, command });
    setName(""); setSchedule(""); setCommand("");
    setShowAdd(false);
    refetch();
  };

  const handleControl = async (id: string, action: "pause" | "resume" | "trigger") => {
    await api.controlCronJob(id, action);
    refetch();
  };

  const handleDelete = async (id: string) => {
    await api.deleteCronJob(id);
    refetch();
  };

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;

  const jobs = data?.jobs || [];

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader
        title="Cron"
        description="定时任务管理"
        actions={
          <button onClick={() => setShowAdd(!showAdd)}
            className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg bg-accent-teal text-white hover:bg-accent-teal-hover transition-all"
          ><Plus size={14} /> 添加任务</button>
        }
      />

      {showAdd && (
        <div className="mb-4 p-4 rounded-lg border border-border space-y-2 max-w-lg">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="任务名称"
            className="w-full px-3 py-1.5 text-sm rounded border border-border bg-background-secondary text-text-primary" />
          <input value={schedule} onChange={(e) => setSchedule(e.target.value)} placeholder="Cron 表达式 (如 */5 * * * *)"
            className="w-full px-3 py-1.5 text-sm rounded border border-border bg-background-secondary text-text-primary" />
          <input value={command} onChange={(e) => setCommand(e.target.value)} placeholder="执行命令"
            className="w-full px-3 py-1.5 text-sm rounded border border-border bg-background-secondary text-text-primary" />
          <button onClick={handleAdd}
            className="px-3 py-1.5 text-sm rounded bg-accent-teal text-white hover:bg-accent-teal-hover">保存</button>
        </div>
      )}

      <div className="space-y-1 max-w-2xl">
        {jobs.map((job: any) => (
          <div key={job.id} className="flex items-center gap-3 px-4 py-3 rounded-lg border border-border">
            <div className="flex-1 min-w-0">
              <div className="text-sm font-medium">{job.name}</div>
              <div className="text-xs text-text-tertiary mt-0.5">
                <code>{job.schedule}</code> — {job.command}
              </div>
            </div>
            <StatusBadge status={job.enabled ? "online" : "disabled"} />
            <button onClick={() => handleControl(job.id, job.enabled ? "pause" : "resume")}
              className="p-1.5 rounded hover:bg-background-hover text-text-tertiary hover:text-midground">
              {job.enabled ? <Pause size={12} /> : <Play size={12} />}
            </button>
            <button onClick={() => handleControl(job.id, "trigger")}
              className="p-1.5 rounded hover:bg-background-hover text-text-tertiary hover:text-midground">
              <Play size={12} />
            </button>
            <button onClick={() => handleDelete(job.id)}
              className="p-1.5 rounded hover:bg-background-hover text-text-muted hover:text-destructive">
              <Trash2 size={12} />
            </button>
          </div>
        ))}
      </div>
    </main>
  );
}
