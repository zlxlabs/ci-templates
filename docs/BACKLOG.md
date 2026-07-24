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
