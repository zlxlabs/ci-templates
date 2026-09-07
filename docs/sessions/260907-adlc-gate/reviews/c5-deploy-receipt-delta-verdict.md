
verdict: pass

# C5 部署回执与回滚锚点修复增量审（H0..H1）

被审 SHA 范围：730b7ad9e32c93f7efa756f7e544744ccd812e59..a30ec48fe2b02639a35c349613c03250f3fbda42

## 总结

本轮按任务卡规则判定 verdict: pass：没有发现 P1。增量中的实现和测试改动均能对应登记在案的 P2-1 至 P2-6，或是这些 finding 的必要验证/收口文档；没有发现无关的生产实现范围扩张。

P1=0，P2=3，P3=0。

仍有三组 P2 残留，均不改变本轮 verdict：

1. P2-1 的失败路径收口不完整：脚本仍有前置校验、忙锁/文件锁退出路径不写回执，且保留了按 rc 反推 deploy_failed 的兜底；前向 pull/compose 两条要求的 fixture 已正确修复。
2. P2-2 的原子替换实现正确，但调用方在写回执失败时保留原部署 rc 并继续结束；这会允许一次成功部署没有本次回执。
3. P2-5 的收紧 fake 没有 blanket success，但 workflow 接缝 fake 对 image_id_for_ref 的参数位置写错，真实调用被生产代码的可选 inspect 分支吞掉，测试仍未断言 image_id。

另有一个同属 P2-1 的测试覆盖缺口：新 helper 建模了 tag 返回 rc=7，但前向失败参数化只跑 pull 和 compose。它不影响当前实现对 tag 失败的实际处理，但没有锁死该分支。

## 审查对象与增量清单

实际增量文件清单：

~~~
 M .github/workflows/build-deploy.yml
 M README.md
 M docs/sessions/260907-adlc-gate/progress/c5-deploy-receipt-progress.md
 M scripts/pull_and_deploy.sh
 M tests/test_pull_and_deploy.py
 M tests/test_workflow_contract.py
~~~

实际统计：6 files changed, 459 insertions(+), 73 deletions(-)。本轮只审上述冻结 SHA 范围，不重审 361a343..730b7ad，也没有修改实现、测试、workflow、registry、README 或首轮 verdict。

## 四问逐问结论

### 1. 本轮是否只修登记在案的 finding？

结论：改动意图是，且没有发现越界的生产实现；但 P2-1 与 P2-5 的修复仍各有残留。逐个变更对应如下：

| 增量提交与当前落点 | 对应 finding | 判定与证据 |
|---|---|---|
| 34e5b5f；scripts/pull_and_deploy.sh:553-558,727-731；README.md:213-226；tests/test_pull_and_deploy.py:193-214,313-327 | P2-1 | 新增 deploy_failed，前向失败不再把 rc=1 反推成 rolled_back；README 同步六值契约，fixture 锁定 pull 与 compose。残留见 Finding P2-1。 |
| 4600196；scripts/pull_and_deploy.sh:610-614；tests/test_pull_and_deploy.py:811-838 | P2-1 的批准同源缺陷 | 回滚 pull/tag/compose 自身失败时，不再保留上一版本的 image_digest，测试断言 image_digest 与 image_id 均为空。 |
| acb6b56；scripts/pull_and_deploy.sh:125-155；tests/test_pull_and_deploy.py:840-862 | P2-2 | 写入改为同目录 mktemp 后 mv；新增替换失败测试，验证旧 JSON 不被截断。 |
| 12b3dee；.github/workflows/build-deploy.yml:492-566；tests/test_workflow_contract.py:387-452 | P2-6 | 成功卡把响应体留在临时文件，失败诊断只写 curl rc、HTTP 状态和白名单 code/msg；新增真实 curl 失败 fixture。 |
| 48c7420；.github/workflows/build-deploy.yml:467-482；tests/test_workflow_contract.py:247-358 | P2-3 | 六个成功卡字段逐一精确映射，并把批准的第六个 OUTCOME 接到卡片。 |
| 42df834；tests/test_workflow_contract.py:173-229 | P2-4 | 用真实 deploy step shell 片段和 fake scp/ssh 覆盖 malformed evidence，断言远端 rc=0/1/255 不被 parser 覆盖。 |
| a30ec48；tests/test_pull_and_deploy.py:39-214,264-276,597-601,881-892,954-957,1137-1151,1275-1278,1551-1559,1604-1631；tests/test_workflow_contract.py:239-272 | P2-5 | 将 Docker fake 的默认成功收紧为精确调用，未处理调用统一 exit 97；新增拒绝未知子命令测试。当前 workflow fake 的 image-id 参数位置错误，见 Finding P2-5。 |
| a30ec48；docs/sessions/260907-adlc-gate/progress/c5-deploy-receipt-progress.md:22-28 | 以上 finding 的进度收口 | 只更新里程碑状态、验证结果和本轮修复摘要，没有新增生产机制；属于本卡允许的审查上下文文档。 |

