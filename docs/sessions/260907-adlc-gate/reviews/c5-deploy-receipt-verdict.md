verdict: pass

# C5 实现审查 R1：部署回执与 digest 回滚锚点

被审 SHA 范围：361a343ea34317e85f0e551a4c7f912f62eada2d..50faa30619e31b0787419e17103410f39823c575

## 总结

按任务卡的判定规则，本轮 verdict 为 pass：没有一条意见同时满足“真实生产使用方式已量到会触发”和“后果命中 internal 档 P1 红线”两问。发现 6 条 P2，均不改变本轮 pass；其中 1 条是回执语义错误，5 条是测试/可观测性/一致性缺口。

P1=0，P2=6，P3=0。

本仓 README 将单镜像 lane 的 rc=0/1/4/5 分别定义为已部署、已回滚且探针通过、状态未证实、对账失败（README.md:213-224）；本次新增 receipt 的 outcome 必须与这套状态语义一致。当前本地 fixture 已复现前向部署失败会错误写成 rolled_back，以及另一些非零码不写回执，列为 P2。任务禁止连接真实目标机，且本仓 workflow-call 在当前仓没有可读取的部署运行记录（gh run list --workflow build-deploy.yml --limit 5 stdout 为空），所以不将这些候选提档为 P1。

## 验证证据

### 规定的验证命令

实际运行：

~~~text
$ python3 scripts/validate_registry.py registry.yaml && python3 -m pytest -q
OK: registry.yaml is valid.
........................................................................ [ 25%]
.................................................................... [ 51%]
.................................................... [ 77%]
.............................................................            [100%]
277 passed in 94.44s (0:01:34)
~~~

退出码为 0。

### actionlint

actionlint 可用，实际运行 actionlint .github/workflows/build-deploy.yml 退出码为 1，报告的是 ShellCheck 信息级问题：

~~~text
.github/workflows/build-deploy.yml:257:9: shellcheck reported issue in this script: SC2086:info:81:7: Double quote to prevent globbing and word splitting [shellcheck]
.github/workflows/build-deploy.yml:257:9: shellcheck reported issue in this script: SC2086:info:86:9: Double quote to prevent globbing and word splitting [shellcheck]
.github/workflows/build-deploy.yml:257:9: shellcheck reported issue in this script: SC2029:info:87:18: Note that, unescaped, this expands on the client side [shellcheck]
.github/workflows/build-deploy.yml:257:9: shellcheck reported issue in this script: SC2086:info:183:9: Double quote to prevent globbing and word splitting [shellcheck]
.github/workflows/build-deploy.yml:257:9: shellcheck reported issue in this script: SC2029:info:183:59: Note that, unescaped, this expands on the client side [shellcheck]
~~~

这些信息不是本次回执/digest 契约的失败证据；脚本本身另经 bash -n scripts/pull_and_deploy.sh，退出码为 0。

### OCR 前置扫描

ocr-review 返回完整 envelope：status=reviewed、coverage=complete、profile minimax；但 22 条候选的 verifier 全部为 unverified，复核器因超时未完成。因此没有直接采纳 OCR 的 severity。已手工核实的本地证据写在下方；其中 OCR 关于“空文件换行不会被命令替换剥掉”的候选被 Bash 实际语义否定，未列为 finding。

## 被审契约逐条判定

1. **兑现。** 健康探针成功后先写 last_good_tag，再由 record_last_good_identity 写 digest 和 image id；digest 查询使用指定的 docker image inspect --format '{{index .RepoDigests 0}}'。证据：scripts/pull_and_deploy.sh:547-553、scripts/pull_and_deploy.sh:77-112、tests/test_pull_and_deploy.py:212-226。

2. **兑现。** 回滚先构造 ACR_IMAGE@prev_good_digest，有 digest 时调用 digest ref；digest 缺失/空串时调用裸 prev_good，由 deploy_tag 补回 ACR tag。证据：scripts/pull_and_deploy.sh:581-594、scripts/pull_and_deploy.sh:318-326、tests/test_pull_and_deploy.py:267-312。fallback 的 tag 可变性是契约明文允许的残余风险，见降层问题二，不另判实现失败。

