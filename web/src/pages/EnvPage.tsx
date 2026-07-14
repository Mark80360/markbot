import { useState, useEffect, useCallback } from "react";
import { Eye, EyeOff, Plus, Trash2, Search, Download, Upload, X, Key, Lock, FileText } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { EmptyState, Section, Feedback, Button } from "@/components/ui";
import { useFeedback } from "@/hooks/useApi";
import { api } from "@/lib/api";

export default function EnvPage() {
  const [env, setEnv] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [revealed, setRevealed] = useState<Set<string>>(new Set());
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");
  const [search, setSearch] = useState("");
  const [showImport, setShowImport] = useState(false);
  const [importContent, setImportContent] = useState("");
  const [envFilePath, setEnvFilePath] = useState("");
  const [showAddForm, setShowAddForm] = useState(false);
  const { feedback, show } = useFeedback();

  const fetchEnv = useCallback(() => {
    api.getEnv().then((d) => setEnv(d.env)).finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchEnv();
    api.getEnvFile().then((d) => setEnvFilePath(d.path)).catch(() => {});
  }, [fetchEnv]);

  const handleReveal = async (key: string) => {
    if (revealed.has(key)) {
      setRevealed((prev) => { const next = new Set(prev); next.delete(key); return next });
      return;
    }
    try {
      const data = await api.revealEnv(key);
      setEnv((prev) => prev.map((e) => e.key === key ? { ...e, value: data.value } : e));
      setRevealed((prev) => new Set(prev).add(key));
    } catch (e: any) {
      show(`查看失败: ${e.message}`, "error");
    }
  };

  const handleDelete = async (key: string) => {
    try {
      await api.deleteEnv(key);
      fetchEnv();
      show(`已删除 ${key}`);
    } catch (e: any) {
      show(`删除失败: ${e.message}`, "error");
    }
  };

  const handleAdd = async () => {
    if (!newKey) return;
    try {
      await api.setEnv(newKey, newValue);
      setNewKey("");
      setNewValue("");
      setShowAddForm(false);
      fetchEnv();
      show(`已添加 ${newKey}`);
    } catch (e: any) {
      show(`添加失败: ${e.message}`, "error");
    }
  };

  const handleExport = async () => {
    try {
      const res = await api.exportEnv();
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = ".env";
      a.click();
      URL.revokeObjectURL(url);
      show("已导出 .env 文件");
    } catch (e: any) {
      show(`导出失败: ${e.message}`, "error");
    }
  };

  const handleImport = async () => {
    if (!importContent) return;
    try {
      const result = await api.importEnv(importContent);
      setShowImport(false);
      setImportContent("");
      fetchEnv();
      show(`已导入 ${result.imported} 个环境变量`);
    } catch (e: any) {
      show(`导入失败: ${e.message}`, "error");
    }
  };

  const filteredEnv = search
    ? env.filter((e) => e.key.toLowerCase().includes(search.toLowerCase()))
    : env;
  const secretCount = env.filter((e) => e.is_secret).length;
  const secretEnv = filteredEnv.filter((e) => e.is_secret);
  const normalEnv = filteredEnv.filter((e) => !e.is_secret);
  const fileCount = env.filter((e) => e.source === "file" || e.source === "both").length;

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader
        title="Environment Variables"
        description={`管理环境变量 · 共 ${env.length} 项 · ${fileCount} 来自 .env · ${secretCount} 个密钥`}
        actions={
          <div className="flex items-center gap-2">
            <Button variant="secondary" size="sm" onClick={handleExport}>
              <Download size={12} /> 导出
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setShowImport(!showImport)}>
              <Upload size={12} /> 导入
            </Button>
            <Button size="sm" onClick={() => setShowAddForm(!showAddForm)}>
              <Plus size={12} /> 添加
            </Button>
          </div>
        }
      />

      <Feedback message={feedback} />

      <div className="max-w-3xl space-y-4">
        {envFilePath && (
          <div className="flex items-center gap-2 text-xs text-text-muted px-3 py-2 rounded-lg bg-background-secondary">
            <FileText size={12} />
            <span>.env 文件:</span>
            <code className="text-text-tertiary">{envFilePath}</code>
          </div>
        )}

        {showImport && (
          <div className="p-4 rounded-lg border border-border space-y-3 bg-background-secondary">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium">导入 .env 文件内容</span>
              <button onClick={() => setShowImport(false)}
                className="p-1 rounded hover:bg-background-hover text-text-tertiary"><X size={14} /></button>
            </div>
            <textarea value={importContent} onChange={(e) => setImportContent(e.target.value)}
              placeholder="粘贴 .env 文件内容 (KEY=VALUE 格式, 每行一个)"
              rows={8}
              className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary font-mono focus:outline-none focus:border-accent-teal transition-colors" />
            <Button onClick={handleImport} disabled={!importContent}>导入</Button>
          </div>
        )}

        {showAddForm && (
          <div className="p-4 rounded-lg border border-border space-y-3 bg-background-secondary">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium">添加环境变量</span>
              <button onClick={() => { setShowAddForm(false); setNewKey(""); setNewValue(""); }}
                className="p-1 rounded hover:bg-background-hover text-text-tertiary"><X size={14} /></button>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <div>
                <label className="text-xs text-text-tertiary block mb-1.5">KEY</label>
                <input value={newKey} onChange={(e) => setNewKey(e.target.value)}
                  placeholder="KEY_NAME"
                  className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary font-mono focus:outline-none focus:border-accent-teal transition-colors" />
              </div>
              <div className="md:col-span-2">
                <label className="text-xs text-text-tertiary block mb-1.5">VALUE</label>
                <input value={newValue} onChange={(e) => setNewValue(e.target.value)}
                  placeholder="value"
                  className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary font-mono focus:outline-none focus:border-accent-teal transition-colors" />
              </div>
            </div>
            <Button onClick={handleAdd} disabled={!newKey}>
              <Plus size={14} /> 添加
            </Button>
          </div>
        )}

        {/* Search */}
        {env.length > 0 && (
          <div className="relative max-w-md">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="搜索环境变量..."
              className="w-full pl-9 pr-3 py-2 text-sm rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal transition-colors"
            />
          </div>
        )}

        {/* Secret variables section */}
        {secretEnv.length > 0 && (
          <Section icon={<Lock size={14} />} title={`密钥 (${secretEnv.length})`}>
            <div className="space-y-1">
              {secretEnv.map((e: any) => (
                <EnvRow key={e.key} env={e} revealed={revealed.has(e.key)} onReveal={handleReveal} onDelete={handleDelete} />
              ))}
            </div>
          </Section>
        )}

        {/* Normal variables section */}
        {normalEnv.length > 0 && (
          <Section icon={<Key size={14} />} title={`普通变量 (${normalEnv.length})`}>
            <div className="space-y-1">
              {normalEnv.map((e: any) => (
                <EnvRow key={e.key} env={e} revealed={revealed.has(e.key)} onReveal={handleReveal} onDelete={handleDelete} />
              ))}
            </div>
          </Section>
        )}

        {filteredEnv.length === 0 && (
          <EmptyState
            icon={<Key size={40} />}
            title={search ? "未找到匹配的环境变量" : "暂无环境变量"}
            description={search ? "尝试其他关键词" : "点击右上角「添加」按钮，或导入 .env 文件"}
          />
        )}
      </div>
    </main>
  );
}

