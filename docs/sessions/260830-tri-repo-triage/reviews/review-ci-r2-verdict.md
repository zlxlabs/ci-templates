# review r2：release 对账入锁 + readarray（换家复验 + H0..H1 增量四问）

- 审查对象（H0 冻结）：`edcb68ff36f785455534572b6bb271c2a28c97f9..c704dfe69cd5bca1b5aea6a5fa6d0beb7daf5b3a`
- HEAD / 产物落点：`c704dfe69cd5bca1b5aea6a5fa6d0beb7daf5b3a`（H1）
- 本轮新证据（换家必须换证据源）：① H0..H1 修复增量 `a0ce605..c704dfe` 的独立 diff 与对抗探针；② 本机 Bash `readarray <<< ""` / `timeout` 124 vs 137 / `2>&1` 吞 stderr 实测；③ HEAD 上 6 条相关测试绿；④ 在 base `edcb68f` 临时 worktree **只拷入**当前 `tests/test_release_deploy.py` 跑 H1 新增两条测试，得到 AssertionError 原文。r1 verdict 只作对照、不当作证据。
- 风险档：本仓 AGENTS.md 无**机读** `risk-tier:` 行，按 **internal** 处理（P1 = 数据丢失 / 静默出错 / 崩溃 / 越权 / 损坏他人数据）。infra/状态机类收敛提档按 saas：连续 2 轮无新增 P1。**提醒仓主补一行机读声明。**
- OCR：本轮未重跑（r1 已扫 status=partial、14 条线索）。a–d 四轴本轮独立核实，见对照表。

## 完成条件·行为验收

| 验收项 | 结论 | 证据 |
|---|---|---|
| 1. 对账在 `do_release` 返回后、`flock -u 9` 前；失败 rc=5 | 通过 | `scripts/release_deploy.sh:873-880` |
| 2. workflow 薄壳 + rc=5 置 `reconcile_failed`、deploy 不假绿 | 通过 | `.github/workflows/build-deploy-release.yml:348-394` |
| 3. `compose config --services` 失败如实上抛 | 通过 | `scripts/release_deploy.sh:268-272`、`:281-282`、`:301-302`、`:339-341` |
| 4. 锁序 / 忙锁 / 信号 / pull lane 零改动 | 通过 | 全范围 `git diff --name-only` 不含 `pull_and_deploy.sh` / `build-deploy.yml` / `tests/test_workflow_contract.py` / `tests/test_pull_and_deploy.py`；锁序仍 fd8 先 fd9 后、释放 fd9 先 fd8 |
| 5. 同一 SHA forward（含 one-shot）在 255 重试链上至多一次 | 通过（P1-1 已修） | `do_release` `:669-671` skip；测试 `test_promoted_sha_reentry_skips_forward_compose_and_oneshot` |
| 6. 对账 docker/compose 套 timeout，超时 fail-loud 走 rc=5 释放锁 | 结构通过；compose_list_services 路径归因被 `2>&1` 吞掉，见 P2-1 | `reconcile_docker` `:250-257`；caller `:877-879` 映射非零→rc=5 后 `:882` 解锁 |
| 7. 持锁顺序断言 | 通过 | `test_reconcile_runs_while_host_lock_held` |

## H0..H1 增量四问

范围：`a0ce605..c704dfe`（4 files, +96/−169）。**四问均通过，不按新增 P1 计。**

### ① 只修登记在案的 P1-1 / P2-1 吗？

是。增量只做两件事：

- P1-1：`do_release` 在 `previous_sha == D3_RELEASE_TAG` 时跳过 `deploy_group`（含 `compose up` / one-shot），直接 return 0，外层仍走持锁对账。
- P2-1：新增 `RECONCILE_CMD_TIMEOUT`（默认 60）与 `reconcile_docker`；对账路径 docker/compose 调用改走该包装；124/137 在包装内归一成 124，调用方再 `return 1`，顶层映射 rc=5。

