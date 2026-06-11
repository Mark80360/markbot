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
        {isStreaming && messages[messages.length - 1]?.role === "user" && (
          <div className="flex items-start gap-3 message-enter">
            <div className="w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0 bg-accent-teal-dim">
              <span className="text-xs font-bold text-accent-teal">M</span>
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-xs text-display mb-1.5 text-text-tertiary">
                Markbot
              </div>
              <div className="flex items-center gap-2">
                <div className="w-1.5 h-1.5 rounded-full bg-accent-teal animate-pulse" />
                <div className="w-1.5 h-1.5 rounded-full bg-accent-teal animate-pulse [animation-delay:0.2s]" />
                <div className="w-1.5 h-1.5 rounded-full bg-accent-teal animate-pulse [animation-delay:0.4s]" />
              </div>
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}
