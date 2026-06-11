export interface ToolCallInfo {
  toolId: string;
  name: string;
  context?: string;
  status: "running" | "completed" | "error";
  summary?: string;
  error?: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: number;
  serverTimestamp?: number;
  streaming?: boolean;
  toolCalls?: ToolCallInfo[];
  media?: string[];
}

export interface SessionInfo {
  id: string;
  title: string;
  messageCount: number;
  lastActive: number;
}
