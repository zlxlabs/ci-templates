<!-- delegate-outcome: succeeded -->

# PR #28 release lane 部署后镜像对账 — 第 2 轮独立终审

- **审查对象**：`origin/main..origin/card/release-reconcile`（HEAD `cb94c117fd8d581db9ee3540a294c582599be6e8`，6 commits）
- **Base**：`ca8cad6b0ea922d5890811cc45cbb4b3ae032074`
- **PR**：https://github.com/zlxlabs/ci-templates/pull/28
- **风险等级**：`internal`（`AGENTS.md:7`）
- **本轮新证据**：① 在被审 ref `origin/card/release-reconcile` checkout 后实跑 `python -m pytest tests/ -q`（240 passed）；② HEAD 干净性三条命令原样输出（§1）；③ bash 探针实跑 `readarray < <(false)` rc=0 vs 命令替换 rc=1（§4.2）；④ 最小 compose fixture 实跑 `docker compose config --images`（§4.1）；⑤ 轴 1 两格红验（§6）；⑥ 全文 diff 与 `release_deploy.sh` promote/identity gate 行号交叉核对。

## 总体 verdict

**pass** — 本轮**新增 P1：0**。第 1 轮 P1（`config --format` Go template 恒红）与 P3（`readarray` 吞退出码）修复经独立复核成立；HEAD 无红验残留；240 项 pytest 全绿。

---

## 一、HEAD 干净性

审查对象 HEAD = `cb94c117`（`origin/card/release-reconcile`）。三条命令在 worktree 对 PR ref 执行：

### 1. `git show HEAD:.github/workflows/build-deploy-release.yml | grep -n 'config --images'`

```
441:            if ! image_ref="$(cd "$DEPLOY_DIR" && D3_RELEASE_TAG="$D3_RELEASE_TAG" docker compose config --images "$svc")"; then
```

变量为 `"$svc"`，非 `$svc_broken`。

### 2. `git status --short`

```

```

（空 — 无未提交改动。）

### 3. `git grep -nE '_broken|BROKEN|XXX|TODO-red' HEAD -- .`

```

```

（无命中 — exit 1。）

**结论**：通过。可继续审查。

---

## 二、降层三问

### 2.1 终态写入成功之前已发生哪些不可逆动作？

对账 step 位于 deploy step 之后（`build-deploy-release.yml:381-385` 紧接 `id: deploy`），且 `if: success() && steps.deploy.outputs.busy_deferred != 'true'`（`:385`）。

Deploy 成功路径下，远端 `release_deploy.sh` 已完成：

1. **镜像拉取 + retag + compose up**（`release_deploy.sh:456-464` `deploy_group` → `compose_release`）
2. **探针通过**（`release_deploy.sh:505-505` `probe_release`）
3. **`last_good_release` promote**（`release_deploy.sh:517-519` `promote "$D3_RELEASE_TAG"`；原子 rename 见 `:424-442`）

此时容器已切换、探针已通过、canonical 状态已推进。**对账红不会撤销上述任何动作**——无回滚调用。

错误文案已说明 post-promote 状态，例如：

- `build-deploy-release.yml:521`：`last_good_release has already been promoted to this SHA`
- `build-deploy-release.yml:611`（契约测试锁死）：`this step does not trigger automatic rollback — manual host verification required`

与 README `:342-345` 文档一致。

### 2.2 守卫用的值，在实际部署形态下自身正确吗？

**守卫值**：`steps.normalize.outputs.image_names`（空格分隔 declared image 名列表）。

**生成**（`build-deploy-release.yml:146-147`）：

```bash
image_names="$(awk -F'\t' '$1=="image"{print $2}' "$RUNNER_TEMP/d3-release.manifest" | paste -sd ' ' -)"
echo "image_names=${image_names}" >> "$GITHUB_OUTPUT"
```

**manifest 语法约束**：

- 构建侧：`normalize_release.py:38-39` `IMAGE_RE = ^[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*$`，最长 128（`:50`）
- 远端：`release_deploy.sh:183` `^[a-z0-9][a-z0-9._-]{0,127}$`，无 tab/空格

image 名**不含空格**；`paste -sd ' '` 与远端 `read -ra DECLARED_IMAGES <<< "$IMAGE_NAMES"`（`:412`）契约一致。

**GitHub Actions step output**：单行空格分隔；多 image 如 `frontend backend` 被 `read -ra` 正确切分。单 image 时数组长度 1，行为正确。

**特殊字符**：manifest 拒绝控制字符（`normalize_release.py:65-66`）；`awk`/`paste` 不 reinterpret 字段。**两端契约一致**。

**`config --images` 返回值匹配**（`:486`）：`"$image_ref" == "${image_name}:${D3_RELEASE_TAG}"`。最小 fixture 实跑：

```
$ cd /tmp/reconcile-fixture && D3_RELEASE_TAG=abc123dead01 docker compose config --images app
myapp:abc123dead01
```