function EnvRow({ env, revealed, onReveal, onDelete }: {
  env: any;
  revealed: boolean;
  onReveal: (key: string) => void;
  onDelete: (key: string) => void;
}) {
  const sourceLabel = env.source === "file" ? ".env" : env.source === "both" ? ".env+proc" : "proc";
  const sourceColor = env.source === "process" ? "text-text-muted" : "text-accent-teal";
  return (
    <div className="flex items-center gap-3 px-3 py-2 rounded-lg border border-border hover:border-border-accent transition-colors text-sm">
      <span className="font-mono text-accent-teal flex-shrink-0 w-40 md:w-56 truncate text-xs">{env.key}</span>
      <span className="flex-1 font-mono text-text-secondary truncate text-xs">
        {env.is_secret && !revealed ? "••••••••••••" : env.value || "(空)"}
      </span>
      <span className={`text-[10px] flex-shrink-0 ${sourceColor}`} title={`来源: ${env.source || "process"}`}>
        {sourceLabel}
      </span>
      {env.is_secret && (
        <button onClick={() => onReveal(env.key)} className="p-1 hover:bg-background-hover rounded text-text-tertiary hover:text-text-primary transition-colors">
          {revealed ? <EyeOff size={12} /> : <Eye size={12} />}
        </button>
      )}
      <button onClick={() => onDelete(env.key)} className="p-1 hover:bg-background-hover rounded text-text-muted hover:text-destructive transition-colors">
        <Trash2 size={12} />
      </button>
    </div>
  );
}
