<!-- delegate-outcome: succeeded -->

# PR #28 release lane 部署后镜像对账 — 第 4 轮独立终审

## 总体 verdict

**pass** — 本轮新增 P1：**0**。

审查对象固定为任务卡给定 H0：`origin/main..origin/card/release-reconcile`，H0=`5e70321d291ec497bff900710a31e1e5b67e0ffa`，基线=`ca8cad6b0ea922d5890811cc45cbb4b3ae032074`。本卡输出分支 `card/review-reconcile-r4` 按 delegate 现场停在基线；凡涉及被审代码的命令均显式以 H0 SHA 读取。

## 本轮新证据与审查方法

- H0 固定 SHA 的三项硬前置输出（§1）。
- 对第 3 轮 P1 修复（commit `5e70321`）的三处改动逐条对照源码与契约测试（§2）。
- 独立重跑全仓退出码捕获穷举扫描，对照第 3 轮表（§3）。
- 换角度审查：`oneshot_services` 跨特性耦合、`success()` 终止形态穷举、错误文案准确性、README 一致性（§4）。
- 在 H0 临时 detached worktree 实跑 `python -m pytest tests/ -q`（§6.1）。
- `bash -c` 实跑验证 `out="$(false)" || rc=$?` 与旧写法对照（§2.3）。
- Docker Compose v5.1.1 最小 fixture 验证 `config --images` 输出形态与 reconcile 字符串匹配前提（§2.4）。
- 红验在已提交 verdict 内容之后于 H0 临时 worktree 执行（§7）。

## 一、HEAD 干净性硬前置

任务卡要求的三条命令对 H0=`5e70321d291ec497bff900710a31e1e5b67e0ffa` 原样输出：

```console
$ git show 5e70321d291ec497bff900710a31e1e5b67e0ffa:.github/workflows/build-deploy-release.yml | grep -n 'config --images'
446:            if ! image_ref="$(cd "$DEPLOY_DIR" && D3_RELEASE_TAG="$D3_RELEASE_TAG" docker compose config --images "$svc")"; then

$ git status --short

$ git grep -nE '_broken|BROKEN|XXX|TODO-red' 5e70321d291ec497bff900710a31e1e5b67e0ffa -- .github scripts tests examples README.md

```

第一条使用 `"$svc"`（非 `svc_broken`）；第二条为空；第三条限定路径下无命中。硬前置**通过**。

## 二、第 3 轮 P1 修复逐条复核

第 3 轮 P1：全部 compose 服务被声明为 `oneshot_services` 时，对账对每个 declared image skip running 检查并最终假绿。修复 commit `5e70321d291ec497bff900710a31e1e5b67e0ffa` 三处改动如下。

### 2.1 拒绝分支位置（`non_oneshot_services` 为空时 fail-loud）

**结论：位置正确；不可能先打 `::notice:: passed` 再报错。**

源码顺序（H0 `build-deploy-release.yml`）：

| 行 | 内容 |
|---|---|
| 433–436 | 构建 `non_oneshot_services` |
| 438–441 | **`if [[ "${#non_oneshot_services[@]}" -eq 0 ]]` → `::error::...refused...` → `exit 1`** |
| 444–451 | 逐 non-oneshot 服务取 `config --images` |
| 474–551 | per-image 循环（含 `::notice:: passed` / skip） |
| 553–556 | 汇总 `reconcile_rc` |

拒绝分支在 per-image 循环**之前**，且直接 `exit 1`，不会进入循环内的 `::notice:: passed`。契约测试 `tests/test_release_workflow_contract.py:660-673` 断言 `idx_services < idx_reject < idx_per_image`，与源码一致。

可复核命令：

```console
$ git show 5e70321:.github/workflows/build-deploy-release.yml | nl -ba | sed -n '433,475p'
$ git show 5e70321:tests/test_release_workflow_contract.py | nl -ba | sed -n '660,673p'
```

### 2.2 有非 oneshot 服务时行为是否与修复前逐字一致

**结论：除新增的全-oneshot 拒绝 guard 外，per-image 路径与修复前一致；契约测试锁死关键不变式，但没有「逐字 diff 旧 commit」的自动化证明。**

