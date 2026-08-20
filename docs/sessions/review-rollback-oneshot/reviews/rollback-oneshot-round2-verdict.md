<!-- delegate-outcome: succeeded -->

# PR #26 第 2 轮独立终审 verdict

- **审查对象（H0 冻结）**：`711d90461d5802847c1de1b337f4e42bbf2092f0..c89e117`（`origin/main..origin/card/rollback-skip-oneshot`，PR https://github.com/zlxlabs/ci-templates/pull/26）
- **本轮新证据**：① 在被审 worktree 抽跑 `python -m pytest tests/ -q`（234 passed）；② bash 探针证实 `readarray < <(false)` 的退出码为 0（见复核 §2.2）；③ 对轴 1 三格做红验（见 §5）；④ 全仓 `*.sh`/`*.yml` grep 全部 `compose up`/`docker start`/`compose restart` 调用点并归类（见降层三问 §3）。
- **风险等级**：`internal`（`AGENTS.md:7`）
- **总体 verdict**：**pass**
- **本轮新增 P1 数量**：**0**
- **收敛计数**：第 1 轮 0 P1 + 本轮 0 新增 P1 → infra/状态机类 diff 已连续 **2** 轮无新增 P1，满足收敛条件。

---

## 1. 降层三问

### 1.1 终态写入成功之前已发生哪些不可逆动作？

#### release lane（`scripts/release_deploy.sh`）

**forward 失败 → 进入回滚前**，按时间序：

| 阶段 | 不可逆动作 | 证据 |
|---|---|---|
| 锁外 staging | 拉取/retag 全部 manifest 镜像为 `<name>:${D3_RELEASE_TAG}` | `release_deploy.sh:206-245` `stage_current_release` → `pull_and_retag` |
| 临界区内 forward | 覆写 `ENV_FILE`（`.d3-release.env`）为**坏** tag | `release_deploy.sh:309-310` `compose_release` 在每次 up 前 `mv` |
| 临界区内 forward | `compose up -d`（**全量**，含 oneshot/migrate） | `release_deploy.sh:370-382` `ROLLBACK_MODE=0` → `up -d` 无服务参数 |
| forward 探针失败 | **不** promote；`last_good_release` 不变 | `release_deploy.sh:505-531` |
| 回滚前 | R4 image-set 对账；拒绝则**不**进入回滚 compose | `release_deploy.sh:549-564` |
| 回滚 | `ROLLBACK_MODE=1`；`ENV_FILE` 覆写为 **previous_sha**；retag/pull 旧组；`compose up -d <子集>` | `release_deploy.sh:600-602` → `compose_release:373-387` |

**回滚 compose 本身失败（rc=4）时残留状态**（PR 前已有、本 PR 未改 forward 语义）：

- 坏 tag 的 forward 已跑过全量 `compose up -d`（migrate 可能已执行，`release_deploy.sh:382`）。
- `ENV_FILE` 在回滚尝试里已被写成 `previous_sha`（`release_deploy.sh:309-310`），但容器组可能处于半切换/探针仍失败。
- `last_good_release` **未**推进（promote 未成功），与原始事故日志一致。
- 本 PR 收窄的是回滚 compose 服务集合（跳过 oneshot），**不**撤销已发生的 migrate。

#### single-image lane（`scripts/pull_and_deploy.sh`）

| 阶段 | 不可逆动作 | 证据 |
|---|---|---|
| forward | `docker tag ${ACR_IMAGE}:${GIT_SHA} ${IMAGE_NAME}:latest` **在** compose up 之前 | `pull_and_deploy.sh:237-238` |
| forward | 全量 `compose up -d` | `pull_and_deploy.sh:251` |
| 探针失败 | 不更新 `last_good_tag` | `pull_and_deploy.sh:297-302` |
| 回滚 | `ROLLBACK_MODE=1`；重新 tag latest 为 prev_good；子集/全量 compose up | `pull_and_deploy.sh:332-334` `deploy_tag` |

**回滚失败后**：`IMAGE_NAME:latest` 可能已指向 prev_good（`pull_and_deploy.sh:238` 在回滚 `deploy_tag` 内再次执行），但探针仍失败 → rc=4（`pull_and_deploy.sh:346-349`）。`last_good_tag` 保持 prev_good。

**结论**：本 PR 的核心不变式——forward 仍全量 up（锁定决策 #2）——意味着 **migrate 在回滚决策前仍可能不可逆**；PR 解决的是「回滚阶段不再重跑 migrate」，不是撤销 forward 已跑的 migrate。回滚失败时的最坏态与 PR 前同类（生产不确定 + last_good 未推进），未引入新的静默成功路径。

### 1.2 守卫值 `ROLLBACK_MODE` 在实际部署形态下是否正确？

#### release lane：既有变量复用

