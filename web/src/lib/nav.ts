import {
  Brain, Cable, Clock, FileText, Key, MessageSquare, Monitor,
  Package, Radio, Settings, Sparkles, type LucideIcon,
} from "lucide-react";

export interface NavItem {
  path: string;
  label: string;
  icon: LucideIcon;
  description: string;
}

/** 高频入口：对话与历史会话。 */
export const PRIMARY_NAV: NavItem[] = [
  { path: "/chat", label: "对话", icon: Sparkles, description: "与 Agent 交流" },
  { path: "/sessions", label: "会话管理", icon: MessageSquare, description: "搜索、重命名、导出历史会话" },
];

/** 配置与运维入口。 */
export const ADMIN_NAV: NavItem[] = [
  { path: "/models", label: "模型", icon: Brain, description: "模型链、提供商与采样参数" },
  { path: "/channels", label: "渠道", icon: Radio, description: "钉钉 / 飞书 / QQ / 微信 / 邮件" },
  { path: "/mcp", label: "MCP", icon: Cable, description: "Model Context Protocol 服务器" },
  { path: "/skills", label: "技能", icon: Package, description: "技能库浏览与管理" },
  { path: "/cron", label: "定时任务", icon: Clock, description: "Cron 调度与手动触发" },
  { path: "/logs", label: "日志", icon: FileText, description: "运行日志检索" },
  { path: "/config", label: "配置", icon: Settings, description: "config.json 在线编辑" },
  { path: "/env", label: "密钥", icon: Key, description: "环境变量与 API Key" },
  { path: "/system", label: "系统", icon: Monitor, description: "运行状态与用量统计" },
];

export const ALL_NAV: NavItem[] = [...PRIMARY_NAV, ...ADMIN_NAV];

export const PAGE_TITLES: Record<string, string> = Object.fromEntries(
  ALL_NAV.map((item) => [item.path, item.label]),
);