未被全-oneshot guard 触发的路径上，以下逻辑与第 3 轮审查时的 H0（`549e713`）相同：

- `service_images_output` 仍只遍历 `non_oneshot_services`（`:444-451`）
- batch `compose ps` 与 per-service `compose ps` 仍只查 non-oneshot（`:455-459`、`:499-504`）
- per-image 匹配仍用 `image_ref == "${image_name}:${D3_RELEASE_TAG}"`（`:489`）
- skip 文案已改为 `not referenced by any non-oneshot service`（`:530`），契约 `:680-681` 排除旧文案

**证明它的断言**（H0 契约测试，改坏应红）：

- `test_release_image_reconciliation_excludes_oneshot_services_when_listing_running`（`:640-648`）
- `test_release_image_reconciliation_uses_per_image_two_stage_contract`（`:571-608`）
- `test_release_image_reconciliation_does_not_pass_on_any_match`（`:651-657`）

**未证明部分**：没有测试直接 diff `549e713..5e70321` 的 reconcile 脚本字节级相等（除三处改动外）；推断，待证：若中间无其他 commit  touching reconcile block，则「除 guard + compose_rc + skip 文案外逐字一致」成立。本卡核对 `git log 549e713..5e70321 --oneline` 仅见 `5e70321 fix(release): fail loud when oneshot_services covers every compose service`。

### 2.3 `compose_output="$(...)" || compose_rc=$?` 退出码捕获

**结论：修复有效；`<compose ps failed>` 分支现已可达。**

`bash -c` 原样输出：

```console
$ bash -c 'compose_rc=0; compose_output=""; compose_output="$(false)" || compose_rc=$?; printf "compose_rc=%s\n" "$compose_rc"'
compose_rc=1

$ bash -c 'compose_rc=0; compose_output=""; if ! compose_output="$(false)"; then compose_rc=$?; fi; printf "compose_rc=%s\n" "$compose_rc"'
compose_rc=0
```

H0 workflow `:457` 使用新写法；`:460-472` 在 `compose_rc != 0` 时写入 `running_ids_detail="<compose ps failed>"`。该标记**仅作诊断**——batch ps 失败本身不置 `reconcile_rc=1`；per-service `compose ps`（`:499-504`）仍会把关联 image 标红。与第 3 轮 N5 描述一致，不构成假绿路径。

契约测试 `test_release_image_reconciliation_compose_ps_failure_evidence_is_after_capture`（`:627-638`）断言 capture 行在 status 分支之前，且 failure evidence 在 capture 之后。

### 2.4 其他「未验证任何 running 容器却报绿」路径穷举

| 路径 | 触发条件 | H0 行为 | 是否假绿 |
|---|---|---|---|
| 全-oneshot | `non_oneshot_services` 为空 | `:438-441` fail-loud + `exit 1` | **已封堵**（第 3 轮 P1 根因） |
| 每个 declared image 仅被 oneshot 引用 | 有 long-running 服务，但 manifest 里 image 只出现在 oneshot 服务的 compose 映射 | `:529-531` skip + notice；`reconcile_rc` 保持 0 | **否**——锁定决策 #3 授权；caller 契约要求 compose 用短名 `image_name:tag`（README `:50-52`、`release-caller-workflow.yml:61-63`） |
| 全部 declared image 均被 skip | 同上，manifest 无 long-running image | 最终 `:556` notice「passed for all declared images requiring running containers」 | **否**——没有 declared image 需要 running 检查；long-running 服务若存在则不在 manifest 声明范围内 |
| `DECLARED_IMAGES` 为空 | 输入异常 | `:414-417` fail-loud | 否 |
| expected tag 不可 inspect | 镜像缺失 | `:523-527` fail per image | 否 |
| 需要 running 检查但无 running 容器 | non-oneshot 服务无 running | `:542-546` fail per image | 否 |
| image ID mismatch | running 容器 ID ≠ expected | `:549-550` fail per image | 否 |
| batch compose ps 失败但 per-service 成功 | 罕见 | 可能报绿（per-service 验证通过） | **否**——per-service 仍做了 running 检查 |
| 误把 long-running 服务列入 `oneshot_services` | caller 配置错误 | 该 image 被 skip（`:488-489` 过滤 oneshot svc） | **可见配置风险（P2，见 §4.1）**，非本轮 P1：需 caller 显式误配；与锁定决策 #3 耦合一致 |
| compose 使用全 registry 路径而非常规短名 | `image: registry/ns/name:tag` | `image_ref == "name:tag"` 不匹配 → 全 skip | **否（在本仓契约下不可达）**——README/示例/identity gate 均假定短名；fixture 实跑见 §2.5 |