- **赋值点**：仅在进入回滚 `deploy_group` 前设 `ROLLBACK_MODE=1`（`release_deploy.sh:600`），脚本进程内**不再复位**（进程随后 `exit`）。
- **早于回滚 compose**：`600` 在 `602 deploy_group` 之前；`deploy_group` → `compose_release`（`release_deploy.sh:456-463`）。
- **晚于全部 forward compose**：forward 路径 `do_release:501` 调用 `deploy_group` 时 `ROLLBACK_MODE` 仍为 0（初始化 `release_deploy.sh:115`）。
- **信号路径**：`check_pending` 在 `ROLLBACK_MODE=1` 时直接 return 0（`release_deploy.sh:137`），回滚期忽略 INT/TERM/HUP——**既有**语义；本 PR 追加用同一变量分支 compose 服务集合（`release_deploy.sh:370-383`）。
- **耦合风险（≤P2）**：将来若有人为「提前结束回滚期信号忽略」把 `ROLLBACK_MODE` 复位为 0，会同时恢复全量 `compose up -d` 回滚。当前代码无复位，单进程单次 `do_release` 内安全。

#### single-image lane：新增变量 + 显式复位

- **赋值**：`pull_and_deploy.sh:332`（回滚 `deploy_tag` 前）。
- **复位**：`pull_and_deploy.sh:334`，在 `deploy_tag` 返回后**无论** rollback_rc 均执行——随后 `health_probe` 在 `ROLLBACK_MODE=0` 下运行（`pull_and_deploy.sh:340`）。
- **trap/exit 中途跳出**：脚本无 EXIT trap；`deploy_tag` 内失败由 `|| rollback_rc=$?` 捕获（`pull_and_deploy.sh:333`），仍执行 `:334` 复位。若进程被 SIGKILL，后续无部署调用——无实际后果。
- **第三条路径**：busy-lock 循环（`pull_and_deploy.sh:372-427`）与 `flock 9`（`433-434`）不调用 `deploy_tag`；`ROLLBACK_MODE` 始终为 0。

**结论**：两条 lane 的赋值时序均满足「forward 时 0、回滚 compose 前 1」；未发现会在非预期 `ROLLBACK_MODE` 下触发部署函数的第三条路径。

### 1.3 保护覆盖的是「写入」还是「行为」？有无 bypass？

本 PR 保护的是 **回滚路径 `compose up -d` 的服务集合行为**（非状态写入）。

**全仓 compose/docker 启动调用点归类**（命令：`rg 'compose up|docker start|compose restart' --glob '*.{sh,yml}'` 于被审分支）：

| 位置 | 操作 | 是否受 ONESHOT/ROLLBACK 分支约束 |
|---|---|---|
| `release_deploy.sh:387` | `docker compose up -d [services…]` | **是**——唯一 release compose up，经 `compose_release` |
| `pull_and_deploy.sh:249,251` | `docker compose up -d [services…]` | **是**——唯一 pull compose up，经 `deploy_tag` |
| `build-deploy.yml:445` | `docker compose ps` | 否（只读对账，非 up） |
| workflow/release 注释 | 文档性提及 compose up | 否 |

**无** `docker start` / `compose restart` 调用点。

**结论**：不存在绕过 `rollback_compose_services` / `ROLLBACK_MODE` 分支的第二条 compose up 路径。保护覆盖行为且无 bypass。

---

## 2. 主脑第 1 轮 3 条判定 — 独立复核

### 2.1 判 P2：single-image lane `ONESHOT_SERVICES='${ONESHOT_SERVICES}'` 裸单引号

**复核结论：同意，维持 P2。**

- **注入源**：`build-deploy.yml:83-87` 声明 `oneshot_services` 为 `workflow_call` input，默认 `""`；`build-deploy.yml:244,302` 透传至 SSH env。
- **非维护者可控路径排查**：
  - 非 fork PR 的 `workflow_call`：input 由 caller workflow YAML 或 caller 内表达式决定；能改 caller YAML 的人本可执行任意 SSH 命令——不构成相对 baseline 的提权。
  - `workflow_dispatch`：仅当 caller **主动**把 `github.event.inputs.*` 接到 `oneshot_services` 时才暴露；属 caller 仓配置选择，不在本 reusable workflow 控制面内。
  - `ci_templates_ref`（`build-deploy.yml:88-91`）：换脚本 ref 是独立风险面（#16 范畴），不创造 ONESHOT 值的注入通道。
  - org/repo 变量：仅当 caller 显式 `${{ vars.* }}` 传入时生效——仍是 caller 维护者配置。
- **单引号破裂**：值含 `'` → remote_cmd 语法错误 → 部署 fail-loud（非静默）。
- **与 release lane 差异**：release 用 `printf %q`（`build-deploy-release.yml:278-282`；契约测试 `test_release_workflow_contract.py:520-527`）。属 lane 形态不一致，非 P1。

