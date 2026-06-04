import { useState } from "react";
import { Search, Trash2, MessageSquare } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";

function formatTime(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export default function SessionsPage() {
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const { data, loading, refetch } = useApi(
    () => search ? api.searchSessions(search) : api.getSessions(),
    [search],
  );

  const handleDelete = async (id: string) => {
    await api.deleteSession(id);
    refetch();
  };

  const sessions = data?.sessions || [];

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader title="Sessions" description="浏览和管理对话历史" />

      <div className="relative mb-4 max-w-md">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-text-tertiary" />
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="搜索对话..."
          className="w-full pl-9 pr-3 py-2 text-sm rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal"
        />
      </div>

      {loading ? (
        <Loading />
      ) : sessions.length === 0 ? (
        <p className="text-sm text-text-tertiary">暂无对话</p>
      ) : (
        <div className="space-y-1 max-w-2xl">
          {sessions.map((s: any) => (
            <div
              key={s.id}
              className="flex items-center gap-3 px-4 py-3 rounded-lg border border-border hover:bg-background-hover cursor-pointer transition-all group"
              onClick={() => navigate(`/chat?session=${s.id}`)}
            >
              <MessageSquare size={14} className="text-text-tertiary flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="text-sm truncate">{s.title}</div>
                <div className="text-xs text-text-muted mt-0.5">
                  {s.message_count} 条消息 · {formatTime(s.last_active)}
                </div>
              </div>
              <button
                onClick={(e) => { e.stopPropagation(); handleDelete(s.id) }}
                className="opacity-0 group-hover:opacity-100 p-1.5 rounded hover:bg-background-hover text-text-muted hover:text-destructive transition-all"
              >
                <Trash2 size={12} />
              </button>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