未发现与 P2-1..P2-6 无法对应的新增生产文件、workflow step、状态源或业务语义。README.md:217,221 仍保留 rc=3/255 的“无回执”历史说明；在本轮任务明确要求“任何终结路径都写回执”的前提下，这不是越界改动，而是 P2-1 未完全收口，见下方 finding。

### 2. 是否新增了未经批准的抽象？

结论：否。

- scripts/pull_and_deploy.sh:125-155 只修改既有 write_deploy_result 的实现；result_tmp 是该 writer 为实现原子替换所需的局部变量，不是新 helper、类、模块或包装层。
- tests/test_pull_and_deploy.py:193-214 的 _mock_docker_forward_failure 是 P2-1 指定 pull/compose 失败 fixture；tests/test_workflow_contract.py:173-229 的 malformed evidence 测试、tests/test_pull_and_deploy.py:840-862 的 mv 失败测试、以及各 fake 的精确白名单都是登记 finding 的行为防线。
- .github/workflows/build-deploy.yml:472,482,511 的 OUTCOME 是任务卡批准的新契约字段，不是未经批准的配置层；response_file/curl_stderr 是成功卡请求的临时工作文件，不是持久化状态。
- 增量没有新增类、模块、公共接口、转发-only 抽象或第二套生产部署实现。

### 3. 状态 / 事实源 / fallback 是否被无依据地增加？

结论：没有新增持久化状态、缓存或重试；原子写和成功卡响应文件是批准的临时机制；但仍有一条不应保留的 rc 推导 fallback。

- scripts/pull_and_deploy.sh:140-154 的临时文件与目标文件同目录，完成后才 mv；它没有新增事实源。RESULT_FILE、GOOD_TAG_FILE、GOOD_DIGEST_FILE 都是基线已经存在的文件。
- .github/workflows/build-deploy.yml:492-565 只增加临时响应/错误文件并在 EXIT trap 清理，没有增加重试。except (OSError, ValueError, KeyError, TypeError) 位于基线已有的成功回执 parser catch 中，本轮只是去掉 raw 回显并补充文件读取错误类型，不是新增一条异常降级机制。
- 残留违规：scripts/pull_and_deploy.sh:727-728 的 rc 非零即设置 DEPLOY_OUTCOME=deploy_failed 仍从退出码反推 outcome。即使当前已知的 do_deploy 分支都会先设置 outcome，这条兜底仍违反“outcome 一律由实际发生了什么决定，禁止从退出码反推”。详见 Finding P2-1。
- scripts/pull_and_deploy.sh:731 的 write_deploy_result ... || log ... 还把回执写入失败从部署结果中放行；它没有新增状态，但形成了回执缺失的静默降级，详见 Finding P2-2。

### 4. 是否留下双路径？

结论：回执文件写入与成功卡日志已各自收敛为单一路径；outcome 仍有显式事实路径与 rc fallback 两条路径，Docker fake 仍有一个参数错误造成的测试假绿。

