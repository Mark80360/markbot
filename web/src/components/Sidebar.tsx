import {
  Bot, MessageSquare, Moon, PanelLeftClose, PanelLeftOpen,
  Plus, Sun, Trash2, Sparkles, Settings, Key, Brain,
  FileText, Package, Clock, Radio, Cable, Monitor,
} from "lucide-react";
import { NavLink, useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import { useTheme } from "@/contexts/ThemeContext";
import { useSidebar } from "@/contexts/SidebarContext";
import { useChatContext } from "@/contexts/ChatContext";

const NAV_ITEMS = [
  { path: "/chat", label: "Chat", icon: Sparkles },
  { path: "/sessions", label: "Sessions", icon: MessageSquare },
  { path: "/config", label: "Config", icon: Settings },
  { path: "/env", label: "Keys", icon: Key },
  { path: "/models", label: "Models", icon: Brain },
  { path: "/logs", label: "Logs", icon: FileText },
  { path: "/skills", label: "Skills", icon: Package },
  { path: "/cron", label: "Cron", icon: Clock },
  { path: "/channels", label: "Channels", icon: Radio },
  { path: "/mcp", label: "MCP", icon: Cable },
  { path: "/system", label: "System", icon: Monitor },
];

function formatTime(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return "刚刚";
  if (diffMin < 60) return `${diffMin}分钟前`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}小时前`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 7) return `${diffDay}天前`;
  return d.toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
}

export function Sidebar() {
  const { theme, toggleTheme } = useTheme();
  const { collapsed, toggle } = useSidebar();
  const navigate = useNavigate();
  const { clearMessages, sessions, currentSessionId, switchSession, deleteSession } = useChatContext();
  const open = !collapsed;

  return (
    <>
      {!open && (
        <button
          onClick={toggle}
          className="fixed top-3 left-3 z-50 p-2 rounded-lg border border-border hover:border-border-accent hover:bg-background-hover text-text-tertiary hover:text-midground transition-all"
          title="展开侧边栏"
        >
          <PanelLeftOpen size={18} />
        </button>
      )}

      <aside
        className={cn(
          "flex-shrink-0 flex flex-col border-r border-border transition-all duration-300 ease-out overflow-hidden",
          open ? "w-64" : "w-0",
        )}
        style={{ background: "var(--background-base)" }}
      >
        <div className="flex items-center justify-between h-14 px-4 border-b border-border">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg flex items-center justify-center bg-accent-teal-dim">
              <Bot size={16} className="text-accent-teal" />
            </div>
            <h1 className="text-sm font-bold text-display text-midground blend-lighter">
              Markbot
            </h1>
          </div>
          <button
            onClick={toggle}
            className="p-1.5 rounded-lg hover:bg-background-hover text-text-tertiary hover:text-midground transition-colors"
            title="收起侧边栏"
          >
            <PanelLeftClose size={16} />
          </button>
        </div>

        <div className="p-3">
          <button
            onClick={() => { clearMessages(); navigate("/chat") }}
            className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm border border-dashed border-border hover:border-border-accent text-text-tertiary hover:text-accent-teal hover:bg-accent-teal-glow transition-all"
          >
            <Plus size={14} />
            新对话
          </button>
        </div>

        <div className="px-3 py-1">
          <div className="text-xs text-display text-text-tertiary mb-2 px-2">导航</div>
          <nav className="space-y-0.5">
            {NAV_ITEMS.map((item) => (
              <NavLink
                key={item.path}
                to={item.path}
                className={({ isActive }) =>
                  cn(
                    "flex items-center gap-2 px-3 py-2 rounded-lg text-sm transition-all",
                    isActive
                      ? "bg-accent-teal-dim text-text-primary"
                      : "text-text-secondary hover:bg-background-hover hover:text-text-primary",
                  )
                }
              >
                <item.icon size={14} className="flex-shrink-0" />
                <span>{item.label}</span>
              </NavLink>
            ))}
          </nav>
        </div>

        <div className="flex-1 overflow-y-auto scrollbar-none px-3 py-2">
          <div className="text-xs text-display text-text-tertiary mb-2 px-2">
            对话历史
          </div>
          {!currentSessionId && (
            <div className="mb-1 px-3 py-2 rounded-lg text-sm bg-accent-teal-dim text-accent-teal flex items-center gap-2">
              <MessageSquare size={14} className="flex-shrink-0" />
              <span className="truncate">当前对话</span>
            </div>
          )}
          {sessions.length === 0 && currentSessionId ? (
            <div className="px-2 py-4 text-xs text-text-muted text-center">
              暂无对话
            </div>
          ) : (
            <div className="space-y-0.5">
              {sessions.map((session) => (
                <div
                  key={session.id}
                  className={cn(
                    "group flex items-center gap-2 px-3 py-2 rounded-lg text-sm cursor-pointer transition-all",
                    session.id === currentSessionId
                      ? "bg-accent-teal-dim text-text-primary"
                      : "text-text-secondary hover:bg-background-hover hover:text-text-primary",
                  )}
                  onClick={() => { switchSession(session.id); navigate("/chat") }}
                >
                  <MessageSquare
                    size={14}
                    className={cn(
                      "flex-shrink-0",
                      session.id === currentSessionId ? "text-accent-teal" : "text-text-tertiary",
                    )}
                  />
                  <div className="flex-1 min-w-0">
                    <div className="truncate">{session.title}</div>
                    <div className="text-xs text-text-muted mt-0.5">
                      {formatTime(session.lastActive)}
                    </div>
                  </div>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      deleteSession(session.id);
                    }}
                    className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-background-hover text-text-muted hover:text-destructive transition-all"
                    title="删除对话"
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="flex items-center justify-between px-4 py-3 border-t border-border">
          <p className="text-xs text-text-muted">
            Markbot Web UI
          </p>
          <button
            onClick={toggleTheme}
            className="p-1.5 rounded-lg hover:bg-background-hover text-text-tertiary hover:text-midground transition-colors"
            title={theme === "dark" ? "切换到亮色模式" : "切换到暗色模式"}
          >
            {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
          </button>
        </div>
      </aside>
    </>
  );
}