3. **兑现（实现）；测试覆盖不足但不改变实现判定。** inspect 失败或空输出转为空 digest；当首个 RepoDigest 指向 LOCAL_IMAGE 时返回空，且 record_last_good_identity 仍继续写 image id/状态，不因 digest 缺失失败。证据：scripts/pull_and_deploy.sh:79-91、scripts/pull_and_deploy.sh:102-112。本次新增测试没有直接给出 local RepoDigests fixture，作为覆盖备注保留。

4. **部分兑现。** 正常五种 receipt 的键集合由单一格式串固定，包含顶层 9 个键及嵌套 probe 4 个键；测试对集合做了精确断言。证据：scripts/pull_and_deploy.sh:125-141、tests/test_pull_and_deploy.py:497-553。但写入是覆盖写且 deploy_id 沿用旧的 PID 默认值，分别见 finding P2-2 和 backlog；这两点使“固定可消费事实”的运行时可靠性/身份语义不完整。

5. **部分兑现。** 五个合法 outcome 的设置点都能到达统一 writer：deployed、rolled_back、rollback_unhealthy、reconcile_failed、skipped_already_deployed 均有证据；测试覆盖五个标准 fixture。证据：scripts/pull_and_deploy.sh:532-537、scripts/pull_and_deploy.sh:547-553、scripts/pull_and_deploy.sh:596-623、scripts/pull_and_deploy.sh:706-723、tests/test_pull_and_deploy.py:504-560。但是前向 deploy_tag 非零返回被外层按 rc=1 推断为 rolled_back，rc=23 等其他错误则完全不写 receipt；见 finding P2-1。

6. **兑现（正常 outcome 路径）。** write_deploy_result 只有一个调用点，成功写入后只打印一行 evidence；五个标准路径共用该调用。证据：scripts/pull_and_deploy.sh:125-142、scripts/pull_and_deploy.sh:722-724、tests/test_pull_and_deploy.py:515-554。文件写失败属于异常路径，会没有 evidence 行，已在降层问题一记录。

7. **兑现。** parser 将 receipt 的 image_digest、probe 的 status/final_code/attempts/elapsed_s 和 outcome 写到 GITHUB_OUTPUT，即六个指定 output。证据：.github/workflows/build-deploy.yml:301-335；回执读取端的五个 probe/digest 字段在 .github/workflows/build-deploy.yml:457-471。

8. **兑现（实现）；测试不满足行为锁定要求。** parser 缺 evidence 或 JSON 解析失败时打印 warning 并 return 0；deploy_once 最后明确 return remote_rc，所以 parser 结果不覆盖 SSH/远端返回码。证据：.github/workflows/build-deploy.yml:301-335、.github/workflows/build-deploy.yml:337-360。现有新增测试只做 shell 文本匹配和真实远端脚本 stdout 解析，没有构造 malformed evidence 并观察 deploy_once 返回码；见 finding P2-4。

9. **兑现（另有日志卫生 finding）。** input 的 type/required/default 正确；成功卡条件、continue-on-error、缺 webhook/请求失败 warning 路径均正确。证据：.github/workflows/build-deploy.yml:69-73、.github/workflows/build-deploy.yml:457-489、.github/workflows/build-deploy.yml:491-556、tests/test_workflow_contract.py:138-143、tests/test_workflow_contract.py:265-374。新增成功卡仍把原始响应/错误文本放进 warning，见 finding P2-6。

10. **部分兑现。** test_real_deploy_stdout_is_the_receipt_parser_fixture 确实运行真实 pull_and_deploy.sh 并从 stdout 字节提取 JSON，再检查 parser 的键名；因此 producer 侧不是测试手写 JSON。证据：tests/test_workflow_contract.py:166-263。但 success receipt 读取端只有 PROBE_FINAL_CODE 做了精确的 steps.deploy.outputs.* 断言；把 .github/workflows/build-deploy.yml:467 的 IMAGE_DIGEST 故意改成 steps.deploy.outputs.probe_status 后，三条相关测试仍为 3 passed。因此“任一侧改坏都会红”未兑现，见 finding P2-3。

