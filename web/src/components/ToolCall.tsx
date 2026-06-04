import { ChevronDown, ChevronRight, Loader2, CheckCircle2, XCircle, Wrench } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";
import type { ToolCallInfo } from "@/types/chat";

interface ToolCallProps {
  toolCall: ToolCallInfo;
}

export function ToolCall({ toolCall }: ToolCallProps) {
  const [expanded, setExpanded] = useState(false);

  const isRunning = toolCall.status === "running";
  const isCompleted = toolCall.status === "completed";
  const isError = toolCall.status === "error";

  return (
    <div
      className={cn(
        "rounded-lg border text-xs overflow-hidden transition-colors",
        isRunning && "border-accent-teal/30 bg-accent-teal-glow",
        isCompleted && "border-border bg-card",
        isError && "border-destructive/30 bg-destructive/5",
      )}
    >
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-background-hover transition-colors"
      >
        {expanded ? (
          <ChevronDown size={12} className="flex-shrink-0 text-text-tertiary" />
        ) : (
          <ChevronRight size={12} className="flex-shrink-0 text-text-tertiary" />
        )}

        {isRunning && (
          <Loader2 size={12} className="flex-shrink-0 text-accent-teal animate-spin" />
        )}
        {isCompleted && (
          <CheckCircle2 size={12} className="flex-shrink-0 text-success" />
        )}
        {isError && (
          <XCircle size={12} className="flex-shrink-0 text-destructive" />
        )}
        {!isRunning && !isCompleted && !isError && (
          <Wrench size={12} className="flex-shrink-0 text-text-tertiary" />
        )}

        <span className="font-medium text-text-primary truncate">
          {toolCall.name}
        </span>

        {isRunning && (
          <span className="ml-auto text-accent-teal animate-pulse">运行中</span>
        )}
        {isCompleted && (
          <span className="ml-auto text-text-tertiary">完成</span>
        )}
        {isError && (
          <span className="ml-auto text-destructive">失败</span>
        )}
      </button>

      {expanded && (
        <div className="px-3 pb-2 space-y-1.5 border-t border-border/50">
          {toolCall.context && (
            <div>
              <span className="text-text-tertiary">输入: </span>
              <code className="text-text-secondary break-all">{toolCall.context}</code>
            </div>
          )}
          {toolCall.summary && (
            <div>
              <span className="text-text-tertiary">结果: </span>
              <code className="text-text-secondary break-all line-clamp-4">{toolCall.summary}</code>
            </div>
          )}
          {toolCall.error && (
            <div>
              <span className="text-text-tertiary">错误: </span>
              <code className="text-destructive break-all">{toolCall.error}</code>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