与 deploy 身份门禁同一 bare-name 形态（`release_deploy.sh:342-352`）。

**compose env-file 不对称（≤P2，见 §5 N1）**：deploy 身份门禁加载 `$DEPLOY_DIR/.env` + `$DEPLOY_DIR/.d3-release.env`（`release_deploy.sh:314-317`）；对账 remote 仅 inline `D3_RELEASE_TAG="$D3_RELEASE_TAG"`（`build-deploy-release.yml:441`）。README 锁定的 compose 模式只用 `${D3_RELEASE_TAG}`（`README.md:49-52`），inline shell env 足够；非标准 compose 插值可能 diverge。

### 2.3 保护覆盖的是「写入」还是「行为」？有无 bypass？

对账保护的是 **「本次 SHA 的 declared long-running 镜像确实在生产 running 容器上」** 这一断言（观察 step，不写入状态）。

| 路径 | 可达？ | 后果 | 定级 |
|---|---|---|---|
| **`non_oneshot_services` 为空**（caller 将全部 compose 服务列入 `oneshot_services`） | 是（`:433-436` 过滤后数组空 → `:526-528` 全 skip → `:552` 绿） | 按锁定决策 #3，oneshot 不参与 running 判据；若误配则 CI 假绿 | **≤P2**（设计内极端；见 §5 N2） |
| **declared image 不在 compose 引用** | deploy 前被 identity gate 拦住（`release_deploy.sh:356-358` `found_any==0` → return 1） | 到不了对账 | 无 bypass |
| **单服务 `config --images` 失败** | 是 | `exit 1` fail-loud（`:441-443`），非 skip | 无假绿 |
| **`continue` 分支** | oneshot-only skip（`:527` `::notice::`）；matched pass（`:532` `::notice::`） | 均有日志，非静默 | 无静默 bypass |
| **`busy_deferred`（rc=3）** | deploy `exit 3`（`:364`）→ `success()` false → 对账 skip；`if` 另有 `busy_deferred != true` 双保险（`:385`） | 不执行对账 | 符合锁定决策 #4 |
| **deploy 失败 rc=1/4** | `success()` false | skip | 符合锁定决策 #4 |

**结论**：无新增 P1 级假绿 bypass。全 oneshot skip 是锁定决策 #3 的推论，非实现疏漏。

---

## 三、主脑第 1 轮两条判定独立复核

### 3.1 原 P1（已修）：`config --format` Go template → per-service `config --images "$svc"`

**修复位置**：`build-deploy-release.yml:439-446` 对 `non_oneshot_services` 循环；`:441` 使用 `"$svc"`。

**语义等价性**：原设计意图是对每个 non-oneshot 服务取 rendered image；旧实现（已删除）在解析 loop 内对 oneshot 做 `continue`。新实现通过 `non_oneshot_services` 集合本身排除 oneshot（`:433-436`），**判定范围等价**。

**单服务渲染失败**：`:441-443` `exit 1` + `::error::...could not render compose image for service ${svc}` — **fail-loud**，不会落入 oneshot-only skip 分支。

**实跑验证**（Docker Compose v2 fixture）：

```
$ D3_RELEASE_TAG=abc123dead01 docker compose config --images app
myapp:abc123dead01
$ D3_RELEASE_TAG=abc123dead01 docker compose config --images nosuch
no such service: nosuch
exit=1
```

**结论**：P1 修复成立；88662ac 红验残留已由 `cb94c11` 还原（§1 已证）。

### 3.2 原 P3（已修）：`readarray < <(cmd)` 吞退出码

**修复位置**：`build-deploy-release.yml:426-431` — `config --services` 失败用命令替换捕获到 `all_services_output`，再 `readarray -t all_services <<< "$all_services_output"`。

**契约锁死**：`tests/test_release_workflow_contract.py:592-595` 断言 reconcile run **不含** `readarray -t all_services < <(`。

**分支内是否彻底**：

```
$ grep -n 'readarray' .github/workflows/*.yml scripts/*.sh
.github/workflows/build-deploy-release.yml:431:          readarray -t all_services <<< "$all_services_output"
scripts/release_deploy.sh:268:  readarray -t all_services < <(compose_list_services "$tag") || return 1
scripts/release_deploy.sh:285:  readarray -t all_services < <(compose_list_services "$tag") || return 1
scripts/release_deploy.sh:379:    readarray -t rollback_services <<< "$_svc_list"
```

workflow reconcile step 已改；`release_deploy.sh` 中 process-substitution 写法是**存量**（非本 PR diff 引入），且 `|| return 1` 在函数返回值路径上仍 fail-loud，与本 P3 根因（workflow 内 inline 吞码）不同类。

**bash 探针**（本机实跑）：

```
readarray procsub rc=0
cmdsub rc=1
```

