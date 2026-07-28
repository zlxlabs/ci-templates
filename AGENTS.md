# AGENTS.md — ci-templates

给在本仓工作的 AI agent 的项目级约定。人类读 `README.md`（仓库结构、契约、发布流程都在那里，本文不重复）。

## 风险等级：`internal`

本仓按 **`internal`** 档评审。词汇与 CI gate 的 `GATE_TIER` 一致，但注意本仓的 pre-merge 门禁
workflow 在 [`zlxlabs/gate`](https://github.com/zlxlabs/gate) 而不在本仓（见 README「门禁（pre-merge）在哪」），
本仓自身的 CI 只跑 `ci.yml` 里的 pytest。所以这条声明的作用是**给 agent 评审定红线**，
不是配置某个 workflow 的输入。

### 为什么不是 `saas`

本仓不对外提供服务，也不接受不可信输入：所有 caller 都是 org 内仓库，输入经
`workflow_call` 的 `inputs:` 类型化声明，凭据一律走 GitHub secrets 显式注入（6 个，不
`inherit`）。因此注入类（SQLi/XSS/SSRF）、认证/授权缺陷、PII 泄露这三类 `saas` 红线在本仓
没有触发路径，相关意见按 ≤P2 处理。

### 为什么不是 `personal`

本仓是**多仓共享的部署基础设施**，消费者包含他人在用的服务（例如 fordeal 团队的
`one-translate-tts`、`web_transcibe_translate`）。一个部署缺陷可以把错版本推到别人正在用的
服务上、或让回滚静默失效，属于「损坏他人数据」，高于自用工具档。

## 本仓 P1 红线的具体形态

`internal` 档的通用红线是：数据丢失、静默出错（结果错但不报错）、崩溃、越权访问、损坏他人
数据。落到本仓，下面这些是**已知会真实发生**的形态，评审时优先盯：

| 形态 | 说明 |
|---|---|
| 部署报成功但跑的是旧镜像 | retag / compose up 顺序错位，探针探到的是没换掉的旧容器 |
| 回滚声称完成但 `last_good_tag` 未推进 | 下一次回滚会退到错误的目标，故障被放大 |
| 探针假绿 | 期望状态、warmup、重试任一配置失效导致坏版本被判健康 |
| 并发部署互相覆盖 | 每主机 flock 或 `concurrency: deploy-<host>` 任一失效 |
| 凭据出现在日志 | secrets 值被 `echo`/`set -x`/错误信息带出 |

低于本档红线的意见一律 ≤P2。典型例子：`runner` 等字符串输入缺少 enum 校验——reusable
workflow 本就不支持 enum，且拼错会导致**显式**部署失败而非静默出错。

## 改动纪律

- 本仓的爆炸半径与 `v1` 发布流程见 README「版本与爆炸半径」，改 `build-deploy.yml` /
  `build-deploy-release.yml` / `scripts/*.sh` 前必须读。
- `build-deploy.yml` 与 `build-deploy-release.yml` 是**刻意分叉**的两个模板，不是副本。
  给其中一个打的补丁禁止直接搬到另一个：release lane 有多镜像顺序构建、原子发布、飞书
  通知卡片和约 170 行内联 SSH 部署脚本（不调 `scripts/pull_and_deploy.sh`）。
- 契约类改动（secrets 数量、`runs-on`、探针语义、tag 不可变性）都有对应的 `tests/test_*_contract.py`
  锁死。改契约必须同步改测试，不许绕过。
