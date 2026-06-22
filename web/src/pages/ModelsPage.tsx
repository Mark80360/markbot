import { useState, useEffect } from "react";
import { Brain, Zap, Settings2, Layers, ChevronRight } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { StatusBadge } from "@/components/StatusBadge";
import { Card, Section, Feedback, Button } from "@/components/ui";
import { useApi, useFeedback } from "@/hooks/useApi";
import { api } from "@/lib/api";

export default function ModelsPage() {
  const { data: info, loading, refetch } = useApi(() => api.getModelInfo(), []);
  const { data: options } = useApi(() => api.getModelOptions(), []);
  const [selectedProvider, setSelectedProvider] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [params, setParams] = useState({
    max_tokens: 8192,
    temperature: 0.1,
    context_window_tokens: 65536,
    reasoning_effort: "",
    max_tool_iterations: 40,
  });
  const [saving, setSaving] = useState(false);
  const [applying, setApplying] = useState(false);
  const { feedback, show } = useFeedback();

  // Initialize params from info once loaded
  useEffect(() => {
    if (info) {
      setParams({
        max_tokens: info.max_tokens || 8192,
        temperature: info.temperature ?? 0.1,
        context_window_tokens: info.context_window || 65536,
        reasoning_effort: info.reasoning_effort || "",
        max_tool_iterations: info.max_tool_iterations || 40,
      });
    }
  }, [info]);

  const handleApply = async () => {
    if (!selectedProvider || !selectedModel) return;
    setApplying(true);
    try {
      await api.setModel(selectedProvider, selectedModel);
      show(`已切换到 ${selectedProvider}/${selectedModel}`);
      setSelectedProvider("");
      setSelectedModel("");
      refetch();
    } catch (e: any) {
      show(`切换失败: ${e.message}`, "error");
    }
    setApplying(false);
  };

  const handleSaveParams = async () => {
    setSaving(true);
    try {
      await api.updateAgentParams({
        max_tokens: params.max_tokens,
        temperature: params.temperature,
        context_window_tokens: params.context_window_tokens,
        reasoning_effort: params.reasoning_effort || null,
        max_tool_iterations: params.max_tool_iterations,
      });
      show("参数已保存");
    } catch (e: any) {
      show(`保存失败: ${e.message}`, "error");
    }
    setSaving(false);
  };

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;

  const providerOptions = options?.options || [];

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader title="Models" description="模型配置与参数调优" />

      <Feedback message={feedback} />

      <div className="max-w-3xl space-y-6">
        {/* Current Model Status */}
        <Section icon={<Brain size={14} />} title="当前模型">
          {info && (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <Card label="Provider" value={info.provider || "未设置"} />
              <Card label="Model" value={info.model || "未设置"} mono />
              <Card label="Max Tokens" value={String(info.max_tokens || 8192)} mono />
              <Card label="Temperature" value={String(info.temperature ?? 0.1)} mono />
              {info.chain && info.chain.length > 1 && (
                <div className="col-span-full p-3 rounded-lg border border-border bg-background-secondary">
                  <div className="text-xs text-text-tertiary mb-2 flex items-center gap-1">
                    <Layers size={10} /> Fallback 链
                  </div>
                  <div className="flex flex-wrap items-center gap-1 text-xs font-mono">
                    {info.chain.map((c: string, i: number) => (
                      <span key={i} className="flex items-center gap-1">
                        <span className={i === 0 ? "text-accent-teal font-medium" : "text-text-secondary"}>{c}</span>
                        {i < info.chain.length - 1 && <ChevronRight size={10} className="text-text-muted" />}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </Section>

        {/* Model Switcher */}
        {options && (
          <Section icon={<Zap size={14} />} title="切换模型">
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-text-tertiary block mb-1.5">Provider</label>
                  <select
                    value={selectedProvider}
                    onChange={(e) => { setSelectedProvider(e.target.value); setSelectedModel(""); }}
                    className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal transition-colors"
                  >
                    <option value="">选择 Provider</option>
                    {providerOptions.map((o: any) => (
                      <option key={o.provider} value={o.provider}>
                        {o.provider} {o.has_api_key ? "" : "(无 API Key)"}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="text-xs text-text-tertiary block mb-1.5">Model</label>
                  <select
                    value={selectedModel}
                    onChange={(e) => setSelectedModel(e.target.value)}
                    className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal transition-colors"
                  >
                    <option value="">选择 Model</option>
                    {providerOptions
                      .find((o: any) => o.provider === selectedProvider)
                      ?.models?.map((m: any) => (
                        <option key={m.id} value={m.id}>
                          {m.id} {m.context_window ? `(${Math.round(m.context_window / 1024)}K)` : ""}
                        </option>
                      ))}
                  </select>
                </div>
              </div>
              <Button onClick={handleApply} disabled={!selectedProvider || !selectedModel || applying}>
                {applying ? "切换中..." : "应用"}
              </Button>
            </div>
          </Section>
        )}

        {/* Provider Status */}
        {options && providerOptions.length > 0 && (
          <Section icon={<Layers size={14} />} title="Provider 状态">
            <div className="space-y-1.5">
              {providerOptions.map((o: any) => (
                <div key={o.provider} className="flex items-center gap-3 px-3 py-2 rounded-lg border border-border bg-background-secondary">
                  <span className="text-sm font-medium flex-1">{o.provider}</span>
                  <StatusBadge status={o.has_api_key ? "online" : "error"} label={o.has_api_key ? "已配置" : "无 Key"} />
                  <span className="text-xs text-text-muted">{o.models?.length || 0} 模型</span>
                </div>
              ))}
            </div>
          </Section>
        )}

        {/* Agent Parameters */}
        <Section icon={<Settings2 size={14} />} title="Agent 参数">
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="text-xs text-text-tertiary block mb-1.5">Max Tokens</label>
                <input type="number" value={params.max_tokens}
                  onChange={(e) => setParams({ ...params, max_tokens: parseInt(e.target.value) || 8192 })}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal transition-colors" />
              </div>
              <div>
                <label className="text-xs text-text-tertiary block mb-1.5">Temperature</label>
                <input type="number" step="0.1" min="0" max="2" value={params.temperature}
                  onChange={(e) => setParams({ ...params, temperature: parseFloat(e.target.value) || 0 })}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal transition-colors" />
              </div>
              <div>
                <label className="text-xs text-text-tertiary block mb-1.5">Context Window</label>
                <input type="number" value={params.context_window_tokens}
                  onChange={(e) => setParams({ ...params, context_window_tokens: parseInt(e.target.value) || 65536 })}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal transition-colors" />
              </div>
              <div>
                <label className="text-xs text-text-tertiary block mb-1.5">Max Tool Iterations</label>
                <input type="number" value={params.max_tool_iterations}
                  onChange={(e) => setParams({ ...params, max_tool_iterations: parseInt(e.target.value) || 40 })}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal transition-colors" />
              </div>
              <div className="col-span-2">
                <label className="text-xs text-text-tertiary block mb-1.5">Reasoning Effort</label>
                <select value={params.reasoning_effort}
                  onChange={(e) => setParams({ ...params, reasoning_effort: e.target.value })}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal transition-colors">
                  <option value="">默认 (不启用)</option>
                  <option value="low">low</option>
                  <option value="medium">medium</option>
                  <option value="high">high</option>
                </select>
              </div>
            </div>
            <Button onClick={handleSaveParams} disabled={saving}>
              {saving ? "保存中..." : "保存参数"}
            </Button>
          </div>
        </Section>
      </div>
    </main>
  );
}
