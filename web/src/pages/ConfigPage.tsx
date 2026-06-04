import { useState, useEffect } from "react";
import { Save } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { JsonEditor } from "@/components/JsonEditor";
import { api } from "@/lib/api";

export default function ConfigPage() {
  const [raw, setRaw] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    api.getRawConfig()
      .then((d) => setRaw(d.raw))
      .catch(() => setRaw("# Error loading config"))
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    setSaving(true);
    setMsg("");
    try {
      await api.saveConfig(raw);
      setMsg("配置已保存");
    } catch (e: any) {
      setMsg(`保存失败: ${e.message}`);
    }
    setSaving(false);
  };

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader
        title="Config"
        description="编辑 Markbot YAML 配置文件"
        actions={
          <button
            onClick={handleSave}
            disabled={saving}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-accent-teal text-white hover:bg-accent-teal-hover disabled:opacity-50 transition-all"
          >
            <Save size={14} />
            {saving ? "保存中..." : "保存"}
          </button>
        }
      />
      {msg && (
        <div className="text-sm mb-3 px-3 py-1.5 rounded-lg bg-accent-teal-dim text-accent-teal">{msg}</div>
      )}
      <JsonEditor value={raw} onChange={setRaw} language="yaml" />
    </main>
  );
}
