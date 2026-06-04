import { useState, useEffect } from "react";
import { Eye, EyeOff, Plus, Trash2 } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { api } from "@/lib/api";

export default function EnvPage() {
  const [env, setEnv] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [revealed, setRevealed] = useState<Set<string>>(new Set());
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");

  const fetchEnv = () => api.getEnv().then((d) => setEnv(d.env)).finally(() => setLoading(false));

  useEffect(() => { fetchEnv() }, []);

  const handleReveal = async (key: string) => {
    if (revealed.has(key)) {
      setRevealed((prev) => { const next = new Set(prev); next.delete(key); return next });
      return;
    }
    const data = await api.revealEnv(key);
    setEnv((prev) => prev.map((e) => e.key === key ? { ...e, value: data.value } : e));
    setRevealed((prev) => new Set(prev).add(key));
  };

  const handleDelete = async (key: string) => {
    await api.deleteEnv(key);
    fetchEnv();
  };

  const handleAdd = async () => {
    if (!newKey) return;
    await api.setEnv(newKey, newValue);
    setNewKey("");
    setNewValue("");
    fetchEnv();
  };

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader title="Environment Variables" description="管理环境变量" />

      <div className="flex gap-2 mb-4 max-w-md">
        <input
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          placeholder="KEY_NAME"
          className="flex-1 px-3 py-1.5 text-sm rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal"
        />
        <input
          value={newValue}
          onChange={(e) => setNewValue(e.target.value)}
          placeholder="value"
          className="flex-1 px-3 py-1.5 text-sm rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal"
        />
        <button onClick={handleAdd} className="p-1.5 rounded-lg bg-accent-teal text-white hover:bg-accent-teal-hover transition-all">
          <Plus size={14} />
        </button>
      </div>

      <div className="space-y-1 max-w-2xl">
        {env.map((e: any) => (
          <div key={e.key} className="flex items-center gap-3 px-3 py-2 rounded-lg border border-border text-sm">
            <span className="font-mono text-accent-teal flex-shrink-0">{e.key}</span>
            <span className="flex-1 font-mono text-text-secondary truncate">
              {e.is_secret && !revealed.has(e.key) ? "****" : e.value}
            </span>
            {e.is_secret && (
              <button onClick={() => handleReveal(e.key)} className="p-1 hover:bg-background-hover rounded text-text-tertiary">
                {revealed.has(e.key) ? <EyeOff size={12} /> : <Eye size={12} />}
              </button>
            )}
            <button onClick={() => handleDelete(e.key)} className="p-1 hover:bg-background-hover rounded text-text-muted hover:text-destructive">
              <Trash2 size={12} />
            </button>
          </div>
        ))}
      </div>
    </main>
  );
}