### 2.2 判 P3：release lane `readarray -t all_services < <(compose_list_services …) || return 1` 丢失内层退出码

**复核结论：同意，维持 P3。**

- **探针**（本轮新证据）：
  ```bash
  readarray -t arr < <(false); echo "readarray rc=$?"
  # 输出 readarray rc=0
  ```
  `|| return 1` **不会**因 `compose_list_services` 失败而触发。
- **「碰巧安全」论证**：
  - `ONESHOT_SERVICES` 非空 + `compose_list_services` 失败 → `all_services` 空数组 → `rollback_compose_services` 中 `keep` 空 → 拒绝回滚（`release_deploy.sh:293-295`）；forward 校验同理（`release_deploy.sh:273-278`）。
  - `ONESHOT_SERVICES` **空** + 回滚：走 `compose_release:381-382` 全量 `up -d`，**不调用** `rollback_compose_services`——readarray bug 不介入；compose 坏时 identity gate / `config --images` 仍 fail（`release_deploy.sh:328-331`）。
- **是否存在空数组导致「放行」组合**：未发现。空 ONESHOT 回滚不读 readarray；非空 ONESHOT 空数组一律拒绝。
- **副作用**：错误信息可能误导（以为 compose 成功解析），但不构成静默出错。

### 2.3 判 P3：`for svc in $ONESHOT_SERVICES` 未加引号

**复核结论：同意，维持 P3；主脑「DEPLOY_DIR 下同名文件」路径在本脚本形态下不成立。**

- glob 展开发生在 **脚本当前工作目录**（SSH 远程默认 `$HOME`），**非** `DEPLOY_DIR`——`validate_oneshot_services` / `rollback_compose_services` 均未 `cd "$DEPLOY_DIR"`（对比 `compose_list_services` 内联 cd：`release_deploy.sh:256`）。
- 含 glob 字符的值：展开结果若不含合法服务名 → forward `validate_oneshot_services` 拒绝（`release_deploy.sh:273-278`）；字面量 `*` 无匹配时保持字面 `*` → unknown service → 拒绝。
- **caller 可控**：写 `oneshot_services` 的人即 caller 维护者；无不可信第三方输入面。
- pull lane 用空格填充 substring match（`pull_and_deploy.sh:215-217`），对 token 边界更安全；release 用关联数组——语义等价，非 P1。

---

## 3. 本轮新发现问题

| # | 定级 | 位置 | 触发路径 | 后果 | 建议修复边界 |
|---|---|---|---|---|---|
| N1 | P3 | `tests/test_pull_and_deploy.py`（缺 `promote_failure`/`pending_signal` 变体） | release 轴 2 三触发器（`test_release_deploy.py:1578-1609`）在 pull lane 仅 `test_oneshot_services_probe_failure_rollback_excludes_migrate:1327` | 回归面不对称：release 回滚触发路径有 3 格断言，pull 仅 1 格；未来若 pull 引入信号/晋升失败路径可能无测试红 | 在 pull 测试补对称用例，或文档声明 pull lane 无 promote/pending 路径故仅 1 格 |
| N2 | P3 | `build-deploy-release.yml:363-364` vs `build-deploy.yml:333-336` | rc=4 且 `ONESHOT_SERVICES` 非空 | release workflow `::error` 不含 schema hint；hint 仅在脚本 stderr（`release_deploy.sh:609,615` `oneshot_schema_hint`）。single-image 在 workflow **和**脚本双侧打印 | 运维在 GitHub Checks 摘要里看到的错误文案不对称；日志里仍有 hint，非静默 | 可选：release workflow rc=4 分支对齐 single-image 追加同文案（≤P2 UX） |
| N3 | P3 | `tests/test_*_deploy.py` 轴 1 `invalid_rollback_unreachable` | 参数 `phase=forward`（`test_release_deploy.py:1530`；`test_pull_and_deploy.py:1283`） | 名称暗示测回滚不可达，实际只断言 forward 拒绝；与锁定决策 #3（回滚不校验）一致但易误读 | 改名或注释说明「invalid 在 forward 拦截故 rollback 不可达」 |

**未计入本 PR P1 计数**（范围外/已有 issue）：release lane 镜像对账 #25、探针 curl rc #19、v1 晋级 #16。

---

## 4. 自主审查摘要

### 4.1 轴表格是否锁死

