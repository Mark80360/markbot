import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { ChatProvider } from "@/contexts/ChatContext";
import { SidebarProvider } from "@/contexts/SidebarContext";
import { ToastProvider } from "@/components/Toast";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Sidebar } from "@/components/Sidebar";
import ChatPage from "@/pages/ChatPage";
import SessionsPage from "@/pages/SessionsPage";
import ConfigPage from "@/pages/ConfigPage";
import EnvPage from "@/pages/EnvPage";
import ModelsPage from "@/pages/ModelsPage";
import LogsPage from "@/pages/LogsPage";
import SkillsPage from "@/pages/SkillsPage";
import CronPage from "@/pages/CronPage";
import ChannelsPage from "@/pages/ChannelsPage";
import McpPage from "@/pages/McpPage";
import SystemPage from "@/pages/SystemPage";

function AppLayout() {
  return (
    <div className="flex h-full bg-background-base text-text-primary antialiased">
      <Sidebar />
      <Routes>
        <Route path="/" element={<Navigate to="/chat" replace />} />
        <Route path="/chat" element={<ErrorBoundary><ChatPage /></ErrorBoundary>} />
        <Route path="/sessions" element={<ErrorBoundary><SessionsPage /></ErrorBoundary>} />
        <Route path="/config" element={<ErrorBoundary><ConfigPage /></ErrorBoundary>} />
        <Route path="/env" element={<ErrorBoundary><EnvPage /></ErrorBoundary>} />
        <Route path="/models" element={<ErrorBoundary><ModelsPage /></ErrorBoundary>} />
        <Route path="/logs" element={<ErrorBoundary><LogsPage /></ErrorBoundary>} />
        <Route path="/skills" element={<ErrorBoundary><SkillsPage /></ErrorBoundary>} />
        <Route path="/cron" element={<ErrorBoundary><CronPage /></ErrorBoundary>} />
        <Route path="/channels" element={<ErrorBoundary><ChannelsPage /></ErrorBoundary>} />
        <Route path="/mcp" element={<ErrorBoundary><McpPage /></ErrorBoundary>} />
        <Route path="/system" element={<ErrorBoundary><SystemPage /></ErrorBoundary>} />
      </Routes>
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