- 回执只有一个 JSON 格式串和一个 writer：scripts/pull_and_deploy.sh:128-154。目标文件不再有 printf > RESULT_FILE 的直写路径，只有“同目录临时文件完整写入 → mv 替换”这一条发布路径。
- 成功卡不再保留响应字符串直传 parser 的旧路径；response_file 是请求体的落盘边界，curl 的 stdout 仅用于 http_status，parser 从该文件读取。位置：.github/workflows/build-deploy.yml:492-565。
- outcome 的实际分支在 scripts/pull_and_deploy.sh:548,556,564,612,618,627,634,723；除此之外仍有 scripts/pull_and_deploy.sh:727-728 的 rc 兜底，属于本轮禁止的第二条真相路径。
- a30ec48 删除了 Docker fake 的 blanket exit 0，当前多数 fake 均以 unexpected-docker + exit 97 收口；但 tests/test_workflow_contract.py:263 检查了错误的参数位置，并且 tests/test_workflow_contract.py:313-327 没有断言 image_id，因此实际 producer 调用被可选 inspect 失败路径吞掉后仍绿，见 Finding P2-5。
- 首轮基线的“失败卡”通知仍有自己的 response/raw 输出路径；本轮没有改动它，首轮 verdict 已明确列为存量 backlog，本轮不翻案。

## 前向失败 fixture：两条必答场景

使用的真实命令为一个直接调用 tests/test_pull_and_deploy.py 内 _base_env、_mock_docker_forward_failure、_run 的 Python fixture。实际输出：

~~~
fixture=pull expected_rc=1 actual_rc=1 receipt_exists=True outcome=deploy_failed
[deploy][evidence] result-json: {"schema_version":1,"deploy_id":"234198","git_sha":"abc1234","tag":"abc1234","image_id":"","image_digest":"","outcome":"deploy_failed","probe":{"status":"skipped","final_code":"","attempts":0,"elapsed_s":0},"finished_at":"2026-09-07T11:15:27Z"}
fixture=compose expected_rc=23 actual_rc=23 receipt_exists=True outcome=deploy_failed
[deploy][evidence] result-json: {"schema_version":1,"deploy_id":"234366","git_sha":"abc1234","tag":"abc1234","image_id":"","image_digest":"","outcome":"deploy_failed","probe":{"status":"skipped","final_code":"","attempts":0,"elapsed_s":0},"finished_at":"2026-09-07T11:15:27Z"}
~~~

结论：两条均写回执，均为 deploy_failed，不再为 rolled_back；第二条保留 compose 的 rc=23。另跑了 helper 已建模但未参数化的 tag 场景，实际输出为：

~~~
fixture=tag actual_rc=7 receipt_exists=True outcome=deploy_failed
~~~

这说明实现分支已覆盖 tag，缺口是测试参数矩阵没有覆盖它，列入 P2-1 残留。

## outcome 六值与所有 exit/return 机械枚举

执行命令：

~~~
rg -n '\b(exit|return)\b' scripts/pull_and_deploy.sh
~~~

约定：函数内部 return 不是脚本终结；表中先列出每个 grep 命中所属的 helper，再列出真正从脚本退出的路径。六个批准值及当前设置点为：deployed（564）、rolled_back（618）、rollback_unhealthy（612,627,634）、reconcile_failed（723）、skipped_already_deployed（548）、deploy_failed（556，以及不应保留的 728 fallback）。event exit 是传给 event 的标签，不是 shell exit。