11. **兑现。** 本次 diff 保留健康探针循环/HTTP+curl rc 判定、回滚与回滚后探针、rc=5 对账及三条失败通知的条件/文案路径。证据：scripts/pull_and_deploy.sh:343-389、scripts/pull_and_deploy.sh:581-623、scripts/pull_and_deploy.sh:703-723、.github/workflows/build-deploy.yml:396-455、.github/workflows/build-deploy.yml:566-720、tests/test_workflow_contract.py:601-672。

## outcome 取值域机械枚举

使用的机械命令：

~~~sh
grep -nE '\b(exit|return)\b|write_deploy_result|result-json|RESULT_FILE' scripts/pull_and_deploy.sh
~~~

下面逐项覆盖该 grep 的动作性命中；event exit 是事件标签，不是 shell exit。注释命中与变量/函数声明也显式列出，避免把它们误当执行路径。

| grep 行号 | 位置/动作 | 五值归类 |
|---|---|---|
| 10, 728, 730 | 注释中的 exit 文字 | 非执行代码；不写 result |
| 64 | RESULT_FILE 路径赋值 | 非终结动作；不写 result |
| 82, 86 | image_digest_for_ref 空/local 分支 return 0 | helper 返回；不写 result |
| 125 | write_deploy_result 函数定义 | 定义；不写 result |
| 140 | printf > RESULT_FILE | 统一 writer；写入当时的 DEPLOY_OUTCOME |
| 141 | evidence stdout echo | 统一 writer 的唯一 evidence 输出 |
| 154, 176, 184, 194, 198, 202 | 参数/LOCAL_IMAGE 校验失败 exit 1 | 不写 result |
| 234 | local pull/tag 成功 return 0 | helper 返回；由 forward/rollback caller 决定，非直接写 |
| 237, 244 | local path 失败 return 1 | fallback helper；由 caller 决定，不直接写 |
| 251, 254, 261 | pull 成功/本地已有 return 0 | pull helper；不直接写 |
| 264 | pull 完全失败 return 1 | forward deploy failure caller；rc=1 会被外层错误归为 rolled_back，否则不直接写 |
| 274, 277 | compose service 列举失败/成功 | helper；不直接写 |
| 282, 283, 291, 293 | oneshot 校验分支 | helper；不直接写 |
| 298, 307, 310 | rollback service 列举/拒绝/成功 | helper；rollback 调用失败最终归 rollback_unhealthy |
| 314 | oneshot hint 空分支 return 0 | helper；不写 result |
| 325, 326, 328 | deploy_tag 对 pull/tag/校验错误透传 | 不直接写；回到 do_deploy，rc=1 会错误落 rolled_back，其他未映射 rc 不写 |
| 334 | rollback service list 失败 return 1 | rollback caller 最终归 rollback_unhealthy |
| 351, 377, 389 | health probe skip/success/failure | probe helper；forward success 最终 deployed，rollback success 最终 rolled_back，失败进入 rollback 分支 |
| 398, 400 | reconcile docker timeout/透传 return | reconcile helper；外层成功 deploy 时最终 reconcile_failed |
| 417, 421, 434, 442, 452, 471, 480, 516 | reconcile 各失败 return 1 | 外层 rc=0 后最终 reconcile_failed |
| 519 | reconcile success return 0 | 外层保留 deployed 或 skipped_already_deployed |
| 536, 543, 552, 600, 609, 617, 622 | event exit | 事件记录标签，不是进程退出；不写 result |
| 537 | 已部署 SHA skip return 0 | skipped_already_deployed |
| 544 | forward deploy_tag failure return deploy_rc | rc=1 → 错误 rolled_back；rc=4/5 → 对应值；其他 rc → 不写 result |
| 553 | forward probe success return 0 | deployed（随后对账失败则改为 reconcile_failed） |
| 601 | rollback pull/compose failure return 4 | rollback_unhealthy |
| 610 | rollback probe success return 1 | rolled_back |
| 618, 623 | rollback probe failure/no previous good return 4 | rollback_unhealthy |
| 645 | busy timeout 参数错误 exit 1 | 不写 result |
| 666, 671 | busy-lock 等待超时 exit 3 | 不写 result；这是 workflow 的 deferred 分支，不在五值域 |
| 674, 686 | flock 配置/系统错误 exit 1 | 不写 result |
| 723 | 调用 write_deploy_result | 写五值中已设置的 outcome |
| 732 | 最终 exit rc | 进程退出码；不新增 result |

