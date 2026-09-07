verdict: pass

# C5 部署回执状态模型收口独立审查

审查范围：`9a9ea6477d82228764e2f801deafb0a1da3a97df..2759e1f8a8a554cccb1e636734fe455d70cb013d`

结论按本卡判据：发现 0 条 P1，3 条 P2，1 条 P3，因此 verdict 为 `pass`。P2/P3 不阻塞本轮，但不应把它们误写成“全部防线已闭合”。

## 一、契约兑现了吗

| 契约 | 结论 | 代码与测试证据 |
|---|---|---|
| 1. 回执边界是 `do_deploy()`，配置/锁/忙锁 deferred/替换窗口前预拉取失败不写回执 | 通过 | `scripts/pull_and_deploy.sh:536-636` 是生产回执边界；`scripts/pull_and_deploy.sh:658-699` 的边界外失败直接退出；`tests/test_pull_and_deploy.py:1118-1122,1620-1661` 锁死 deferred、锁错误和预拉取失败无回执；`README.md:214-219` 与代码一致。 |
| 2. `do_deploy()` 每条返回路径显式设置 `DEPLOY_OUTCOME` 并写回执 | 当前生产路径通过 | 设置点为 `scripts/pull_and_deploy.sh:548,556,564,612,618,627,634`；对应返回点为 `:550,558,567,614,623,631,636`。六种 outcome 的有效回执由 `tests/test_pull_and_deploy.py:313-330,639-689` 覆盖，回滚动作失败的身份清空由 `:838-864` 覆盖。 |
| 3. 返回后 outcome 为空必须报 `::error::`、保留 rc、不写任何回执 | 部分失败，P2 | 检查在 `scripts/pull_and_deploy.sh:720-730`，但先执行 rc=0 的镜像对账；对账失败会把空值改成 `reconcile_failed`，随后仍写回执。下面的反向实测复现了这一点。现有测试 `tests/test_pull_and_deploy.py:333-355` 只把失败路径改成空值，未覆盖 rc=0 路径。 |
| 4. outcome 固定为六个，不新增 output/state/retry/fallback | 通过 | 生产代码中唯一的六个值是 `deployed`、`skipped_already_deployed`、`deploy_failed`、`rolled_back`、`rollback_unhealthy`、`reconcile_failed`，见 `scripts/pull_and_deploy.sh:548-634,723`。本增量未改 `.github/workflows/`，未新增 step output、状态文件、重试或兜底分支；`tests/test_pull_and_deploy.py:642-648,685` 覆盖六值。 |
| 5. 缺回执不被消费端当成成功或失败 | 通过 | `capture_deploy_result()` 只在 `tests/test_workflow_contract.py` 对应的 workflow 生产代码 ` .github/workflows/build-deploy.yml:300-360` 解析 evidence，缺失时 warning 并保持 `deploy_once()` 的远端 rc；`tests/test_workflow_contract.py:170-230` 对 rc=0/1/255 的缺证据情况逐一锁死。 |

### `::error::` 流向核验

本增量新增的两条错误输出在 `scripts/pull_and_deploy.sh:728,730` 都使用 `>&2`。用真实 workflow 的 `deploy_once()` 片段、fake `scp`/`ssh` 实测：

```text
rc=4
stdout:
[deploy][evidence] probe-attempts: 500(curl=0)
::warning::deploy result evidence missing
deploy_once_rc=4
stderr:
::error::remote diagnostic
```

结论：远端 stderr 没有被 `remote_output="$(ssh ...)"` 吞掉，也没有被当成 `result-json` evidence 误解析；它直接流到本地 stderr。它不是 GitHub Actions workflow command 的标准 stdout 通道，因此不会可靠地产生 Actions error annotation，只会保留为原始 stderr 日志。这是 P2 finding F-2，不是 P1：部署 rc 仍被保留，且缺 evidence 会触发 stdout warning。

### 生产诊断输出核验

本增量新增的日志/注解只有：

- `scripts/pull_and_deploy.sh:728`：固定错误文案和 rc；
- `scripts/pull_and_deploy.sh:730`：固定错误文案。

