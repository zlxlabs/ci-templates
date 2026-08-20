<!-- delegate-outcome: succeeded -->

# PR #28 release lane 部署后镜像对账——第 5 轮独立终审

## 一、总体 verdict 与本轮新证据

**verdict：pass**；本轮新增 P1：**0**。

审查对象固定为 base ca8cad6b0ea922d5890811cc45cbb4b3ae032074 到 H0
991ae8fa4094d6f820bb7876057390edb931a994。本轮新证据是：

- H0 固定 SHA 的 HEAD 干净性三条硬前置命令（本节）。
- git diff 5e70321..<H0> -- .github scripts tests 的实现增量为空（本节）。
- H0 源码逐条核对全-oneshot guard、退出码捕获和 per-image 分支
  （.github/workflows/build-deploy-release.yml:423-555）。
- bash -c 实跑 out="$(false)" || rc=$? 与 if ! ...; then rc=$? 的差异；
  Docker Compose v5.1.1 最小 fixture；H0 normalize_release.py 外部网关探针实跑
  （§六）。
- H0 detached worktree 全量测试和两处红验（§七）。
- ocr-review 前置扫描返回 status=reviewed、profile=minimax、coverage=complete，
  6 条候选均经复核未确认；扫描的候选验证器把部分 H0 代码与基线混淆，故只记录状态，
  不把其标签当作结论。

### 收敛计数

| 轮次 | 审查者 | 新增 P1 | 连续无新增 P1 计数 |
|---|---|---:|---:|
| 第 1 轮 | 主脑 | 1（config --format 不被 Compose 支持） | 0 |
| 第 2 轮 | Cursor | 0 | 1 |
| 第 3 轮 | Codex | 1（全-oneshot 假绿） | 0 |
| 第 4 轮 | Cursor | 0 | 1 |
| **第 5 轮（本卡）** | **Codex** | **0** | **2** |

第 3 轮有 P1，所以第 2 轮的 0 不能把第 1 轮的 P1 算进连续计数；正确演变是
0 → 1 → 0 → 1 → 2。本轮 0 P1 后连续计数到 **2**，达到 internal 档
infra/状态机类 diff 要求的“连续 2 轮无新增 P1”，**本轮之后收敛，PR 可进入合并流程**。

## 二、HEAD 干净性硬前置

以下三条命令均以 H0 SHA 原样执行：

~~~console
$ git show 991ae8fa4094d6f820bb7876057390edb931a994:.github/workflows/build-deploy-release.yml | grep -n 'config --images'
446:            if ! image_ref="$(cd "$DEPLOY_DIR" && D3_RELEASE_TAG="$D3_RELEASE_TAG" docker compose config --images "$svc")"; then

$ git status --short

$ git grep -nE '_broken|BROKEN|XXX|TODO-red' 991ae8fa4094d6f820bb7876057390edb931a994 -- .github scripts tests examples README.md
~~~

第一条显示 "$svc"，第二条为空，第三条限定路径无命中，硬前置通过。

相对第 3 轮修复的实现增量命令原样无输出：

~~~console
$ git diff 5e70321d291ec497bff900710a31e1e5b67e0ffa 991ae8fa4094d6f820bb7876057390edb931a994 -- .github scripts tests
~~~

因此新增的 31fe2d8 README 与 991ae8f 第 4 轮 verdict 不改变实现；上述 H0
源码行号可直接复核。

## 三、第 3 轮 P1 修复逐条复核

### 3.1 全-oneshot 拒绝分支位置

**结论：位置正确，先拒绝、后取映射和 per-image 检查。**

H0 在 build-deploy-release.yml:423-429 先列 Compose 服务；:433-436 构造
non_oneshot_services；:438-441 在任何 per-image 循环前执行
non_oneshot_services 为空的 error 和 exit 1；取每个非 one-shot 服务的映射从
:443-451 才开始，declared image 循环从 :474-550 才开始。因此全-oneshot
路径不会先进入 :534-535 的 passed notice，也不会进入 :529-531 的 skip 后再报错。
契约测试 tests/test_release_workflow_contract.py:660-680 锁死了
non_oneshot_services=() → reject → service_images_output= 的顺序和错误文案。

### 3.2 有非 one-shot 服务时的行为

**结论：功能行为与修复前一致；日志文案不是逐字一致，这是有意的第三处修复。**

