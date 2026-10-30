import { ArrowUp, Image, Square, X } from "lucide-react";
import { type KeyboardEvent, useRef, useState } from "react";
import { cn } from "@/lib/utils";

interface ChatInputProps {
  onSend: (message: string, files?: File[]) => void;
  disabled: boolean;
  onStop?: () => void;
  isStreaming?: boolean;
}

const MAX_FILES = 5;
const MAX_CHARS = 20000;

export function ChatInput({ onSend, disabled, onStop, isStreaming }: ChatInputProps) {
  const [value, setValue] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const resetHeight = () => {
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  };

  const handleSend = () => {
    const trimmed = value.trim();
    if ((!trimmed && files.length === 0) || disabled) return;
    onSend(trimmed, files.length > 0 ? files : undefined);
    setValue("");
    setFiles([]);
    resetHeight();
    textareaRef.current?.focus();
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // 中文输入法组词期间的回车不应触发发送
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleInput = () => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`;
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(e.target.files || []);
    setFiles((prev) => [...prev, ...selected].slice(0, MAX_FILES));
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  };

  const canSend = (value.trim().length > 0 || files.length > 0) && !disabled;
  const nearLimit = value.length > MAX_CHARS * 0.9;

  return (
    <div className="safe-bottom flex-shrink-0 border-t border-border bg-background-base">
      <div className="mx-auto max-w-3xl px-3 py-2.5 sm:px-4 sm:py-3">
        {files.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-2">
            {files.map((f, i) => (
              <div
                key={`${f.name}-${i}`}
                className="flex items-center gap-1.5 rounded-lg border border-border bg-background-secondary px-2.5 py-1.5 text-xs"
              >
                <span className="max-w-[120px] truncate text-text-muted">{f.name}</span>
                <button
                  onClick={() => removeFile(i)}
                  className="text-text-muted hover:text-text-primary"
                  aria-label={`移除附件 ${f.name}`}
                >
                  <X size={12} />
                </button>
              </div>
            ))}
          </div>
        )}

        <div
          className={cn(
            "flex items-end gap-1.5 rounded-2xl border px-2.5 py-2 transition-colors sm:gap-2 sm:px-4",
            "border-border bg-background-secondary focus-within:border-border-accent",
          )}
        >
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={disabled || files.length >= MAX_FILES}
            className="mb-0.5 flex-shrink-0 rounded-lg p-1.5 text-text-muted transition-all hover:bg-background-tertiary hover:text-text-primary disabled:opacity-40"
            title={`添加附件（最多 ${MAX_FILES} 个）`}
            aria-label="添加附件"
          >
            <Image size={16} />
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept="image/*,.pdf,.txt,.json,.csv,.xlsx,.docx"
            className="hidden"
            onChange={handleFileSelect}
          />
          <textarea
            ref={textareaRef}
            id="chat-input"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            onInput={handleInput}
            placeholder={isStreaming ? "正在生成中…" : "输入消息…"}
            disabled={disabled}
            rows={1}
            maxLength={MAX_CHARS}
            aria-label="消息输入框"
            className="max-h-[200px] flex-1 resize-none bg-transparent py-1 text-sm text-text-primary outline-none placeholder:text-text-muted disabled:opacity-60"
          />
          {isStreaming ? (
            <button
              onClick={onStop}
              className="flex-shrink-0 rounded-xl bg-destructive p-2 text-destructive-foreground transition-all hover:opacity-90"
              title="停止生成"
              aria-label="停止生成"
            >
              <Square size={16} fill="currentColor" />
            </button>
          ) : (
            <button
              onClick={handleSend}
              disabled={!canSend}
              className={cn(
                "flex-shrink-0 rounded-xl p-2 transition-all",
                canSend
                  ? "bg-accent-teal text-background-base hover:bg-accent-teal-hover"
                  : "cursor-not-allowed bg-background-tertiary text-text-muted",
              )}
              title="发送 (Enter)"
              aria-label="发送消息"
            >
              <ArrowUp size={16} />
            </button>
          )}
        </div>

        <div className="mt-1.5 flex items-center justify-between px-1">
          <p className="hidden text-[11px] text-text-muted sm:block">
            Enter 发送 · Shift + Enter 换行 · 支持图片与文档附件
          </p>
          <p className="text-[11px] text-text-muted sm:hidden">Enter 发送 · 支持附件</p>
          {nearLimit && (
            <span className="text-[11px] text-warning">
              {value.length} / {MAX_CHARS}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
