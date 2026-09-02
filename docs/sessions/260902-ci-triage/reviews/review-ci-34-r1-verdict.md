verdict: fail

# review r1：pull lane 对账下沉 host 锁内（PR #38，rc=5）

审查对象冻结为 `9131e7b30b32cb7266e4eb6be89c3fb24de294ea..822bcf6c5c67d968d04c0bbbfde6c5874115ed53`（H0），不随分支名漂移。风险档 **internal**（`AGENTS.md`「风险等级」）；改动核心是失败路径/锁临界区，按 core-lead.md infra 例外用 **saas 收敛条件**（连续 2 轮无新增 P1），本轮必做降层三问。

## 本轮方向与覆盖清单

第 1 轮，方向 = **正向全量**（PR #38 正文每句是否兑现）+ **降层三问** + **一条反向**（`timeout --kill-after=1s` 把 137 映射成 124，但 `docker compose ps` 自身 OOM 137 也会被当成超时——是否可接受）。

本轮新证据（开启本轮的外部证据，不是同一份 diff 重读）：

- H0 临时 worktree `/tmp/review-ci34` 跑 `uv run --with pytest,pyyaml,jsonschema python -m pytest -q tests/test_pull_and_deploy.py tests/test_workflow_contract.py`：**94 passed in 26.46s**。
- base `9131e7b` 临时 worktree 仅拷入当前两份测试文件后抽 3 条：**3 failed / AssertionError**（见「红验抽查」），证明这些断言锁的是本次改动。
- 本机 GNU coreutils 9.4 实测 `timeout` 退出码：TERM 超时 → **124**；`--kill-after` 走 SIGKILL → **137**；`timeout` 包装下子进程 `kill -9 $$` → **137**。
- `ocr-review` status=**reviewed**（primary MiniMax-M3，`primary_selected`），10 条 findings，复核腿不可用（`unverified`）。每条按本仓 P1 两问重判，见对照表；**没有把 skipped 说成扫过**。
- 对照物：已合并 #35（`1774c19e`，含 `c704dfe`「已 promote 的 SHA 重入只对账」）的 `scripts/release_deploy.sh:669-672` 与 `build-deploy-release.yml` 的 255 边界。

已覆盖的问题清单：

1. PR 正文每句是否落地（对账位置、三段语义、rc=5、timeout、oneshot 过滤/全覆盖拒绝、薄壳、不新增 required input、README、测试）。
2. 降层三问：rc=5 终态与通知分流；`latest_id`/`running_match` 在多副本与同 host 多 `DEPLOY_DIR` 下是否仍指向本次部署；fd9/fd8 释放顺序。
3. 反向：137 → 124 映射是否把 OOM 误标成超时。
4. 熵增：`reconcile_docker`、`RECONCILE_CMD_TIMEOUT`、`compose_args=(compose)`、workflow 薄壳；与 release lane 是否该抽共享。
5. #35 定稿对齐：是否漏了「last_good 已是本 SHA 则 skip forward、只对账」这条为堵住对账期 255 重放而加的守卫。

## 正向：PR 正文兑现

| PR 正文句子 | 结论 | 证据 |
|---|---|---|
| `reconcile_deployed_image()` 在 `do_deploy` 返回 0 之后、`flock -u 9` 之前 | 兑现 | `scripts/pull_and_deploy.sh:572-581` |
| 三段语义：expected SHA / `IMAGE_NAME:latest` / running 容器 image ID | 兑现 | `:340-416`；`running_match=1` 仍是「至少一个」 |
| 对账失败 rc=5；探针已过、`last_good` 已推进、不自动回滚、不重试 rc=5 | 脚本与 workflow 的 **rc=5 分支**兑现；**255 重试会重放整次部署**，见 P1-1 | `:574-578`、`:341-344`；255 循环仍在 `:346-390` |
| `reconcile_docker` + `RECONCILE_CMD_TIMEOUT` 默认 60s，超时当对账失败 | 兑现 | `:54`、`:75-78`、`:296-304` |
| `compose ps` 带非 oneshot 服务过滤；全覆盖拒绝 | 代码兑现；pull 测试只锁了「带服务名、不是裸 ps」，**没有全覆盖拒绝用例** | `:327-338`、`:1410-1411` |
| Deploy step 识别 rc=5 写 `reconcile_failed` 并 `exit 5`；Reconcile 改薄壳 | 兑现 | `build-deploy.yml:341-344`、`:393-400` |
| 不新增 required input | 兑现 | `oneshot_services` 仍 default `""` |
| README 退出码表补 rc=5 与对账位置说明 | **位置说明有，表没有** | 对账节 `:315-323` 已写 rc=5；退出码表 `:213-220` 仍无 `rc=5` 行。见 P2-1 |
| 契约测试 + 锁序断言 + mismatch/pass/timeout | 兑现 | `tests/test_workflow_contract.py:213-269`；`tests/test_pull_and_deploy.py:1399-1479` |

