verdict: pass

# review r2：PR #38 H0..H1 增量四问 + 全量复验（换家：255 重放窗口）

- 审查对象冻结：H1 = `6f395585bfb27a3b2d1c8740714ecb6832c1de1b`；增量 `822bcf6c5c67d968d04c0bbbfde6c5874115ed53..6f395585bfb27a3b2d1c8740714ecb6832c1de1b`（5 commit，6 files，+51/−4）。审查中分支再有新提交不改变本轮对象。
- spec = PR #38 正文 + 第 1 轮 verdict（`origin/card/ci-templates-20260902-04` 的 `docs/sessions/260902-ci-triage/reviews/review-ci-34-r1-verdict.md`）。
- 风险档 **internal**（`AGENTS.md`「风险等级」）；改动核心仍是失败路径 / 锁临界区 / `last_good` 账本，infra 提档按 **saas 收敛条件**（连续 2 轮无新增 P1）。
- 本轮方向 = **A. H0..H1 增量四问** + **B. 全量复验换视角**（255 重放窗口实测、矩阵格语义、飞书文案）。第 1 轮已审的正向兑现、降层三问、137/124 映射 **不重复**。
- 本轮新证据（换家必须换证据源，不是同一份 diff 重读）：
  1. 临时 worktree `/tmp/review-ci38-r2` @ H1，PATH stub `docker`/`curl` 对真实 `scripts/pull_and_deploy.sh` 连跑三次（第一次健康部署写 `last_good_tag`；第二次同 `GIT_SHA` 重入且 curl 桩返回 500；第三次模拟「第二次对账期间 255」再重放）。stub 调用记录原文见 B.1。
  2. 同 worktree `uv run --with pytest,pyyaml,jsonschema python -m pytest -q tests/test_pull_and_deploy.py tests/test_workflow_contract.py`：**96 passed in 23.59s**。
  3. `git diff 822bcf6..6f39558` 与 `git show 6f39558` 行号；对照 `scripts/release_deploy.sh:669-672`（只对照同构 skip，不审 release lane）。
- OCR：本轮未重跑（r1 已对 H0 `reviewed`；本轮对象是 +51 行修复增量，新证据是运行时重放而不是再扫同一 diff）。
- 已否决方案（不作为 finding 重提）：让 workflow 对 255 不重试；对账改回第二条 ssh；在 workflow 层再加一次 flock。

## A. H0..H1 增量四问

范围：`822bcf6..6f39558`。**四问均通过，不按新增 P1 计。**

### ① 是否只修登记在案的 findings（P1-1 / P2-1 / P2-2）？

是。增量只做登记三项及其必要配套，没有新功能、没有重开已否决方案。

| 登记 finding | 落地 | 依据 |
|---|---|---|
| P1-1 skip-forward | `do_deploy` 在 `prev_good == GIT_SHA` 时跳过 `deploy_tag` / 探针，`return 0` 后主流程仍持锁对账 | `scripts/pull_and_deploy.sh:433-437`（守卫）、`:577-587`（`do_deploy` 后、`flock -u 9` 前仍跑 `reconcile_deployed_image`） |
| P2-1 README 退出码表 | 表加 `rc=5` 行；`rc=0` 改为探针 **且** 三段对账 | `README.md:215`、`:219` |
| P2-2 飞书双红误导 | 首句改为「Deploy 红且 **没有** Reconcile 红」；新增第三句「同时红 = rc=5，按 Reconcile 处置，不要按已回滚」 | `.github/workflows/build-deploy.yml:440`、`:445` |

配套（不算超出；缺了测试会红 / r1 修法明确要求）：

- `tests/test_pull_and_deploy.py` 新增 `test_promoted_sha_reentry_skips_forward_and_reconciles_only`（r1 建议的对称锁）。
- 矩阵格 `probe-fails-with-same-prev-good` 改名为 `promoted-sha-skips-forward`、期望 `rc=4 → 0`（P1-1 的直接测试后果；语义审查见 B.2）。
- `tests/test_workflow_contract.py` 新增 11 行锁飞书新文案（P2-2）。
- `docs/sessions/260902-ci-triage/progress/ci-34-progress.md` 修复轮记录。

超出产品行为：无。255 循环、第二条 ssh、workflow 层 flock 均未改。