git diff --unified=3 5e70321^ 5e70321 -- .github/workflows/build-deploy-release.yml
显示第 3 轮修复仅新增 :438-441 guard，替换 :457 的退出码捕获，并把旧
:529-531 的 only referenced by oneshot service(s) 改成
not referenced by any non-oneshot service。因此只要 non_oneshot_services 非空，
服务映射、batch/per-service running 查询、expected ID、逐镜像匹配和汇总退出路径均未改；
这些不变式由 tests/test_release_workflow_contract.py:570-624、:640-657 锁死。
没有一个测试做旧新代码的字节级差分；这里“功能一致”的直接证据是上述 H0 前一提交
到 5e70321 的精确 diff，而“逐字一致”不能对 skip notice 成立。

### 3.3 compose ps 退出码捕获与失败分支可达性

**结论：修复有效，<compose ps failed> 分支可达。**

原样 shell 实跑：

~~~console
$ bash -c 'rc=0; out="$(false)" || rc=$?; printf "out=%q rc=%s\n" "$out" "$rc"'
out='' rc=1
$ bash -c 'rc=0; if ! out="$(false)"; then rc=$?; fi; printf "out=%q rc=%s\n" "$out" "$rc"'
out='' rc=0
~~~

H0 的实际写法是 build-deploy-release.yml:453-471 的
compose_output="$(...)" || compose_rc=$?，随后 :459-471 按 compose_rc 写入
running_ids_detail=<compose ps failed>；契约测试
tests/test_release_workflow_contract.py:626-637 也锁死 capture → status branch →
failure evidence 的顺序。远端 heredoc 使用 set -u -o pipefail 而不是 -e
（:409-410），因此 if ! 形态在 :427、:446、:499 只用于纯 fail-loud，
不读取被 ! 反转的 $?，符合任务卡约定。

### 3.4 “没有验证 running 容器却报绿”的路径穷举

对 H0 :418-555 的所有终止分支复核如下：

| 路径 | H0 行为 | 结论 |
|---|---|---|
| Compose 服务列表失败 | :427-429 直接 error + exit 1 | fail-loud，不假绿 |
| 全部服务是 one-shot | :438-441 直接 error + exit 1 | 第 3 轮 P1 已封堵 |
| declared image 仅被 one-shot 引用 | :485-492 不加入 svc_using_image，:529-531 notice + skip | **允许的锁定决策 #3**，不是假绿；README:339-353 明文记录 |
| 全部 declared image 都是上述 skip | 仍走最终 :552-555 通过 | 仍是锁定决策 #3 的授权范围；没有 declared image 需要 running 证明 |
| config --images "$svc" 失败 | :446-449 error + exit 1 | fail-loud |
| expected tag inspect 失败/空 | :477-483 记录失败，:523-527 置失败 | fail-loud |
| 需要 running 但无容器 | :499-505 后 :542-546 置失败 | fail-loud |
| running image mismatch/inspect 失败 | :507-515 后 :548-550 置失败 | fail-loud |
| batch compose ps 失败但 per-service 查询成功 | :469-471 只影响全景取证；:497-516 仍逐服务验证 | 不会绕过必要的 per-image 检查 |
| batch 失败且 per-service 失败 | :499-502 置 reconcile_rc=1，:552-553 退出 | fail-loud |

因此没有发现除锁定决策 #3 授权 skip 外的“零 running 验证报绿”路径，也没有新增 P1。

## 四、退出码捕获穷举扫描

独立扫描命令：

~~~console
$ git grep -nE 'readarray|mapfile|<[[:space:]]*<\\(|local[[:space:]]+[^=[:space:]]+[[:space:]]*=\\$\\(|if[[:space:]]+![^;]*=\\$\\(' 991ae8fa4094d6f820bb7876057390edb931a994 -- .github scripts tests examples README.md
991ae8fa...:.github/workflows/build-deploy-release.yml:431: readarray -t all_services <<< "$all_services_output"
991ae8fa...:scripts/release_deploy.sh:268: readarray -t all_services < <(compose_list_services "$tag") || return 1
991ae8fa...:scripts/release_deploy.sh:285: readarray -t all_services < <(compose_list_services "$tag") || return 1
991ae8fa...:scripts/release_deploy.sh:379: readarray -t rollback_services <<< "$_svc_list"
~~~

按实际完整路径复核的表：