issue #34 三条完成条件（对账在 `flock -u 9` 前、契约断言随新结构更新、持锁顺序断言）结构上成立；P1 来自下沉后与既有 255 重试的交互，不是这三条字面落空。

## 降层三问

### ① 终态写入之前已发生哪些不可逆动作；rc=5 世界状态有没有交代给人

`do_deploy` 成功返回之前已经做完：`deploy_tag`（pull / retag `${IMAGE_NAME}:latest` / `compose up -d`，forward **不排除** oneshot，`:247-260`）→ `health_probe` 通过 → **写入 `last_good_tag`**（`:435-439`）才 `return 0`。对账从 `:574` 才开始。因此 rc=5 时：

- 生产容器已换成新 SHA（或至少 compose 认为已 up）；
- `last_good_tag` **已经**是本次 `GIT_SHA`；
- host 锁此时仍持有（`:581` 才 `flock -u 9`）；
- **不**走 `deploy_tag "$prev_good"` 回滚。

这与 PR 正文、脚本头注释 `:14-15`、Deploy step 注释 `:272-273` 一致。

通知分流：

| 通道 | rc=5 时实际行为 | 是否把「新版本在跑、不回滚」讲清楚 |
|---|---|---|
| Deploy step `::error::` | `:343`「deployment may have succeeded, but production image identity is not proven」 | 清楚 |
| Reconcile 薄壳 `::error::` | `:399` 同文案；`if: failure() && reconcile_failed` | 清楚；step 仍叫 Reconcile，飞书「按红掉的 step 分流」才对得上 |
| 飞书普通 P0 红卡 | `:408-409`：`failure() && deferred != true && rollback_unhealthy != true` → **会发**、`@全员` | 卡内 **有**「Reconcile 步骤红…无自动回滚…必须上机核对」（`:442-444`） |
| README 退出码表 | 表无 rc=5 行 | 不清楚，见 P2-1 |

缺口：rc=5 时 **Deploy 与 Reconcile 两个 step 都红**。飞书第一句「**Deploy 步骤**红:未部署成功；若是探针失败,已按探针门**自动回滚到 last_good**」（`:440-441`）在这条路径上是错的——Deploy 红的原因是对账，不是探针失败，也没有回滚。第二句 Reconcile 才是对的。见 P2-2。黄卡只绑 `deferred`，不会误收 rc=5。

### ② 守卫用的值在真实部署形态下是否唯一

workflow 把 `HOST_LOCK` 固定成 `/var/lock/fleet-deploy.lock`（`build-deploy.yml:296`），与 release lane 同一把整机锁，跨仓同 host 仍串行。对账里 `cd "$DEPLOY_DIR"` + `compose ps … "${non_oneshot_services[@]}"`（`:361-362`）绑定的是 **当前 compose 项目**，不是宿主机上所有容器。

- **`deploy.replicas>1`**：`running_match` 在任一容器 image ID == `expected_id` 时置 1（`:370`），与 PR 正文「至少一个运行容器」一致。混合新旧副本（滚动未完成/部分失败）可以过闸。这不是本次新引入的语义，记 backlog，不升 P1。
- **同 host 多个 `DEPLOY_DIR` 共用 `IMAGE_NAME:latest`**：`latest_id` 来自 daemon 全局 tag（`:350`），不是目录作用域。持 fd9 期间本次 `deploy_tag` 刚 retag 过，对账读到的 `latest` 仍是这次的 SHA；对账结束后下一仓才能拿到同一把 host 锁再 retag。文档化形态下（README `(host, deploy_dir)` 唯一、各服务不同 `image_name`）判定指向本次部署。若两个服务故意共用同一个 `IMAGE_NAME`，属于既有 caller 契约问题，不是本 diff 新开的洞。
- **oneshot 过滤**：把长期服务误写入 `oneshot_services` 会让对账不再要求它 running（与 release 相同耦合）。input description 仍写「skipped on rollback only」（`:84`），与新行为不完全一致，记 P3。

### ③ 保护覆盖的是「写入」还是「行为」；`flock -u 8` 是否仍晚于 9

对账是 **读取行为**（inspect / `compose ps`），放在 `do_deploy` 返回后、`flock -u 9` 前（`:572-581`），所以覆盖的是「持锁期内的对账读」，不是新的写入。每条 docker 调用有 `reconcile_docker` 超时（#35 修过的那类挂死），挂死会 rc=5 并释放锁，而不是占到 job 默认 360 分钟。