它们没有 response body、镜像引用原值或异常原文。增量没有修改 workflow 的通知 response 处理；本项通过。

## 二、边界是否划对

按任务要求重新运行：

```text
$ grep -nE '^[[:space:]]*(exit|return)[[:space:]]' scripts/pull_and_deploy.sh | wc -l
50
```

以下是该命令命中的 50 条逐条对账；“内部返回”表示它回到调用者，不是脚本终止，是否写回执由最终调用上下文决定。

| 行 | 终结路径及回执判定 |
|---:|---|
| 86 | `image_digest_for_ref` 无 digest，内部返回；不单独写回执，正确。 |
| 142 | 临时回执文件创建失败，内部返回；保留旧目标/无目标，正确。 |
| 147 | 临时回执写入失败，内部返回；清理临时文件，正确。 |
| 152 | 原子替换失败，内部返回；不覆盖旧目标，正确。 |
| 167 | `RECONCILE_CMD_TIMEOUT` 配置失败，进入 `do_deploy()` 前退出，不写回执，正确。 |
| 189 | registry host 校验失败，边界外退出，不写回执，正确。 |
| 197 | `LOCAL_PULL_RETRIES` 非正整数，边界外退出，不写回执，正确。 |
| 207 | `LOCAL_PULL_RETRIES` 超上限，边界外退出，不写回执，正确。 |
| 211 | `LOCAL_PULL_RETRY_DELAY` 非非负整数，边界外退出，不写回执，正确。 |
| 215 | local retry delay 超上限，边界外退出，不写回执，正确。 |
| 247 | 本地 registry pull+retag 成功，内部成功返回；之后仍由部署流程决定，正确。 |
| 250 | 本地 pull 成功但 retag 失败，内部失败返回并回退 ACR，未结束脚本，正确。 |
| 257 | 本地 registry 耗尽，内部失败返回并回退 ACR，未结束脚本，正确。 |
| 264 | `pull_image` 本地路径成功，内部成功返回；正确。 |
| 274 | ACR 不可达但镜像已在本地，内部成功返回；若由 `do_deploy()` 调用，最终写回执，正确。 |
| 277 | pull 全部失败且本地无镜像；作为 `do_deploy()` 内路径时由 `:556` 设置 `deploy_failed` 后写回执，作为忙锁预拉取时在边界外失败、不写回执，正确。 |
| 287 | compose service 列举失败；由部署/对账调用者映射，未单独写回执，正确。 |
| 290 | compose service 列举成功，内部返回，正确。 |
| 304 | forward oneshot service 无效；由 `do_deploy()` 的 `deploy_tag` 失败路径设置 `deploy_failed`，正确。 |
| 306 | oneshot service 校验成功，内部返回，正确。 |
| 320 | rollback 可运行 service 为空；由 `do_deploy()` rollback 失败路径设置 `rollback_unhealthy`，正确。 |
| 323 | rollback service 列举成功，内部返回，正确。 |
| 347 | rollback compose service 计算失败；回到 `do_deploy()` 后设置 `rollback_unhealthy`，正确。 |
| 364 | 无健康检查 URL，probe 跳过并返回成功；由 `do_deploy()` 设置 `deployed`，正确。 |
| 390 | 健康探针成功；由 `do_deploy()` 设置 `deployed`，正确。 |
| 402 | 健康探针耗尽失败；继续进入 rollback/no-previous 分支，由 `do_deploy()` 设置对应 outcome，正确。 |
| 411 | reconcile 单命令超时；由外层映射 `reconcile_failed`，正确。 |
| 413 | reconcile docker 返回普通 rc；由外层映射 `reconcile_failed`，正确。 |
| 430 | compose service reconcile 超时；由外层映射 `reconcile_failed`，正确。 |
| 434 | compose service reconcile 普通失败；由外层映射 `reconcile_failed`，正确。 |
| 447 | oneshot 覆盖全部服务，无法证明运行身份；由外层映射 `reconcile_failed`，正确。 |
| 455 | expected image inspect 超时；由外层映射 `reconcile_failed`，正确。 |
| 465 | latest image inspect 超时；由外层映射 `reconcile_failed`，正确。 |
| 484 | running container inspect 超时；由外层映射 `reconcile_failed`，正确。 |
| 493 | running container 查询失败/超时路径；由外层映射 `reconcile_failed`，正确。 |
| 529 | reconcile 比对失败；由外层设置 `reconcile_failed`，正确。 |
| 532 | reconcile 比对成功；回到主流程，保留 `do_deploy()` 已设置的 outcome，正确。 |
| 550 | `do_deploy()` 已是 last-good，设置 `skipped_already_deployed` 后写回执，正确。 |
| 558 | forward deploy 失败，已设置 `deploy_failed` 后写回执，正确。 |
| 567 | forward probe 成功，已设置 `deployed` 后写回执，正确。 |
| 614 | rollback 动作失败，已设置 `rollback_unhealthy` 后写回执，正确。 |
| 623 | rollback probe 成功，已设置 `rolled_back` 后写回执，正确。 |
| 631 | rollback probe 失败，已设置 `rollback_unhealthy` 后写回执，正确。 |
| 636 | 无 previous good，已设置 `rollback_unhealthy` 后写回执，正确。 |
| 658 | busy-lock timeout 配置失败，进入 `do_deploy()` 前退出，不写回执，正确。 |
| 679 | busy-lock 等待预算耗尽，未进入 `do_deploy()`，不写回执，正确。 |
| 684 | busy-lock `flock` 超时，未进入 `do_deploy()`，不写回执，正确。 |
| 687 | busy-lock `flock` 本身出错，未进入 `do_deploy()`，不写回执，正确。 |
| 699 | host-lock `flock` 本身出错，未进入 `do_deploy()`，不写回执，正确。 |
| 739 | 统一脚本退出；此前若有非空 outcome 已尝试写回执，否则只报 invariant error，rc 保持不变，正确，但 rc=0 空 outcome 在对账失败时存在 F-1 的先写入问题。 |