- **轴 1（7 格 × 2 lane）**：`test_oneshot_services_axis1` 两文件各 7 参数化用例（`test_release_deploy.py:1522-1575`；`test_pull_and_deploy.py:1275-1324`）。每格断言 rc、compose up 次数、服务参数 include/exclude——非仅跑通。
- **轴 2（6 格，设计意图）**：release 三回滚触发器 × 1 格 pull 探针失败 = 4 格有测试；workflow 契约 2 格（input default + 透传 quoting）× 2 lane = 4 格；**缺** release workflow rc=4 schema hint 契约（对比 `test_workflow_contract.py:526-532`）。见 N1/N2。
- **零回归（默认空值）**：轴 1 `empty_forward`/`empty_rollback` 断言 `ONESHOT_SERVICES=""` 时 forward/rollback 仍全量 `up -d`（无服务参数）；契约测试 `test_*_workflow_contract.py:512-517` 锁 default `""`。未做与 base commit 的字节级 diff，但新增逻辑均被 `-z/-n ONESHOT` 短路，forward 路径无行为分叉。
- **既有守卫未削弱**：R4 image-set（`release_deploy.sh:549-564`，测试 `test_oneshot_services_image_set_guard_still_blocks_before_rollback:1612`）、identity gate（`release_deploy.sh:328-360`）、busy-lock（`release_deploy.sh:636-698`）、`check_pending`/trap（`release_deploy.sh:136-140,601`）在回滚前仍执行；本 PR 仅在 `compose_release`/`deploy_tag` 内增加 oneshot 分支。

### 4.2 两 lane 行为对称性

| 维度 | release | single-image | 一致？ |
|---|---|---|---|
| forward compose | 全量 up | 全量 up | ✓ |
| rollback compose | 子集 up | 子集 up | ✓ |
| forward 校验 | `validate_oneshot_services` | 同 | ✓ |
| schema hint rc=4 | 脚本 stderr | 脚本 + workflow `::error` | 不对称（N2） |
| 服务列表解析 | `D3_RELEASE_TAG="$tag"` + ENV_FILE overlay | 当前 compose + latest tag | 形态差异为 lane 固有问题 |

### 4.3 `compose config --services` 回滚时机

回滚调用链：`compose_release("$previous_sha")` → 先写 `ENV_FILE=previous_sha`（`release_deploy.sh:309-310`）→ identity gate → `rollback_compose_services("$tag")` → `compose_list_services("$tag")` 内联 `D3_RELEASE_TAG="$tag"` 并读 `ENV_FILE`（`release_deploy.sh:248-256,375`）。

**结论**：服务名来自**主机当前** `compose.yml`；镜像 tag 上下文为 **previous_sha**。若本次发布改动了 compose 服务集合，R4 守卫在 `release_deploy.sh:563` 拒绝回滚——不会 silent partial rollback。若仅改服务定义不改集合，推断，待证：与 PR 前相同局限。

---

## 5. 红验记录

在被审 worktree `/home/zlx/projects/personal/ci-templates-worktrees/rollback-skip-oneshot` 执行；改坏后仅还原该处，已 `git status` 确认 scripts/ 干净。

| 轴 1 格 | 改坏方式 | 命令 | 结果 |
|---|---|---|---|
| release / `valid_rollback` | `rollback_compose_services` 不再 skip oneshot（keep 含 migrate） | `pytest test_release_deploy.py::test_oneshot_services_axis1[valid_rollback-migrate-rollback]` | **FAIL** — `migrate` 出现在 `up -d` 服务列表 |
| pull / `empty_forward` | `deploy_tag` else 分支改为 `up -d app` | `pytest test_pull_and_deploy.py::test_oneshot_services_axis1[empty_forward--forward]` | **FAIL** — 行末非 ` up -d` |
| release / `all_oneshot_rollback` | 删除 keep 为空拒绝逻辑 | `pytest test_release_deploy.py::test_oneshot_services_axis1[all_oneshot_rollback-app migrate-rollback]` | **FAIL** — 缺少 `nothing would remain` |

---

## 6. 抽跑验证

```text
$ cd rollback-skip-oneshot && python -m pytest tests/ -q
234 passed in 43.12s
```

---

## 7. 提交物自检

verdict 文件共 5 个 commit（降层三问 → 复核 → 自主发现 → 自检框架 → git 输出）：

```text
$ git log --oneline -5
64f491f review: PR #26 round2 verdict — 填入提交物 git 输出
5508224 review: PR #26 round2 verdict — 提交物自检
0d8ad7b review: PR #26 round2 verdict — 自主发现、红验与 pytest
7bded5c review: PR #26 round2 verdict — 复核主脑 3 条判定
9d77b99 review: PR #26 round2 verdict — 降层三问
```

```text
$ git show --stat --format= HEAD
 .../reviews/rollback-oneshot-round2-verdict.md | 204 +++++++++++++++++++++
 1 file changed, 204 insertions(+)
```

（`git show` 为系列首 commit `9d77b99` 相对父提交的全文件增量，非末 commit 单行 patch。）

- 审查分支：`card/review-rollback-oneshot`
- verdict 路径：`docs/sessions/review-rollback-oneshot/reviews/rollback-oneshot-round2-verdict.md`
- 被审分支/worktree 无残留改动（红验已还原）