其余是配套测试 / 契约收紧、进度文档。无新功能、无 pull lane 改动。

### ② 新增抽象勾稽

- `reconcile_docker`：不是转发-only。它给 GNU `timeout --kill-after=1s` 加了 124/137 归因。生产消费者：`compose_list_services`（validate / rollback / reconcile 三条共用）以及 `reconcile_release_images` 内全部 `config --images` / `ps` / `inspect` / `image inspect`。单消费者必要性：P2-1 要求对账 docker 调用有界，包装比在每个调用点复制 `timeout` 更小。
- 重入判据：不是新状态机，只是 `do_release` 入口对**已有** `previous_sha` 加一道相等守卫。
- `RECONCILE_CMD_TIMEOUT`：生产只被本脚本读取（workflow `remote_cmd` 未下发，走默认 60）；测试覆盖可覆盖写。为修 P2-1 所必需，未做成 workflow 新 input。

### ③ 状态 / 事实源 / fallback 有无依据增加？

没有新持久事实源。skip 读的仍是 `do_release` 原有 `previous_sha` 解析：

1. 若存在 `last_good_release`：第一行 SHA（正则 `^[0-9a-f]{12}$`），这是唯一权威源（`promote` `:607` 同目录 `mv` 原子提交）。
2. **否则**才读 legacy `last_good_sha` + `last_good_manifest`（`:663-666`）。

这条 elif 在 H0 已用于 rollback 目标，H1 没有新开兼容读取。canonical 提交成功、legacy `mv` 失败时，后续 run 仍先看见 `last_good_release`，skip 不看 stale legacy。`promote` 注释（`:612-614`）与实现一致：legacy 是操作员视图，失败只 WARN。

### ④ 双路径检查

读路径双源存在，但是 **H0 已有的 rollback 目标解析**，skip 复用而非新开。写入仍是「canonical 原子提交 → legacy best-effort」，没有第二套 skip 开关、没有「legacy 命中但 canonical 不同仍 skip」的窗口：canonical 文件存在时根本不读 legacy。仅 legacy 存在且 SHA 等于本 tag 时 skip，语义是「旧格式已经把这枚 SHA 记成 last good」，随后对账仍 fail-loud。不构成新 P1。

## 降层三问

### ① 终态写入成功之前已发生哪些不可逆动作？

对账在 `do_release` **成功返回之后**才跑（`:873-877`）。因此 rc=5 的**左侧**已经发生：

| 不可逆动作 | 相对 rc=5 | 位置 |
|---|---|---|
| pull / retag | 左侧；且在拿 fd9 排他锁之前 | `stage_current_release` `:634-640`，调用点 `:808` |
| forward `compose up -d`（**含 one-shot / migration**） | 左侧 | `compose_release` 非 rollback 分支；one-shot 只在 rollback 排除 |
| 探针通过 | 左侧 | `probe_release` |
| `last_good_release` 原子推进到本 SHA | 左侧 | `promote` `:605-610` |
| legacy `last_good_sha` / `last_good_manifest` | 左侧、best-effort | `promote` `:615-619` |

rc=5 时世界状态：新版本已被探针判健康并 promote；镜像身份**未经**对账证明；脚本**不**自动 rollback。通知文案（脚本 `:350/:437/:453/:458` 与 workflow `:371/:393`）写明「last_good 已推进到本 SHA、本次不自动回滚、需上机核验」，与该状态相符。

255 重入（P1-1 修复后）：若 last_good 已是本 SHA，跳过 forward / one-shot，只再对账。one-shot 不会因为对账窗口的 255 再跑一遍。

### ② 守卫用的值在实际部署形态下自身唯一吗？

