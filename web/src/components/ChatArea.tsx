import { useEffect, useRef } from "react";
import { ChatMessage } from "@/components/ChatMessage";
import { WelcomeScreen } from "@/components/WelcomeScreen";
import type { Message } from "@/types/chat";

interface ChatAreaProps {
  messages: Message[];
  isStreaming: boolean;
  onSuggestionClick?: (text: string) => void;
  onEdit?: (serverTimestamp: number, newContent: string) => void;
  onRegenerate?: (serverTimestamp: number) => void;
}

function TypingIndicator() {
  return (
    <div className="flex items-start gap-3 message-enter">
      <div className="w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0 bg-accent-teal-dim">
        <span className="text-xs font-bold text-accent-teal">M</span>
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-xs text-display mb-1.5 text-text-tertiary">
          Markbot
        </div>
        <div className="flex items-center gap-1.5 py-1">
          <div className="flex items-center gap-1">
            <span className="inline-block w-2 h-2 rounded-full bg-accent-teal/60 animate-[typing-bounce_1.4s_ease-in-out_infinite]" />
            <span className="inline-block w-2 h-2 rounded-full bg-accent-teal/60 animate-[typing-bounce_1.4s_ease-in-out_0.2s_infinite]" />
            <span className="inline-block w-2 h-2 rounded-full bg-accent-teal/60 animate-[typing-bounce_1.4s_ease-in-out_0.4s_infinite]" />
          </div>
          <span className="text-xs text-text-muted ml-1 animate-pulse">思考中...</span>
        </div>
      </div>
    </div>
  );
}

export function ChatArea({ messages, isStreaming, onSuggestionClick, onEdit, onRegenerate }: ChatAreaProps) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="flex-1 overflow-y-auto">
        <WelcomeScreen onSuggestionClick={onSuggestionClick} />
      </div>
    );
  }

  // Show typing indicator when streaming and the last message is from user (AI hasn't started responding yet)
  const lastMsg = messages[messages.length - 1];
  const showTyping = isStreaming && lastMsg?.role === "user";
  // Also show when the assistant message is empty and streaming (just started)
  const showTypingEmpty = isStreaming && lastMsg?.role === "assistant" && !lastMsg?.content;

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-3xl mx-auto px-4 py-6 space-y-6">
        {messages.map((msg) => (
          <ChatMessage
            key={msg.id}
            message={msg}
            isStreaming={isStreaming}
            onEdit={onEdit}
            onRegenerate={onRegenerate}
          />
        ))}
        {(showTyping || showTypingEmpty) && <TypingIndicator />}
        <div ref={endRef} />
      </div>
    </div>
  );
}
