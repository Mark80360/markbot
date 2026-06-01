import { useState } from "react";
import { ChatArea } from "@/components/ChatArea";
import { ChatInput } from "@/components/ChatInput";
import { Sidebar } from "@/components/Sidebar";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { useChat } from "@/hooks/useChat";

export interface ToolCallInfo {
  toolId: string;
  name: string;
  context?: string;
  status: "running" | "completed" | "error";
  summary?: string;
  error?: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: number;
  streaming?: boolean;
  toolCalls?: ToolCallInfo[];
}

export interface SessionInfo {
  id: string;
  title: string;
  messageCount: number;
  lastActive: number;
}

function AppContent() {
  const {
    messages,
    sendMessage,
    isStreaming,
    clearMessages,
    currentSessionId,
    sessions,
    switchSession,
    deleteSession,
  } = useChat();
  const [sidebarOpen, setSidebarOpen] = useState(true);

  return (
    <div className="flex h-full bg-background-base text-text-primary antialiased">
      <Sidebar
        open={sidebarOpen}
        onToggle={() => setSidebarOpen(!sidebarOpen)}
        onClear={clearMessages}
        sessions={sessions}
        currentSessionId={currentSessionId}
        onSwitchSession={switchSession}
        onDeleteSession={deleteSession}
      />
      <main className="flex-1 flex flex-col min-w-0">
        <ChatArea
          messages={messages}
          isStreaming={isStreaming}
          onSuggestionClick={sendMessage}
        />
        <ChatInput onSend={sendMessage} disabled={isStreaming} />
      </main>
    </div>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <AppContent />
    </ThemeProvider>
  );
}