- `HOST_LOCK` 由 workflow 固定为 `/var/lock/fleet-deploy.lock`（`.github/workflows/build-deploy-release.yml:210`）：整机一把，跨仓、不同 `DEPLOY_DIR` 仍串行。
- `BUSY_LOCK_FILE` 按 `DEPLOY_DIR` opt-in，只挡 admission，不是对账身份。
- 对账 `cd "$DEPLOY_DIR"` + 该目录 compose 项目。跨仓不同 `DEPLOY_DIR`：锁串行，读到的是**当前 caller** 的容器集合。同 `DEPLOY_DIR` 多 caller：共享同一 compose 项目与 `last_good`，README 的 `(host, deploy_dir)` 唯一约束禁止把两个服务写成同一目标。

rc=5 读到的容器归属：当前持 fd9 的那次 `DEPLOY_DIR` compose 项目。

### ③ 保护覆盖的是「写入」还是「行为」？挂死靠什么解除？

锁临界区盖住了对账**读取行为**。H1 给每次对账 docker 调用套了 `timeout --kill-after=1s ${RECONCILE_CMD_TIMEOUT}s`（默认 60s）。超时后包装返回 124，`reconcile_release_images` `return 1`，顶层 `rc=5`，然后 `:882` `flock -u 9`；进程退出时 EXIT trap（`:60-61`）再释放。内核关 fd 是最后兜底。

**解除不靠 job timeout。** `build-deploy-release.yml` 无 `timeout-minutes`（本轮 `rg` 零命中）。GitHub Actions 默认 job 上限 360 分钟，只在 per-call timeout 失效时才成为背书。`BUSY_LOCK_TIMEOUT` 只约束拿 fd8 的等待，不约束已持锁的对账。

最坏锁占用：O(服务数×镜像数) 次调用 × 61s，有限，远小于 360min。r1 P2-1「无 per-call timeout」已修。

## Findings

### Finding P2-1（本轮新）：`compose_list_services` 的 `2>&1` 吞掉 timeout 的 fail-loud 归因

- 严重度：**P2**（未同时命中 internal P1 红线）。
- 违反溯源：H1 追加不变式「对账全部 docker/compose 调用套 timeout，超时 fail-loud 走 rc=5」。进程仍以 rc=5 失败，但 **timeout 专用 `::error::` 在该调用点不可见**，fail-loud 的「原因」被换成泛化的 compose 列表失败。
- 代码证据：`reconcile_docker` 把超时写到 stderr（`:254`）。`compose_list_services` `:268`：

  `services="$(cd "$DEPLOY_DIR" && ... reconcile_docker ... config --services 2>&1)" || config_rc=$?`

  `2>&1` 把包装函数的 stderr 卷进命令替换。随后 `:269` `(( config_rc == 124 )) && return 1` **不打印** `$services`，也不走 `:270-272` 的「compose config --services failed」日志。
- 本机探针（与脚本同构）：`services="$(reconcile_docker ... 2>&1)"` 时 `captured=[::error::release image reconcile timed out after 1s holding host lock]`，真实 stderr 为空。
- 消费方：
  - **reconcile**（`:339-341`）：外层还能打出 `could not list compose services`，rc=5。失败可见，原因像 compose 文件坏了。
  - **validate_oneshot_services**（`:281-282`）/ **rollback_compose_services**（`:301-302`）：没有第二条 `::error::`，forward 路径上 timeout 可能只剩「new release failed before health gate」。
- 其它对账调用（`config --images` / `ps` / `inspect`）**没有** `2>&1`，`test_reconcile_docker_timeout_returns_rc5_and_keeps_last_good` 打在 `inspect` 上，所以绿的是未吞路径。
- P1 两问：
  1. 真实使用下会触发吗？会。`compose_list_services` 是对账第一条 docker 调用，也是 oneshot 校验/rollback 列表的入口；daemon 挂死先撞这里。
  2. 后果能接受吗？作为「非零退出」可以接受（不是静默成功），但不能当作「超时 fail-loud」已在该调用点兑现。未造成数据损坏 / 假绿 / 越权，故 P2。
