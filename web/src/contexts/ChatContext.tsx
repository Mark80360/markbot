import { createContext, useContext, type ReactNode } from "react";
import { useChat } from "@/hooks/useChat";

export type Message = any;
export type SessionInfo = any;

interface ChatContextValue {
  messages: Message[];
  sendMessage: (content: string, files?: File[]) => void;
  isStreaming: boolean;
  clearMessages: () => void;
  currentSessionId: string | null;
  sessions: SessionInfo[];
  switchSession: (id: string) => void;
  deleteSession: (id: string) => void;
  stopStreaming: () => void;
  editAndResend: (serverTimestamp: number, content: string, files?: File[]) => void;
  regenerate: (serverTimestamp: number) => void;
}

const ChatContext = createContext<ChatContextValue | null>(null);

export function ChatProvider({ children }: { children: ReactNode }) {
  const chat = useChat();
  return <ChatContext.Provider value={chat}>{children}</ChatContext.Provider>;
}

export function useChatContext(): ChatContextValue {
  const ctx = useContext(ChatContext);
  if (!ctx) throw new Error("useChatContext must be used within ChatProvider");
  return ctx;
}
