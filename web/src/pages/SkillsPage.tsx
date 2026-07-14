import { useState } from "react";
import { Plus, Trash2, Eye, X, Package, Code, FileText, Star, Search } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { StatusBadge } from "@/components/StatusBadge";
import { EmptyState, Feedback, Button } from "@/components/ui";
import { useApi, useFeedback } from "@/hooks/useApi";
import { api } from "@/lib/api";

export default function SkillsPage() {
  const { data, loading, refetch } = useApi(() => api.getSkills(), []);
  const [showAdd, setShowAdd] = useState(false);
  const [detailSkill, setDetailSkill] = useState<any | null>(null);
  const [search, setSearch] = useState("");
  const [formData, setFormData] = useState({ name: "", description: "", when_to_use: "", content: "" });
  const { feedback, show } = useFeedback();

  const handleAdd = async () => {
    if (!formData.name) return;
    try {
      await api.createSkill(formData);
      setFormData({ name: "", description: "", when_to_use: "", content: "" });
      setShowAdd(false);
      show(`已创建技能 "${formData.name}"`);
      refetch();
    } catch (e: any) {
      show(`创建失败: ${e.message}`, "error");
    }
  };

  const handleDelete = async (name: string) => {
    if (!confirm(`确定删除技能 "${name}"？`)) return;
    try {
      await api.deleteSkill(name);
      if (detailSkill?.name === name) setDetailSkill(null);
      show(`已删除技能 "${name}"`);
      refetch();
    } catch (e: any) {
      show(`删除失败: ${e.message}`, "error");
    }
  };

  const handleViewDetail = async (name: string) => {
    try {
      const detail = await api.getSkillDetail(name);
      setDetailSkill(detail);
    } catch (e: any) {
      show(`加载详情失败: ${e.message}`, "error");
    }
  };

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;

  const allSkills = data?.skills || [];
  const skills = search
    ? allSkills.filter((s: any) => s.name.toLowerCase().includes(search.toLowerCase()) || s.description?.toLowerCase().includes(search.toLowerCase()))
    : allSkills;
  const builtinCount = allSkills.filter((s: any) => s.is_builtin).length;
  const customCount = allSkills.length - builtinCount;

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader
        title="Skills"
        description={`技能插件管理 · 共 ${allSkills.length} 个 · ${builtinCount} 内置 · ${customCount} 自定义`}
        actions={
          <Button size="sm" onClick={() => setShowAdd(!showAdd)}>
            <Plus size={14} /> 新建技能
          </Button>
        }
      />

      <Feedback message={feedback} />

      <div className="max-w-3xl">
        {/* Search at top */}
        {allSkills.length > 0 && (
          <div className="relative mb-4 max-w-md">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="搜索技能..."
              className="w-full pl-9 pr-3 py-2 text-sm rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal transition-colors"
            />
          </div>
        )}

        {showAdd && (
          <div className="mb-4 p-4 rounded-lg border border-border space-y-3 bg-background-secondary">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium">新建技能</span>
              <button onClick={() => setShowAdd(false)}
                className="p-1 rounded hover:bg-background-hover text-text-tertiary"><X size={14} /></button>
            </div>
            <div>
              <label className="text-xs text-text-tertiary block mb-1.5">技能名称</label>
              <input value={formData.name} onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                placeholder="小写字母、数字、._-"
                className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary font-mono focus:outline-none focus:border-accent-teal transition-colors" />
            </div>
            <div>
              <label className="text-xs text-text-tertiary block mb-1.5">描述</label>
              <input value={formData.description} onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                placeholder="一句话描述技能用途"
                className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary focus:outline-none focus:border-accent-teal transition-colors" />
            </div>
            <div>
              <label className="text-xs text-text-tertiary block mb-1.5">使用场景 (when_to_use)</label>
              <input value={formData.when_to_use} onChange={(e) => setFormData({ ...formData, when_to_use: e.target.value })}
                placeholder="何时使用此技能"
                className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary focus:outline-none focus:border-accent-teal transition-colors" />
            </div>
            <div>
              <label className="text-xs text-text-tertiary block mb-1.5">SKILL.md 内容 (Markdown)</label>
              <textarea value={formData.content} onChange={(e) => setFormData({ ...formData, content: e.target.value })}
                rows={6}
                className="w-full px-3 py-2 text-sm rounded-lg border border-border bg-background text-text-primary font-mono focus:outline-none focus:border-accent-teal transition-colors" />
            </div>
            <Button onClick={handleAdd} disabled={!formData.name}>创建</Button>
          </div>
        )}

        {detailSkill && (
          <div className="mb-4 p-4 rounded-lg border border-border bg-background-secondary">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <FileText size={16} className="text-accent-teal" />
                <span className="text-sm font-medium">{detailSkill.name}</span>
                {detailSkill.is_builtin && <StatusBadge status="disabled" label="内置" />}
              </div>
              <button onClick={() => setDetailSkill(null)}
                className="p-1 rounded hover:bg-background-hover text-text-tertiary"><X size={14} /></button>
            </div>
            {detailSkill.description && (
              <div className="text-xs text-text-tertiary mb-3">{detailSkill.description}</div>
            )}
            <div className="space-y-1 mb-3 pb-3 border-b border-border">
              <div className="flex justify-between items-center py-1.5 text-xs">
                <span className="text-text-muted">使用场景</span>
                <span className="text-text-primary">{detailSkill.when_to_use || "—"}</span>
              </div>
              <div className="flex justify-between items-center py-1.5 text-xs">
                <span className="text-text-muted">状态</span>
                <span className="text-text-primary">{detailSkill.state || "—"}</span>
              </div>
              <div className="flex justify-between items-center py-1.5 text-xs">
                <span className="text-text-muted">使用次数</span>
                <span className="text-text-primary font-mono">{detailSkill.use_count || 0}</span>
              </div>
              <div className="flex justify-between items-center py-1.5 text-xs">
                <span className="text-text-muted">查看次数</span>
                <span className="text-text-primary font-mono">{detailSkill.view_count || 0}</span>
              </div>
              <div className="flex justify-between items-center py-1.5 text-xs">
                <span className="text-text-muted">路径</span>
                <span className="text-text-primary font-mono text-xs">{detailSkill.path || "—"}</span>
              </div>
            </div>
            {detailSkill.scripts && detailSkill.scripts.length > 0 && (
              <div className="mb-3">
                <div className="text-xs text-text-muted mb-2 flex items-center gap-1"><Code size={10} /> 脚本</div>
                <div className="space-y-1">
                  {detailSkill.scripts.map((s: any, i: number) => (
                    <div key={i} className="text-xs text-text-secondary px-3 py-1.5 rounded bg-background flex items-center gap-2">
                      <span className="font-mono text-accent-teal">{s.name}</span>
                      <span className="text-text-muted">({s.language})</span>
                      {s.description && <span className="text-text-tertiary">— {s.description}</span>}
                    </div>
                  ))}
                </div>
              </div>
            )}
            {detailSkill.files && detailSkill.files.length > 0 && (
              <div className="mb-3">
                <div className="text-xs text-text-muted mb-1">文件</div>
                <div className="flex flex-wrap gap-1">
                  {detailSkill.files.map((f: string, i: number) => (
                    <span key={i} className="text-xs px-2 py-0.5 rounded bg-background text-text-tertiary font-mono">{f}</span>
                  ))}
                </div>
              </div>
            )}
            {detailSkill.content && (
              <div>
                <div className="text-xs text-text-muted mb-1">SKILL.md 内容</div>
                <pre className="p-3 rounded bg-background text-text-secondary text-xs overflow-x-auto max-h-64 overflow-y-auto">{detailSkill.content}</pre>
              </div>
            )}
          </div>
        )}

        {allSkills.length === 0 ? (
          <EmptyState
            icon={<Package size={40} />}
            title="暂无技能"
            description="创建自定义技能来扩展 Bot 的能力"
          />
        ) : (
          <div className="space-y-1">
            {skills.length === 0 && (
              <div className="text-center py-8 text-text-muted text-sm">未找到匹配的技能</div>
            )}
            {skills.map((skill: any) => (
              <div key={skill.name} className="flex items-center gap-3 px-4 py-3 rounded-lg border border-border hover:border-border-accent transition-colors">
                <div className={`p-1.5 rounded ${skill.is_builtin ? "bg-background-tertiary text-text-muted" : "bg-accent-teal-dim text-accent-teal"}`}>
                  <Package size={14} />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">{skill.name}</span>
                    {skill.is_builtin && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-background-tertiary text-text-tertiary">内置</span>
                    )}
                    {skill.is_always_active && (
                      <Star size={10} className="text-accent-teal fill-accent-teal" />
                    )}
                  </div>
                  {skill.description && (
                    <div className="text-xs text-text-tertiary mt-0.5 truncate">{skill.description}</div>
                  )}
                  {skill.use_count > 0 && (
                    <div className="text-xs text-text-muted mt-0.5">
                      使用 {skill.use_count} 次 · 查看 {skill.view_count} 次
                    </div>
                  )}
                </div>
                <button onClick={() => handleViewDetail(skill.name)} title="查看详情"
                  className="p-1.5 rounded-lg hover:bg-background-hover text-text-tertiary hover:text-text-primary transition-colors">
                  <Eye size={14} />
                </button>
                {!skill.is_builtin && (
                  <button onClick={() => handleDelete(skill.name)} title="删除"
                    className="p-1.5 rounded-lg hover:bg-background-hover text-text-muted hover:text-destructive transition-colors">
                    <Trash2 size={14} />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </main>
  );
}
