import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowDown } from "lucide-react";
import { ChatMessage } from "@/components/ChatMessage";
import { WelcomeScreen } from "@/components/WelcomeScreen";
import type { Message } from "@/types/chat";

interface ChatAreaProps {
  messages: Message[];
  isStreaming: boolean;
  /** 切换会话时重置滚动位置（值变化即视为一次会话切换）。 */
  conversationKey?: string | null;
  onSuggestionClick?: (text: string) => void;
  onEdit?: (serverTimestamp: number, newContent: string) => void;
  onRegenerate?: (serverTimestamp: number) => void;
}

/** 距离底部多少像素以内仍视为"贴底"，避免用户轻微上滑就被判定为离开。 */
const STICK_THRESHOLD = 96;

function TypingIndicator() {
  return (
    <div className="message-enter flex items-start gap-3">
      <div className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-md bg-accent-teal-dim">
        <span className="text-xs font-bold text-accent-teal">M</span>
      </div>
      <div className="min-w-0 flex-1">
        <div className="mb-1.5 text-xs text-display text-text-tertiary">Markbot</div>
        <div className="flex items-center gap-1.5 py-1">
          <div className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 animate-[typing-bounce_1.4s_ease-in-out_infinite] rounded-full bg-accent-teal/60" />
            <span className="inline-block h-2 w-2 animate-[typing-bounce_1.4s_ease-in-out_0.2s_infinite] rounded-full bg-accent-teal/60" />
            <span className="inline-block h-2 w-2 animate-[typing-bounce_1.4s_ease-in-out_0.4s_infinite] rounded-full bg-accent-teal/60" />
          </div>
          <span className="ml-1 animate-pulse text-xs text-text-muted">思考中…</span>
        </div>
      </div>
    </div>
  );
}

export function ChatArea({
  messages,
  isStreaming,
  conversationKey,
  onSuggestionClick,
  onEdit,
  onRegenerate,
}: ChatAreaProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  const [showJump, setShowJump] = useState(false);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "smooth") => {
    const el = scrollRef.current;
    if (!el) return;
    pinnedRef.current = true;
    setShowJump(false);
    el.scrollTo({ top: el.scrollHeight, behavior });
  }, []);

  // 只有用户本来就贴底时才自动跟随，否则会打断向上翻阅历史
  useEffect(() => {
    if (!pinnedRef.current) return;
    endRef.current?.scrollIntoView({
      behavior: isStreaming ? "auto" : "smooth",
      block: "end",
    });
  }, [messages, isStreaming]);

  // 切换会话：回到最新一条
  useEffect(() => {
    if (messages.length === 0) return;
    scrollToBottom("auto");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationKey]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const pinned = distance <= STICK_THRESHOLD;
    pinnedRef.current = pinned;
    setShowJump((prev) => (prev === !pinned ? prev : !pinned));
  };

  if (messages.length === 0) {
    return (
      <div className="flex-1 overflow-y-auto">
        <WelcomeScreen onSuggestionClick={onSuggestionClick} />
      </div>
    );
  }

  const lastMsg = messages[messages.length - 1];
  const showTyping = isStreaming && lastMsg?.role === "user";
  const showTypingEmpty = isStreaming && lastMsg?.role === "assistant" && !lastMsg?.content;

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto overscroll-contain"
      >
        <div className="mx-auto max-w-3xl space-y-5 px-3 py-4 sm:space-y-6 sm:px-4 sm:py-6">
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
          <div ref={endRef} className="h-px w-full" aria-hidden="true" />
        </div>
      </div>

      {showJump && (
        <button
          onClick={() => scrollToBottom("smooth")}
          className="dialog-enter absolute bottom-4 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-border bg-background-base/95 px-3.5 py-2 text-xs text-text-secondary shadow-lg backdrop-blur transition-colors hover:border-border-accent hover:text-text-primary"
          aria-label="回到最新消息"
        >
          <ArrowDown size={13} />
          最新消息
        </button>
      )}
    </div>
  );
}