忙锁顺序：注释 `:581-587` 写明「fd 8 必须活过整个 `do_deploy()`…并且 **晚于 fd 9 释放**」。代码是 `flock -u 9`（`:581`）然后 `[ -n "$BUSY_LOCK_FILE" ] && flock -u 8`（`:587`）。对账夹在两次解锁之前，因此 **对账期间 fd8 与 fd9 都还在**；8 仍晚于 9。opt-out（无 `BUSY_LOCK_FILE`）不打开 fd8，只有 9。这一问通过。

## 反向：137 映射成 124，OOM 也会被标成超时——可接受吗

`reconcile_docker`（`:296-304`）把 `rc == 124 || rc == 137` 都打印「timed out after … holding host lock」并 `return 124`。这是从 #35 `release_deploy.sh:250-257` 原样对齐的。

本机实测（coreutils 9.4）：

- `timeout --kill-after=1s 0.2s sleep 5` → **124**（SIGTERM 已足够）；
- `timeout --kill-after=1s 0.2s bash -c 'trap "" TERM; sleep 8'` → **137**（必须 SIGKILL 才死）；
- `timeout 5s bash -c 'kill -9 $$'` → **137**（子进程自己被 SIGKILL，**包括 OOM killer**）。

所以 `137` 分支 **不是多余的**：忽略 TERM 的 docker/compose 挂死走 `--kill-after` 时，timeout 本身就返回 137，不映射就会掉进「compose ps 失败」而不是「持锁超时」。代价是：compose 被内核 OOM 杀掉（同样 137）也会打超时文案。

**可接受。** 两条路径都是对账失败 → rc=5 → 不回滚、不上绿；人仍然必须上机。错的只是归因文案。不会假绿，不命中 internal P1 红线。记 backlog：若以后要区分，应在映射前看 timeout 是否真的到期，而不是再加一条失败码。

OCR F1 声称 timeout 杀掉 `compose ps` 后半截 stdout 会进入 `running_match` 循环。代码上 124 走 `else` 并立刻 `return 1`（`:381-383`），**不会**进入 `:365` 的 read 循环。该条不成立。

## 熵增审查

| 新增物 | 是否熵 +1 | 裁决 |
|---|---|---|
| `reconcile_docker` 包装 | 表面上是转发层，但承载 **timeout + 124/137 归一** 的边界语义，不是空转发 | 保留。issue #34 已写明两 lane 对账语义不同（三段 vs 两段、有无 latest），**不值得**抽成共享库；与 `release_deploy.sh:250` 同名复制是有意的所有权隔离 |
| `RECONCILE_CMD_TIMEOUT` | 新环境变量 | 有真实消费者（对账每条 docker 调用 + 测试 `RECONCILE_CMD_TIMEOUT=1`）。#35 曾因持锁 docker 无超时记 P2，这是那条的对齐，不是投机开关 |
| `compose_args=(compose)` | 轻微熵 +1 | pull lane **从未**往数组里追加 `--env-file`（release 会，`:354-359`）。此处只是 `compose` 一个词的数组。可内联；不阻塞。P3 |
| workflow 薄壳 step | 不是无消费者转发 | 飞书卡按 step 名分流、契约 `reconcile_index == deploy_index + 1` 都依赖它还在。progress 记录已否决「删 step」 |

未发现为 P2/P3 新造的 fallback/重试机器。P1-1 恰恰是 **已有** 外层 255 重试边界被对账下沉扩大，应对齐 #35 做减法式重入跳过，而不是再加一层重试。

## Findings

### P1-1：对账下沉进同一条 SSH 后，rc=255 会重放整次 `pull_and_deploy.sh`，且没有 #35 的「已 promote 则 skip forward」