### ② 是否新增未经批准的抽象？

否。没有新函数、新包装层、新环境变量、新文件（进度文档除外）。skip 是 `do_deploy` 入口对**已有** `prev_good` 的 6 行相等守卫（`:433-437`），与 #35 `release_deploy.sh:669-672` 同构。`reconcile_docker` / `RECONCILE_CMD_TIMEOUT` 是 H0 已有物，本增量未再加一层。

### ③ 状态 / 事实源 / fallback 是否无依据增加？

否。skip 读的仍是 `GOOD_TAG_FILE`（`last_good_tag`）经 `prev_good="$(cat …)"`（`:431`），与回滚目标同源（`:473` 的 `prev_good != GIT_SHA`）。没有新持久文件、没有第二套「是否已 promote」标记、没有新 fallback。`last_good_tag` 仍只在探针成功路径写入（`:441-442`）；skip 路径只读不写。

### ④ 是否留下双路径？

否。skip 是 `do_deploy` 内单一 early return；对账只有主流程 `:580-585` 这一条（`rc==0` 且持 fd9）。没有「workflow 再 skip 一次」、没有「legacy 文件命中但 canonical 不同仍 skip」的窗口（pull lane 本来就只有一个 `last_good_tag`）。255 外层循环仍在（已否决「关掉 255」），重放时第二次走进同一条 skip，不是第二条部署路径。

## B. 全量复验（换视角）

### B.1 255 重放窗口实测

环境：`/tmp/review-ci38-r2` @ `6f39558`；`PATH` 前置 stub `docker`/`curl`（`DOCKER_BIN`/`CURL_BIN` 指向同一对桩）；真实脚本 `scripts/pull_and_deploy.sh`；`GIT_SHA=abc1234`；第一次 `CURL_STATUS=200`，第二、三次 `CURL_STATUS=500`（若探针被调用会 500）。状态目录跨三次共享，模拟「第一次 SSH 已 promote → 255 → 再跑整份脚本」。

**第一次（正常部署写 `last_good_tag`）** rc=0。stub 调用记录原文：

```
# docker.log
pull registry.example.com/ns/demo:abc1234
tag registry.example.com/ns/demo:abc1234 demo:latest
compose up -d
compose config --services
image inspect registry.example.com/ns/demo:abc1234 --format {{.Id}}
image inspect demo:latest --format {{.Id}}
compose ps -q --status running app
inspect cid-app --format {{.Image}}

# curl.log
-s -o /dev/null -w %{http_code} --max-time 1 http://localhost/health
```

stdout 含 `deploy of abc1234 healthy; recorded as last good` 与 `image reconcile starting (host lock still held)` / `image reconcile passed`。`last_good_tag` = `abc1234`。

**第二次（同 `GIT_SHA` 重入 = 255 重放）** rc=0。stub 调用记录原文：

```
# docker.log
compose config --services
image inspect registry.example.com/ns/demo:abc1234 --format {{.Id}}
image inspect demo:latest --format {{.Id}}
compose ps -q --status running app
inspect cid-app --format {{.Image}}

# curl.log
(empty, 0 bytes)
```

stdout 首句：`this SHA already in last_good_tag; skip forward deploy; reconcile only`，随后仍是 `image reconcile starting (host lock still held)`。**无 `pull`、无 `tag`、无 `compose up -d`、无探针 curl。** 对账五条 docker 调用仍发生，且发生在 skip 之后、锁释放之前（脚本 `:433-437` return 0 → `:580-581` 对账 → `:587` `flock -u 9`）。

**第三次（255 落在第二次对账期间再重放）** rc=0。stub 调用记录原文与第二次逐字节相同：

```
# docker.log
compose config --services
image inspect registry.example.com/ns/demo:abc1234 --format {{.Id}}
image inspect demo:latest --format {{.Id}}
compose ps -q --status running app
inspect cid-app --format {{.Image}}

# curl.log
(empty, 0 bytes)
```

`last_good_tag` 仍是 `abc1234`（skip 只读）。第三次同样只对账，幂等。

结论：P1-1 的 255 重放窗口 **已关死**——forward `pull` / `compose up` / 探针在第二次及以后都不会再跑；对账是只读，255 落在第二次对账期间，第三次仍只对账。migrate / oneshot 不会因为对账窗口的 255 再跑一遍。

