import { Check, Copy, FileText, Pencil, RotateCcw, User, X, Check as CheckIcon } from "lucide-react";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";
import { ToolCall } from "@/components/ToolCall";
import type { Message } from "@/types/chat";

interface ChatMessageProps {
  message: Message;
  isStreaming?: boolean;
  onEdit?: (serverTimestamp: number, newContent: string) => void;
  onRegenerate?: (serverTimestamp: number) => void;
}

export function ChatMessage({ message, isStreaming, onEdit, onRegenerate }: ChatMessageProps) {
  const [copied, setCopied] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState(message.content);
  const isUser = message.role === "user";

  const handleCopy = async () => {
    await navigator.clipboard.writeText(message.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const handleEditSubmit = () => {
    if (editValue.trim() && message.serverTimestamp && onEdit) {
      onEdit(message.serverTimestamp, editValue.trim());
      setEditing(false);
    }
  };

  const handleEditCancel = () => {
    setEditValue(message.content);
    setEditing(false);
  };

  const handleEditKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleEditSubmit();
    }
    if (e.key === "Escape") {
      handleEditCancel();
    }
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
            <div className="inline-block rounded-xl rounded-tr-sm px-4 py-2.5 text-sm bg-user-bubble border border-user-bubble-border text-text-primary text-left">
              {message.media && message.media.length > 0 && (
                <div className="flex flex-wrap gap-2 mb-2">
                  {message.media.map((url, i) => {
                    const isImage = /\.(png|jpg|jpeg|gif|webp|svg)$/i.test(url);
                    if (isImage) {
                      return (
                        <a key={i} href={url} target="_blank" rel="noopener noreferrer">
                          <img
                            src={url}
                            alt={`attachment ${i + 1}`}
                            className="max-w-[200px] max-h-[200px] rounded-lg border border-user-bubble-border object-cover"
                          />
                        </a>
                      );
                    }
                    return (
                      <a key={i} href={url} target="_blank" rel="noopener noreferrer" className="flex items-center gap-1 text-xs text-accent-teal underline">
                        <FileText size={12} />
                        {url.split("/").pop()}
                      </a>
                    );
                  })}
                </div>
              )}
              {editing ? (
                <div className="min-w-[200px]">
                  <textarea
                    value={editValue}
                    onChange={(e) => setEditValue(e.target.value)}
                    onKeyDown={handleEditKeyDown}
                    className="w-full bg-transparent border border-border-accent rounded-lg p-2 text-sm text-text-primary resize-none outline-none min-h-[60px]"
                    autoFocus
                  />
                  <div className="flex gap-1.5 mt-1.5 justify-end">
                    <button
                      onClick={handleEditCancel}
                      className="p-1 rounded text-text-muted hover:text-text-primary hover:bg-background-tertiary transition-all"
                      title="取消"
                    >
                      <X size={14} />
                    </button>
                    <button
                      onClick={handleEditSubmit}
                      className="p-1 rounded text-accent-teal hover:bg-accent-teal-dim transition-all"
                      title="发送"
                    >
                      <CheckIcon size={14} />
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  {message.content && <div className="whitespace-pre-wrap">{message.content}</div>}
                  {!message.content && message.media && message.media.length > 0 && (
                    <span className="text-text-muted text-xs">附件已发送</span>
                  )}
                </>
              )}

              {!editing && !isStreaming && message.serverTimestamp && onEdit && (
                <button
                  onClick={() => { setEditing(true); setEditValue(message.content); }}
                  className="absolute -bottom-1 right-7 opacity-0 group-hover:opacity-100 p-1 rounded-md transition-all bg-background-tertiary border border-border text-text-tertiary hover:text-midground"
                  title="编辑"
                >
                  <Pencil size={12} />
                </button>
              )}
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

              {message.media && message.media.length > 0 && (
                <div className="flex flex-wrap gap-2 mb-3">
                  {message.media.map((url, i) => {
                    const isImage = /\.(png|jpg|jpeg|gif|webp|svg)$/i.test(url);
                    if (isImage) {
                      return (
                        <a key={i} href={url} target="_blank" rel="noopener noreferrer">
                          <img
                            src={url}
                            alt={`attachment ${i + 1}`}
                            className="max-w-[300px] max-h-[300px] rounded-lg border border-border object-cover hover:opacity-90 transition-opacity"
                          />
                        </a>
                      );
                    }
                    return (
                      <a
                        key={i}
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="flex items-center gap-1.5 text-xs bg-background-secondary border border-border rounded-lg px-3 py-2 hover:bg-background-tertiary transition-colors"
                      >
                        <FileText size={14} className="text-text-muted" />
                        <span className="text-text-primary truncate max-w-[150px]">{url.split("/").pop()}</span>
                      </a>
                    );
                  })}
                </div>
              )}

              {message.content && (
                <div className="prose-chat">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={{
                      code({ className, children, ...props }) {
                        const match = /language-(\w+)/.exec(className || "");
                        const isInline = !match;
                        const codeStr = String(children).replace(/\n$/, "");
                        if (isInline) return <code className={className} {...props}>{children}</code>;
                        const [copied, setCopied] = useState(false);
                        return (
                          <div className="relative group">
                            <div className="absolute right-2 top-2 opacity-0 group-hover:opacity-100 transition-all z-10">
                              <button
                                onClick={async () => {
                                  await navigator.clipboard.writeText(codeStr);
                                  setCopied(true);
                                  setTimeout(() => setCopied(false), 1500);
                                }}
                                className="p-1 rounded bg-background-tertiary border border-border text-text-tertiary hover:text-midground"
                              >
                                {copied ? <Check size={12} /> : <Copy size={12} />}
                              </button>
                            </div>
                            <code className={className} {...props}>{children}</code>
                          </div>
                        );
                      },
                    }}
                  >
                    {message.content}
                  </ReactMarkdown>
                  {message.streaming && <span className="typing-cursor" />}
                </div>
              )}

              {!message.streaming && message.content && (
                <div className="flex items-center gap-1 absolute -bottom-1 right-0 opacity-0 group-hover:opacity-100 transition-all">
                  {message.serverTimestamp && onRegenerate && (
                    <button
                      onClick={() => onRegenerate(message.serverTimestamp!)}
                      className="p-1 rounded-md bg-background-tertiary border border-border text-text-tertiary hover:text-midground"
                      title="重新生成"
                    >
                      <RotateCcw size={12} />
                    </button>
                  )}
                  <button
                    onClick={handleCopy}
                    className="p-1 rounded-md bg-background-tertiary border border-border text-text-tertiary hover:text-midground"
                    title="复制"
                  >
                    <Copy size={12} />
                  </button>
                </div>
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