| grep 行号 | 路径/动作 | 六值归类或不写回执 |
|---:|---|---|
| 10 | 注释中的 exit | 非执行代码；不写回执不构成路径 |
| 82,86 | image_digest_for_ref 无 digest/local digest 分支 | helper 返回；由 caller 继续，最终落到 caller 的六值；自身不写回执 |
| 142,147,152 | write_deploy_result 创建/写临时文件或 mv 失败 | writer 失败；当前回执为上一份完整 JSON 或不存在；当前 run 不写新回执（见 P2-2） |
| 167 | RECONCILE_CMD_TIMEOUT 非正整数 | 顶层 exit 1；不写回执 |
| 189 | local registry host 非法 | 顶层 exit 1；不写回执 |
| 197 | LOCAL_PULL_RETRIES 非正整数 | 顶层 exit 1；不写回执 |
| 207 | LOCAL_PULL_RETRIES 超上限 | 顶层 exit 1；不写回执 |
| 211 | LOCAL_PULL_RETRY_DELAY 非非负整数 | 顶层 exit 1；不写回执 |
| 215 | LOCAL_PULL_RETRY_DELAY 超上限 | 顶层 exit 1；不写回执 |
| 247 | local pull + retag 成功 | helper 返回 0；caller 继续 forward，最终为 deployed/deploy_failed |
| 250 | local retag 失败 | helper 返回 1；caller 按既有明确 local→ACR fallback 继续，不直接写回执 |
| 257 | local registry 重试耗尽 | helper 返回 1；caller 按既有明确 local→ACR fallback 继续，不直接写回执 |
| 264 | local path 成功 | helper 返回 0；caller 继续 forward，最终为 deployed/deploy_failed |
| 267 | ACR pull 成功 | helper 返回 0；caller 继续 forward |
| 274 | ACR pull 失败但镜像已在本地 | helper 返回 0；caller 继续 forward，最终为 deployed |
| 277 | ACR pull 重试耗尽且本地不存在 | helper 返回 1；deploy_tag/do_deploy 设置 deploy_failed |
| 287 | compose service 列举失败 | helper 返回 1；forward 为 deploy_failed，rollback 为 rollback_unhealthy，reconcile 为 reconcile_failed |
| 290 | compose service 列举成功 | helper 返回 0；caller 继续 |
| 295 | ONESHOT_SERVICES 为空 | helper 返回 0；caller 继续 |
| 296 | oneshot 校验委托失败 | helper 返回 1；forward deploy_tag 失败，do_deploy 设置 deploy_failed |
| 304 | oneshot service 名非法 | helper 返回 1；forward deploy_failed |
| 306 | oneshot 校验成功 | helper 返回 0；caller 继续 |
| 311 | rollback service 列举委托失败 | helper 返回 1；rollback rollback_unhealthy |
| 320 | oneshot 覆盖全部 service，拒绝回滚 | helper 返回 1；rollback rollback_unhealthy |
| 323 | rollback service 列举成功 | helper 返回 0；caller 继续 |
| 327 | 无 oneshot 时 hint helper 返回 | helper 返回 0；不直接写回执 |
| 338 | forward/rollback pull 失败透传 | deploy_tag helper 返回；forward 最终 deploy_failed，rollback 最终 rollback_unhealthy |
| 339 | tag 失败透传 | deploy_tag helper 返回；forward deploy_failed，rollback rollback_unhealthy |
| 341 | forward oneshot 校验失败透传 | deploy_tag helper 返回；forward deploy_failed |
| 347 | rollback service 列表失败 | deploy_tag helper 返回 1；rollback rollback_unhealthy |
| 364 | 无健康 URL，probe skipped | helper 返回 0；forward deployed，随后对账失败则 reconcile_failed |
| 390 | 健康探针成功 | helper 返回 0；forward deployed，rollback rolled_back |
| 402 | 健康探针失败 | helper 返回 1；有旧版本时进入 rollback，最终 rolled_back/rollback_unhealthy；无旧版本 rollback_unhealthy |
| 411,413 | reconcile docker timeout/普通返回 | helper 返回；外层最终 reconcile_failed，或由其 caller 继续 |
| 430,434,447,455,465,484,493,529 | reconcile 各失败 guard | helper 返回 1；外层设置 reconcile_failed |
| 532 | reconcile 成功 | helper 返回 0；保留 deployed 或 skipped_already_deployed |
| 549 | event exit 标签 | 不是 shell 终结；对应下一行 550 |
| 550 | last-good SHA 已相同的 skip | skipped_already_deployed，统一 writer 写回执 |
| 557 | event exit 标签 | 不是 shell 终结；对应下一行 558 |
| 558 | forward deploy_tag 返回非零 | deploy_failed，统一 writer 写回执 |
| 566 | event exit 标签 | 不是 shell 终结；对应下一行 567 |
| 567 | forward health probe 成功 | deployed，统一 writer 写回执 |
| 613 | event exit 标签 | 不是 shell 终结；对应下一行 614 |
| 614 | rollback pull/tag/compose 失败 | rollback_unhealthy，统一 writer 写回执 |
| 622 | event exit 标签 | 不是 shell 终结；对应下一行 623 |
| 623 | rollback health probe 成功 | rolled_back，统一 writer 写回执 |
| 630 | event exit 标签 | 不是 shell 终结；对应下一行 631 |
| 631 | rollback health probe 失败 | rollback_unhealthy，统一 writer 写回执 |
| 635 | event exit 标签 | 不是 shell 终结；对应下一行 636 |
| 636 | 没有 previous good tag | rollback_unhealthy，统一 writer 写回执 |
| 658 | busy timeout 配置非法 | 顶层 exit 1；不写回执 |
| 679 | 忙锁预算耗尽 | 顶层 exit 3；不写回执 |
| 684 | 忙锁命令返回锁超时 | 顶层 exit 3；不写回执 |
| 687 | 忙锁 flock 非超时错误 | 顶层 exit 1；不写回执 |
| 699 | host lock flock 非竞争错误 | 顶层 exit 1；不写回执 |
| 736,738 | 注释中的 exit | 非执行代码；不写回执不构成路径 |
| 740 | 最终 exit rc | writer 成功时落到前述六值；writer 失败时当前 run 不写新回执（P2-2） |

