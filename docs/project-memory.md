# 仓专属事实

本文件收录只在 `zlxlabs/ci-templates` 成立的工作事实，给后续在本仓开工的 agent 会话读。
2026-09-03 从 Claude Code 自动记忆迁出（memory-fleet 分流：仓专属、条目不足 15，不建 `memory/` 目录）。
每条带观测日期；过期了以仓库现况为准，不要当永恒不变式。

## 本仓 PR 检查没有 gate 模型主审

观测于 2026-08-20。

`zlxlabs/ci-templates` 的 PR 检查集合只有 `test` 一个（实测 PR #18/#22/#23/#26 全部如此）。
仓内 `.github/workflows/` 只有 `build-deploy.yml`、`build-deploy-release.yml`、`ci.yml`，
**没有 gate.yml**——gate 早前迁去了 org 级的 `zlxlabs/gate`，本仓没有接进来。

**Why**：全局纪律里「本地漏斗绿 → 标 ready → 等完整 gate → 处理主审 finding」这条收尾节点，
在本仓走到「标 ready」就到头了：ready 前后检查集合完全一样，不存在模型主审兜底。
2026-08-20 处理 issue #24 时按惯例说了「标 ready 触发完整 gate」，实际查 statusCheckRollup
才发现历史 PR 全是单个 `test`，说法当场作废。

**How to apply**：本仓 PR 的质量把关 = lint/test + 主脑自己组织的 review 轮次。
派 review 卡不是「保一手」的可选项，是唯一的模型审查来源，别指望 gate 接住漏网的。
判断检查状态仍用 `gh pr checks <N>` 或 `gh pr view <N> --json statusCheckRollup`
看 conclusion，但在本仓要额外意识到：全绿只证明 test 没坏。

相关：[[exit-code-guard-world-state]]、[[verify-before-attributing]]