**第 3 轮 P1 根形态（全-oneshot 假绿）已修复；未发现新的 P1 级假绿路径。**

### 2.5 Compose `config --images` 输出形态（匹配前提）

fixture（Docker Compose v5.1.1，目录 `/tmp/compose-reconcile-r4b.*`）：

```console
$ docker compose config --images app
myapp:abc123
$ docker compose config --images migrate
migrate:abc123
```

与 H0 reconcile 的 `"${image_name}:${D3_RELEASE_TAG}"` 精确匹配一致；与 `release_deploy.sh:341-355` identity gate 的 `"${image_name}:"*` 前缀匹配一致。全 registry 路径 fixture（`/tmp/compose-reconcile-r4.*`）输出 `registry.example.com/myns/myapp:abc123`，**不在本仓 caller 契约内**（README `:50-52` 使用短名）。

## 三、退出码捕获穷举扫描表独立验证

本卡对 H0 重跑扫描，对照第 3 轮表：

| 位置 | 写法 | 本卡结论 | 与第 3 轮表 |
|---|---|---|---|
| `build-deploy-release.yml:457` | `compose_output="$(cmd)" \|\| compose_rc=$?` | 已修，正确 | 一致 |
| `build-deploy-release.yml:427,446,499` | `if ! out="$(cmd)"; then <fail-loud>` | 不读 `$?`，当前写法有效 | 一致 |
| `build-deploy.yml:445-459` | `if out="$(cmd)"; then ... else rc=$?; fi` | 正确对照 | 一致 |
| `release_deploy.sh:268,285` | `readarray -t arr < <(cmd) \|\| return 1` | **未修**（issue #29） | 一致 |
| `while ... done < <(cmd)` / `mapfile` / `local x="$(cmd)"` / 管道取 `$?` | — | 全仓无命中 | 一致 |

扫描命令（H0）：

```console
$ git show 5e70321:.github/workflows/build-deploy-release.yml | nl -ba | grep 'if !'
427:          if ! all_services_output="$(cd "$DEPLOY_DIR" && docker compose config --services)"; then
446:            if ! image_ref="$(cd "$DEPLOY_DIR" && D3_RELEASE_TAG="$D3_RELEASE_TAG" docker compose config --images "$svc")"; then
499:              if ! container_id="$(cd "$DEPLOY_DIR" && docker compose ps -q --status running "$svc")"; then
592:          if ! response="$(python3 - ...
636:          if ! response="$(python3 - ...
697:          if ! response="$(python3 - ...

$ for f in .github/workflows/build-deploy-release.yml .github/workflows/build-deploy.yml scripts/release_deploy.sh scripts/pull_and_deploy.sh; do
    echo "=== $f ==="
    git show 5e70321:$f | nl -ba | grep -E 'readarray.*<\(|mapfile|while.*<\(|local .*=\$\(' || echo "(no matches)"
  done
=== .github/workflows/build-deploy-release.yml ===
431:          readarray -t all_services <<< "$all_services_output"
=== scripts/release_deploy.sh ===
268:  readarray -t all_services < <(compose_list_services "$tag") || return 1
285:  readarray -t all_services < <(compose_list_services "$tag") || return 1
```

**通知 step（`:592,636,697,732` 一带）**：均为 `continue-on-error: true` 的 Feishu fail-open 卡片；`if ! response="$(python3|curl...)"` 失败时 `exit 0` 打 warning，**不影响** deploy/reconcile 结论。与第 3 轮表一致，无漏报/错报。

## 四、独立自主审查（换角度）

### 4.1 `oneshot_services` 与对账的跨特性耦合

