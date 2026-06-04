import { useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";

export default function ModelsPage() {
  const { data: info, loading } = useApi(() => api.getModelInfo(), []);
  const { data: options } = useApi(() => api.getModelOptions(), []);
  const [selectedProvider, setSelectedProvider] = useState("");
  const [selectedModel, setSelectedModel] = useState("");

  const handleApply = async () => {
    if (selectedProvider && selectedModel) {
      await api.setModel(selectedProvider, selectedModel);
    }
  };

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader title="Models" description="当前模型配置" />

      {info && (
        <div className="grid grid-cols-2 gap-4 max-w-lg mb-6">
          <div className="p-4 rounded-lg border border-border">
            <div className="text-xs text-text-tertiary mb-1">Provider</div>
            <div className="text-sm font-medium">{info.provider}</div>
          </div>
          <div className="p-4 rounded-lg border border-border">
            <div className="text-xs text-text-tertiary mb-1">Model</div>
            <div className="text-sm font-medium">{info.model}</div>
          </div>
        </div>
      )}

      {options && (
        <div className="space-y-3 max-w-lg">
          <div>
            <label className="text-xs text-text-tertiary block mb-1">Provider</label>
            <select
              value={selectedProvider}
              onChange={(e) => setSelectedProvider(e.target.value)}
              className="w-full px-3 py-1.5 text-sm rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal"
            >
              <option value="">选择 Provider</option>
              {options.options?.map((o: any) => (
                <option key={o.provider} value={o.provider}>{o.provider}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="text-xs text-text-tertiary block mb-1">Model</label>
            <select
              value={selectedModel}
              onChange={(e) => setSelectedModel(e.target.value)}
              className="w-full px-3 py-1.5 text-sm rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal"
            >
              <option value="">选择 Model</option>
              {options.options
                ?.find((o: any) => o.provider === selectedProvider)
                ?.models?.map((m: string) => (
                  <option key={m} value={m}>{m}</option>
                ))}
            </select>
          </div>
          <button
            onClick={handleApply}
            disabled={!selectedProvider || !selectedModel}
            className="px-3 py-1.5 text-sm rounded-lg bg-accent-teal text-white hover:bg-accent-teal-hover disabled:opacity-50 transition-all"
          >
            应用
          </button>
        </div>
      )}
    </main>
  );
}
