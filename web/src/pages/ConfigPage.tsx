import { useState, useEffect, useMemo } from "react";
import {
  Save, Code, FormInput, Search, X, RotateCcw,
  Settings, Bot, Brain, Shield, Globe, Wrench,
  ChevronRight, ChevronDown,
} from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Loading } from "@/components/Loading";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { useToast } from "@/components/Toast";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

const CATEGORY_ICONS: Record<string, typeof Settings> = {
  general: Settings,
  agent: Bot,
  agents: Bot,
  memory: Brain,
  security: Shield,
  tools: Wrench,
  web: Globe,
  channels: Globe,
  providers: Brain,
};

function getNestedValue(obj: any, path: string): any {
  return path.split(".").reduce((acc, key) => acc?.[key], obj);
}

function setNestedValue(obj: any, path: string, value: any): any {
  const keys = path.split(".");
  const result = { ...obj };
  let current = result;
  for (let i = 0; i < keys.length - 1; i++) {
    current[keys[i]] = { ...current[keys[i]] };
    current = current[keys[i]];
  }
  current[keys[keys.length - 1]] = value;
  return result;
}

interface FlatField {
  key: string;
  value: any;
  type: string;
}

function flattenConfig(obj: any, prefix = ""): FlatField[] {
  const result: FlatField[] = [];
  if (obj === null || obj === undefined) return result;
  if (typeof obj !== "object" || Array.isArray(obj)) {
    const type = Array.isArray(obj) ? "array" : typeof obj;
    result.push({ key: prefix, value: obj, type });
    return result;
  }
  for (const [k, v] of Object.entries(obj)) {
    const fullKey = prefix ? `${prefix}.${k}` : k;
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      result.push(...flattenConfig(v, fullKey));
    } else {
      result.push({ key: fullKey, value: v, type: Array.isArray(v) ? "array" : typeof v });
    }
  }
  return result;
}

function getCategory(key: string): string {
  return key.split(".")[0] || "general";
}

/** Get the sub-group key within a category (2nd segment of the path). */
function getSubGroup(key: string): string {
  const parts = key.split(".");
  return parts.length > 1 ? parts[1] : "_root";
}

/** Group fields by sub-group within a category. */
function groupBySubGroup(fields: FlatField[]): Map<string, FlatField[]> {
  const map = new Map<string, FlatField[]>();
  for (const f of fields) {
    const sg = getSubGroup(f.key);
    if (!map.has(sg)) map.set(sg, []);
    map.get(sg)!.push(f);
  }
  return map;
}

function ConfigField({
  fieldKey,
  value,
  type,
  onChange,
}: {
  fieldKey: string;
  value: any;
  type: string;
  onChange: (key: string, value: any) => void;
}) {
  const label = fieldKey.split(".").pop()?.replace(/_/g, " ") || fieldKey;

  if (type === "boolean") {
    return (
      <div className="flex items-center justify-between py-2">
        <label className="text-sm text-text-secondary">{label}</label>
        <button
          onClick={() => onChange(fieldKey, !value)}
          className={cn(
            "w-9 h-5 rounded-full transition-all relative",
            value ? "bg-accent-teal" : "bg-background-tertiary",
          )}
        >
          <span
            className={cn(
              "absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all",
              value ? "left-[18px]" : "left-0.5",
            )}
          />
        </button>
      </div>
    );
  }

  if (type === "number") {
    return (
      <div className="flex items-center justify-between py-2">
        <label className="text-sm text-text-secondary">{label}</label>
        <input
          type="number"
          value={value ?? ""}
          onChange={(e) => onChange(fieldKey, e.target.value === "" ? null : Number(e.target.value))}
          className="w-28 px-2 py-1 text-sm text-right rounded border border-border bg-background text-text-primary outline-none focus:border-accent-teal transition-colors"
        />
      </div>
    );
  }

  if (type === "array") {
    const strValue = Array.isArray(value) ? value.join(", ") : String(value ?? "");
    return (
      <div className="flex items-center justify-between py-2">
        <label className="text-sm text-text-secondary">{label}</label>
        <input
          type="text"
          value={strValue}
          onChange={(e) => onChange(fieldKey, e.target.value.split(",").map((s) => s.trim()).filter(Boolean))}
          className="w-48 px-2 py-1 text-sm rounded border border-border bg-background text-text-primary outline-none focus:border-accent-teal transition-colors"
          placeholder="逗号分隔"
        />
      </div>
    );
  }

  return (
    <div className="flex items-center justify-between py-2">
      <label className="text-sm text-text-secondary">{label}</label>
      <input
        type="text"
        value={value ?? ""}
        onChange={(e) => onChange(fieldKey, e.target.value)}
        className="w-48 px-2 py-1 text-sm rounded border border-border bg-background text-text-primary outline-none focus:border-accent-teal transition-colors"
      />
    </div>
  );
}

