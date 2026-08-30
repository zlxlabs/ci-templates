# review r3：release 对账入锁 + readarray（换视角：独立运行时矩阵探针，最终收口轮）

- 审查对象（H0 冻结）：`edcb68ff36f785455534572b6bb271c2a28c97f9..c704dfe69cd5bca1b5aea6a5fa6d0beb7daf5b3a`，不使用分支名。
- 产物落点：`c704dfe69cd5bca1b5aea6a5fa6d0beb7daf5b3a`（分支 `card/ci-templates-20260831-02` HEAD==H1）。
- 本轮新证据（换家换视角必须换证据源）：**自写 bash 探针**（`/tmp/ci-r3-probe.sh`，fake docker/curl 全新编写，仅手法参考 `tests/test_release_deploy.py`）在真实 bash 进程里执行 `scripts/release_deploy.sh` 的 7 格状态矩阵；外加本人在 base `edcb68f` 临时 worktree 对两条此前未被红验过的新增测试（`:1804`/`:1816`）的红验；HEAD 全量 `102 passed`。r1/r2 verdict 只作登记对照，不当证据。
- 风险档：本仓 AGENTS.md 有「## 风险等级：`internal`」文字段，但**无机读单行 `risk-tier: internal`**（`grep -n '^risk-tier:' AGENTS.md` 无命中），按 internal 处理（P1 = 数据丢失 / 静默出错 / 崩溃 / 越权 / 损坏他人数据）。infra/状态机类收敛提档按 saas：连续 2 轮无新增 P1。**提醒仓主补一行机读声明**（r1/r2 已两次提醒，仍缺）。
- OCR：本轮不重跑（r1 status=partial 的 14 条线索四轴在 r1/r2/本轮各经独立核实，见对照表）。

## 完成条件·行为验收

| 验收项（卡面 spec） | 结论 | 证据 |
|---|---|---|
| 1. 对账在 `do_release` 返回后、`flock -u 9` 前同一持锁进程执行；失败 rc=5，与 1/3/4/130 互斥 | 通过（运行时实证） | `scripts/release_deploy.sh:873-882`；探针 S1 锁探针 `reconcile-lock=held`、S2/S5 rc=5 |
| 2. workflow 薄壳 `reconcile_index == deploy_index + 1`、rc=5 置 `reconcile_failed` 后 exit 5、薄壳 `if: failure()` 转发 | 通过 | `.github/workflows/build-deploy-release.yml:348-394`；契约测试 `tests/test_release_workflow_contract.py` |
| 3. `readarray` 改命令替换 + `if !` + here-string；`compose config --services` 失败如实归因返回非零 | 通过（运行时实证） | `scripts/release_deploy.sh:268-276,281-284,301-304`；探针 S6a rc=1 / S6b rc=5 均含 `compose config --services failed` |
| 4. 锁序（fd8 先 fd9 后）、释放（fd9 先 fd8 后）、busy_deferred 不跑对账、对账判定语义、pull lane 零改动、无静默失败 | 通过 | `scripts/release_deploy.sh:807-887`；探针 S7 exit 3 零对账输出；`git diff --name-only edcb68f..c704dfe` 不含 pull lane 四文件 |
| 5. 「对账在持锁期执行」有顺序断言测试且做过锁序红验 | 通过 | `tests/test_release_deploy.py:1779`；r1 红验 + 本轮对 `:1804`/`:1816` 的补验（见红验段） |

## r3 运行时矩阵探针（7 格）

探针：`bash /tmp/ci-r3-probe.sh`（自写 fake docker/curl；每格独立 mktemp 沙箱；`HEALTHCHECK_*=0/1`、`PULL_RETRIES=1` 加速）。锁探针 = fake docker 在对账 `config --services` 调用内 `flock -n "$HOST_LOCK"` 试拿锁；`lock_free_after` = 脚本退出后探针进程 `flock -n` 复验。

