export function Loading({ text = "加载中..." }: { text?: string }) {
  return (
    <div className="flex items-center justify-center py-12 text-sm text-text-tertiary">
      <div className="w-4 h-4 rounded-full border-2 border-accent-teal border-t-transparent animate-spin mr-2" />
      {text}
    </div>
  );
}