结论：五个被命名 outcome 的标准设置路径均到达 723；机械表同时暴露 P2-1 的前向失败漏口和 deferred/配置/传输前置路径的“不写 result”边界。

## 必须显式回答的降层问题

### 1. 不可逆动作的时序

在 last_deploy_result.json 写入前，已发生的动作按路径是：

- 获取 host flock/busy lock；它是保护动作，不是业务变更。
- 拉取镜像（local registry 或 ACR）、把目标镜像 retag 为 IMAGE_NAME:latest。
- docker compose up -d 替换/重启服务；这一步可能停止旧容器、启动新容器。单镜像 lane 没有改 compose 文件，也没有删镜像。
- 健康探针和镜像对账是外部 HTTP/docker 读取；探针失败前还会读取 compose ps 和尾部日志。
- 探针失败时，再拉取 digest/tag、retag，并执行回滚 compose up -d，这是第二次可能替换容器的不可逆动作。
- 健康成功后写 last_good_tag、last_good_digest；这些是状态事实写入。其后的 image inspect/compose ps 对账是读取。
- 成功回执 webhook 在本文件写入之后才由 workflow 条件步骤发送；失败卡也在 Deploy 步骤失败之后发送。此 diff 没有在 result writer 之前发通知。

RESULT_FILE 的写失败不会触发回滚，也不会撤销已经完成的 compose、探针、回滚或 last-good 状态。实跑将 last_deploy_result.json 预置为目录，得到：脚本 rc=1、路径仍是目录、evidence 行数为 0，stderr 为 line 140 ... Is a directory。host flock 会随进程退出由内核释放；workflow 会看到 Deploy 失败，成功卡不会发送，失败卡可能发送。因而写回执失败是在不可逆动作之后把 job 变红，而不是把生产状态恢复到旧版本。

### 2. 回滚锚点的自身唯一性

digest 存在时，ACR_IMAGE@sha256:... 是内容寻址锚点；fallback 裸 SHA tag 没有同等保证。fallback 会在旧状态尚未有 last_good_digest、local-registry-only 记录空 digest、或 inspect 得不到 digest 时走到。它可能仍然从 local registry 取 tag，再失败回 ACR，但 tag 本身在注册表被改写时不再保证是当初探针通过的镜像，确实会抵消 digest 的收益。

这不是本次实现违反契约 2：契约明确要求缺 digest 时保留既有裸 SHA fallback；本轮将其作为明文接受的残余风险。部署组织依赖 SHA tag 不变的发布纪律，只能降低而不能消除该风险。

### 3. deploy_id 的唯一性

脚本生成方式是 DEPLOY_ID 环境变量为空时回退到 shell PID（scripts/pull_and_deploy.sh:60）。workflow 没有传 DEPLOY_ID，所以实际 SSH 执行默认使用目标机 shell PID。它不是跨仓、跨目标机或跨重跑的稳定全局唯一 ID；PID 退出后可复用。

同一 host 的 workflow concurrency 只按仓库维度生效，且 host flock 会把使用固定 /var/lock/fleet-deploy.lock 的脚本串行化，因此同一锁覆盖下不会同时写同一 STATE_DIR。若两个 job 真的共享一个 STATE_DIR，它们会按锁取得顺序覆盖同一个 last_deploy_result.json，不会保留历史，且 PID 不能可靠区分两次部署。该问题来自 base commit 的 ID 生成方式，本轮只把它写入新 receipt，列 backlog，不作为本轮 diff finding。

### 4. 保护覆盖的是写入还是行为

实现行为是正确的：缺 evidence 或 JSON 缺键/非法时 parser warning+return 0，随后 deploy_once 返回捕获的 remote_rc（.github/workflows/build-deploy.yml:357-360），不是 parser 的状态。

测试没有落在行为上。tests/test_workflow_contract.py:146-164 主要是字符串/结构匹配，test_real_deploy_stdout_is_the_receipt_parser_fixture 只直接运行远端脚本，不运行包含 capture_deploy_result 的 workflow shell，也没有 malformed evidence + 非零 remote rc 的 fixture。因此该要求部分兑现，见 P2-4。

