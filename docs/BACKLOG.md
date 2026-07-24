# BACKLOG

审查中判"接受不修"的事项，附理由。重新评估的触发条件写在各条目里。

## 2026-07-24 · Codex review R1 · P2 · 忙锁临界区内的二次 pull

**现象**：opt-in 门禁路径先在锁外预拉镜像，但拿到 LOCK_EX 后 `do_deploy()` → `deploy_tag()` 仍会再调一次 `pull_image()`。registry 恰在此刻抖动时，重试与退避（≤3 次、退避 10s×n）会在持有忙锁+整机锁期间发生，延长 admission 关闭窗口并阻塞同机其他部署。

**接受理由**：immutable SHA 已预拉，registry 健康时二次 pull 是秒级 no-op；registry 异常时 `pull_image` 有重试上限和本地镜像 fallback，锁内延长**有界**且仅发生在"registry 恰好在部署瞬间不可用"的小概率场景。消除它需要引入"已预拉标记"一类新状态，违反"不为 P2 新增机制"的修复纪律。

**重评触发**：若实际观测到因 registry 抖动导致部署互相阻塞/服务长时间无法接单，再考虑在 deploy_tag 内加"本地已有该 SHA 则跳过 pull"的判断（注意会同时改变未 opt-in 路径的行为，需补测试）。

## 2026-07-24 · 架构 · P3 · 忙锁逻辑在两条 lane 各有一份拷贝

**现象**：`pull_and_deploy.sh`（单镜像 lane）与 `release_deploy.sh`（多镜像 release lane）各自实现了同一套 busy-lock 契约（BUSY_LOCK_FILE/BUSY_LOCK_TIMEOUT、只读 fd、等锁不互持、rc=3 deferred），约 60 行 ×2。

**接受理由**：按「第三个使用者出现前不抽公共库」的既定纪律暂不提取；两条 lane 的部署脚本本就各自独立 scp 到目标机执行，提取公共 shell 库会引入跨文件分发问题。

**重评触发**：出现第三份拷贝，或两份实现开始语义漂移（修了一边忘了另一边）时，提取为共享 shell 片段并在 CI 里加一致性测试。

## 2026-07-24 · Codex review(release lane R2) · P2 · 单镜像 lane 的 ssh env 插值未做 %q 硬化

**现象**：`build-deploy.yml` 部署命令的 env 串（IMAGE_NAME=... DEPLOY_DIR=... 等）沿用单引号插值，未像 release lane 主命令那样全量 %q。输入来自 caller workflow 与 registry（经 schema 校验），信任级别为运维自控。

**接受理由**：pre-existing 模式，输入源可信；本轮已对新增的复核 cat 调用补齐 %q。全量改造涉及整段 env 串重写与回归风险，收益有限。

**重评触发**：caller 输入信任模型变化（如开放给非自控仓库），或该段代码因其他原因重写时顺带 %q 化。

## 2026-07-24 · Codex review(release lane R4) · P2(降级自 P1) · 同 SHA 重建的 digest 漂移无回滚

**现象**：忙锁延期或手动 re-run 同一 commit 时会在新 runner 上重建镜像；若 Dockerfile 基镜像/依赖未钉死，同名 `:sha` tag 可能被不同 digest 覆盖。此时探针失败的"回滚到自身"会被 same-SHA 守卫跳过，坏容器保留。

**接受理由**：SHA-tag 状态模型的系统性局限，单镜像 lane 与 D3 之前的脚本完全相同，非 release lane 引入；彻底修复需 digest 级状态记录与部署（重架构）。缓解手段是保持构建可复现（钉基镜像、锁依赖）。

**重评触发**：真实发生"同 SHA 重跑后探针失败且产物已漂移"事故，或舰队开始使用不可复现构建。届时评估 manifest 记录 digest 并按 digest 部署。

## 2026-07-24 · Codex review(release lane R4) · P2(降级自 P1) · registry/namespace 迁移会破坏回滚引用

**现象**：canonical manifest 只存裸镜像名，回滚用当前 run 的 acr_registry/acr_namespace 重建旧引用；若两次发布间迁移了 registry 且主机本地旧 tag 已被清理，回滚会从错误路径拉取失败。

**接受理由**：需要 registry 迁移 + 本地 tag 被清 + 恰好回滚三重条件；registry 迁移完全是运维自控、低频的计划性动作。

**重评触发**：计划迁移 acr_registry/acr_namespace 前，先改 manifest 存完整镜像引用（或 digest），再执行迁移。

## 2026-07-24 · 减法决策 · 拆除「255 后自动判成功」证明机器（两条 lane）

**背景**：该机器为修复"断连后 deferred 可能反向失实"而建，先后经基线捕获、__unknown__ 守卫、历史同值三条件加固，两轮 review 各被打洞一次，维护面大于价值。

**现语义**：出过 255 的 run 收到 rc=3 一律延期 + 状态不确定告警；重跑幂等收敛，必要时人工在主机上确认。

**重评触发**：若"断连后人工确认"实际频次成为负担，再考虑重建（届时需 run 级 nonce/digest 证明，而非可被历史同值污染的 last-good 比对）。

## 2026-07-24 · 契约债 · CI_TEMPLATES_PAT 已无实际用途但仍在 6-secret 契约中

**现象**：ci-templates 转 public 后 checkout 不再需要 PAT，两条 lane 已停用该 token；但 workflow_call 契约仍声明它为 required——立刻摘除会打破全舰队 caller 的显式 secrets 传递。

**重评触发**：下一次需要动全舰队 caller 的大版本（v2）时，随版本一起从契约与所有 caller 中摘除；届时 test_workflow_contract 的 6-secret 断言同步改 5。