- 处置方向：对该行去掉 `2>&1`（或超时分支把 `$services` 打到 stderr）；`config_rc==124` 不要静默 `return 1`。不要再加 fallback。

r1 的 P1-1 / P2-1 已修，不重复计。r1 P3 测试锚点脆弱仍在（`tests/test_release_deploy.py:1720` 精确计数、`tests/test_release_workflow_contract.py` 多处 `.index` 首次命中），记 backlog，本轮不新开。

## 全量复验对抗（H1 重点）

**重入边界。** skip 判据是 canonical 第一行 == `D3_RELEASE_TAG`。`promote` 的 commit point 是整文件 `mv`（`:607`），不存在「只写了一半 SHA」的 canonical。legacy 未刷新时 canonical 已在，skip 仍走 canonical，安全。若 last_good 已是本 SHA 但容器不是这枚镜像：skip 后对账 rc=5，**不会**再跑 one-shot，符合 P1-1 修复目标。

canonical 畸形（第一行不是 12 hex）：`previous_sha=""`（`:658-661`），**不 skip**，可能再跑 forward/one-shot。这是「认不出 last good」而非「误 skip」。存量畸形处理，不升 P1。

**`stage_current_release` 在 skip 路径。** `:808` 在 `do_release` 之前无条件 `pull_and_retag`。skip 只跳过 `deploy_group`/`compose up`，不跳过 pull。pull 对不可变 SHA tag 幂等；one-shot 不在 staging 里。`test_promoted_sha_reentry_skips_forward_compose_and_oneshot` 锁死二次 run 无 `up -d`。255 重入若 registry 不可达，会在 skip 之前因 pull 失败以 rc=1 退出、到不了对账——失败可见，不是静默错。不升 P1。

**timeout 124 / 137 与锁。** 本机：`timeout --kill-after=1s 1s sleep 5` → 124；忽略 TERM 的 sleep → 137。包装把两者都变成 return 124。`reconcile_release_images` 各调用点 `(( … == 124 )) && return 1`，顶层只把非零映射成 **rc=5**（`:877-879`）。进程**不会** `exit 124`。workflow `:348` 只把 255 当传输重试，rc=5 走 `reconcile_failed` + `exit 5`，与 124 不撞车。解锁：`:882` 然后 `exit "$rc"`；trap 双保险。

**busy_deferred（exit 3）不跑对账：** `:875` 仅 `rc==0` 才 reconcile。未改。

## 工具标注 / 本仓判定 / 两问对照表

| 轴 | 工具标注或线索 | 本仓判定 | P1 两问 |
|---|---|---|---|
| a 持锁 Docker 无超时 | OCR / r1 P2-1 | **已修**（`reconcile_docker` 默认 60s）。残余：`compose_list_services` 超时归因被吞，见本轮 P2-1；workflow 仍无 `timeout-minutes`（降层③，不升 P1） | 会挂 daemon；现有界失败。残留是误报原因，不是无限占锁 |
| b `readarray <<<` 空输入 | OCR | **不成立**。本机：`readarray -t a <<< ""` → `a=([0]="")` len=1；`< <(true)` / `< <(printf "")` → 空数组。三个 `all_services` 消费方都 `[[ -n "$svc" ]]` 跳过空串；空服务集在 reconcile 走全 oneshot fail-loud（`:349`）。compose 失败发生在 readarray 之前，由命令替换传播 | 不构成生产缺陷 |
| c 旧对账 3×rc=255 | OCR / r1 P1-1 | **已修**。workflow 仍对整段 `deploy_once` 重试 255，但远端 `last_good_release==本 SHA` 时 skip forward。HEAD 测试二次 run 无 `compose up`。promote **前** 255 仍整段重放——卡面明确保留 | 对账窗口 255 不再重复 one-shot。会触发 skip；后果可接受 |
| d `.index` 首次命中 / 精确计数 | OCR / r1 P3 | **P3 backlog，不重复计**。`text.count('readarray -t all_services <<< …') == 3`（`:1720`）；契约测试大量 `.index` 首次命中 | 不触发生产；削弱回归检测 |

