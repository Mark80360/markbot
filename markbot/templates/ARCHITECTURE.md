---
summary: "系统架构文档"
read_when:
  - 手动引导工作区
  - 需要理解系统结构时
---

# ARCHITECTURE.md

## 系统概览

- 产品: MarkBot — 轻量级 AI Agent 框架
- 主要用户流程: 接收消息 → 构建上下文 → 调用 LLM → 执行工具 → 返回响应
- 运行面: CLI / 多渠道(飞书/钉钉/微信/QQ/邮件) / Cron 调度
- 行为真相源: `AGENTS.md` + `feature_list.json`

## 域图

| 域 | 职责 | 主要入口 | 相关规范 |
|----|------|----------|----------|
| Agent | 核心推理循环、上下文构建、压缩 | `agent/loop.py` | `AGENTS.md` |
| Session | 会话持久化、交接、启动验证 | `session/session.py` | `session-handoff.md` |
| Memory | 长期记忆、每日日志、偏好编码 | `memory/manager.py` | `MEMORY.md` |
| Tools | 工具注册、绑定、执行 | `tools/registry.py` | `TOOLS.md` |
| Skills | 技能加载、条件激活、安全扫描 | `skills/core/registry.py` | 各 `SKILL.md` |
| Channels | 多渠道消息收发 | `channels/manager.py` | 渠道配置 |
| Autopilot | 自动化任务流水线 | `autopilot/service.py` | `feature_list.json` |
| Config | 配置加载、验证、模型解析 | `config/schema.py` | `pyproject.toml` |
| Providers | LLM 提供商适配、降级回退 | `providers/fallback.py` | 模型配置 |

## 层模型

使用固定方向模型，Agent 不得发明临时架构：

`Types → Config → Session → Agent → Tools → Runtime → Channels`

跨层关注点必须通过显式 Provider 或 Adapter 边界进入，而非直接跨层访问。

## 硬依赖规则

- 低层不得依赖高层
- Channels 不得绕过 Agent 或 Session 契约
- 数据访问必须通过 Session Manager 或等效 Adapter
- 共享工具必须保持通用，不得积累域逻辑
- 新依赖应在匹配的计划或设计文档中说明理由

## 跨层接口

| 关注点 | 批准的边界 | 备注 |
|--------|-----------|------|
| 日志和追踪 | `loguru` | 结构化日志，禁止 ad hoc print |
| 认证 | Provider 配置 | API Key 管理 |
| 外部 API | Provider 适配器 | 降级回退策略 |
| 消息总线 | `bus/queue.py` | 异步发布/订阅 |

## 当前热点

- 上下文压缩策略在长会话中的边界情况
- 多渠道并发会话的状态一致性

## 变更检查清单

当触及架构相关代码时：

1. 如果域图或允许边界发生变化，更新本文件
2. 如果推理发生变化，更新 `docs/` 中的相关设计文档
3. 如果规则应被机械执行，添加或更新可执行检查