补充检查了该正则不会命中的 9 条内联 `return`：`82,267,295,296,311,327,338,339,341`。前 6 条是 digest/pull/oneshot/rollback helper 的内部返回；后 3 条是 `deploy_tag()` 向 `do_deploy()` 传播 pull/tag/validation 失败，最终由 `scripts/pull_and_deploy.sh:556` 设置 `deploy_failed`。`scripts/pull_and_deploy.sh:728` 只是错误文案中的 “returned”，不是 shell return。没有遗漏一条实际终结路径。

## 三、是否新增未经批准的抽象或状态

- 生产函数：无新增。`write_deploy_result()`、`do_deploy()`、`reconcile_deployed_image()` 都是基线已有函数。
- 生产变量：无新增。`DEPLOY_OUTCOME` 是基线已有状态；本增量只删除了从 rc 推导它的兜底，并保留六个既有值。
- 文件：增量未新增生产文件、状态文件或 workflow 文件；本审查产出文件除外。
- 配置项：无新增 workflow input、环境变量或配置键。
- step output：无新增；`tests/test_workflow_contract.py:263-266` 只是给测试 fake 增加 `.Id`  fixture，`tests/test_workflow_contract.py:317` 增加对应断言。
- 测试函数：新增 `tests/test_pull_and_deploy.py:333`、`:1627`、`:1642` 三个测试函数；它们不是生产抽象，也没有引入运行时状态。
- 重试/兜底：生产 diff 没有新增；原有前向/回滚调用流只改为依赖显式 outcome。

结论：没有未经批准的生产抽象、状态、输出键、状态文件、重试或兜底分支。

## 四、是否留下双路径或死代码

通过。旧的 `if [ "$rc" -eq 0 ] || DEPLOY_OUTCOME="deploy_failed"` 退出码推导和“非空就写、空值不区分”的旧流已删除，见固定范围 diff `scripts/pull_and_deploy.sh:725-733`。当前 outcome 设置点只有 `scripts/pull_and_deploy.sh:548-634,723`，回执发布只有 `write_deploy_result()` 一条流，未发现旧分支残留。