- **位置**：`scripts/pull_and_deploy.sh:426-439`（`do_deploy` 无条件 `deploy_tag` + 先写 `last_good`）、`:572-578`（对账在同进程、同把 fd9 里）；`.github/workflows/build-deploy.yml:289-390`（`deploy_once` 含整份远端脚本，外层对 255 最多再跑 2 次）。对照 #35 定稿 `scripts/release_deploy.sh:669-672`（`c704dfe`，随 `1774c19e` 合并）：`previous_sha == D3_RELEASE_TAG` → `skip forward deploy; reconcile only`；测试锁在 `tests/test_release_deploy.py:1827-1836`。
- **违反**：PR 正文「对齐 #35 形态」+ issue #34「按其定稿形态对齐实现」；infra 不变式「对账失败不重试部署」。base 的独立 Reconcile step 对 255 只重试 **只读** heredoc（`9131e7b:.github/workflows/build-deploy.yml` 约 386-515 行），失败不会再 `compose up`。本次把对账塞进 `deploy_once` 之后，255 落在「`last_good` 已写、对账未完成」窗口会重放 forward `compose up -d`（`:259`，**含 oneshot/migrate**）。
- **复现/推理**：第一次 SSH 跑完探针并 `echo "$GIT_SHA" > last_good_tag` 后，对账的 `inspect`/`compose ps` 期间连接被 reset（workflow 自己写过 tailnet `Connection reset`、keepalive 15s×4）。ssh 返回 255 → 外层再 scp + 再跑脚本。第二次 `do_deploy` 读到的 `prev_good` **已经是本次 SHA**（`:431`），仍会 `deploy_tag`（再 up、再跑 migrate）。若这次探针失败，`:467` 因 `prev_good == GIT_SHA` 拒绝回滚，走 rc=4「生产可能不可用」。
- **工具标注 / 本仓判定 / 两问答案**：
  - 工具标注：OCR 未抓到（OCR F7 甚至反向抱怨「对账不再重试」）；本轮独立对照 #35 `c704dfe` 与 base 第二条 ssh 得出。
  - 本仓判定：**P1**（internal 红线：损坏他人数据；亦可能把已上线的新版本在重试探针失败时升级成 rc=4 紧急卡且无法回滚）。
  - 两问：①真实使用下会触发吗？会。255 重试是生产路径（`build-deploy.yml:261-262,388`）；对账把 SSH 再延长最多 `RECONCILE_CMD_TIMEOUT`×多次 docker 调用；forward `compose up -d` 不排除 migrate（examples/caller-workflow.yml 把 `oneshot_services: migrate` 标成可选，且 **即使不声明 oneshot，migrate 服务仍在无参数 `up -d` 里**）。本机 `gh` 代码搜索 org 内 caller 返回 404/空，不能据此当「没有消费者」。②后果能接受吗？不能。非幂等 migration 会被同一 run 跑第二遍；即使没有 migrate，重试探针失败会 rc=4 且 `last_good` 已是未完成对账的 SHA。#35 用 skip-forward 堵住的就是这个窗口，本 PR 漏抄。
- **建议修法一句**：对齐 `release_deploy.sh:669-672`——若 `last_good_tag` 已是本次 `GIT_SHA`，skip `deploy_tag`/`health_probe`，只跑 `reconcile_deployed_image`；补一条与 `test_promoted_sha_reentry_skips_forward_compose_and_oneshot` 对称的 pull 测试。

### P2-1：README 退出码表没有 rc=5，rc=0 仍只写「通过健康探针」

- **位置**：`README.md:213-220`（「on-call 处置以这张表为准」）；对账节 `:315-323` 有 rc=5 叙述。diff 只改了对账节和 A3 表，没改退出码表。
- **违反**：PR 正文「`README.md`：退出码表补 rc=5 与对账位置说明」。
- **复现/推理**：打开 README 退出码表看不到 rc=5；rc=0 行仍暗示「探针过 = 成功」，与现在 rc=0 = 探针过 **且** 对账过不一致。
- **工具标注 / 本仓判定 / 两问**：OCR 未提。本仓 **P2**。①会触发：人按表值班。②后果：分流文案在飞书卡和对账节里有，不至于静默当成功；未达 P1 红线。
- **建议**：表加 rc=5 行（新版本在跑、`last_good` 已推进、不上自动回滚、立即上机核对身份）；rc=0 改为「探针过且三段对账过」。

### P2-2：rc=5 双红时，飞书普通红卡第一句仍把「Deploy 红」说成已回滚

- **位置**：`.github/workflows/build-deploy.yml:437-444`。rc=5 时 Deploy `exit 5` 且薄壳 Reconcile 也红。
- **违反**：降层三问①「通知必须把 rc=5 世界状态交代给人」；卡注释 `:433-436` 自己也怕 on-call 读成「未上线不用管」。
- **复现/推理**：只看飞书、不点进 Run 时，第一句「Deploy 步骤红 → 已自动回滚」与事实相反。第二句 Reconcile 是对的，两句并列互相打架。
- **工具标注 / 本仓判定 / 两问**：OCR 未提（F5 是 GITHUB_OUTPUT 写入失败，另判）。本仓 **P2**。①会触发：每条 rc=5 都发这张卡。②后果：有 @全员 和第二句正确指引，Deploy step 日志里的 `::error::` 也不说已回滚；误判风险在人，不是假绿。不升 P1。
- **建议**：rc=5 走独立卡，或第一句改成「只看红的 **Reconcile** step；Deploy 红且带 reconcile_failed 时不要按已回滚处置」。