### B.2 矩阵格语义变更（`probe-fails-with-same-prev-good`：rc=4 → rc=0）

commit `4daa08a` 把该格改名为 `promoted-sha-skips-forward`：`prev_good == GIT_SHA == new2222` 且 `status_sequence` 仍是 500，期望从 rc=4 改为 rc=0（`tests/test_pull_and_deploy.py:283-290`）。这就是「同 SHA 重入不再重探针」。

生产上这一格何时发生：

1. **255 重放**：第一次 SSH 已探针成功并写入 `last_good_tag`，对账中途传输层 255，外层再跑整份脚本（`build-deploy.yml:308-390`，最多 3 次）。这是 P1-1 要关的窗口。
2. **人肉重跑同一 SHA**：workflow 自己写过「re-run rebuilds the same immutable SHA」（`:329`）。`last_good` 已是本次 SHA 时走进同一守卫。

服务若此刻真不健康（容器还在跑该 SHA，但 HTTP 500）：skip 不调 `health_probe`，对账只比 image ID。身份匹配则 rc=0 + `image reconcile passed`。B.1 第二次实测就是这个组合：`CURL_STATUS=500` 且 curl 桩 0 字节，rc=0。

对照 release lane `scripts/release_deploy.sh:669-672`（`previous_sha == D3_RELEASE_TAG` → skip forward，外层仍对账）：**同构**。#35 定稿与 r1 P1-1 修法要求的就是这条减法，不是漏掉健康门。

- **工具标注**：本轮独立重放 + 矩阵 diff；OCR 未跑。
- **本仓判定：不是 P1**（不构成「探针假绿把坏版本判健康」）。接受为 skip-forward 的有意语义。
- **两问**：
  ① 真实使用下会触发吗？会。255 重试是生产路径；同 SHA 重跑也是。
  ② 后果能接受吗？能。`last_good_tag` **只在探针成功后写入**（`:441-442`）；走进这格时，该 SHA 已经在一次真实探针里被判过健康。rc=0 在重入路径上的含义是「这个 SHA 已经是 last_good，且此刻镜像身份仍匹配」，不是「我刚刚又探了一次 HTTP」。对账仍能抓住「容器没在跑 / 镜像 ID 不对」（走 rc=5）。把「running 但 HTTP 500」再探一遍，会回到 r1 P1-1：`compose up -d`（含 oneshot/migrate）+ 探针失败后 `prev_good == GIT_SHA` 无法回滚 → rc=4。那才是 internal 红线（损坏他人数据）。健康持续监控不是本脚本重入职责。

文档小缝：`README.md:215` rc=0 生产状态写「已验证在应答」，对重入路径是「上次部署时验证过」，不是「本次 curl 过」。`README.md:237-238` rc=4 叙述仍把「`last_good` 等于本次 SHA」列为 rc=4 来源——skip 之后这条路径不再发生（无 `last_good` 的首次失败仍是 rc=4）。两处都不假绿，记 P3，不阻塞。

### B.3 飞书文案落地核对

H1 `build-deploy.yml:437-447` 三条分流：

1. **Deploy 步骤红且没有 Reconcile 红**：未部署成功；若是探针失败，已按探针门自动回滚到 last_good，回滚后已用同预算探针验证，不需要紧急上机。
2. **Reconcile 步骤红**：deploy 已过探针、last_good 已推进，无自动回滚，生产可能正在跑未验证的镜像，必须上机核对，不可按「未上线」处置。
3. **Deploy 与 Reconcile 同时红 = 对账失败（rc=5）**：按 Reconcile 那条处置，**不要**按已回滚处置。

对照 `README.md:219` rc=5 行：「探针已过、`last_good` 已推进、镜像对账失败 | 新版本在跑但身份未证明 | **立即上机核对**，不自动回滚，不要重跑」。

句义一致：双红 = 对账失败 = 新版本可能在跑、身份未证明、不上自动回滚、上机核对、不要当成已回滚。README 多一句「不要重跑」，飞书用「必须上机核对」表达同一处置，不矛盾。

契约测试锁了新文案：`tests/test_workflow_contract.py:213-221`（增量 +11 行）断言：

