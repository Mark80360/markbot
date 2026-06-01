import { Code, Globe, Sparkles, Terminal } from "lucide-react";

interface WelcomeScreenProps {
  onSuggestionClick?: (text: string) => void;
}

export function WelcomeScreen({ onSuggestionClick }: WelcomeScreenProps) {
  const suggestions = [
    { icon: Code, text: "帮我写一个 Python 快速排序" },
    { icon: Terminal, text: "解释一下什么是 Docker" },
    { icon: Globe, text: "搜索最新的 AI 新闻" },
    { icon: Sparkles, text: "你能做什么？" },
  ];

  return (
    <div className="flex flex-col items-center justify-center h-full px-4">
      <div className="w-16 h-16 rounded-2xl flex items-center justify-center mb-6 bg-accent-teal-dim">
        <Sparkles size={28} className="text-accent-teal" />
      </div>

      <h2 className="text-2xl font-bold mb-2 text-midground blend-lighter">
        你好，我是 Markbot
      </h2>
      <p className="mb-8 text-center max-w-md text-sm text-text-secondary">
        一个轻量级的个人 AI 助手框架，可以帮你写代码、搜索信息、管理文件等。
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 w-full max-w-lg">
        {suggestions.map((item, i) => (
          <button
            key={i}
            className="flex items-center gap-3 p-3 rounded-xl border transition-all text-left bg-card border-border hover:border-border-accent hover:bg-background-hover"
            onClick={() => onSuggestionClick?.(item.text)}
          >
            <item.icon size={16} className="flex-shrink-0 text-text-tertiary" />
            <span className="text-sm text-text-secondary">
              {item.text}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