### 5. 量纲一致性

producer 侧量纲是对的：tests/test_workflow_contract.py:166-245 运行真实 pull_and_deploy.sh，从它的 stdout 行解析 receipt，没有手写 result-json。这能看见 producer 的 JSON 键漂移。

consumer 侧不完整：同一测试只精确检查 PROBE_FINAL_CODE 的 steps.deploy.outputs.probe_final_code，其余五个成功卡 env 映射没有等价断言。故 producer 侧真实字节通过，完整三段接缝仍未锁死；把 line 467 的 digest output 改错而三条测试仍通过，就是反证。

### 6. 写入原子性

last_deploy_result.json 是直接覆盖写：printf ... > RESULT_FILE（scripts/pull_and_deploy.sh:140），不是临时文件加同目录 rename。写入期间读者可能拿到 0 字节或截断 JSON；进程在写中途被杀/主机重启时，残留文件可能永久不是合法 receipt。当前脚本本身不读取该文件，下一次部署主要读取两个 last-good 文件；未来巡检/编排若直接读它必须把解析失败当不完整事实。该问题列 P2-2。

## Findings

### P2-1：前向部署失败被伪装成 rolled_back，其他非零码无回执

- 违反契约：第 5 条；同时违反 README 的 rc=1 “已回滚且回滚探针通过”不变式。
- 位置：scripts/pull_and_deploy.sh:540-545、scripts/pull_and_deploy.sh:714-723。
- 失败场景：前向 docker pull/tag/compose up 在尚未进入健康探针和回滚前返回 rc=1，do_deploy 直接 return deploy_rc；外层 case 只看 rc，把它写成 outcome=rolled_back，但没有发生 rollback。我的 fixture 实际得到 pull_failure_rc=1、pull_failure_receipt_exists=True、pull_failure_outcome=rolled_back。若前向 compose up 返回 rc=23，则 case 无分支，compose_failure_receipt_exists=False。
- 后果：机器可读回执/失败卡会声称已回滚；若 compose 已部分替换，值班人员可能按“旧版本已证实健康”处置。job 会变红，所以不是静默绿；真实目标机不能连接且当前仓无运行记录，按 P1 第一问不提档。
- 建议修法：不要用通用 rc=1 推断 rolled_back；为前向失败在契约允许的域中定义明确表示，或明确把非五值的 pre-deploy failure 作为“不写 outcome、只走失败卡”并让测试锁死。若产品要求所有终结都写 receipt，则先修订“恰五值”契约，不能伪造回滚；补 pull/tag/compose failure 的行为测试。

### P2-2：receipt 直接覆盖写，失败/中断后没有原子事实

- 违反契约：第 4、5 条的固定可消费 receipt 不变式。
- 位置：scripts/pull_and_deploy.sh:125-141，实际覆盖写为 scripts/pull_and_deploy.sh:140。
- 失败场景：reader 在 > 已 truncate 但 printf 尚未完成时读取，得到空/截断 JSON；主机在写中途 OOM/重启时留下非法文件。预置结果路径为目录的实跑得到 rc=1、无 result-json evidence，且之前已完成的部署没有回滚。
- 后果：未来巡检/编排看到非法或缺失 receipt；当前 workflow 解析依赖 stdout，因此不会替生产状态回滚，问题是状态事实损坏和 job 误红。
- 建议修法：同一目录写唯一临时文件，完成后 mv 原子替换；对写失败保留原 rc/状态语义，并新增中断/reader fixture。

### P2-3：成功回执读取端只锁了一个 output 映射

- 违反契约：第 10 条“任一侧改坏都会变红”不变式。
- 位置：workflow producer/outputs 在 .github/workflows/build-deploy.yml:318-326，成功卡映射在 .github/workflows/build-deploy.yml:467-471；测试只精确锁定 tests/test_workflow_contract.py:260-262 的 final code。
- 失败场景：把 IMAGE_DIGEST 改为 steps.deploy.outputs.probe_status，真实脚本 producer 和 parser 仍可工作；运行 test_real_deploy_stdout_is_the_receipt_parser_fixture、test_success_receipt_card_is_opt_in_fail_open_and_complete、test_success_receipt_card_producer_emits_payload_with_receipt_fields 仍为 3 passed in 0.67s。
- 后果：成功飞书卡把探针状态当 digest 发出，部署 job 仍可成功，属于错误回执。
- 建议修法：对六个 success-card env 映射逐一精确断言，并执行完整的输出消费 fixture；保留当前真实脚本 stdout producer fixture。