| # | 场景 | 期望 | 实测输出摘要 | 结论 |
|---|---|---|---|---|
| 1 | 正常部署→对账一致 | rc=0，锁在对账后释放 | `rc=0 elapsed=1s lock_free_after=yes`；`lock_during_reconcile=reconcile-lock=held`；`up_d_count=1`；`reconcile passed for all declared images…` | 符合 |
| 2 | 部署成功→对账 mismatch | rc=5，last_good 已推进，锁释放 | `rc=5 elapsed=0s lock_free_after=yes`；`last_good_first_line=abc123456789`；`reconcile mismatch for frontend: expected_id=sha256:frontend, running=frontend=cid-fronten…`；`reconcile assertion failed; deployment may have succeeded…` | 符合 |
| 3 | promote 后重入（last_good==本 SHA） | 跳过 forward/one-shot（无 up -d），直接对账 | `rc=0 lock_free_after=yes`；`up_d_count=0`；`skip forward deploy; reconcile only`；`reconcile passed for all declared images…` | 符合 |
| 4 | promote 前失败→重入 | 完整重放（现状语义） | `run1 rc=4`（无 previous，拒绝伪回滚）；`run2 rc=0 lock_free_after=yes`；`up_d_run1=1 up_d_total=2`；`promoted atomically` | 符合 |
| 5 | 对账 inspect 挂死（`RECONCILE_CMD_TIMEOUT=1`，fake `exec sleep 30`） | 有界返回 rc=5，不挂死，锁释放 | `rc=5 elapsed=2s lock_free_after=yes`；`last_good_first_line=abc123456789`；`reconcile timed out after 1s holding host lock` | 符合 |
| 6 | `compose config --services` 失败 | 归因输出含 compose config 失败（#29），rc 非零 | s6a（forward validate 路径，ONESHOT_SERVICES=migrate）：`rc=1` + `compose config --services failed; compose up will not run`；s6b（reconcile 路径）：`rc=5 lock_free_after=yes` + 同上归因 + `reconcile could not list compose services` | 符合 |
| 7 | busy_deferred（忙锁外部占用） | exit 3，无对账输出 | `rc=3 elapsed=2s`；`service busy: deferred after 2s`；`reconcile_lines=0`；`up_d_count=0` | 符合 |

7 格全部符合期望，**无一格产生 finding**。

## 降层三问（infra 必答，本轮以运行时证据支撑）

### ① 终态写入成功之前已发生哪些不可逆动作？rc=5 时世界处于什么状态？

对账在 `do_release` 成功返回之后才跑（`scripts/release_deploy.sh:873-877`）。rc=5 左侧已完成：pull/retag（staging，`:634-641`，在 fd9 之前）、forward `compose up -d`（含 one-shot/migration，`:538-559`）、探针（`:561-590`）、`last_good_release` 原子推进（promote `:605-610`）及 legacy 视图 best-effort 刷新。rc=5 的世界 = 新版本已探针健康并已 promote、镜像身份未被对账证明、**不自动回滚**。通知如实描述该状态：脚本 `::error`（`:437/:453/:458`）与 workflow 飞书卡片（`.github/workflows/build-deploy-release.yml:528`「部署已成功但对账失败(rc=5)，last_good 已推进到本 SHA，对账不触发自动回滚，需上机核验」）均明示。

探针实证：S2 mismatch 后 `last_good_release` 首行即本 SHA、锁正常释放；S3 证明 promote 后重入（含 255 重试链）跳过 forward/one-shot 只对账——r1 P1-1 的「对账窗口 255 → 重放整次 release（含非幂等 migration）」路径在运行时闭合。S4 证明 promote 前失败的重入仍是完整重放，与现状语义一致。

### ② 守卫用的值在实际部署形态下自身唯一吗？rc=5 读到的容器集合归属谁？

