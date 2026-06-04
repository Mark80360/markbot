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
  } = useChatContext();

  useEffect(() => {
    if (sessionParam && sessionParam !== currentSessionId) {
      switchSession(sessionParam);
    }
  }, [sessionParam]);

  return (
    <main className="flex-1 flex flex-col min-w-0">
      <ChatArea
        messages={messages}
        isStreaming={isStreaming}
        onSuggestionClick={sendMessage}
      />
      <ChatInput onSend={sendMessage} disabled={isStreaming} />
    </main>
  );
}