对账 step 读取 caller 传入的 `ONESHOT_SERVICES`（workflow `:396-397`、remote `:419-422`），与 #24 回滚路径共用同一输入。caller **无法**表达「回滚 skip migrate，但对账仍要求 migrate image 有 running 容器」——两者绑定。

- **是否引入新失效模式**：误把 long-running 服务列入 `oneshot_services` 时，对账 skip 该 image 的 running 检查（§2.4 末行）。forward 路径仍 `compose up -d` 全量服务，探针可绿，对账 skip → **可见 misconfig 风险，非静默**（skip 打 `::notice::`）。
- **能否分开表达**：不能；无独立 input。
- **定级**：**可接受的简化（P3，文档/backlog）**。锁定决策 #3 明确 oneshot 不参与 running 判据；回滚路径已有对称拒绝文案（`release_deploy.sh:294`）。全-oneshot 假绿（第 3 轮 P1）已由 `:438-441` 对称封堵。建议在 README 增一句：误配 oneshot 会同时影响回滚范围与对账 skip。

### 4.2 `if: success()` 完整语义

对账触发：`success() && steps.deploy.outputs.busy_deferred != 'true'`（`:385`）。

| deploy 终止形态 | 远端/脚本行为 | deploy step 结果 | 对账执行？ | 生产是否可能已变？ |
|---|---|---|---|---|
| rc=0 成功 | promote + 探针通过 | step success | **执行** | 是，且对账验证 |
| rc=1 已回滚 | 旧版本健康 | `exit 1`，job fail | **跳过**（`success()` false） | 回滚后旧版本；无 promote 到新 SHA |
| rc=3 busy 延期 | 未进入 deploy | `busy_deferred=true`，`exit 3` | **跳过** | 否（忙锁拒绝） |
| rc=4 回滚不健康 | 生产可能不可用 | `rollback_unhealthy=true`，`exit 4` | **跳过** | 可能（失败路径） |
| rc=130 信号 | 中断 | `exit 130` | **跳过** | 不确定，job 红 |
| SSH rc=255 耗尽（deploy） | 远端可能已跑完 | `exit 1`（`:373-374`），无 `deploy_rc` | **跳过** | **可能**——README `:343-347` 与 failure card `:712-718` 已 fail-loud 要求上机确认；**可见未证实，非假绿** |
| job cancellation / runner 掉线 | 中断 | step 未完成 | **跳过** | 不确定，job 非绿 |
| deploy 成功 + reconcile SSH 255 耗尽 | 部署已 promote | reconcile `exit 1`（`:570-571`） | 执行但**失败** | 是，job 红「identity not proven」 |

**有没有部署实际改变生产但对账被跳过？** 仅 **deploy SSH 255 耗尽** 与 **cancellation/掉线** 两类；均属 job 非绿 + 文档化「状态未知/需上机」，不是静默假绿。不新增 P1。

### 4.3 错误文案准确性

| 分支 | 文案关键词 | 触发是否准确 | 备注 |
|---|---|---|---|
| 无 declared images | `has no declared images` | 是（`:414-417`） | |
| compose services 列表失败 | `could not list compose services` | 是（`:427-429`） | |
| 全-oneshot 拒绝 | `refused: oneshot_services covers every compose service` | 是（`:438-440`） | 与 `release_deploy.sh:294` 对称 |
| config --images 失败 | `could not render compose image for service` | 是（`:446-448`） | |
| expected 不可用 | `expected tag image is unavailable` | 是（`:523-525`） | |
| skip running | `not referenced by any non-oneshot service` | 是（`:529-531`） | 旧文案 `only referenced by oneshot` 已移除 |
| 无 running 容器 | `no running container uses` | **部分准确** | per-service `compose ps` 失败时 `saw_running_for_image=0` 也走此分支（`:499-504` → `:542-546`），实际原因是 ps 失败而非「无容器」——第 3 轮 N5 已登记，不重复计 P1 |
| mismatch | `mismatch for` | 是（`:549-550`） | |
| compose ps 全局失败 | `<compose ps failed>` in `running_ids_detail` | 是（`:471-472`），仅诊断 | |

### 4.4 README 与实现一致性