补充：BUSY_LOCK_FILE 开启时的 pre-pull 在 scripts/pull_and_deploy.sh:661 直接调用 pull_image，失败会受 set -e 终止，也绕过 writer；这不是 grep 的显式 exit/return 命中，但同样属于“无回执”边界。

机械结论：当前仍有“不写回执”行，故“任何终结路径都写回执”这一条没有完全兑现；这些路径需要在契约上给出不伪造语义的合法结局，不能简单把 deferred、配置错误或未执行远端部署映射为 deploy_failed。这也是为什么本轮保留 P2-1，而不是把它当作完全关闭。

## 原子写核验

实现位置：scripts/pull_and_deploy.sh:140-154。临时文件模式是 RESULT_FILE.tmp.XXXXXX，即和目标文件同一个父目录；只有临时内容完整写出后才执行 mv -f。因此在同一文件系统上，替换失败不会让目标暴露为临时文件的截断内容。

同文件系统命令与实际输出：

~~~
tmpdir=$(mktemp -d)
result="$tmpdir/last_deploy_result.json"
tmp=$(mktemp "${result}.tmp.XXXXXX")
stat -c 'device=%d path=%n' "$tmpdir" "$tmp"
printf 'temp_parent=%s\ntemp_pattern=%s\n' "$(dirname "$tmp")" "$result.tmp.XXXXXX"
~~~

~~~
device=2049 path=/tmp/tmp.bQSIbohFBT
device=2049 path=/tmp/tmp.bQSIbohFBT/last_deploy_result.json.tmp.INZBXu
temp_parent=/tmp/tmp.bQSIbohFBT
temp_pattern=/tmp/tmp.bQSIbohFBT/last_deploy_result.json.tmp.XXXXXX
~~~

实际替换失败 fixture 输出：

~~~
mv_rc=1 script_rc=0 target_exists=True target_bytes=b'{"complete":true}\n' json={'complete': True}
::warning::failed to atomically replace deploy result file
~~~

对应 pytest 命令实际输出：

