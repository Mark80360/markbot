import { Check, Copy, FileText, Pencil, RotateCcw, User, X } from "lucide-react";
import { useState, type KeyboardEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { authUrl, cn } from "@/lib/utils";
import { ToolCall } from "@/components/ToolCall";
import type { Message } from "@/types/chat";

interface ChatMessageProps {
  message: Message;
  isStreaming?: boolean;
  onEdit?: (serverTimestamp: number, newContent: string) => void;
  onRegenerate?: (serverTimestamp: number) => void;
}

async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* 非安全上下文会抛错，退回下面的兜底方案 */
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

/** 独立组件：ReactMarkdown 的 code 渲染回调里不能直接调用 Hooks。 */
function CodeBlock({ className, children }: { className?: string; children: React.ReactNode }) {
  const [copied, setCopied] = useState(false);
  const language = /language-(\w+)/.exec(className || "")?.[1];
  const codeStr = String(children).replace(/\n$/, "");

  return (
    <div className="group/code relative">
      <div className="absolute top-1.5 right-1.5 z-10 flex items-center gap-1">
        {language && (
          <span className="rounded bg-background-tertiary px-1.5 py-0.5 font-mono text-[10px] text-text-muted">
            {language}
          </span>
        )}
        <button
          onClick={async () => {
            if (await copyText(codeStr)) {
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            }
          }}
          className="rounded border border-border bg-background-tertiary p-1 text-text-tertiary transition-colors hover:text-midground"
          aria-label={copied ? "已复制代码" : "复制代码"}
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
        </button>
      </div>
      <code className={className}>{children}</code>
    </div>
  );
}

export function ChatMessage({ message, isStreaming, onEdit, onRegenerate }: ChatMessageProps) {
  const [copied, setCopied] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState(message.content);
  const isUser = message.role === "user";

  const handleCopy = async () => {
    if (await copyText(message.content)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
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

  const handleEditKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleEditSubmit();
    }
    if (e.key === "Escape") handleEditCancel();
  };

  return (
    <div className={cn("message-enter flex items-start gap-2.5 sm:gap-3", isUser && "flex-row-reverse")}>
      <div
        className={cn(
          "flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-md",
          isUser ? "border border-user-bubble-border bg-user-bubble" : "bg-accent-teal-dim",
        )}
      >
        {isUser ? (
          <User size={13} className="text-accent-teal" />
        ) : (
          <span className="text-xs font-bold text-accent-teal">M</span>
        )}
      </div>

      <div className={cn("min-w-0 flex-1", isUser && "flex flex-col items-end")}>
        <div className="mb-1.5 text-xs text-display text-text-tertiary">
          {isUser ? "You" : "Markbot"}
        </div>

        <div className="group relative inline-block max-w-full text-left">
          {isUser ? (
            <div className="inline-block max-w-[min(100%,42rem)] rounded-xl rounded-tr-sm border border-user-bubble-border bg-user-bubble px-3.5 py-2.5 text-left text-sm break-words text-text-primary sm:px-4">
              {message.media && message.media.length > 0 && (
                <div className="mb-2 flex flex-wrap gap-2">
                  {message.media.map((url, i) => {
                    const isImage = /\.(png|jpg|jpeg|gif|webp|svg)$/i.test(url);
                    if (isImage) {
                      return (
                        <a key={i} href={authUrl(url)} target="_blank" rel="noopener noreferrer">
                          <img
                            src={authUrl(url)}
                            alt={`附件 ${i + 1}`}
                            loading="lazy"
                            className="max-h-[200px] max-w-[200px] rounded-lg border border-user-bubble-border object-cover"
                          />
                        </a>
                      );
                    }
                    return (
                      <a
                        key={i}
                        href={authUrl(url)}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="flex items-center gap-1 text-xs text-accent-teal underline"
                      >
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
                    className="min-h-[60px] w-full resize-none rounded-lg border border-border-accent bg-transparent p-2 text-sm text-text-primary outline-none"
                    autoFocus
                  />
                  <div className="mt-1.5 flex justify-end gap-1.5">
                    <button
                      onClick={handleEditCancel}
                      className="rounded p-1 text-text-muted transition-all hover:bg-background-tertiary hover:text-text-primary"
                      title="取消"
                      aria-label="取消编辑"
                    >
                      <X size={14} />
                    </button>
                    <button
                      onClick={handleEditSubmit}
                      className="rounded p-1 text-accent-teal transition-all hover:bg-accent-teal-dim"
                      title="发送"
                      aria-label="发送编辑后的内容"
                    >
                      <Check size={14} />
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  {message.content && (
                    <div className="whitespace-pre-wrap">{message.content}</div>
                  )}
                  {!message.content && message.media && message.media.length > 0 && (
                    <span className="text-xs text-text-muted">附件已发送</span>
                  )}
                </>
              )}

              {!editing && !isStreaming && message.serverTimestamp && onEdit && (
                <button
                  onClick={() => {
                    setEditing(true);
                    setEditValue(message.content);
                  }}
                  className="absolute -bottom-1 right-7 rounded-md border border-border bg-background-tertiary p-1 text-text-tertiary transition-all hover:text-midground md:opacity-0 md:group-hover:opacity-100"
                  title="编辑"
                  aria-label="编辑这条消息"
                >
                  <Pencil size={12} />
                </button>
              )}
            </div>
          ) : (
            <div className="text-sm">
              {message.toolCalls && message.toolCalls.length > 0 && (
                <div className="mb-2 max-w-md space-y-1.5">
                  {message.toolCalls.map((tc) => (
                    <ToolCall key={tc.toolId} toolCall={tc} />
                  ))}
                </div>
              )}

              {message.media && message.media.length > 0 && (
                <div className="mb-3 flex flex-wrap gap-2">
                  {message.media.map((url, i) => {
                    const isImage = /\.(png|jpg|jpeg|gif|webp|svg)$/i.test(url);
                    if (isImage) {
                      return (
                        <a key={i} href={authUrl(url)} target="_blank" rel="noopener noreferrer">
                          <img
                            src={authUrl(url)}
                            alt={`附件 ${i + 1}`}
                            loading="lazy"
                            className="max-h-[300px] max-w-full rounded-lg border border-border object-cover transition-opacity hover:opacity-90"
                          />
                        </a>
                      );
                    }
                    return (
                      <a
                        key={i}
                        href={authUrl(url)}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="flex items-center gap-1.5 rounded-lg border border-border bg-background-secondary px-3 py-2 text-xs transition-colors hover:bg-background-tertiary"
                      >
                        <FileText size={14} className="text-text-muted" />
                        <span className="max-w-[150px] truncate text-text-primary">
                          {url.split("/").pop()}
                        </span>
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
                        const isBlock = /language-/.test(className || "");
                        if (!isBlock) {
                          return (
                            <code className={className} {...props}>
                              {children}
                            </code>
                          );
                        }
                        return <CodeBlock className={className}>{children}</CodeBlock>;
                      },
                      a({ href, children, ...props }) {
                        return (
                          <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
                            {children}
                          </a>
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
                <div className="absolute -bottom-1 right-0 flex items-center gap-1 transition-all md:opacity-0 md:group-hover:opacity-100">
                  {message.serverTimestamp && onRegenerate && (
                    <button
                      onClick={() => onRegenerate(message.serverTimestamp!)}
                      className="rounded-md border border-border bg-background-tertiary p-1 text-text-tertiary hover:text-midground"
                      title="重新生成"
                      aria-label="重新生成回复"
                    >
                      <RotateCcw size={12} />
                    </button>
                  )}
                  <button
                    onClick={handleCopy}
                    className="rounded-md border border-border bg-background-tertiary p-1 text-text-tertiary hover:text-midground"
                    title="复制"
                    aria-label="复制回复内容"
                  >
                    <Copy size={12} />
                  </button>
                </div>
              )}
            </div>
          )}

          {copied && (
            <span className="dialog-enter absolute -top-1 -right-1 rounded bg-accent-teal-dim px-1.5 py-0.5 text-xs text-accent-teal">
              已复制
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
