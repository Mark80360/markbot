import { useState, useCallback, useRef, useEffect } from "react";
import {
  Search, Trash2, MessageSquare, Download, FileJson,
  Pencil, Check, X, ChevronLeft, ChevronRight, Eraser,
} from "lucide-react";
import { useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { useToast } from "@/components/Toast";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 20;

function formatTime(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export default function SessionsPage() {
  const navigate = useNavigate();
  const { toast } = useToast();
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [sessions, setSessions] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [stats, setStats] = useState<{ total: number; active: number; messages: number } | null>(null);

  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [emptyDeleteOpen, setEmptyDeleteOpen] = useState(false);
  const lastClickedRef = useRef<number | null>(null);

  const loadSessions = useCallback(async (p: number, q?: string) => {
    setLoading(true);
    try {
      if (q) {
        const data = await api.searchSessions(q);
        setSessions(data.sessions || []);
      } else {
        const data = await api.getSessions({ limit: PAGE_SIZE, offset: p * PAGE_SIZE });
        setSessions(data.sessions || []);
      }
    } catch { /* ignore */ }
    setLoading(false);
  }, []);

  const loadStats = useCallback(async () => {
    try {
      const s = await api.getSessionStats();
      setStats(s);
      setTotal(s.total);
    } catch { /* ignore */ }
  }, []);

  const doLoad = useCallback((p: number, q?: string) => {
    setPage(p);
    loadSessions(p, q);
    loadStats();
  }, [loadSessions, loadStats]);

  useEffect(() => { doLoad(0); }, [doLoad]);

  const handleSearch = (v: string) => {
    setSearch(v);
    setSelectedIds(new Set());
    doLoad(0, v || undefined);
  };

  const handleDelete = async (id: string) => {
    try {
      await api.deleteSession(id);
      toast("会话已删除", "success");
      doLoad(page, search || undefined);
    } catch {
      toast("删除失败", "error");
    }
    setDeleteTarget(null);
  };

  const handleBulkDelete = async () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    try {
      await api.bulkDeleteSessions(ids);
      toast(`已删除 ${ids.length} 个会话`, "success");
      setSelectedIds(new Set());
      setBulkDeleteOpen(false);
      doLoad(page, search || undefined);
    } catch {
      toast("批量删除失败", "error");
    }
  };

  const handleRename = async (id: string) => {
    const value = renameValue.trim();
    if (!value) { setRenamingId(null); return; }
    try {
      await api.patchSession(id, { title: value });
      setRenamingId(null);
      toast("已重命名", "success");
      doLoad(page, search || undefined);
    } catch {
      toast("重命名失败", "error");
    }
  };

  const filtered = sessions;
  const pageCount = Math.ceil(total / PAGE_SIZE);

  const toggleSelect = (id: string, index: number, e: React.MouseEvent) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (e.shiftKey && lastClickedRef.current !== null) {
        const [lo, hi] = lastClickedRef.current <= index
          ? [lastClickedRef.current, index]
          : [index, lastClickedRef.current];
        const willAdd = !next.has(id);
        for (let i = lo; i <= hi; i++) {
          const sid = filtered[i]?.id;
          if (sid) willAdd ? next.add(sid) : next.delete(sid);
        }
      } else if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
    lastClickedRef.current = index;
  };

  const handleExportMarkdown = async (id: string, title: string) => {
    try {
      const res = await api.exportSession(id, "markdown");
      if (res.ok) {
        const blob = await res.blob();
        const safeTitle = title.replace(/[^a-zA-Z0-9\u4e00-\u9fff _-]/g, "").trim() || "session";
        downloadBlob(blob, `${safeTitle}.md`);
        toast("已导出 Markdown", "success");
      }
    } catch { /* ignore */ }
  };

  const handleExportJson = async (id: string, title: string) => {
    try {
      const res = await api.exportSession(id, "json");
      if (res.ok) {
        const blob = await res.blob();
        const safeTitle = title.replace(/[^a-zA-Z0-9\u4e00-\u9fff _-]/g, "").trim() || "session";
        downloadBlob(blob, `${safeTitle}.json`);
        toast("已导出 JSON", "success");
      }
    } catch { /* ignore */ }
  };

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader title="Sessions" description="浏览和管理对话历史" />

      {stats && (
        <div className="flex items-center gap-6 mb-4 px-4 py-3 rounded-lg border border-border bg-background-secondary">
          <div className="flex flex-col">
            <span className="text-lg font-semibold">{stats.total}</span>
            <span className="text-xs text-text-muted">总会话</span>
          </div>
          <div className="flex flex-col">
            <span className="text-lg font-semibold text-success">{stats.active}</span>
            <span className="text-xs text-text-muted">有消息</span>
          </div>
          <div className="flex flex-col">
            <span className="text-lg font-semibold">{stats.messages}</span>
            <span className="text-xs text-text-muted">总消息</span>
          </div>
        </div>
      )}

      <div className="flex items-center gap-2 mb-4 flex-wrap">
        <div className="relative flex-1 max-w-md">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-text-tertiary" />
          <input
            type="text"
            value={search}
            onChange={(e) => handleSearch(e.target.value)}
            placeholder="搜索对话..."
            className="w-full pl-9 pr-3 py-2 text-sm rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal"
          />
        </div>
        {selectedIds.size > 0 && (
          <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-accent-teal/30 bg-accent-teal-dim">
            <span className="text-xs text-accent-teal">已选 {selectedIds.size} 项</span>
            <button
              onClick={() => setSelectedIds(new Set())}
              className="text-xs text-text-muted hover:text-text-primary"
            >
              清除
            </button>
            <button
              onClick={() => setBulkDeleteOpen(true)}
              className="flex items-center gap-1 text-xs text-destructive hover:opacity-80"
            >
              <Trash2 size={11} />
              删除
            </button>
          </div>
        )}
      </div>

      {loading ? (
        <Loading />
      ) : filtered.length === 0 ? (
        <p className="text-sm text-text-tertiary">暂无对话</p>
      ) : (
        <>
          <div className="space-y-1 max-w-2xl">
            {filtered.map((s: any, index: number) => (
              <div
                key={s.id}
                className={cn(
                  "flex items-center gap-3 px-4 py-3 rounded-lg border cursor-pointer transition-all group",
                  selectedIds.has(s.id)
                    ? "border-accent-teal/40 bg-accent-teal-dim"
                    : "border-border hover:bg-background-hover",
                )}
                onClick={() => navigate(`/chat?session=${s.id}`)}
              >
                <input
                  type="checkbox"
                  checked={selectedIds.has(s.id)}
                  onClick={(e) => { e.stopPropagation(); toggleSelect(s.id, index, e); }}
                  onChange={() => {}}
                  className="w-3.5 h-3.5 rounded border-border accent-accent-teal flex-shrink-0"
                />
                <MessageSquare size={14} className="text-text-tertiary flex-shrink-0" />
                <div className="flex-1 min-w-0">
                  {renamingId === s.id ? (
                    <div className="flex items-center gap-1.5" onClick={(e) => e.stopPropagation()}>
                      <input
                        autoFocus
                        value={renameValue}
                        onChange={(e) => setRenameValue(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") handleRename(s.id);
                          if (e.key === "Escape") setRenamingId(null);
                        }}
                        className="flex-1 px-2 py-0.5 text-sm rounded border border-accent-teal bg-transparent outline-none"
                      />
                      <button onClick={() => handleRename(s.id)} className="text-accent-teal p-0.5">
                        <Check size={12} />
                      </button>
                      <button onClick={() => setRenamingId(null)} className="text-text-muted p-0.5">
                        <X size={12} />
                      </button>
                    </div>
                  ) : (
                    <div className="text-sm truncate">{s.title}</div>
                  )}
                  <div className="text-xs text-text-muted mt-0.5">
                    {s.message_count} 条消息 · {formatTime(s.last_active)}
                  </div>
                </div>
                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-all">
                  <button
                    onClick={(e) => { e.stopPropagation(); setRenamingId(s.id); setRenameValue(s.title); }}
                    className="p-1.5 rounded hover:bg-background-hover text-text-muted hover:text-text-primary transition-all"
                    title="重命名"
                  >
                    <Pencil size={12} />
                  </button>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleExportMarkdown(s.id, s.title) }}
                    className="p-1.5 rounded hover:bg-background-hover text-text-muted hover:text-accent-teal transition-all"
                    title="导出 Markdown"
                  >
                    <Download size={12} />
                  </button>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleExportJson(s.id, s.title) }}
                    className="p-1.5 rounded hover:bg-background-hover text-text-muted hover:text-accent-teal transition-all"
                    title="导出 JSON"
                  >
                    <FileJson size={12} />
                  </button>
                  <button
                    onClick={(e) => { e.stopPropagation(); setDeleteTarget(s.id); }}
                    className="p-1.5 rounded hover:bg-background-hover text-text-muted hover:text-destructive transition-all"
                    title="删除"
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              </div>
            ))}
          </div>

          {pageCount > 1 && (
            <div className="flex items-center justify-between mt-4 max-w-2xl">
              <span className="text-xs text-text-muted">
                {(page * PAGE_SIZE) + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} / {total}
              </span>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => doLoad(page - 1, search || undefined)}
                  disabled={page === 0}
                  className="p-1.5 rounded border border-border hover:bg-background-hover disabled:opacity-30 transition-all"
                >
                  <ChevronLeft size={14} />
                </button>
                <span className="text-xs text-text-muted px-2">
                  {page + 1} / {pageCount}
                </span>
                <button
                  onClick={() => doLoad(page + 1, search || undefined)}
                  disabled={(page + 1) * PAGE_SIZE >= total}
                  className="p-1.5 rounded border border-border hover:bg-background-hover disabled:opacity-30 transition-all"
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          )}
        </>
      )}

      <ConfirmDialog
        open={!!deleteTarget}
        title="删除会话"
        description="确定要删除这个会话吗？此操作不可撤销。"
        confirmLabel="删除"
        destructive
        onConfirm={() => deleteTarget && handleDelete(deleteTarget)}
        onCancel={() => setDeleteTarget(null)}
      />

      <ConfirmDialog
        open={bulkDeleteOpen}
        title={`删除 ${selectedIds.size} 个会话`}
        description="确定要删除选中的所有会话吗？此操作不可撤销。"
        confirmLabel="全部删除"
        destructive
        onConfirm={handleBulkDelete}
        onCancel={() => setBulkDeleteOpen(false)}
      />
    </main>
  );
}