/** A sub-group section within a category. */
function SubGroupSection({
  name,
  fields,
  onChange,
  defaultOpen = true,
}: {
  name: string;
  fields: FlatField[];
  onChange: (key: string, value: any) => void;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const displayName = name === "_root" ? "基本" : name.replace(/_/g, " ");

  return (
    <div className="border-b border-border last:border-b-0">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-4 py-2.5 hover:bg-background-hover/50 transition-colors"
      >
        {open ? <ChevronDown size={12} className="text-text-muted" /> : <ChevronRight size={12} className="text-text-muted" />}
        <span className="text-xs font-medium text-accent-teal uppercase tracking-wider">{displayName}</span>
        <span className="text-[10px] text-text-muted ml-auto">{fields.length} 项</span>
      </button>
      {open && (
        <div className="px-4 pb-3">
          {fields.map((field) => (
            <div key={field.key}>
              <div className="flex items-center gap-1 py-0.5">
                <span className="text-[10px] text-text-muted font-mono truncate">{field.key}</span>
              </div>
              <ConfigField
                fieldKey={field.key}
                value={field.value}
                type={field.type}
                onChange={onChange}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function ConfigPage() {
  const { toast } = useToast();
  const [config, setConfig] = useState<any>(null);
  const [raw, setRaw] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [yamlMode, setYamlMode] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [activeCategory, setActiveCategory] = useState("");
  const [confirmReset, setConfirmReset] = useState(false);

  useEffect(() => {
    Promise.all([
      api.getConfig().catch(() => ({})),
      api.getRawConfig().catch(() => ({ raw: "" })),
    ]).then(([cfg, rawCfg]) => {
      setConfig(cfg);
      setRaw(rawCfg.raw || "");
    }).finally(() => setLoading(false));
  }, []);

  const flatFields = useMemo(() => flattenConfig(config), [config]);

  const categories = useMemo(() => {
    const cats = new Set(flatFields.map((f) => getCategory(f.key)));
    return Array.from(cats).sort();
  }, [flatFields]);

  useEffect(() => {
    if (categories.length > 0 && !activeCategory) {
      setActiveCategory(categories[0]);
    }
  }, [categories, activeCategory]);

  const isSearching = searchQuery.trim().length > 0;
  const lowerSearch = searchQuery.toLowerCase();

  const searchResults = useMemo(() => {
    if (!isSearching) return [];
    return flatFields.filter(
      (f) =>
        f.key.toLowerCase().includes(lowerSearch) ||
        f.key.split(".").pop()?.toLowerCase().includes(lowerSearch) ||
        String(f.value).toLowerCase().includes(lowerSearch),
    );
  }, [isSearching, lowerSearch, flatFields]);

  const categoryFields = useMemo(
    () => flatFields.filter((f) => getCategory(f.key) === activeCategory),
    [flatFields, activeCategory],
  );

  /** Sub-groups within the active category. */
  const categorySubGroups = useMemo(() => groupBySubGroup(categoryFields), [categoryFields]);

  /** Search results grouped by sub-group. */
  const searchSubGroups = useMemo(() => groupBySubGroup(searchResults), [searchResults]);

  const handleFieldChange = (key: string, value: any) => {
    setConfig((prev: any) => setNestedValue(prev, key, value));
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      await api.saveConfig(config);
      toast("配置已保存", "success");
    } catch (e: any) {
      toast(`保存失败: ${e.message}`, "error");
    }
    setSaving(false);
  };

  const handleYamlSave = async () => {
    setSaving(true);
    try {
      await api.saveConfig(raw);
      toast("YAML 配置已保存", "success");
      const cfg = await api.getConfig().catch(() => ({}));
      setConfig(cfg);
    } catch (e: any) {
      toast(`保存失败: ${e.message}`, "error");
    }
    setSaving(false);
  };

  const handleReset = () => {
    setConfirmReset(true);
  };

  const executeReset = async () => {
    setConfirmReset(false);
    const defaultCfg = await api.getConfig().catch(() => ({}));
    if (isSearching) {
      let next = config;
      for (const f of searchResults) {
        next = setNestedValue(next, f.key, getNestedValue(defaultCfg, f.key));
      }
      setConfig(next);
    } else {
      let next = { ...config };
      for (const f of categoryFields) {
        next = setNestedValue(next, f.key, getNestedValue(defaultCfg, f.key));
      }
      setConfig(next);
    }
    toast("已重置为默认值", "success");
  };

  if (loading) return <main className="flex-1 p-6"><Loading /></main>;

  const activeSubGroups = isSearching ? searchSubGroups : categorySubGroups;
  const totalFields = isSearching ? searchResults.length : categoryFields.length;

  return (
    <main className="flex-1 flex flex-col min-w-0 p-6 overflow-y-auto">
      <PageHeader
        title="Config"
        description="编辑 Markbot 配置"
        actions={
          <div className="flex items-center gap-2">
            <button
              onClick={handleReset}
              className="p-1.5 rounded-lg border border-border hover:bg-background-hover transition-all"
              title="重置当前分类"
            >
              <RotateCcw size={14} />
            </button>
            <div className="w-px h-5 bg-border" />
            <button
              onClick={() => setYamlMode(!yamlMode)}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg border transition-all",
                yamlMode
                  ? "border-accent-teal bg-accent-teal-dim text-accent-teal"
                  : "border-border hover:bg-background-hover",
              )}
            >
              {yamlMode ? <FormInput size={14} /> : <Code size={14} />}
              {yamlMode ? "表单" : "YAML"}
            </button>
            <button
              onClick={yamlMode ? handleYamlSave : handleSave}
              disabled={saving}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-accent-teal text-white hover:bg-accent-teal-hover disabled:opacity-50 transition-all"
            >
              <Save size={14} />
              {saving ? "保存中..." : "保存"}
            </button>
          </div>
        }
      />

      {yamlMode ? (
        <div className="rounded-lg border border-border bg-background-secondary overflow-hidden">
          <div className="flex items-center gap-2 px-4 py-2 border-b border-border">
            <Code size={14} className="text-text-muted" />
            <span className="text-xs text-text-secondary">config.json</span>
          </div>
          <textarea
            value={raw}
            onChange={(e) => setRaw(e.target.value)}
            spellCheck={false}
            className="w-full font-mono text-sm p-4 bg-transparent text-text-primary outline-none resize-none"
            style={{ height: "calc(100vh - 260px)", minHeight: "400px" }}
          />
        </div>
      ) : (
        <div className="flex gap-4">
          <aside className="w-48 shrink-0">
            <div className="sticky top-4">
              <div className="rounded-lg border border-border bg-background-secondary overflow-hidden">
                <div className="px-3 py-2 border-b border-border flex items-center gap-2">
                  <Search size={12} className="text-text-muted" />
                  <input
                    type="text"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    placeholder="搜索配置..."
                    className="flex-1 bg-transparent text-xs outline-none text-text-primary placeholder:text-text-muted"
                  />
                  {searchQuery && (
                    <button onClick={() => setSearchQuery("")} className="text-text-muted hover:text-text-primary">
                      <X size={10} />
                    </button>
                  )}
                </div>
                <div className="p-1.5 max-h-[calc(100vh-300px)] overflow-y-auto">
                  {categories.map((cat) => {
                    const Icon = CATEGORY_ICONS[cat] || Settings;
                    const count = flatFields.filter((f) => getCategory(f.key) === cat).length;
                    return (
                      <button
                        key={cat}
                        onClick={() => { setSearchQuery(""); setActiveCategory(cat); }}
                        className={cn(
                          "w-full flex items-center gap-2 px-2.5 py-1.5 rounded text-xs transition-all",
                          !isSearching && activeCategory === cat
                            ? "bg-accent-teal-dim text-accent-teal"
                            : "text-text-secondary hover:bg-background-hover hover:text-text-primary",
                        )}
                      >
                        <Icon size={12} className="flex-shrink-0" />
                        <span className="flex-1 text-left truncate capitalize">{cat}</span>
                        <span className="text-text-muted">{count}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            </div>
          </aside>

          <div className="flex-1 min-w-0">
            <div className="rounded-lg border border-border bg-background-secondary overflow-hidden">
              <div className="px-4 py-2.5 border-b border-border flex items-center justify-between">
                <span className="text-sm font-medium text-text-primary capitalize">
                  {isSearching ? `搜索: ${searchQuery}` : activeCategory}
                </span>
                <span className="text-xs text-text-muted">
                  {totalFields} 项 · {activeSubGroups.size} 组
                </span>
              </div>
              <div>
                {Array.from(activeSubGroups.entries()).map(([groupName, fields]) => (
                  <SubGroupSection
                    key={groupName}
                    name={groupName}
                    fields={fields}
                    onChange={handleFieldChange}
                    defaultOpen={activeSubGroups.size <= 5}
                  />
                ))}
                {totalFields === 0 && (
                  <p className="text-sm text-text-muted text-center py-8">
                    {isSearching ? "没有匹配的配置项" : "该分类下没有配置项"}
                  </p>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirmReset}
        title="重置配置"
        description={`确定要将${isSearching ? "搜索结果" : `「${activeCategory}」分类`}的配置重置为默认值吗？`}
        confirmLabel="重置"
        destructive
        onConfirm={executeReset}
        onCancel={() => setConfirmReset(false)}
      />
    </main>
  );
}