| 位置 | 写法 | 独立结论 |
|---|---|---|
| build-deploy-release.yml:457 | assignment + || compose_rc=$? | 已修，正确；失败可到 :469-471 |
| build-deploy-release.yml:427,446,499 | if ! assignment; then | 不读 $?；远端 :409-410 没有 -e，当前有效 |
| build-deploy.yml:430,437,445,450 | if assignment; then ... else rc=$?; fi | 正确对照；源码 :430-460 |
| release_deploy.sh:256,328,375,405,569,584 | assignment + || rc=$? | 正确；源码同各行 |
| pull_and_deploy.sh:184-186,198,213,244,307-316,319-326 | assignment 后 || config_rc=$? / || return / || list_rc=$? | 正确；源码各处显式保留失败 |
| release_deploy.sh:268,285 | readarray ... < <(compose_list_services) || return 1 | **未修，已出账 issue #29，本轮不重复计数** |
| release_deploy.sh:379、build-deploy-release.yml:431 | here-string <<<，不是进程替换 | 无命中；输入来自已捕获变量 |
| while ... done < <(...)、mapfile、local x="$(cmd)"、管道后取 $? | 扫描无生产命中 | 无漏报 |

通知步骤也单独核对：release workflow:578-580、617-620、677-679 均声明
continue-on-error；:592-615、:636-675、:697-747 的请求/解析失败只打印 warning 并
exit 0。因此它们是 fail-open 通知，不改变 deploy/reconcile 的结果；没有把通知失败
误判成部署成功的路径。issue #29 的 readarray 仍按任务卡列为既有 P3，不是本轮发现。

## 五、独立自主审查

### 5.1 oneshot_services 的跨特性耦合

实现仍让同一个 input 同时决定回滚筛选与对账 running 筛选：
build-deploy-release.yml:71-74 定义 input，:216 和 :394 分别传给 deploy 与 reconcile，
远端对账在 :418-435 建表、:485-492 排除 one-shot 服务；回滚侧在
scripts/release_deploy.sh:283-297 拒绝空回滚服务集。不能独立表达“回滚跳过但对账仍要求
该服务 running”，这正是已登记的 N8；README:350-353 已如实记录，属于锁定的简化，
本轮不重复登记，也不计 P1。

新发现 N9：input 自身的描述仍写成 treated as one-shot/migration services; skipped on
rollback only（build-deploy-release.yml:71-74），与同一 input 实际进入对账
（:386-394、:418-492）及 README:350-353 矛盾。触发路径是 caller 只看到 reusable
workflow schema 描述并把长期服务列入 oneshot；后果是对账打印 notice 并跳过该镜像的
running 证明（:529-531），但 job 仍可能通过。定级 **P2**，建议边界仅改 input
description，与 README 和 notice 对齐；不新增第二个 input。

### 5.2 if: success() 的完整终止形态

对账触发条件固定在 build-deploy-release.yml:381-385：
success() 且 busy_deferred 不为 true。逐形态结论：

| deploy 终止形态 | 代码证据 | 对账 | 生产状态与结论 |
|---|---|---|---|
| 正常 rc=0 | deploy loop :340-343；promote 在 scripts/release_deploy.sh:505-520 | 执行 | 已 promote，随后执行两段对账 |
| rc=1 已回滚/首次失败 | workflow :348-369；脚本 :501-534 | 跳过 | job 红；没有把失败当绿 |
| rc=3 忙锁延期 | workflow :350-367 写 busy_deferred 后退出 | 跳过 | job 红且通知延期；不执行对账 |
| rc=4 回滚未证明健康 | workflow :350-352 写 rollback_unhealthy 后退出 | 跳过 | job 红，生产可能不可用；通知要求人工介入 |
| SSH 255 重试耗尽 | workflow :371-379 最终 exit 1 | 跳过 | 生产可能已改变但 job 红且状态未知；不是静默绿 |
| job cancellation / runner 掉线 | 对账依赖 :385 的后续 step，取消/掉线不会得到成功 job | 跳过 | job 非绿/未完成；没有成功结论可掩盖状态 |
| deploy 成功、reconcile SSH 255 耗尽 | reconcile loop :559-576 最终 exit 1 | 执行但失败 | 已 promote 但 identity 未证明，job 红 |

因此没有发现“生产实际改变且 job 绿色、对账被跳过”的路径。SSH 255、取消和掉线
只会留下非绿或未知状态，README:355-359 也明确要求人工确认。

### 5.3 错误文案准确性

逐条对照 build-deploy-release.yml：

