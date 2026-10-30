import { Code, Globe, Sparkles, Terminal } from "lucide-react";

interface WelcomeScreenProps {
  onSuggestionClick?: (text: string) => void;
}

const SUGGESTIONS = [
  { icon: Code, text: "帮我写一个 Python 快速排序", hint: "代码生成" },
  { icon: Terminal, text: "解释一下什么是 Docker", hint: "概念讲解" },
  { icon: Globe, text: "搜索最新的 AI 新闻", hint: "联网检索" },
  { icon: Sparkles, text: "你能做什么？", hint: "能力概览" },
];

export function WelcomeScreen({ onSuggestionClick }: WelcomeScreenProps) {
  return (
    <div className="flex min-h-full w-full flex-col items-center justify-center px-4 py-10">
      <div className="mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-accent-teal-dim sm:h-16 sm:w-16">
        <Sparkles size={26} className="text-accent-teal" />
      </div>

      <h2 className="mb-2 text-center text-xl font-bold text-midground blend-lighter sm:text-2xl">
        你好，我是 Markbot
      </h2>
      <p className="mb-7 max-w-md text-center text-sm text-text-secondary">
        一个轻量级的个人 AI 助手框架，可以帮你写代码、检索信息、管理文件，也能接管定时任务与多渠道消息。
      </p>

      <div className="grid w-full max-w-lg grid-cols-1 gap-2.5 sm:grid-cols-2 sm:gap-3">
        {SUGGESTIONS.map((item) => (
          <button
            key={item.text}
            onClick={() => onSuggestionClick?.(item.text)}
            className="flex items-start gap-3 rounded-xl border border-border bg-card p-3 text-left transition-all hover:border-border-accent hover:bg-background-hover active:scale-[0.99]"
          >
            <span className="mt-0.5 flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-background-tertiary">
              <item.icon size={15} className="text-text-tertiary" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-sm leading-snug text-text-secondary">{item.text}</span>
              <span className="mt-0.5 block text-[11px] text-text-muted">{item.hint}</span>
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
