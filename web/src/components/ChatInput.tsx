import { ArrowUp, Image, X } from "lucide-react";
import { type KeyboardEvent, useRef, useState } from "react";
import { cn } from "@/lib/utils";

interface ChatInputProps {
  onSend: (message: string, files?: File[]) => void;
  disabled: boolean;
}

export function ChatInput({ onSend, disabled }: ChatInputProps) {
  const [value, setValue] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleSend = () => {
    const trimmed = value.trim();
    if ((!trimmed && files.length === 0) || disabled) return;
    onSend(trimmed, files.length > 0 ? files : undefined);
    setValue("");
    setFiles([]);
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

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(e.target.files || []);
    setFiles((prev) => [...prev, ...selected].slice(0, 5));
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  };

  const canSend = (value.trim() || files.length > 0) && !disabled;

  return (
    <div className="border-t border-border bg-background-base">
      <div className="max-w-3xl mx-auto px-4 py-3">
        {files.length > 0 && (
          <div className="flex gap-2 mb-2 flex-wrap">
            {files.map((f, i) => (
              <div
                key={i}
                className="flex items-center gap-1.5 text-xs bg-background-secondary border border-border rounded-lg px-2.5 py-1.5"
              >
                <span className="text-text-muted truncate max-w-[120px]">{f.name}</span>
                <button onClick={() => removeFile(i)} className="text-text-muted hover:text-text-primary">
                  <X size={12} />
                </button>
              </div>
            ))}
          </div>
        )}
        <div
          className={cn(
            "flex items-end gap-2 rounded-2xl border px-4 py-2 transition-colors",
            "bg-background-secondary border-border",
            "focus-within:border-border-accent",
          )}
        >
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={disabled || files.length >= 5}
            className="p-1.5 rounded-lg text-text-muted hover:text-text-primary hover:bg-background-tertiary transition-all flex-shrink-0 mb-0.5"
            title="附件"
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