| 分支 | 行 | 文案与触发原因 |
|---|---:|---|
| 无 declared image | 412-416 | has no declared images，准确 |
| Compose 服务列表失败 | 426-429 | could not list compose services，准确 |
| 全-oneshot | 438-441 | 说明覆盖全部服务、无法证明、已 promote、不自动回滚，准确于该非空服务集分支 |
| per-service config 失败 | 443-449 | 点名 service，准确且 fail-loud |
| expected 不可用 | 477-483、523-527 | 点名 expected tag 与 inspect 失败，准确 |
| 仅 one-shot 引用 | 529-531 | notice 说明没有 non-oneshot 引用，准确 |
| 无 running | 542-546 | 说明没有 running 容器使用目标镜像；已知 per-service ps 失败也会归入此文案，属于 N5 已知取证边界，本轮不重复 |
| mismatch | 548-550 | 同时打印 expected、service/container/image，准确 |
| batch ps 失败 | 469-471 | <compose ps failed> 仅为全景证据；per-service 分支仍负责结论，准确 |

新发现 N10：若 Compose 配置是 services: {}，docker compose config --services 实跑
返回 rc=0 且无输出；bash 实跑 readarray -t all_services <<< "" 得到
len=1、first 为空，于是 :438-441 会把空服务集写成“oneshot_services covers every
compose service”。这是文案不准确，定级 **P3**。它不是本有效发布路径的 P1：同一空配置
会先在 scripts/release_deploy.sh:328-359 的 identity gate 因 declared image 缺失而
拒绝，deploy 不会成功后进入对账。建议仅在 :433-441 前区分“无 Compose 服务”和
“oneshot 覆盖全部服务”，不改变闸门语义。

### 5.4 README 与实现一致性

README:327-332 明确 release lane 是 expected vs running 两段、无 latest；实现
build-deploy-release.yml:474-550 逐 declared image 做 expected inspect 与 running
匹配，未引入 latest。README:339-341 的 one-shot 排除与 skip 规则对应实现
:418-435、:485-531；README:345-349 的全-oneshot fail-loud、探针可指向 Compose
之外网关与实现 :438-441 及 scripts/normalize_release.py:119-163 一致。

“探针可打 compose 之外网关”不是过头描述：normalize_release.py 只校验 URL 非空、
http(s)、hostname、端口和字符（:119-147），不读取 Compose 服务名；H0 实跑
https://gateway.example.test/healthz 被接受并写入 probe manifest。README:350-353
对 N8 的描述也与实现相符。N7/N8 的 README 补充已覆盖实现行为，没有发现文档声称
实现不存在的成功或回滚行为；只剩 input description 的 N9。

## 六、本轮新发现清单

| ID | 定级 | 文件:行 | 触发路径 | 后果 | 建议修复边界 |
|---|---|---|---|---|---|
| N9 | P2 | .github/workflows/build-deploy-release.yml:71-74 | caller 依据 schema 误解 oneshot 只影响回滚，并把长期服务列入 | 对账 :529-531 skip running 证明；README 已警告但 schema 描述相反 | 只更新 input description，明确同时影响回滚与对账；不新增 input |
| N10 | P3 | .github/workflows/build-deploy-release.yml:438-441 | Compose config services 成功但输出为空 | 错把“无服务”写成“全 oneshot”；有效部署前已被 release_deploy.sh:328-359 拒绝 | 在现有 guard 前区分空服务文案；不改 fail-loud 语义 |

既有 issue #29、#30，及 N1/N5/N6 不重复登记、不改变本轮 P1 计数。

## 七、实跑验证与红验

### 7.1 H0 全量 pytest

在 detached H0 worktree 执行 python -m pytest tests/ -q，原样输出：

~~~console
$ python -m pytest tests/ -q
......................... [ 29%]
.......................................... [ 59%]
........................................................... [ 89%]
..........................                                               [100%]
242 passed in 41.58s
~~~

H0 测试通过；执行 worktree 已移除，当前本卡工作树未被测试改动。

### 7.2 Shell 语义实跑

除 §三的退出码对照外，以下就是本轮用于判定的原样结果：

~~~console
$ bash -c 'rc=0; out="$(false)" || rc=$?; printf "out=%q rc=%s\n" "$out" "$rc"'
out='' rc=1
$ bash -c 'rc=0; if ! out="$(false)"; then rc=$?; fi; printf "out=%q rc=%s\n" "$out" "$rc"'
out='' rc=0
~~~

