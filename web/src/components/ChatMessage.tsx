import { Copy, User } from "lucide-react";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";
import { ToolCall } from "@/components/ToolCall";
import type { Message } from "@/App";

interface ChatMessageProps {
  message: Message;
}

export function ChatMessage({ message }: ChatMessageProps) {
  const [copied, setCopied] = useState(false);
  const isUser = message.role === "user";

  const handleCopy = async () => {
    await navigator.clipboard.writeText(message.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className={cn("flex items-start gap-3 message-enter", isUser && "flex-row-reverse")}>
      <div
        className={cn(
          "w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0",
          isUser ? "bg-user-bubble border border-user-bubble-border" : "bg-accent-teal-dim",
        )}
      >
        {isUser ? (
          <User size={13} className="text-accent-teal" />
        ) : (
          <span className="text-xs font-bold text-accent-teal">M</span>
        )}
      </div>

      <div className={cn("flex-1 min-w-0", isUser && "text-right")}>
        <div className={cn("text-xs text-display mb-1.5 text-text-tertiary", isUser && "text-right")}>
          {isUser ? "You" : "Markbot"}
        </div>

        <div className="relative group inline-block max-w-full text-left">
          {isUser ? (
            <div className="inline-block rounded-xl rounded-tr-sm px-4 py-2.5 text-sm whitespace-pre-wrap bg-user-bubble border border-user-bubble-border text-text-primary">
              {message.content}
            </div>
          ) : (
            <div className="text-sm">
              {message.toolCalls && message.toolCalls.length > 0 && (
                <div className="mb-2 space-y-1.5 max-w-md">
                  {message.toolCalls.map((tc) => (
                    <ToolCall key={tc.toolId} toolCall={tc} />
                  ))}
                </div>
              )}

              {message.content && (
                <div className="prose-chat">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {message.content}
                  </ReactMarkdown>
                  {message.streaming && <span className="typing-cursor" />}
                </div>
              )}

              {!message.streaming && message.content && (
                <button
                  onClick={handleCopy}
                  className="absolute -bottom-1 right-0 opacity-0 group-hover:opacity-100 p-1 rounded-md transition-all bg-background-tertiary border border-border text-text-tertiary hover:text-midground"
                  title="复制"
                >
                  <Copy size={12} />
                </button>
              )}
            </div>
          )}

          {copied && (
            <span className="absolute -top-1 -right-1 text-xs px-1.5 py-0.5 rounded dialog-enter text-accent-teal bg-accent-teal-dim">
              已复制
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