### P3（不阻塞）

- **P3-1** `compose_args=(compose)` 在 pull lane 无第二消费者（无 `--env-file`），可内联。熵审查。
- **P3-2** 全 oneshot 拒绝（`:335-338`）无 pull 测试；契约只断言 `"${non_oneshot_services[@]}"`。缺测试，代码路径在。
- **P3-3** `oneshot_services` input description 仍写「skipped on rollback only」（`:84`），未提对账 skip 范围。
- **P3-4** OCR F0：超时与断言都映射 rc=5——这是设计（脚本已有 timeout 专文案）。不成立为缺陷。
- **P3-5** OCR F5：`GITHUB_OUTPUT` `|| ::warning::` 失败时薄壳不跑。Deploy 仍 `exit 5` 且 `::error::` 文案正确，飞书仍发。CI 输出文件写失败属罕见畸形，≤P2 纪律下不进 P1；接受现状（deferred/rc=4 同一写法）。

## 工具标注 / 本仓判定 / 两问对照表

| 来源 | 工具标注 | 本仓判定 | P1 两问 |
|---|---|---|---|
| 独立对照 #35 `c704dfe` | （OCR 漏）对账期 255 重放整次 deploy | **P1-1** | 会：255 是生产重试；不可接受：migrate 重跑 / rc=4 且无法回滚 |
| OCR F1 high | timeout 半截 stdout 进入 running 循环 | **不成立** | 124/137 在 `:381-383` 直接 return，不进 read 循环 |
| OCR F5 high | GITHUB_OUTPUT 写失败导致薄壳不跑、保护被吞 | **P3-5**（不升 P1） | 极少触发；Deploy 仍 fail-loud + 正确 `::error::` |
| OCR F0/F7 | 超时折叠进 rc=5 / 对账不再 255 重试 | 设计如此；F7 说反了 | 不构成假绿 |
| OCR F2/F3/F4/F6/F8/F9 | 锁、长度、notice、schema_hint、重复 `::error::`、if 耦合 | ≤P3 或否决 | 无数据损坏/静默错 |
| README 表 | （OCR 漏） | **P2-1** | 会误导值班表；飞书/对账节有补，不假绿 |
| 飞书双红文案 | （OCR 漏） | **P2-2** | 每条 rc=5 触发；第二句正确，不假绿 |

## 红验抽查

base worktree `/tmp/review-ci34-base` @ `9131e7b`，仅拷入 H0 的 `tests/test_pull_and_deploy.py` 与 `tests/test_workflow_contract.py`：

```text
FAILED test_reconcile_runs_while_host_lock_held
  AssertionError: 'image reconcile starting (host lock still held)' not in deploy-healthy log
FAILED test_reconcile_mismatch_returns_rc5
  assert 0 == 5   # base 探针过后直接 rc=0，脚本内无对账
FAILED test_post_deploy_image_reconciliation_is_success_only_and_checks_all_layers
  assert "success() && … deferred != 'true'" == "failure() && … reconcile_failed == 'true'"
3 failed in 0.58s
```

失败类型均为 AssertionError（不是 ImportError/注入未生效）。这 3 条锁的是本次改动。

H0 行为测试：94 passed / 26.46s。

## OCR 前置

`ocr-review` status=**reviewed**（minimax / MiniMax-M3，`primary_selected`），10 findings，复核器超时故全部 `unverified`。已逐条核实，见上表。不是 skipped。

## Backlog（本轮不占用循环）

- 多副本混合镜像时「至少一个 running 匹配即过」——存量三段语义，#35 之前的对账就这样；任务卡要求不审 #35 之前的存量对账逻辑。
- 137/OOM 与 timeout 文案不分（反向结论：可接受）。
- release lane 不在本轮（#37 / issue #25 另卡）。
- pull 缺 `test_promoted_sha_reentry_*`、缺全 oneshot 拒绝测试（P1 修复卡应顺手带前者）。
- 同 host 复用 `IMAGE_NAME` 的 caller 契约，文档化禁止即可。

## 收敛判定

internal + infra 提档：需要连续 2 轮无新增 P1。本轮 **新增 P1-1**，不能收敛。下一轮先审 `H0..H1` 增量四问，重点看 skip-forward 是否只修登记 findings、有没有新抽象、255 窗口是否还在。

执行器：cursor / cursor-grok-4.6-high。只写本 verdict 文件，未改被审代码。
