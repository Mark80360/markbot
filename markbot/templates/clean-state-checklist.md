---
summary: "干净状态检查清单"
read_when:
  - 会话结束前
  - 提交代码前
---

# Clean State Checklist

- [ ] 标准启动路径仍然可用（`./init.sh`）
- [ ] 标准验证路径仍然运行（测试/lint/类型检查）
- [ ] 当前进度已记录到 `MEMORY.md`
- [ ] 特性状态反映实际通过 vs 未验证（`feature_list.json`）
- [ ] 没有未文档化的半完成步骤
- [ ] 下次会话可以无需手动修复直接继续