但 F-1 是检查顺序缺陷：这不是第二条实现路径，而是唯一 invariant guard 放在 reconcile 之后，导致 guard 可能被 reconcile 分支绕过。

## 五、测试防线是否有牙齿

### `do_deploy()` 每条返回路径

当前七个 `do_deploy()` return 点的 outcome 覆盖如下：

| 返回点 | outcome | 覆盖 |
|---|---|---|
| `scripts/pull_and_deploy.sh:550` | `skipped_already_deployed` | `tests/test_pull_and_deploy.py:642-648,650-689` |
| `scripts/pull_and_deploy.sh:558` | `deploy_failed` | `tests/test_pull_and_deploy.py:313-330`，pull/tag/compose 三种失败 |
| `scripts/pull_and_deploy.sh:567` | `deployed` | `tests/test_pull_and_deploy.py:642-648,650-689` |
| `scripts/pull_and_deploy.sh:614` | `rollback_unhealthy` | `tests/test_pull_and_deploy.py:838-864`，rollback pull 失败 |
| `scripts/pull_and_deploy.sh:623` | `rolled_back` | `tests/test_pull_and_deploy.py:642-648,650-689` |
| `scripts/pull_and_deploy.sh:631` | `rollback_unhealthy` | `tests/test_pull_and_deploy.py:642-648,650-689`，rollback probe 失败 |
| `scripts/pull_and_deploy.sh:636` | `rollback_unhealthy` | `tests/test_pull_and_deploy.py:642-648,650-689`，无 previous good |

七个返回点均有可达测试；没有未覆盖的 `do_deploy()` return。未覆盖的是 invariant guard 的另一种状态：`do_deploy()` 返回 0 但 outcome 为空后再进入 reconcile，这不是一个额外的 `do_deploy()` return，已列为 F-1。

### workflow Docker fake 未预期调用

实际从 `tests/test_workflow_contract.py:240-278` 的 fake 构造脚本并调用：

```text
call=image inspect ref --format {{.Unexpected}}
rc=0
stdout='sha256:image\n'
stderr=''
call=rm -f container
rc=97
stdout=''
stderr='unexpected-docker: rm -f container\n'
```

因此 fake 对明显未知的 `rm` 会失败，但 `image inspect ref --format {{.Unexpected}}` 被 `tests/test_workflow_contract.py:267-269` 的宽泛条件吸收，没有真正做到所有未预期调用 fail-loud。列为 F-3。

### 回执发布失败

以真实 `pull_and_deploy.sh`、fake `mv` 返回 1 实测，成功部署 rc 不变；有旧文件时保持完整旧字节，无旧文件时目标不存在：

```text
prior=True rc=0 exists=True content='{"complete":true}\n'
error='::warning::failed to atomically replace deploy result file\n::error::failed to write deploy result\n'
prior=False rc=0 exists=False content='<absent>'
error='::warning::failed to atomically replace deploy result file\n::error::failed to write deploy result\n'
```

代码实现符合契约：`scripts/pull_and_deploy.sh:140-152` 使用同目录临时文件和 `mv`，`scripts/pull_and_deploy.sh:730-733` 不覆盖部署 rc。现有回归测试 `tests/test_pull_and_deploy.py:870-884` 锁了“有旧文件”分支；“无旧文件”分支本轮以真实脚本实测通过，但尚未被 pytest 锁死，列为 P3 F-4。

## Findings

### F-1 — P2 — 第 1、5 问

- 位置：`scripts/pull_and_deploy.sh:720-730`
- 失败场景：`do_deploy()` 返回 `rc=0` 但 `DEPLOY_OUTCOME` 为空时，代码先运行 reconcile；若 reconcile 失败，就设置 `DEPLOY_OUTCOME=reconcile_failed`、`rc=5` 并写回执，未执行“空 outcome 必须无回执”的 invariant 处理。
- 实测：将成功路径 `DEPLOY_OUTCOME="deployed"` 反向改为空，并让 reconcile mismatch，输出为 `rc=5`、`receipt_exists=True`、`outcome="reconcile_failed"`，stderr 只有 `::error::image reconcile assertion failed...`，没有 invariant error。
- 建议修法：`do_deploy || rc=$?` 后立即检查空 outcome；空值时直接 stdout 报 invariant error、跳过 reconcile 和回执写入，保持原 rc。

