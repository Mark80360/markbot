import { ArrowUp } from "lucide-react";
import { type KeyboardEvent, useRef, useState } from "react";
import { cn } from "@/lib/utils";

interface ChatInputProps {
  onSend: (message: string) => void;
  disabled: boolean;
}

export function ChatInput({ onSend, disabled }: ChatInputProps) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSend = () => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  };

  const handleKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleInput = () => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 200) + "px";
  };

  const canSend = value.trim() && !disabled;

  return (
    <div className="border-t border-border bg-background-base">
      <div className="max-w-3xl mx-auto px-4 py-3">
        <div
          className={cn(
            "flex items-end gap-2 rounded-2xl border px-4 py-2 transition-colors",
            "bg-background-secondary border-border",
            "focus-within:border-border-accent",
          )}
        >
          <textarea
            ref={textareaRef}
            id="chat-input"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            onInput={handleInput}
            placeholder="输入消息..."
            disabled={disabled}
            rows={1}
            className="flex-1 bg-transparent resize-none text-sm outline-none max-h-[200px] py-1 text-text-primary placeholder:text-text-muted"
          />
          <button
            onClick={handleSend}
            disabled={!canSend}
            className={cn(
              "p-2 rounded-xl transition-all flex-shrink-0",
              canSend
                ? "bg-accent-teal text-background-base cursor-pointer"
                : "bg-background-tertiary text-text-muted cursor-not-allowed",
            )}
          >
            <ArrowUp size={16} />
          </button>
        </div>
        <p className="text-xs text-center mt-2 text-text-muted">
          Enter 发送 · Shift+Enter 换行
        </p>
      </div>
    </div>
  );
}