H0 `README.md:327-347` 描述两段 per-image 对账、oneshot 排除、skip 仅 oneshot 引用的 image、busy/rc=1/rc=4/传输耗尽 skip——**与实现一致**。

**缺口**：README **未**描述 commit `5e70321` 新增的全-oneshot fail-loud（`oneshot_services covers every compose service` → 对账拒绝）。实现与 `release_deploy.sh:294` 回滚对称约束一致，但文档滞后。

- **定级**：**P2（文档）** — `README.md:339-347` 应增一句全-oneshot 对账拒绝语义。
- **不计 P1**：行为 fail-loud，非静默。

## 五、本轮新发现清单

| ID | 定级 | 位置 | 触发路径 | 后果 | 建议修复边界 |
|---|---|---|---|---|---|
| N7 | P2 | `README.md:339-347` | caller 全部服务声明 oneshot | 文档未说明对账会 fail-loud；运维可能不知预期 | README release lane 对账段增一句，与 `build-deploy-release.yml:438-440` 对齐 |
| N8 | P3 | `inputs.oneshot_services` 耦合 | 误把 long-running 标 oneshot | skip 该 image running 检查 + notice；探针仍可绿 | README 警告或 backlog；不新增 input 除非有第二消费者 |

已出账项（#29、#30、N1/N5/N6）本轮不重复登记。

## 六、实跑验证

### 6.1 完整 pytest（H0 detached worktree）

```console
$ python -m pytest tests/ -q
........................................................................ [ 29%]
........................................................................ [ 59%]
........................................................................ [ 89%]
..........................                                               [100%]
242 passed in 41.57s
```

运行目录：临时 detached H0 worktree（本卡执行后已 remove）。

## 七、红验记录

红验在 §1–§2 已提交的真实 verdict 内容**之后**执行；每次只改一处，确认红后用精确还原，未使用整文件 checkout。

### 红验 1：全-oneshot guard

| 项 | 内容 |
|---|---|
| 改坏 | 删除 `build-deploy-release.yml` 中 `if [[ "${#non_oneshot_services[@]}" -eq 0 ]]; then` … `exit 1` … `fi` 整块 |
| 命令 | `python -m pytest tests/test_release_workflow_contract.py::test_release_image_reconciliation_rejects_all_oneshot_compose_services -q` |
| 结果 | `F [100%]`，`1 failed in 0.05s` |
| 还原 | 仅恢复该 guard 块 |

### 红验 2：per-image `config --images "$svc"`

| 项 | 内容 |
|---|---|
| 改坏 | `:446` 的 `"$svc"` → `"$svc_broken"` |
| 命令 | `python -m pytest tests/test_release_workflow_contract.py::test_release_image_reconciliation_uses_per_image_two_stage_contract -q` |
| 结果 | `F [100%]`，`1 failed in 0.05s` |
| 还原 | 仅该行改回 `"$svc"` |

红验在 H0 临时 detached worktree 执行；完成后 worktree 删除，本卡分支 `git status --short` 为空。

## 八、收敛计数

| 轮次 | 审查者 | 新增 P1 | 连续无新增 P1 计数 |
|---|---|---:|---:|
| 第 1 轮 | 主脑 | 1（`config --format` 不被支持） | 0 |
| 第 2 轮 | Cursor | 0 | 1 |
| 第 3 轮 | Codex | 1（全-oneshot 假绿） | 0 |
| **第 4 轮（本卡）** | **Cursor** | **0** | **1** |

**收敛结论**：本轮 0 新增 P1，连续计数升至 **1**；按 `internal` 档 infra/状态机类 diff 规则，需**连续 2 轮**无新增 P1 才收敛。**尚未收敛，还需第 5 轮。** 本轮不得宣告收敛。

## 九、收工自检

```console
$ git status --short

$ git log --oneline -1
f962592 docs(review): PR #28 round-4 verdict §9 self-check git output

$ git show --stat --format= HEAD
 .../reviews/release-reconcile-round4-verdict.md                     | 6 ++++--
 1 file changed, 4 insertions(+), 2 deletions(-)
```

本 verdict 共 5 个 commit（`c61fa1f`..`f962592`），产物路径：`docs/sessions/review-release-reconcile-r4/reviews/release-reconcile-round4-verdict.md`。
