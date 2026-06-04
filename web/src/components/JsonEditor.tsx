interface JsonEditorProps {
  value: string;
  onChange: (value: string) => void;
  language?: "yaml" | "json";
  height?: string;
}

export function JsonEditor({ value, onChange, language = "yaml", height = "400px" }: JsonEditorProps) {
  return (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-full font-mono text-sm p-4 rounded-lg border border-border bg-background-secondary text-text-primary focus:outline-none focus:border-accent-teal resize-none"
      style={{ height, minHeight: "200px" }}
      spellCheck={false}
    />
  );
}
