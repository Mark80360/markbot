import {
  ArrowRight, CornerDownLeft, Moon, Plus, Search, Sun, type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import { useTheme } from "@/contexts/ThemeContext";
import { useChatActions } from "@/contexts/ChatContext";
import { ADMIN_NAV, PRIMARY_NAV } from "@/lib/nav";

export const IS_MAC =
  typeof navigator !== "undefined" && /Mac|iPhone|iPad|iPod/.test(navigator.platform || "");

export const SHORTCUT_LABEL = IS_MAC ? "⌘K" : "Ctrl K";

interface CommandItem {
  id: string;
  label: string;
  group: string;
  icon: LucideIcon;
  hint?: string;
  run: () => void;
}

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
}

export function CommandPalette({ open, onClose }: CommandPaletteProps) {
  const navigate = useNavigate();
  const { toggleTheme, theme } = useTheme();
  const { clearMessages } = useChatActions();
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const items = useMemo<CommandItem[]>(() => {
    const navItems: CommandItem[] = [...PRIMARY_NAV, ...ADMIN_NAV].map((item) => ({
      id: `nav:${item.path}`,
      label: item.label,
      group: PRIMARY_NAV.some((p) => p.path === item.path) ? "导航" : "管理",
      icon: item.icon,
      hint: item.description,
      run: () => navigate(item.path),
    }));

    const actions: CommandItem[] = [
      {
        id: "action:new-chat",
        label: "新建对话",
        group: "操作",
        icon: Plus,
        hint: "清空当前上下文并开始新会话",
        run: () => {
          clearMessages();
          navigate("/chat");
        },
      },
      {
        id: "action:theme",
        label: theme === "dark" ? "切换到亮色模式" : "切换到暗色模式",
        group: "操作",
        icon: theme === "dark" ? Sun : Moon,
        hint: "外观偏好会保存在本地",
        run: () => toggleTheme(),
      },
    ];

    return [...actions, ...navItems];
  }, [navigate, clearMessages, theme, toggleTheme]);

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((item) =>
      `${item.label} ${item.group} ${item.hint ?? ""} ${item.id}`
        .toLowerCase()
        .includes(q),
    );
  }, [items, query]);

  // 每次打开都重置搜索与选中项，并把焦点交给输入框
  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIndex(0);
    const id = window.setTimeout(() => inputRef.current?.focus(), 30);
    return () => window.clearTimeout(id);
  }, [open]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  // 让选中项始终留在可视区域
  useEffect(() => {
    const node = listRef.current?.querySelector<HTMLElement>(`[data-index="${activeIndex}"]`);
    node?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  if (!open) return null;

  const execute = (item: CommandItem | undefined) => {
    if (!item) return;
    onClose();
    // 先关闭浮层再执行导航，避免路由切换时浮层残留在屏幕上
    window.setTimeout(() => item.run(), 0);
  };

  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((prev) => (results.length ? (prev + 1) % results.length : 0));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((prev) => (results.length ? (prev - 1 + results.length) % results.length : 0));
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      execute(results[activeIndex]);
    }
  };

  return (
    <div className="fixed inset-0 z-[9990] flex items-start justify-center px-4 pt-[12vh] sm:pt-[16vh]">
      <div
        className="absolute inset-0 bg-black/50 backdrop-blur-[2px]"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="命令面板"
        onKeyDown={handleKeyDown}
        className="relative w-full max-w-lg overflow-hidden rounded-xl border border-border bg-background-base shadow-2xl dialog-enter"
      >
        <div className="flex items-center gap-2 border-b border-border px-4">
          <Search size={16} className="flex-shrink-0 text-text-muted" />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索页面或执行操作…"
            aria-label="搜索页面或执行操作"
            className="no-focus-ring flex-1 bg-transparent py-3.5 text-sm text-text-primary outline-none placeholder:text-text-muted"
          />
          <kbd className="flex-shrink-0 rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-text-muted">
            Esc
          </kbd>
        </div>

        <div ref={listRef} className="max-h-[52vh] overflow-y-auto p-2" role="listbox">
          {results.length === 0 ? (
            <p className="px-3 py-8 text-center text-xs text-text-muted">
              没有匹配「{query}」的结果
            </p>
          ) : (
            results.map((item, index) => {
              const showGroup = index === 0 || results[index - 1].group !== item.group;
              const active = index === activeIndex;
              return (
                <div key={item.id}>
                  {showGroup && (
                    <div className="px-3 pt-2 pb-1 text-[11px] font-medium tracking-wide text-text-muted">
                      {item.group}
                    </div>
                  )}
                  <button
                    type="button"
                    data-index={index}
                    role="option"
                    aria-selected={active}
                    onMouseEnter={() => setActiveIndex(index)}
                    onClick={() => execute(item)}
                    className={cn(
                      "flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left transition-colors",
                      active ? "bg-accent-teal-dim text-text-primary" : "text-text-secondary hover:bg-background-hover",
                    )}
                  >
                    <item.icon
                      size={15}
                      className={cn("flex-shrink-0", active ? "text-accent-teal" : "text-text-tertiary")}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm">{item.label}</div>
                      {item.hint && (
                        <div className="truncate text-[11px] text-text-muted">{item.hint}</div>
                      )}
                    </div>
                    {active ? (
                      <CornerDownLeft size={12} className="flex-shrink-0 text-accent-teal" />
                    ) : (
                      <ArrowRight size={12} className="flex-shrink-0 text-text-muted opacity-0" />
                    )}
                  </button>
                </div>
              );
            })
          )}
        </div>

        <div className="flex items-center justify-between border-t border-border px-4 py-2 text-[11px] text-text-muted">
          <span className="flex items-center gap-3">
            <span>↑↓ 选择</span>
            <span>↵ 执行</span>
            <span>Esc 关闭</span>
          </span>
          <span>{results.length} 项</span>
        </div>
      </div>
    </div>
  );
}
