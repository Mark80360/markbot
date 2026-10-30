import { lazy, Suspense, useEffect, useState, type ReactNode } from "react";
import { BrowserRouter, Routes, Route, Navigate, useLocation, useNavigate } from "react-router-dom";
import { Compass, Menu, Search } from "lucide-react";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { ChatProvider } from "@/contexts/ChatContext";
import { SidebarProvider, useSidebar } from "@/contexts/SidebarContext";
import { ToastProvider } from "@/components/Toast";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Loading } from "@/components/Loading";
import { Sidebar } from "@/components/Sidebar";
import { CommandPalette, SHORTCUT_LABEL } from "@/components/CommandPalette";
import { PAGE_TITLES } from "@/lib/nav";
import ChatPage from "@/pages/ChatPage";

// 管理页按需加载：首屏只下载对话所需的代码
const SessionsPage = lazy(() => import("@/pages/SessionsPage"));
const ConfigPage = lazy(() => import("@/pages/ConfigPage"));
const EnvPage = lazy(() => import("@/pages/EnvPage"));
const ModelsPage = lazy(() => import("@/pages/ModelsPage"));
const LogsPage = lazy(() => import("@/pages/LogsPage"));
const SkillsPage = lazy(() => import("@/pages/SkillsPage"));
const CronPage = lazy(() => import("@/pages/CronPage"));
const ChannelsPage = lazy(() => import("@/pages/ChannelsPage"));
const McpPage = lazy(() => import("@/pages/McpPage"));
const SystemPage = lazy(() => import("@/pages/SystemPage"));

/** 懒加载页面的统一占位，保持与管理页一致的内边距。 */
function PageFallback() {
  return (
    <div className="flex-1 p-4 md:p-6">
      <Loading />
    </div>
  );
}

function Page({ children }: { children: ReactNode }) {
  return (
    <ErrorBoundary>
      <Suspense fallback={<PageFallback />}>{children}</Suspense>
    </ErrorBoundary>
  );
}

function NotFoundPage() {
  const navigate = useNavigate();
  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-accent-teal-dim">
        <Compass size={26} className="text-accent-teal" />
      </div>
      <h1 className="text-lg font-semibold text-text-primary">页面不存在</h1>
      <p className="max-w-xs text-sm text-text-secondary">
        你访问的地址没有对应的页面，可能是链接已失效或路径输入有误。
      </p>
      <button
        onClick={() => navigate("/chat")}
        className="mt-2 rounded-lg bg-accent-teal px-4 py-2 text-sm text-white transition-colors hover:bg-accent-teal-hover"
      >
        返回对话
      </button>
    </main>
  );
}

/** 移动端顶部栏：抽屉入口 + 当前页面标题 + 命令面板入口。 */
function MobileHeader({ onOpenPalette }: { onOpenPalette: () => void }) {
  const { toggleMobile } = useSidebar();
  const { pathname } = useLocation();
  const title = PAGE_TITLES[pathname] ?? "Markbot";

  return (
    <header className="safe-x flex h-14 flex-shrink-0 items-center gap-1 border-b border-border bg-background-base px-2 md:hidden">
      <button
        onClick={toggleMobile}
        className="rounded-lg p-2 text-text-secondary transition-colors hover:bg-background-hover hover:text-text-primary"
        aria-label="打开导航菜单"
      >
        <Menu size={20} />
      </button>
      <span className="flex-1 truncate text-sm font-semibold text-text-primary">{title}</span>
      <button
        onClick={onOpenPalette}
        className="rounded-lg p-2 text-text-secondary transition-colors hover:bg-background-hover hover:text-text-primary"
        aria-label={`打开命令面板 (${SHORTCUT_LABEL})`}
      >
        <Search size={18} />
      </button>
    </header>
  );
}

function AppLayout() {
  const { isMobile, toggle } = useSidebar();
  const [paletteOpen, setPaletteOpen] = useState(false);

  // 全局快捷键：Ctrl/⌘+K 命令面板，Ctrl/⌘+B 折叠侧边栏
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.altKey) return;
      const key = event.key.toLowerCase();
      if (key === "k") {
        event.preventDefault();
        setPaletteOpen((prev) => !prev);
      } else if (key === "b" && !isMobile) {
        event.preventDefault();
        toggle();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isMobile, toggle]);

  return (
    <div className="flex h-full bg-background-base text-text-primary antialiased">
      <Sidebar onOpenPalette={() => setPaletteOpen(true)} />
      <div className="flex min-w-0 flex-1 flex-col">
        <MobileHeader onOpenPalette={() => setPaletteOpen(true)} />
        <Routes>
          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="/chat" element={<Page><ChatPage /></Page>} />
          <Route path="/sessions" element={<Page><SessionsPage /></Page>} />
          <Route path="/config" element={<Page><ConfigPage /></Page>} />
          <Route path="/env" element={<Page><EnvPage /></Page>} />
          <Route path="/models" element={<Page><ModelsPage /></Page>} />
          <Route path="/logs" element={<Page><LogsPage /></Page>} />
          <Route path="/skills" element={<Page><SkillsPage /></Page>} />
          <Route path="/cron" element={<Page><CronPage /></Page>} />
          <Route path="/channels" element={<Page><ChannelsPage /></Page>} />
          <Route path="/mcp" element={<Page><McpPage /></Page>} />
          <Route path="/system" element={<Page><SystemPage /></Page>} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </div>
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <ThemeProvider>
        <ToastProvider>
          <SidebarProvider>
            <ChatProvider>
              <AppLayout />
            </ChatProvider>
          </SidebarProvider>
        </ToastProvider>
      </ThemeProvider>
    </BrowserRouter>
  );
}