`HOST_LOCK` 由 workflow 固定 `/var/lock/fleet-deploy.lock`（`.github/workflows/build-deploy-release.yml:210`），整机一把：跨仓同 host 不同 `DEPLOY_DIR`、同 `DEPLOY_DIR` 多 caller 两种形态都被 fd9 串行。`BUSY_LOCK_FILE` 按 `DEPLOY_DIR` opt-in，只挡 admission，不是对账身份。对账 `cd "$DEPLOY_DIR"` + 该目录 compose 项目参数，读到的是**当前持 fd9 那次部署自己**的容器集合——S1 锁探针（对账中途从子进程试拿 host lock 失败）实证「对账期间锁确实在本进程手里」，不存在跨 caller 串台窗口（issue #30 的目标）。同 `DEPLOY_DIR` 被两个服务声明违反 README `(host, deploy_dir)` 唯一约束，属存量契约外形态，不计本轮。

### ③ 保护覆盖的是「写入」还是「行为」？锁内挂死靠什么解除？

锁临界区盖住了对账**读取行为**本身。H1 起每次对账 docker/compose 调用经 `reconcile_docker` 套 `timeout --kill-after=1s ${RECONCILE_CMD_TIMEOUT}s`（默认 60s，`:104,:250-258`），超时归一 124 → `return 1` → 顶层 rc=5 → `:882` 解锁，EXIT trap（`:60-61`）与内核关 fd 双兜底。S5 实测：单调用挂死（sleep 30）在 **2s 墙钟内**有界返回 rc=5、锁释放、last_good 保持已推进状态。最坏锁占用 ≈ O(服务数×镜像数) × 61s，有限，**不靠** workflow job timeout 解除——`build-deploy-release.yml` 无 `timeout-minutes`（`rg` 零命中），GitHub 默认 360min 只是兜底背书（存量 backlog，r2 已记）。`BUSY_LOCK_TIMEOUT` 只约束 fd8 admission 等待，与本问无关。

残余已知项：`compose_list_services` 捕获行的 `2>&1` 会把 timeout 专用 `::error::` 归因吞进变量不打印（r2 P2-1，已登记不重复计）；S6b 实测非 timeout 的 config 失败归因（`compose config --services failed`）在该路径仍可见，进程仍以 rc=5 fail-loud。

## Findings（本轮新增）

无。7 格探针全符合、红验成立、全量复读 diff 未发现 r1/r2 未登记的新缺陷。

已登记不重复计：r1 P1-1（255 重放，已修，S3/S4 运行时闭合）、r1 P2-1（无 per-call timeout，已修，S5 闭合）、r2 P2-1（`compose_list_services` 的 `2>&1` 吞 timeout 归因，backlog）、r1 P3（测试锚点脆弱：`.index` 首次命中、`count(...) == 3` 精确计数，backlog）。

存量 backlog 观察（非本 diff，不占循环）：首部署无 previous 时 probe 失败也走 rc=4（`MUTATED==1 && rollback_healthy==0`，`:794-795`），workflow 侧显示为 `rollback_unhealthy`——语义标签与「从未有过可回滚对象」不完全贴合，S4 run1 实测如此；base 既有行为，记 backlog。

## 工具标注 / 本仓判定 / 两问对照表（a-d 四轴）

| 轴 | 工具标注/线索 | 本仓判定（本轮含运行时证据） | P1 两问 |
|---|---|---|---|
| a 持锁 docker 调用无超时（O(N×M) 次） | OCR/r1 P2-1 | **已修并运行时闭合**：`reconcile_docker`（`:250-258`）默认 60s；S5 实测挂死 2s 内有界 rc=5 + 锁释放。残余归因吞没为 r2 P2-1（已登记） | 会触发（daemon 挂）；现有界 fail-loud，可接受。未命中红线 |
| b `readarray <<<` 空输入产生 `[""]` | OCR 线索 | **不成立**：r1/r2 本机实测空串单元素 vs 旧式空数组；三消费方（validate `:286-287`、rollback `:309-310`、reconcile `:345-346`）均 `[[ -n "$svc" ]]` 跳过空串，空服务集在 reconcile 走全-oneshot fail-loud（`:349-352`）。本轮 S1/S3 空 ONESHOT 路径运行时再证无行为差异 | 不构成生产缺陷 |
| c 旧 workflow 对账 3×rc=255 重试语义等价性 | OCR/r1 P1-1 | **已修并运行时闭合**：外层仍对整段 `deploy_once` 重试 255（`:338-385`），但远端 `last_good_release==本 SHA` 时 `do_release` skip forward（`:669-671`）；S3 实测重入零 `up -d`，one-shot 不重放。promote 前 255 完整重放为卡面明确保留语义（S4） | 会触发 skip；后果收敛为只读对账，可接受 |
| d 测试锚点脆弱（`.index` 首次命中、精确计数） | OCR/r1 P3 | **P3 backlog，不重复计**：`tests/test_release_deploy.py:1720` 精确计数、契约测试 `.index` 多处 | 不触发生产；削弱回归检测，低于红线 |