### P2-4：契约 8 没有 malformed evidence 的返回码行为测试

- 违反契约：第 8 条的行为不变式。
- 位置：.github/workflows/build-deploy.yml:301-360、tests/test_workflow_contract.py:146-164。
- 失败场景：远端 stdout 缺 result-json 或 JSON 缺 probe/字段时，生产代码应 warning 并让 deploy_once 保留 ssh 的 rc；当前测试只检查函数名、prefix、文本和真实远端脚本 JSON，不构造该输入，也不观察 deploy_once 返回值。
- 后果：未来有人删除显式 return remote_rc、让 parser 状态覆盖远端状态，现有测试仍可能全绿。
- 建议修法：从真实 Deploy step 提取可执行 shell fixture，用 fake scp/ssh 产出 malformed evidence，分别断言 remote rc=0/1/255 原样返回且 warning 存在；不要用“源码含 return 0”代替行为断言。

### P2-5：新增 workflow 接缝测试的 Docker fake 对未处理调用一律 exit 0

- 违反契约：第 11 条“探针/部署/对账路径不削弱”的测试锁定不变式。
- 位置：tests/test_workflow_contract.py:170-190，同类 helper 在 tests/test_pull_and_deploy.py:64-73。
- 失败场景：真实脚本调用 docker pull、docker tag、docker compose up -d 时 fake 未匹配，直接落到底部 blanket exit 0；删掉 pull、改错 compose 参数或跳过 tag，新增真实 stdout 测试仍会通过。
- 后果：部署行为回归可以在 CI 绿灯下进入 workflow；当前实现不因此立即失败，但防线量纲没有覆盖终态动作。
- 建议修法：未处理 docker 子命令显式失败或记录 unexpected-docker 并断言完整调用序列；保留已有对账专用分支。

### P2-6：成功 webhook 失败诊断把原始响应/错误文本打进 Actions 日志

- 违反契约：第 9 条 fail-open 失败路径，以及项目日志不变量“生产诊断只输出结构化状态/白名单字段”。
- 位置：.github/workflows/build-deploy.yml:535-548。
- 失败场景：curl 请求失败时把 response 原样拼进 warning；响应解析失败时把 raw[:300] 原样输出。webhook/代理返回非预期错误正文时，正文进入 Actions 日志。
- 后果：成功回执仍 fail-open，但错误正文可能含内部诊断、标识或不应扩散的内容；这不是本仓不可信输入注入路径，按 internal 档为 P2。
- 建议修法：只打印 curl 退出码/HTTP 状态和白名单的截断业务 code/msg；不要回显 response body。已有失败卡路径的同类存量行为另记 backlog。

## P1 严重度重判

| 候选 | 工具/静态标注 | 真实触发条件 | 后果问题 | 本仓判定 |
|---|---|---|---|---|
| 前向失败 receipt | OCR high；本地实跑确认 | README 记录过真实 ACR 拉取失败（README.md:140-144），本机仍禁止目标机访问且当前仓无运行记录 | job 明红但 receipt/失败文案可能错误；不是静默绿，局部替换场景需真实目标量证 | P2-1 |
| 非原子写 | OCR high；代码/故障 fixture 确认 | 读写竞态或主机在写中断；本机未连接真实状态 reader | 事实文件损坏，当前部署不自动回滚 | P2-2 |
| 接缝映射 | OCR 未给该项；变异实跑确认 | 测试侧可重复触发，生产只有映射被改错时触发 | 成功通知字段错但部署 job 成功 | P2-3 |
| parser 行为测试缺失 | OCR 未给该项；静态核对确认 | malformed evidence 生产可达，但本机未运行真实 SSH | 当前实现保留 remote rc；缺回归防线 | P2-4 |