## 红验抽查

临时 worktree 基于 `edcb68ff36f785455534572b6bb271c2a28c97f9`，**仅拷入**当前 `tests/test_release_deploy.py`。注入证明：拷贝后的测试文件含 `skip forward deploy` / `reconcile_docker` 各 1 次；base 脚本 0 次。

```text
python3 -m pytest -q \
  tests/test_release_deploy.py::test_promoted_sha_reentry_skips_forward_compose_and_oneshot \
  tests/test_release_deploy.py::test_reconcile_docker_timeout_returns_rc5_and_keeps_last_good
```

原文（两项均为 AssertionError，不是 ImportError / AttributeError / SyntaxError）：

```text
=================================== FAILURES ===================================
_________ test_promoted_sha_reentry_skips_forward_compose_and_oneshot __________
E       AssertionError: assert ('skip forward deploy' in '[deploy][evidence] probe-attempts url=http://localhost/frontend 200(curl=0)\n[deploy][evidence] probe-attempts url=http://localhost/api/health 200(curl=0)\n[release] release abc123456789 healthy; promoted atomically\n')
________ test_reconcile_docker_timeout_returns_rc5_and_keeps_last_good _________
E       AssertionError: ...
E       assert 0 == 5
E        +  where 0 = CompletedProcess(..., returncode=0, ...).returncode
=========================== short test summary info ============================
FAILED tests/test_release_deploy.py::test_promoted_sha_reentry_skips_forward_compose_and_oneshot
FAILED tests/test_release_deploy.py::test_reconcile_docker_timeout_returns_rc5_and_keeps_last_good
2 failed in 0.36s
```

判据：第一条证明 base 无 skip-forward（255 重入仍会再 promote/部署）；第二条证明 base 无对账 timeout→rc=5（inspect 睡眠后脚本仍以 0 成功返回）。红验后临时 worktree 已删除。

HEAD 对照（同两条 + 锁序 / mismatch / 两条契约）：`6 passed in 2.20s`。

## 熵增审查

对照 REFACTOR-guide 坏味道词表：

- `reconcile_docker`：不是无消费者面，也不是转发-only；承载 timeout 边界语义，消费者 ≥3。为修 P2-1 引入，允许。
- skip 守卫：无新文件、无新持久状态、无第二套事实源。
- `RECONCILE_CMD_TIMEOUT`：单生产消费者 + 测试覆盖写；未做成无主 workflow input。投机通用性不成立。
- workflow 薄壳仍是 Checks / `reconcile_index == deploy_index + 1` 契约，不是无消费者转发层。
- 未为 P2/P3 新增 fallback / 重试 / 防御式 catch。255 外层重试仍在，靠 skip 收口而不是再堆状态机。

H0 引入的 `reconcile_release_images` 下沉：必须存在于持锁远端脚本才能满足 spec 1，单消费者必要性仍成立。

## 存量 backlog（不占本轮）

- pull lane 对账仍不持锁（issue #34）。
- workflow 无 `timeout-minutes`（job 默认 360min 只作背书）。
- 测试锚点脆弱（r1 P3）。
- `stage_current_release` 在拿 fd9 排他锁之前 pull（H0 已有，非本 diff 引入）。
- 「未验证 SHA 已进入 last_good，后续失败回滚可能以它为 previous」是 promote-then-reconcile 的既有账本形状，base 独立对账 step 同样如此；r1 已记存量。

## 收敛判定

infra 提档需要连续 2 轮无**新增 P1**。r1 有 P1-1；本轮（换家 + H0..H1 新证据）**无新增 P1**，计数 1/2，尚未收敛。本轮 P2 不阻塞 verdict。

verdict: pass
