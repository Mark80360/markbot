import { createContext, useContext, useMemo, type ReactNode } from "react";
import { useChat } from "@/hooks/useChat";
import type { Message, SessionInfo } from "@/types/chat";

interface ChatStreamValue {
  messages: Message[];
  isStreaming: boolean;
}

interface ChatSessionsValue {
  sessions: SessionInfo[];
  currentSessionId: string | null;
}

interface ChatActionsValue {
  sendMessage: (content: string, files?: File[]) => void;
  clearMessages: () => void;
  switchSession: (id: string) => void;
  deleteSession: (id: string) => void;
  stopStreaming: () => void;
  editAndResend: (serverTimestamp: number, content: string, files?: File[]) => void;
  regenerate: (serverTimestamp: number) => void;
}

interface ChatContextValue extends ChatStreamValue, ChatSessionsValue, ChatActionsValue {}

const ChatStreamContext = createContext<ChatStreamValue | null>(null);
const ChatSessionsContext = createContext<ChatSessionsValue | null>(null);
const ChatActionsContext = createContext<ChatActionsValue | null>(null);

/**
 * 拆成三个上下文：流式消息每帧都会变化，而会话列表与操作方法基本不变。
 * 合并成一个对象会让侧边栏在每次 token 到达时都重渲染。
 */
export function ChatProvider({ children }: { children: ReactNode }) {
  const chat = useChat();

  const stream = useMemo<ChatStreamValue>(
    () => ({ messages: chat.messages, isStreaming: chat.isStreaming }),
    [chat.messages, chat.isStreaming],
  );

  const sessions = useMemo<ChatSessionsValue>(
    () => ({ sessions: chat.sessions, currentSessionId: chat.currentSessionId }),
    [chat.sessions, chat.currentSessionId],
  );

  const actions = useMemo<ChatActionsValue>(
    () => ({
      sendMessage: chat.sendMessage,
      clearMessages: chat.clearMessages,
      switchSession: chat.switchSession,
      deleteSession: chat.deleteSession,
      stopStreaming: chat.stopStreaming,
      editAndResend: chat.editAndResend,
      regenerate: chat.regenerate,
    }),
    [
      chat.sendMessage,
      chat.clearMessages,
      chat.switchSession,
      chat.deleteSession,
      chat.stopStreaming,
      chat.editAndResend,
      chat.regenerate,
    ],
  );

  return (
    <ChatActionsContext.Provider value={actions}>
      <ChatSessionsContext.Provider value={sessions}>
        <ChatStreamContext.Provider value={stream}>{children}</ChatStreamContext.Provider>
      </ChatSessionsContext.Provider>
    </ChatActionsContext.Provider>
  );
}

function useRequired<T>(context: React.Context<T | null>, name: string): T {
  const value = useContext(context);
  if (!value) throw new Error(`${name} must be used within ChatProvider`);
  return value;
}

/** 只关心消息流与生成状态。 */
export function useChatStream(): ChatStreamValue {
  return useRequired(ChatStreamContext, "useChatStream");
}

/** 只关心会话列表与当前会话。 */
export function useChatSessions(): ChatSessionsValue {
  return useRequired(ChatSessionsContext, "useChatSessions");
}

/** 只关心操作方法（引用稳定）。 */
export function useChatActions(): ChatActionsValue {
  return useRequired(ChatActionsContext, "useChatActions");
}

/** 需要全部聊天能力时的便捷入口。 */
export function useChatContext(): ChatContextValue {
  return { ...useChatStream(), ...useChatSessions(), ...useChatActions() };
}