**结论**：本 PR 范围内 P3 修复彻底；存量 `release_deploy.sh` 写法记 backlog（≤P3），不阻塞本 PR。

---

## 四、自主审查

### 4.1 实跑验证

```
$ python -m pytest tests/ -q
240 passed in 40.76s
```

（在被审 ref `origin/card/release-reconcile` checkout 后执行。）

### 4.2 轴表格子锁死情况

**轴 1（六格）** — 对应 `tests/test_release_workflow_contract.py` 六个 reconcile 契约测试：

| 格 | 测试 | 锁死判据 |
|---|---|---|
| 1 | `test_release_normalize_step_exports_image_names_for_reconcile` `:537-547` | normalize step 导出 `image_names` 到 GITHUB_OUTPUT |
| 2 | `test_release_post_deploy_image_reconciliation_is_success_only_and_skips_busy_deferred` `:550-567` | 紧接 deploy；`if` 含 `success()` + `busy_deferred != true` |
| 3 | `test_release_image_reconciliation_uses_per_image_two_stage_contract` `:570-603` | 两段；无 `latest`；`config --images "$svc"`；禁 Go template |
| 4 | `test_release_image_reconciliation_per_image_mismatch_and_missing_running_branches` `:606-612` | mismatch / no-running + post-promote 文案 |
| 5 | `test_release_image_reconciliation_excludes_oneshot_services_when_listing_running` `:615-623` | oneshot 过滤在 `compose ps` 之前 |
| 6 | `test_release_image_reconciliation_requires_all_declared_images_not_any_match` `:626-632` | 禁 any-match 语义 |

**轴 2（五格）** — 错误/跳过/传输路径：

| 格 | 证据 |
|---|---|
| 1 expected unavailable | `build-deploy-release.yml:520-523` |
| 2 running mismatch | `:545-546` |
| 3 no running container | `:539-541` |
| 4 oneshot-only skip | `:526-528` |
| 5 SSH 255 重试 | `:556-573` |

### 4.3 与单镜像 lane 对称性

release lane 两段 per-image、无 latest、oneshot 排除、post-promote 文案均为锁定决策或 lane 差异；SSH 校验/`%q`/255 重试与 `build-deploy.yml` 对齐。唯一疏漏：对账未复用 deploy `--env-file` 链（§5 N1）。

### 4.4 SSH 契约

对账 step 复用 deploy 同一套 `ssh_user`/`host` 校验（`:398-399`）、`printf %q` + heredoc stdin（`:404-409`）。无新增注入面。

---

## 五、本轮新发现问题清单（只登记，不修）

| ID | 级别 | 位置 | 触发路径 | 后果 | 建议修复边界 |
|---|---|---|---|---|---|
| N1 | P2 | `build-deploy-release.yml:427-441` vs `release_deploy.sh:314-328` | compose 用非 D3_RELEASE_TAG env 决定 image 行 | 对账渲染与 identity gate 不一致 | 对账 remote 补 `--env-file` 链，或 README 禁止非标准插值 |
| N2 | P2 | `build-deploy-release.yml:526-528` | 全部服务标 oneshot | 全 skip → 对账绿但不验 running | 文档警告；或拒绝全 oneshot（需主脑决策） |
| N3 | P3 | `tests/test_release_workflow_contract.py:570-632` | 契约仅 YAML 文本 | 88662ac 类事故可再发生 | canary 集成测或文档标明边界 |

**本轮新增 P1：0**。

---

## 六、红验记录

基线 commit = `cb94c117`（`git status --short` 空）。

| 轴 1 格 | 改坏方式 | 命令 | 结果 | 还原 |
|---|---|---|---|---|
| 格 3 | `:441` `"$svc"` → `"$svc_broken"` | `pytest ...::test_release_image_reconciliation_uses_per_image_two_stage_contract -q` | **FAIL** | 单行改回 |
| 格 2 | reconcile `if:` 去掉 `busy_deferred` 条件 | `pytest ...::test_release_post_deploy_image_reconciliation_is_success_only_and_skips_busy_deferred -q` | **FAIL** | 单行改回 |

红验后 `git status --short`：**空**。

---

## 七、契约测试证明力边界

`tests/test_release_workflow_contract.py` reconcile 断言均为 workflow YAML 文本 `in run` — **不 SSH、不跑 docker**。

- 能证明：字符串形态、step 顺序、`if` 门控、错误文案存在
- 不能证明：远端 compose 命令成功、promote 后 running 容器 ID 匹配

240 passed **不等于** canary 主机实部署验证；合并前仍需 canary 观测（本卡非目标）。

---

## 八、收敛计数

| 轮次 | 新增 P1 |
|---|---|
| 第 1 轮（主脑） | 1（已修） |
| 第 2 轮（本卡） | **0** |

infra/状态机类「连续 2 轮无新增 P1」：**已满足**。

---

## 九、收工自证

```bash
$ git status --short

```

Verdict 分支：`card/review-release-reconcile`。