### F-2 — P2 — 第 1 问

- 位置：`scripts/pull_and_deploy.sh:728,730`
- 失败场景：两条新增 `::error::` 都写 stderr。`deploy_once()` 中远端 stderr 会直达本地 stderr，而 workflow command 标准解析通道是 stdout；因此 Actions 原始日志有文本，但不可靠地产生 error annotation。
- 建议修法：把应作为 Actions command 的错误行写到 stdout；保留非 command 的诊断到 stderr 时，另确保 stdout 有一条固定、无敏感信息的 `::error::`。

### F-3 — P2 — 第 5 问

- 位置：`tests/test_workflow_contract.py:267-269`
- 失败场景：fake 接受未预期的 `docker image inspect ref --format {{.Unexpected}}` 并返回 `sha256:image`，未来生产代码调用错误格式/参数时测试仍可能假绿。
- 建议修法：按生产者实际 argv 对 `image inspect` 分支做精确匹配，并新增该未预期格式的红验断言。

### F-4 — P3 — 第 5 问

- 位置：`tests/test_pull_and_deploy.py:870-884`
- 失败场景：回执原子发布失败测试只提供上一份文件，没有 pytest 覆盖“目标原本不存在”的分支；当前实现实测为不存在，但后续误改可能只破坏该分支而测试不红。
- 建议修法：将该测试参数化为“旧回执存在/不存在”，同时断言 rc 保持 0、旧字节保持不变或目标不存在。

## 校验与交付证据

### 验证命令

命令：`python3 scripts/validate_registry.py registry.yaml && python3 -m pytest -q`

实际输出：

```text
OK: registry.yaml is valid.
........................................................................ [ 24%]
........................................................................ [ 49%]
........................................................................ [ 74%]
........................................................................ [ 98%]
...                                                                      [100%]
291 passed in 80.02s (0:01:20)
```

`actionlint` 可用，命令：`actionlint .github/workflows/build-deploy.yml`

实际输出：

```text
.github/workflows/build-deploy.yml:257:9: shellcheck reported issue in this script: SC2086:info:81:7: Double quote to prevent globbing and word splitting [shellcheck]
.github/workflows/build-deploy.yml:257:9: shellcheck reported issue in this script: SC2086:info:86:9: Double quote to prevent globbing and word splitting [shellcheck]
.github/workflows/build-deploy.yml:257:9: shellcheck reported issue in this script: SC2029:info:87:18: Note that, unescaped, this expands on the client side [shellcheck]
.github/workflows/build-deploy.yml:257:9: shellcheck reported issue in this script: SC2086:info:183:9: Double quote to prevent globbing and word splitting [shellcheck]
.github/workflows/build-deploy.yml:257:9: shellcheck reported issue in this script: SC2029:info:183:59: Note that, unescaped, this expands on the client side [shellcheck]
```

均为 ShellCheck `info`，没有 syntax/error 诊断；命令因这些 info 返回 exit code 1。

### 独立初筛

`ocr-review` 返回 `status=partial`，profile/model 为 `minimax/MiniMax-M3`，2 条 finding 均为 `unverified`（复核器超时）。其中“字符串替换脆弱”经本地检查不成立：替换目标消失时测试会在“receipt 不应存在”断言处失败；“tag rc=7 应补注释”是低严重度可读性建议。两条均未计入 Findings。

### 版本与工作树

```text
$ git log --oneline -1
2759e1f test(deploy): lock receipt boundary and image id

$ git status --short
```

工作树在写入本 verdict 前干净；本文件是本卡唯一允许新增文件。

## 汇总

- verdict：`pass`
- P1：0
- P2：3（F-1/F-2/F-3）
- P3：1（F-4）
- 五问：1 通过；2 通过；3 有 P2 顺序缺陷；4 通过；5 主路径有牙齿但有 F-3/F-4 测试缺口。