- `"**Deploy 步骤**红且 **没有** Reconcile 红:"`
- `"Deploy 与 Reconcile **同时**红 = 对账失败（rc=5）"`
- `"按 Reconcile 那条处置"`
- `"**不要**按已回滚处置"`
- 旧句 `"**Deploy 步骤**红:未部署成功"` **不在** 文本里

P2-1 / P2-2 文案落地。本项无新 finding。

## Findings

本轮 **无新增 P1**。P1-1 / P2-1 / P2-2 均已修。下面两条为文档滞后，不占用收敛计数。

### P3-1：README rc=4 叙述仍把「last_good 等于本次 SHA」列为 rc=4 来源

- **位置**：`README.md:237-238`。skip-forward 后该组合走 rc=0（或对账失败 rc=5），不再 `deploy_tag` + 探针失败 + 拒回滚。
- **违反**：文档与实现不一致。不是 PR 正文未兑现（P2-1 要求的是退出码**表**，表已改）。
- **工具标注 / 本仓判定 / 两问**：本轮读 README 发现。本仓 **P3**。①值班若读到这段叙述可能以为同 SHA 重入仍会 rc=4——会触发误判，但表本身 rc=0/rc=5 行是对的，飞书分流也是对的。②后果：文档过时，不会假绿、不会损坏数据。不阻塞。
- **建议**：删掉「含……`last_good` 等于本次 SHA」；改成「无 `last_good_tag` 可回滚（含首次部署）」。

### P3-2：rc=0「已验证在应答」对 skip-forward 重入略过满

- **位置**：`README.md:215`。见 B.2。
- **工具标注 / 本仓判定 / 两问**：本仓 **P3**。①会触发：人按表读重入成功。②后果：把「身份仍匹配」读成「此刻 HTTP 健康」；对账仍挡住容器没在跑。不假绿坏版本。不阻塞。

## 工具标注 / 本仓判定 / 两问对照表

| 来源 | 工具标注 | 本仓判定 | P1 两问 |
|---|---|---|---|
| B.1 三次 stub 重放 | 第二次/第三次无 pull、无 compose up、无 curl；只对账 | P1-1 **已关死** | 255 仍会触发重入；后果现在可接受（只对账） |
| B.2 矩阵格 rc=4→0 | 同 SHA + 探针 500 现期望 rc=0 | **不是 P1**（skip 设计语义，#35 同构） | 会触发；不能接受的是旧路径（重 up + rc=4 无法回滚） |
| B.3 飞书 / README / 契约 | 三条分流与 rc=5 行句义一致，11 行测试锁死 | P2-2 **已修** | — |
| README :237-238 | rc=4 叙述未删「last_good == SHA」 | **P3-1** | 会误导叙述；不假绿 |
| README :215 | rc=0「已验证在应答」对重入过满 | **P3-2** | 会过读；不假绿 |

## 红验 / 行为测试（取证，非通过条件）

H1 worktree 指定测试：**96 passed in 23.59s**。其中包含新增 `test_promoted_sha_reentry_skips_forward_and_reconciles_only` 与改期望后的矩阵格 `promoted-sha-skips-forward`。B.1 是独立于 pytest 的真实脚本三次重放，不是单测替身。

## Backlog（本轮不占用循环）

- P3-1 / P3-2 文档滞后。
- r1 已记、本轮不重审：多副本「至少一个 running 匹配」；137/OOM 文案不分；全 oneshot 拒绝缺 pull 测试；`oneshot_services` input description 仍写 skipped on rollback only。
- release lane 不在本轮（#37）。

## 收敛判定

internal + infra 提档 = saas 收敛条件：**连续 2 轮无新增 P1**，且相邻两轮须换执行器或换视角。

| 轮 | 执行器 | 视角 | 新增 P1 |
|---|---|---|---|
| r1 | Cursor | 正向兑现 + 降层三问 + 137/124 | P1-1 |
| r2（本轮） | Grok | H0..H1 四问 + 255 重放实测 + 矩阵格 + 飞书文案 | **无** |

本轮 **无新增 P1**。A 段四问通过。P1-1 的 255 重放窗口经 stub 三次重放证明已关死。收敛计数 **1/2**，尚未收敛；下一轮需继续换执行器或换视角，且不再重复本轮已查方向。

执行器：grok / grok-4.6。只写本 verdict 文件，未改被审代码。临时 worktree `/tmp/review-ci38-r2` 用完删除。
