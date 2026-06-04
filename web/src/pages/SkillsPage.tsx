import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { StatusBadge } from "@/components/StatusBadge";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";

export default function SkillsPage() {
  const { data, loading, refetch } = useApi(() => api.getSkills(), []);

  const handleToggle = async (name: string, enabled: boolean) => {
    await api.toggleSkill(name, !enabled);
    refetch();
  };

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;

  const skills = data?.skills || [];

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader title="Skills" description="管理技能插件" />

      {skills.length === 0 ? (
        <p className="text-sm text-text-tertiary">暂无技能</p>
      ) : (
        <div className="space-y-2 max-w-2xl">
          {skills.map((skill: any) => (
            <div key={skill.name} className="flex items-center justify-between px-4 py-3 rounded-lg border border-border">
              <div>
                <div className="text-sm font-medium">{skill.name}</div>
                {skill.description && (
                  <div className="text-xs text-text-tertiary mt-0.5">{skill.description}</div>
                )}
              </div>
              <div className="flex items-center gap-3">
                <StatusBadge status={skill.enabled ? "online" : "disabled"} />
                <button
                  onClick={() => handleToggle(skill.name, skill.enabled)}
                  className={`relative w-9 h-5 rounded-full transition-all ${
                    skill.enabled ? "bg-accent-teal" : "bg-background-tertiary"
                  }`}
                >
                  <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-all ${
                    skill.enabled ? "translate-x-4" : ""
                  }`} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