~~~
$ python3 -m pytest -q tests/test_pull_and_deploy.py -k test_result_replace_failure_keeps_previous_complete_receipt -vv
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.0.3, pluggy-1.6.0 -- /usr/bin/python3
cachedir: .pytest_cache
rootdir: /home/zlx/projects/personal/ci-templates-worktrees/ci-templates-20260907-01-worktrees/ci-templates-20260907-01-20260907-04
plugins: httpx-0.36.2, asyncio-1.3.0, anyio-4.13.0
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None
collecting ... collected 80 items / 79 deselected / 1 selected
tests/test_pull_and_deploy.py::test_result_replace_failure_keeps_previous_complete_receipt PASSED [100%]
======================= 1 passed, 79 deselected in 0.33s =======================
~~~

结论：mv 原子性成立；旧文件保留且仍是完整 JSON。P2-2 残留只在调用方对 writer 失败的处置：scripts/pull_and_deploy.sh:731 保留 script_rc=0，所以本次部署可以成功结束而没有本次回执。

## 成功卡日志核验

新的成功通知分支位于 .github/workflows/build-deploy.yml:492-565：

- curl response body 通过 -o response_file 写入临时文件，stderr 也不回显；失败时只打印 curl_rc 和 http_status（.github/workflows/build-deploy.yml:543-546）。
- JSON 解析失败只打印 http_status（:552-558）；业务失败只打印 code 和截断、换行归一化后的 msg（:560-562）。这些属于允许的白名单业务字段，不是整段 body 或异常原文。
- 相关行为测试实际通过：

~~~
$ python3 -m pytest -q tests/test_workflow_contract.py -k 'success_receipt_diagnostics or success_receipt_request_failure' -vv
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.0.3, pluggy-1.6.0 -- /usr/bin/python3
cachedir: .pytest_cache
rootdir: /home/zlx/projects/personal/ci-templates-worktrees/ci-templates-20260907-01-worktrees/ci-templates-20260907-01-20260907-04
plugins: httpx-0.36.2, asyncio-1.3.0, anyio-4.13.0
collecting ... collected 45 items / 43 deselected / 2 selected
tests/test_workflow_contract.py::test_success_receipt_diagnostics_do_not_echo_response_body PASSED [ 50%]
tests/test_workflow_contract.py::test_success_receipt_request_failure_reports_status_without_body PASSED [100%]
======================= 2 passed, 43 deselected in 0.28s =======================
~~~

结论：成功卡的新诊断分支不整段打印 response body；首轮 verdict 已列出的旧失败卡 response/raw 回显属于基线存量，本轮没有改动，按范围不重复提报。

## Docker fake 收紧核验

a30ec48 的 fake 改动逐项采取“补精确期望调用 + 未知调用显式失败”，没有靠放宽 fake 让测试变绿：

| fake/测试区域 | 收紧后的具体处理 | 判定 |
|---|---|---|
| tests/test_pull_and_deploy.py:56-83 基础成功、digest fake | 明确允许 pull 两参数、tag 三参数、compose up -d 三参数；RepoDigest 专用分支只返回测试 digest；未知调用 exit 97 | 补精确白名单 |
| tests/test_pull_and_deploy.py:115-138 rollback matrix | compose ps、compose logs --tail 100 --no-color、compose up -d、pull、tag 分别判断参数；其他 compose 与 Docker 调用 exit 97 | 补精确白名单 |
| tests/test_pull_and_deploy.py:142-169 rollback evidence fake | ps/log/up、pull/tag、image inspect 与 container inspect 分别匹配；未知调用 exit 97 | 补精确白名单 |
| tests/test_pull_and_deploy.py:173-214 rollback pull failure / forward failure | 回滚 digest inspect、pull、tag、up 精确匹配；forward helper 只对建模的 pull/tag/compose 注入指定 rc，其余 exit 97 | 补精确白名单并注入失败 |
| tests/test_pull_and_deploy.py:881-892,939-957,1130-1151 compose failure、flaky pull、admission | 只补上真实调用所需的精确 pull/tag/up、ps/log 和锁探针分支，移除底部默认 exit 0 | 补精确白名单 |
| tests/test_pull_and_deploy.py:1255-1278,1539-1559,1600-1631 local registry / oneshot | local→ACR tag、compose up、services/ps、RepoDigest、inspect 和 oneshot service 参数分别收紧，未知调用 exit 97 | 补精确白名单 |
| tests/test_workflow_contract.py:239-272 workflow producer fixture | pull/tag/up、compose config/ps、RepoDigest/inspect、container inspect 分支显式列出，未知调用 exit 97 | 形式上补精确白名单，但 image-id 分支参数位置错误，见 P2-5 |

