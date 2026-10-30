import {
  Bot, ChevronRight, Command, MessageSquare, Moon, PanelLeftClose,
  PanelLeftOpen, Plus, Search, Sun, Trash2, X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import { useTheme } from "@/contexts/ThemeContext";
import { useSidebar } from "@/contexts/SidebarContext";
import { useChatActions, useChatSessions } from "@/contexts/ChatContext";
import { ADMIN_NAV, PRIMARY_NAV } from "@/lib/nav";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import type { SessionInfo } from "@/types/chat";

function formatTime(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const diffMin = Math.floor((Date.now() - d.getTime()) / 60000);
  if (diffMin < 1) return "刚刚";
  if (diffMin < 60) return `${diffMin} 分钟前`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr} 小时前`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 7) return `${diffDay} 天前`;
  return d.toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
}

const linkBase =
  "group flex items-center gap-2.5 rounded-lg text-sm transition-colors focus-visible:outline-2";
const linkIdle = "text-text-secondary hover:bg-background-hover hover:text-text-primary";
const linkActive = "bg-accent-teal-dim text-text-primary";

export function Sidebar({ onOpenPalette }: { onOpenPalette?: () => void }) {
  const { theme, toggleTheme } = useTheme();
  const { collapsed, toggle, mobileOpen, closeMobile, isMobile } = useSidebar();
  const { sessions, currentSessionId } = useChatSessions();
  const { switchSession, deleteSession, clearMessages } = useChatActions();
  const navigate = useNavigate();
  const { pathname } = useLocation();

  const [query, setQuery] = useState("");
  const [pendingDelete, setPendingDelete] = useState<SessionInfo | null>(null);

  // 移动端：进入新路由后自动收起抽屉，避免遮挡内容
  useEffect(() => {
    if (isMobile) closeMobile();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname]);

  // 移动端抽屉支持 Esc 关闭
  useEffect(() => {
    if (!mobileOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeMobile();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [mobileOpen, closeMobile]);

  const filteredSessions = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter((s) => (s.title || "").toLowerCase().includes(q));
  }, [sessions, query]);

  const startNewChat = () => {
    clearMessages();
    navigate("/chat");
    if (isMobile) closeMobile();
  };

  return (
    <>
      {/* 移动端遮罩 */}
      <div
        className={cn(
          "fixed inset-0 z-40 bg-black/50 transition-opacity duration-300 md:hidden",
          mobileOpen ? "opacity-100" : "pointer-events-none opacity-0",
        )}
        onClick={closeMobile}
        aria-hidden="true"
      />

      {/* 桌面端折叠后留下的悬浮展开按钮 */}
      {collapsed && !isMobile && (
        <button
          onClick={toggle}
          className="fixed top-3 left-3 z-50 rounded-lg border border-border bg-background-base p-2 text-text-tertiary shadow-sm transition-all hover:border-border-accent hover:bg-background-hover hover:text-midground"
          title="展开侧边栏 (Ctrl+B)"
          aria-label="展开侧边栏"
        >
          <PanelLeftOpen size={18} />
        </button>
      )}

      <aside
        id="app-sidebar"
        aria-label="主导航"
        inert={isMobile && !mobileOpen}
        className={cn(
          "fixed inset-y-0 left-0 z-50 flex w-[280px] max-w-[85vw] flex-col border-r border-border bg-background-base",
          "transition-transform duration-300 ease-out",
          mobileOpen ? "translate-x-0 shadow-2xl" : "-translate-x-full max-md:pointer-events-none",
          "md:static md:z-auto md:max-w-none md:translate-x-0 md:shadow-none md:transition-[width]",
          collapsed ? "md:w-0 md:overflow-hidden md:border-r-0" : "md:w-64",
        )}
      >
        {/* 品牌区 */}
        <div className="flex h-14 flex-shrink-0 items-center justify-between border-b border-border px-4">
          <div className="flex items-center gap-2.5">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent-teal-dim">
              <Bot size={16} className="text-accent-teal" />
            </div>
            <div className="leading-tight">
              <h1 className="text-sm font-bold text-display text-midground blend-lighter">Markbot</h1>
              <p className="text-[11px] text-text-muted">个人 AI 助手</p>
            </div>
          </div>
          <button
            onClick={isMobile ? closeMobile : toggle}
            className="rounded-lg p-1.5 text-text-tertiary transition-colors hover:bg-background-hover hover:text-midground"
            title={isMobile ? "关闭菜单" : "收起侧边栏 (Ctrl+B)"}
            aria-label={isMobile ? "关闭菜单" : "收起侧边栏"}
          >
            {isMobile ? <X size={16} /> : <PanelLeftClose size={16} />}
          </button>
        </div>

        {/* 新建对话 */}
        <div className="flex-shrink-0 p-3">
          <button
            onClick={startNewChat}
            className="flex w-full items-center gap-2 rounded-lg border border-dashed border-border px-3 py-2 text-sm text-text-tertiary transition-all hover:border-border-accent hover:bg-accent-teal-glow hover:text-accent-teal"
          >
            <Plus size={14} />
            新对话
          </button>
        </div>

        <div className="flex-1 overflow-y-auto overscroll-contain px-3 pb-3">
          {/* 主导航 */}
          <nav className="space-y-0.5" aria-label="主要">
            {PRIMARY_NAV.map((item) => (
              <NavLink
                key={item.path}
                to={item.path}
                className={({ isActive }) =>
                  cn(linkBase, "px-3 py-2", isActive ? linkActive : linkIdle)
                }
              >
                {({ isActive }) => (
                  <>
                    <item.icon
                      size={15}
                      className={cn("flex-shrink-0", isActive ? "text-accent-teal" : "text-text-tertiary")}
                    />
                    <span className="flex-1">{item.label}</span>
                  </>
                )}
              </NavLink>
            ))}
          </nav>

          {/* 会话历史 */}
          <div className="mt-4">
            <div className="mb-2 flex items-center justify-between px-2">
              <span className="text-[11px] font-medium tracking-wide text-text-tertiary">
                对话历史
              </span>
              <span className="text-[11px] text-text-muted">{sessions.length}</span>
            </div>

            {sessions.length > 5 && (
              <div className="relative mb-2 px-1">
                <Search
                  size={12}
                  className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-text-muted"
                />
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="搜索会话"
                  aria-label="搜索会话"
                  className="w-full rounded-md border border-transparent bg-background-secondary py-1.5 pr-2 pl-7 text-xs text-text-primary placeholder:text-text-muted focus:border-border-accent focus:outline-none"
                />
              </div>
            )}

            {!currentSessionId && (
              <div className="mb-1 flex items-center gap-2 rounded-lg bg-accent-teal-dim px-3 py-2 text-sm text-accent-teal">
                <MessageSquare size={14} className="flex-shrink-0" />
                <span className="truncate">当前对话</span>
              </div>
            )}

            {filteredSessions.length === 0 ? (
              <p className="px-2 py-4 text-center text-xs text-text-muted">
                {query ? "没有匹配的会话" : "暂无历史会话"}
              </p>
            ) : (
              <ul className="space-y-0.5">
                {filteredSessions.map((session) => {
                  const active = session.id === currentSessionId;
                  return (
                    <li key={session.id}>
                      <div
                        role="button"
                        tabIndex={0}
                        onClick={() => {
                          switchSession(session.id);
                          navigate("/chat");
                          if (isMobile) closeMobile();
                        }}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            switchSession(session.id);
                            navigate("/chat");
                            if (isMobile) closeMobile();
                          }
                        }}
                        className={cn(
                          "group flex cursor-pointer items-center gap-2 rounded-lg px-3 py-2 text-sm transition-colors",
                          active ? linkActive : linkIdle,
                        )}
                      >
                        <MessageSquare
                          size={14}
                          className={cn("flex-shrink-0", active ? "text-accent-teal" : "text-text-tertiary")}
                        />
                        <div className="min-w-0 flex-1">
                          <div className="truncate">{session.title || "未命名会话"}</div>
                          <div className="mt-0.5 text-[11px] text-text-muted">
                            {formatTime(session.lastActive)}
                          </div>
                        </div>
                        <button
                          onClick={(event) => {
                            event.stopPropagation();
                            setPendingDelete(session);
                          }}
                          className="rounded p-1 text-text-muted opacity-0 transition-all hover:bg-background-hover hover:text-destructive focus-visible:opacity-100 group-hover:opacity-100 md:opacity-0 max-md:opacity-60"
                          title="删除会话"
                          aria-label={`删除会话 ${session.title}`}
                        >
                          <Trash2 size={12} />
                        </button>
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          {/* 管理入口 */}
          <div className="mt-4">
            <div className="mb-2 px-2 text-[11px] font-medium tracking-wide text-text-tertiary">
              管理
            </div>
            <nav className="space-y-0.5" aria-label="管理">
              {ADMIN_NAV.map((item) => (
                <NavLink
                  key={item.path}
                  to={item.path}
                  className={({ isActive }) =>
                    cn(linkBase, "px-3 py-1.5 text-[13px]", isActive ? linkActive : linkIdle)
                  }
                >
                  {({ isActive }) => (
                    <>
                      <item.icon
                        size={14}
                        className={cn("flex-shrink-0", isActive ? "text-accent-teal" : "text-text-tertiary")}
                      />
                      <span className="flex-1">{item.label}</span>
                      {isActive && <ChevronRight size={12} className="text-accent-teal" />}
                    </>
                  )}
                </NavLink>
              ))}
            </nav>
          </div>
        </div>

        {/* 底栏 */}
        <div className="flex-shrink-0 border-t border-border px-3 py-2.5">
          <button
            onClick={onOpenPalette}
            className="mb-1.5 flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-xs text-text-tertiary transition-colors hover:bg-background-hover hover:text-text-primary"
            title="打开命令面板 (Ctrl+K)"
          >
            <Command size={13} className="flex-shrink-0" />
            <span className="flex-1 text-left">命令面板</span>
            <kbd className="rounded border border-border bg-background-secondary px-1.5 py-0.5 font-mono text-[10px] text-text-muted">
              Ctrl K
            </kbd>
          </button>
          <div className="flex items-center justify-between px-2">
            <p className="text-[11px] text-text-muted">Markbot Web</p>
            <button
              onClick={toggleTheme}
              className="rounded-lg p-1.5 text-text-tertiary transition-colors hover:bg-background-hover hover:text-midground"
              title={theme === "dark" ? "切换到亮色模式" : "切换到暗色模式"}
              aria-label={theme === "dark" ? "切换到亮色模式" : "切换到暗色模式"}
            >
              {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
            </button>
          </div>
        </div>
      </aside>

      <ConfirmDialog
        open={pendingDelete !== null}
        title="删除这个会话？"
        description={
          pendingDelete
            ? `「${pendingDelete.title || "未命名会话"}」及其消息将被永久移除，该操作不可撤销。`
            : undefined
        }
        confirmLabel="删除"
        destructive
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          if (pendingDelete) deleteSession(pendingDelete.id);
          setPendingDelete(null);
        }}
      />
    </>
  );
}