## 红验抽查

临时 worktree 基于 base `edcb68ff36f785455534572b6bb271c2a28c97f9`，**仅拷入**当前 `tests/test_release_deploy.py`，跑本轮新抽的两条（r1 验过 `:1751`/`:1779`、r2 验过 `:1827`/`:1839`，`:1804`/`:1816` 此前未被红验）。

注入生效证明：`grep -c 'reconcile_mismatch_returns_rc5\|reconcile_compose_config_failure_returns_rc5'` 拷入后测试文件 = 2，base 脚本 = 0（base 无 rc=5 对账，测试测的正是本次改动）。

命令与原文输出：

```text
cd /tmp/ci-r3-red.PPboYj && python3 -m pytest -q \
  tests/test_release_deploy.py::test_reconcile_mismatch_returns_rc5_and_keeps_last_good \
  tests/test_release_deploy.py::test_reconcile_compose_config_failure_returns_rc5

_________ test_reconcile_mismatch_returns_rc5_and_keeps_last_good _________
>       assert result.returncode == 5, out
E       AssertionError: [deploy][evidence] probe-attempts url=http://localhost/frontend 200(curl=0)
E         [release] release abc123456789 healthy; promoted atomically
E       assert 0 == 5
tests/test_release_deploy.py:1811: AssertionError
_________ test_reconcile_compose_config_failure_returns_rc5 ______________
>       assert result.returncode == 5, out
E       AssertionError: ...
E       assert 0 == 5
tests/test_release_deploy.py:1821: AssertionError
=========================== short test summary info ============================
FAILED tests/test_release_deploy.py::test_reconcile_mismatch_returns_rc5_and_keeps_last_good
FAILED tests/test_release_deploy.py::test_reconcile_compose_config_failure_returns_rc5
2 failed in 0.63s
```

判据：两条均为 `AssertionError`（`assert 0 == 5`），非 ImportError/AttributeError/SyntaxError；证明 base 上 mismatch 与 config 失败都不会得到 rc=5——测试锁的是本次改动。红验后临时 worktree 已 `git worktree remove` 回收。

HEAD 对照：`python3 -m pytest -q tests/test_release_deploy.py tests/test_release_workflow_contract.py` → `102 passed in 29.49s`。

## 熵增审查

引用 r2「无变化」并复核成立：本 diff 新增面为 `reconcile_release_images`（spec 1 要求对账必须在持锁远端脚本内，单消费者必要性成立）、`reconcile_docker`（timeout 边界语义，消费者 = reconcile 全部调用 + `compose_list_services` 三路径，非转发-only）、`RECONCILE_CMD_TIMEOUT`（单生产消费者 + 测试覆盖写，未做成无主 workflow input）、workflow 薄壳（Checks/`reconcile_index == deploy_index + 1` 契约所需）。无新增持久状态、无第二事实源、无 fallback/重试/防御式 catch；255 重试靠 skip 守卫收口而非新状态机。本轮探针未发现熵 +1 项。

## 收敛判定

infra/状态机类提档按 saas：连续 2 轮无新增 P1。r1 有 P1-1；r2（换家 + H0..H1 增量四问）无新增 P1 = 1/2；本轮 r3（换视角：独立运行时矩阵探针 + 补位红验）无新增 P1 = **2/2，收敛**。r2 P2-1 与 P3 锚点按已登记 backlog 处理，不阻塞。

verdict: pass