没有发现“把 fake 改回任意调用均成功”或“修改生产实现规避 fake 失败”的行为。实际未知子命令测试输出：

~~~
$ python3 -m pytest -q tests/test_pull_and_deploy.py -k 'docker_fake' -vv
tests/test_pull_and_deploy.py::test_docker_fake_rejects_unexpected_subcommand PASSED [100%]
======================= 1 passed, 79 deselected in 0.15s =======================
~~~

## 验证命令

### 规定的 registry + pytest

实际命令：

~~~
python3 scripts/validate_registry.py registry.yaml && python3 -m pytest -q
~~~

实际输出（退出码 0）：

~~~
OK: registry.yaml is valid.
........................................................................ [ 25%]
.............
........................................................
... [ 50%]
......................................................
... [ 75%]
.......................................................................  [100%]
287 passed in 110.45s (0:01:50)
~~~

### actionlint

actionlint 可用，命令退出码 1；只报既有 inline shell 的 ShellCheck 信息级提示：

~~~
.github/workflows/build-deploy.yml:257:9: shellcheck reported issue in this script: SC2086:info:81:7: Double quote to prevent globbing and word splitting [shellcheck]
.github/workflows/build-deploy.yml:257:9: shellcheck reported issue in this script: SC2086:info:86:9: Double quote to prevent globbing and word splitting [shellcheck]
.github/workflows/build-deploy.yml:257:9: shellcheck reported issue in this script: SC2029:info:87:18: Note that, unescaped, this expands on the client side [shellcheck]
.github/workflows/build-deploy.yml:257:9: shellcheck reported issue in this script: SC2086:info:183:9: Double quote to prevent globbing and word splitting [shellcheck]
.github/workflows/build-deploy.yml:257:9: shellcheck reported issue in this script: SC2029:info:183:59: Note that, unescaped, this expands on the client side [shellcheck]
~~~

### OCR 前置扫描

ocr-review 实际 envelope 为 status=partial、coverage=partial；主腿返回 12 条候选，其中 2 条 confirmed，其他候选因复核超时未验证。本 verdict 只采纳本地代码/fixture 可独立复核的结论，没有把未验证的 OCR severity 当作结论。

## Findings

### P2-1：P2-1 失败路径仍有无回执终结与 rc 推导 fallback

- 严重度：P2
- 对应问题：第 1、3、4 问。
- 文件与行号：scripts/pull_and_deploy.sh:167,189,197,207,211,215,658,679,684,687,699,727-728；scripts/pull_and_deploy.sh:661；README.md:217,221；tests/test_pull_and_deploy.py:308-312。
- 失败场景：配置校验错误、忙锁 deferred、锁命令错误和 busy-lock 期间的 pre-pull 失败都在 writer 之前直接终止，当前 run 没有六值 outcome 的回执；同时 727-728 用 rc != 0 推导 deploy_failed，没有以实际动作事实决定 outcome。_mock_docker_forward_failure 明确支持 tag rc=7，但 test_forward_deploy_failure_writes_deploy_failed_receipt 仅参数化 pull rc=1 与 compose rc=23，没有锁住 tag 分支。
- 证据：busy-lock fixture 实际输出 fixture=busy-lock-deferred actual_rc=3 receipt_exists=False outcome=<missing>；前向 pull/compose fixture 已在本 verdict 上方证明两条正常路径均为 deploy_failed。
- 建议修法：逐个给这些真正终结分支定义契约允许且不伪造语义的回执结局，或先明确哪些是“任务未开始”而不属于回执终结；在契约未批准新增值前，不得把 deferred/配置/传输类状态塞进 deploy_failed。删除 727-728 的 rc 推导，所有 outcome 只在实际动作分支设置；将 tag rc=7 加入参数化并断言回执。
- 本仓判定：P2，不是 P1。当前主要是显式失败/延期和测试防线缺口，没有证据证明在本仓真实生产使用方式下会静默把生产状态判成健康。