没有一条候选满足 P1 两问，因此按任务卡规则保持 verdict: pass。

## 红验抽查记录

抽查新增测试：tests/test_pull_and_deploy.py::test_healthy_deploy_records_last_good_digest。

第一次注入把 scripts/pull_and_deploy.sh:89 的 sha256:* 分支改为固定 sha256:RED_VERIFY；sed -n '88,91p' 确认注入，但该 fixture 的 producer 输出是 registry...@sha256:gooddigest，实际走 *@sha256:* 分支，测试 1 passed。按 review-discipline 判定这是不可达注入，不能据此放行测试。

立即恢复后，第二次注入把实际执行的 scripts/pull_and_deploy.sh:90 改为固定 sha256:RED_VERIFY；sed -n '88,91p' 实际输出为：

~~~text
case "$raw" in
  sha256:*) printf '%s\n' "$raw" ;;
  *@sha256:*) printf '%s\n' "sha256:RED_VERIFY" ;;
esac
~~~

运行同一测试得到退出码 1，断言失败为期望 sha256:gooddigest、实际 sha256:RED_VERIFY。实现随即恢复，清除了已核实的 tests/__pycache__ 文件；工作树回到干净状态。

另做接缝反向变异：将 .github/workflows/build-deploy.yml:467 的 IMAGE_DIGEST 临时改为 steps.deploy.outputs.probe_status，sed -n '464,470p' 确认后，三条相关测试仍为 3 passed in 0.67s；这不是红验通过，而是 P2-3 的测试缺口证据。该变异也已恢复并清理缓存。

## 熵增审查

- GOOD_DIGEST_FILE：契约 1/2 的新事实源，消费者是回滚路径和 receipt writer；不是无第二消费者的抽象。
- RESULT_FILE：契约 4/5 明确要求的新事实文件；workflow stdout parser 是第二消费边界；必要，但当前写入非原子，见 P2-2。
- DEPLOY_OUTCOME 与五值映射：需要在 do_deploy、reconcile 与最终 writer 之间传递状态；消费者不止一个，但 rc=1 的推断过宽，见 P2-1。
- IMAGE_ID、IMAGE_DIGEST、PROBE_*：receipt 和 workflow output/成功卡共同消费，直接对应契约字段，没有额外镜像状态。
- image_digest_for_ref、image_id_for_ref、record_last_good_identity、json_quote、write_deploy_result：分别被多个字段/路径复用，或是固定 schema 的唯一写边界；没有新增转发-only 层。
- capture_deploy_result：workflow 内唯一解析边界，但同时被每次 deploy_once 调用，是契约 8/10 的必经点；不属于无依据通用化。
- notify_on_success：明文契约要求的 boolean input，并同时被 step condition、缺失检查和 webhook step 消费；不是熵 +1 配置。
- 测试 helper/RESULT_KEYS/PROBE_KEYS：只服务于新增契约测试；没有发现单实现接口或未使用状态。新增 diff 约 715 行，超过任务卡 160 行 target 与 300 行 hard budget；这是拆卡/实现预算问题，但没有另造契约 finding。

## Backlog（存量，不阻塞本次）

- DEPLOY_ID 环境变量为空时回退 shell PID 的逻辑在 base commit 已存在，workflow 当前未传入稳定的仓库/run/attempt 身份；本轮只将它序列化进新 receipt，按范围不把它作为本轮 finding。
- base 的失败卡已有对 response/raw 前 300 字符的输出习惯；本轮新增成功卡复制了该模式，新增部分已列 P2-6，base 部分不在本轮修复范围。
- base 的 workflow concurrency 是仓库级语义；跨仓同 host 的实际互斥依赖远端固定 host flock。当前脚本保留该既有设计，未发现本次 diff 削弱锁路径。
- actionlint 的 SC2086/SC2029 信息来自完整 inline SSH shell 扫描；本轮没有将它们提升为契约 finding，后续可单独做 shell quoting review。

## 交付自检

- 仅新增本 verdict 文件；被审实现、测试和 workflow 均已恢复。
- bash -n scripts/pull_and_deploy.sh 退出码 0。
- 最终提交与工作树状态写入派发 report.md。