这直接证明 workflow:457 的新写法能保留 compose ps 的非零退出码，而旧 if-! 写法
不能；源码与契约测试位置见 build-deploy-release.yml:453-471 和
tests/test_release_workflow_contract.py:626-637。

### 7.3 Docker Compose 最小 fixture

Docker Compose 版本和 fixture 命令输出：

~~~console
$ docker compose version
Docker Compose version v5.1.1
$ D3_RELEASE_TAG=abc123 docker compose config --services
app
gateway
migrate
$ D3_RELEASE_TAG=abc123 docker compose config --images app
myapp:abc123
$ D3_RELEASE_TAG=abc123 docker compose config --images migrate
migrate:abc123
$ D3_RELEASE_TAG=abc123 docker compose config --images gateway
nginx:1.27
$ D3_RELEASE_TAG=abc123 docker compose config --images nosuch
no such service: nosuch
nosuch_rc=1
~~~

fixture 的 Compose 内容是 app、migrate、gateway 三个服务，其中 app/migrate 使用
D3_RELEASE_TAG，gateway 使用 nginx:1.27；它验证了逐服务 config --images 的输出形态和
不存在服务的 fail-loud 行为，与 build-deploy-release.yml:443-451、:446-449 对应。

空服务文案检查的原样输出：

~~~console
$ docker compose -f empty.yaml config --services
empty_services_rc=0
$ bash -c 'all_services_output=""; readarray -t all_services <<< "$all_services_output"; declare -p all_services'
declare -a all_services=([0]="")
~~~

这只支持 N10 的 P3 文案结论；有效发布会先经过 scripts/release_deploy.sh:328-359。

### 7.4 normalize_release.py 外部网关探针

以 H0 的脚本内容直接通过 stdin 执行，原样输出：

~~~console
$ git show 991ae8fa4094d6f820bb7876057390edb931a994:scripts/normalize_release.py | python3 - --images-json '[{"image_name":"app","build_context":"app","dockerfile":"Dockerfile"}]' --probes-json '[{"url":"https://gateway.example.test/healthz","expect_status":200}]' --manifest-out "$tmp/manifest" --builds-out "$tmp/builds"
validated 1 image(s), 1 probe(s), 1 build(s)
--- manifest ---
D3_RELEASE_MANIFEST=1
image	app	app
probe	https://gateway.example.test/healthz	200
--- builds ---
D3_RELEASE_BUILDS=1
build	app	app	Dockerfile	app	app
~~~

这与 scripts/normalize_release.py:119-163 的校验范围一致，证明 README:348-349 的
“探针可以打 Compose 之外网关”没有说过头。

### 7.5 红验记录

红验严格遵守“先 commit 真内容 → 改坏 → 确认红 → 只还原改坏处”。真实内容首次提交
是 f62cac7；红验在 detached H0 worktree 完成，恢复后状态为空并移除该 worktree。

红验 1：删除 build-deploy-release.yml:438-441 的全-oneshot guard，仅改这一块。

~~~console
$ python -m pytest tests/test_release_workflow_contract.py::test_release_image_reconciliation_rejects_all_oneshot_compose_services -q
F                                                                        [100%]
E       AssertionError: reconciliation must fail before per-image checks when every compose service is declared one-shot
1 failed in 0.11s
~~~

随后只恢复 :438-441，未整文件还原。

红验 2：把 build-deploy-release.yml:446 的 "$svc" 临时改为 "$svc_broken"，仅改这一行。

~~~console
$ python -m pytest tests/test_release_workflow_contract.py::test_release_image_reconciliation_uses_per_image_two_stage_contract -q
F                                                                        [100%]
E       assert 'docker compose config --images "$svc"' in run
1 failed in 0.05s
~~~

随后只把 "$svc_broken" 改回 "$svc"；没有把任何 broken 内容写入本卡分支。

## 八、收口结论

本轮没有新增 P1；N9 为 P2、N10 为 P3，issue #29/#30 和 N1/N5/N6 仍按既有账单处理。
五轮连续计数已经从第 1 轮的 0，经第 2 轮 1、第 3 轮 0、第 4 轮 1，到第 5 轮
**2**，满足收敛条件。总体 verdict 保持 **pass**，PR #28 可进入合并流程；合并前
仍应处理正常的 P2/P3 backlog，但不需要再开本收敛轮。