### P2-2：回执原子替换失败仍被调用方放行

- 严重度：P2
- 对应问题：第 3、4 问。
- 文件与行号：scripts/pull_and_deploy.sh:140-152,731；tests/test_pull_and_deploy.py:840-862。
- 失败场景：mv 替换失败时，writer 正确保留上一份完整 JSON，但 write_deploy_result ... || log ... 保留部署原 rc；实际 fixture 得到 mv_rc=1 script_rc=0 target_exists=True target_bytes=b'{"complete":true}\n'。没有上一份文件时会是“无本次回执”，而部署仍可成功结束。
- 后果：生产镜像已完成部署时，workflow 的 deploy_once 可能只看到远端 rc=0，随后因没有 result evidence 跳过成功卡；机器可读回执缺失。目标文件没有截断损坏，这是原子写已经解决的部分。
- 建议修法：保留临时文件 + 同目录 mv，但让回执发布失败成为 workflow 可见的非零错误，或在契约中明确“回执发布失败也是任务失败”；不能只用普通 [deploy] 日志吞掉 writer 失败。
- 本仓判定：P2，不是 P1。失败会有 warning，且旧文件保留，没有把错误生产身份静默写成新身份。

### P2-5：workflow Docker fake 的 image-id 期望参数错误且缺少值断言

- 严重度：P2
- 对应问题：第 1、4 问。
- 文件与行号：tests/test_workflow_contract.py:259-272,300-327。
- 失败场景：生产 image_id_for_ref 实际调用参数为 image inspect --format {{.Id}} <ref>，应检查 $3=--format、$4={{.Id}}；当前 fake 第 263 行却检查 $4=--format。该调用因此落到 fake 的 unexpected-docker/exit 97，但生产函数在 scripts/pull_and_deploy.sh:94-99 里把 inspect 失败转为空 image_id，stderr 还被重定向，导致测试继续通过；test_real_deploy_stdout_is_the_receipt_parser_fixture 只断言 digest/probe/outcome，不断言 receipt[image_id]。
- 实际复现输出：

~~~
script_rc=0 receipt_image_id='' receipt_image_digest='sha256:image'
docker_log_image_id_call=['image inspect --format {{.Id}} registry.example.com/ns/demo:abc1234']
stderr_has_unexpected=False
~~~

- 建议修法：把 fake 分支改为真实 argv 的精确 $3=--format、$4={{.Id}}（并保留 $#=5），同时在 producer fixture 中断言 image_id 的值；不要靠生产的 optional-inspect 空值路径吸收 fake 未匹配。
- 本仓判定：P2。它是测试防线假绿，不是当前生产部署逻辑直接错误。

## 非 finding 备注

- docs/sessions/260907-adlc-gate/progress/c5-deploy-receipt-progress.md:11,18 在 H0 已写“五种”，本增量没有修改这两行；H1 新增行 25 写“六个字段”。这是基线文档遗留的不一致，按“只审本次 diff”不另开 finding。
- 首轮 verdict 的失败卡 raw response 回显位于本轮未修改的基线分支；本轮只核验新的成功卡分支，结果为不回显。
- actionlint 的 5 条提示均在 .github/workflows/build-deploy.yml:257 的既有 inline SSH 脚本，未发现由本增量新增的 actionlint 级别问题。
- git diff --check 730b7ad9e32c93f7efa756f7e544744ccd812e59..a30ec48fe2b02639a35c349613c03250f3fbda42 无输出。
