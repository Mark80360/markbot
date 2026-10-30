import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { ChatArea } from "@/components/ChatArea";
import { ChatInput } from "@/components/ChatInput";
import { useChatContext } from "@/contexts/ChatContext";

export default function ChatPage() {
  const [searchParams] = useSearchParams();
  const sessionParam = searchParams.get("session");
  const {
    messages, sendMessage, isStreaming, switchSession, currentSessionId,
    stopStreaming, editAndResend, regenerate,
  } = useChatContext();

  // 支持 /chat?session=xxx 深链；sessionParam 变化时才切换，避免每次渲染重复拉取
  useEffect(() => {
    if (sessionParam && sessionParam !== currentSessionId) {
      switchSession(sessionParam);
    }
  }, [sessionParam, currentSessionId, switchSession]);

  return (
    <main className="flex min-w-0 flex-1 flex-col">
      <ChatArea
        messages={messages}
        isStreaming={isStreaming}
        conversationKey={currentSessionId}
        onSuggestionClick={sendMessage}
        onEdit={editAndResend}
        onRegenerate={regenerate}
      />
      <ChatInput
        onSend={sendMessage}
        disabled={isStreaming}
        onStop={stopStreaming}
        isStreaming={isStreaming}
      />
    </main>
  );
}
