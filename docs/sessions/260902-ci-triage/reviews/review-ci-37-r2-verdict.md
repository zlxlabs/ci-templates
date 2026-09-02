verdict: pass

# PR #37 第 2 轮独立审查（换家：H0..H1 增量四问 + `ps -a` 映射边界）

- 审查对象冻结 H1=`a6ea0d7666ae8f17ca2f2072f530f1100cb0a26d`（H0=`48a67f9`）。增量 `48a67f9..a6ea0d7` 四 commit；全量对照 base `9131e7b`。
- 风险档 **internal**，infra/失败路径提档按 saas 收敛（连续 2 轮无新增 P1）。本轮是第 2 轮、换执行器 grok。
- **本轮新证据**（换家换证据源）：① H0..H1 增量 diff；② PATH stub 四案（旧 `.Image` / 双行顺序）；③ 本机 Compose v5.5.0 profiles/`scale: 0`/scale 2→1/image Recreate 实测；④ 契约测试把 `ps -a --format` 改回 `config --format json` 的红验 AssertionError。不重复第 1 轮正向兑现/降层三问。
- spec = PR #37 正文 + 第 1 轮 verdict。PR 正文仍写 python3/json，H1 已按文件头「无 jq/Python」改成 `ps -a`；正文滞后记 P3。OCR 本轮未跑。

## A. `48a67f9..a6ea0d7` 增量四问

范围：3 files, +53/−114。**四问均通过，不按新增 P1 计。**

### ① 是否只修登记 findings？

**是。** 登记项 = python3 契约漂移、P3-1 历史注释、契约测试改锁新形态。`24dec66` 删 python3、映射改 `ps -a`（`:363`）；`762e03f` stub 改形态并删 python3 用例；`1027d1d` 过渡保活注释；`a6ea0d7` 契约改锁新形态并删保活，留 `#25` 指针（`:361`）。终态 `grep python3` 除文件头第 5 行无命中。未改 pull lane、持锁、rc=5、超时。

### ② 是否新增未经批准的抽象？

**否。** `service_images_map` 是 H0 已有局部表；无新函数/文件/配置项/包装层。熵 −1（删 python3 内联解析与历史注释）。

### ③ 状态/事实源/fallback 是否无依据增加？

**否。** 映射事实源是**替换**不是叠加：`config --format json` + python3 → `ps -a`（登记的无 python3 修复）。`ps` 失败 / 124 超时仍 `return 1`（→ rc=5），无静默兜底、无退回 json 的第二条路径。语义差（声明 vs 实际容器）见 B.2，不构成本问的「无依据增加」。

### ④ 双路径还是各司其职？

**各司其职，不是双路径。**

- `:516-548` `config --images`（无服务参数）是 **compose up 前**身份门：校验 compose **声明**里每个 declared image 都解析为 `<name>:<tag>`。H0 已有，本增量未改。README `:334-337` 写明它防的是「compose 文件还指着旧 tag 就 up」。
- `:363` `ps -a` 是 **promote 之后**对账用的服务→镜像映射，供 `:422` 精确反查。防的是「探针 200 但 running 容器 image ID 不是本 SHA」。

生命周期与问题都不同。测试也写 identity gate 仍用 `config --images`、对账走 `ps -a`。不是同一问的两套实现。

## B. 全量换视角（`ps -a` 作为映射源的边界）

### 1. 映射源前提：`ps -a` 只列出已创建过容器的服务

`non_oneshot_services` 来自 `compose_list_services` → `config --services`（`:339-347`）。缺映射则 `:377-381` fail-loud（文案仍写 compose config，见 P3-1）。

本机 Compose v5.5.0 实测（项目 `reviewci37r2`）：

| 形态 | `config --services` | `up -d` 后 `ps -a` | 对账后果 |
|---|---|---|---|
| 未激活 profile（`worker.profiles: [extra]`） | 不含 worker | 不含 worker | 不进 `non_oneshot_services`，不误红 |
| `scale: 0`（服务名 `scaledown`） | **含** scaledown | **无**该服务行 | 缺映射 → rc=5 |
| `up` 部分失败 | — | — | `compose_release` 返回 `compose_rc`（`:575-578`），不 promote、到不了对账 |

README 无 profiles/scale 条款。唯一 release 消费者 `web_transcibe_translate/docker-compose.yml` 无 profiles、无 `scale`/`replicas`。

**判定：缺映射 → rc=5 是应当红（fail-loud），不是误红。** 对账必须证明每个非 oneshot 服务的镜像；没容器就无法证明。`scale: 0` 若将来被用会在 promote 后 rc=5，与现有「对账失败不自动回滚」同形，且当前消费者不触发。

### 2. `.Image` 字段形态与旧容器

compose v5.5.0：`ps --format '{{.Image}}'` 是创建时的镜像引用（`alpine:3.19` / `transcribe-backend:<tag>`），不是 digest。image 变更会 `Recreate`，旧容器删除，`ps -a` 只留新 tag 一行。

PATH stub（H1 脚本 + 测试同款 docker mock；backend 的 `.Image` 为旧 tag，frontend 为当前 SHA）：

```
CASE B2-backend-old-image  rc=0
[release][evidence] service_images: frontend=frontend:abc123456789,backend=backend:oldoldoldold
::notice::release image reconcile passed for frontend: expected_id=sha256:frontend
::notice::release image reconcile skipped running check for backend (not referenced by any non-oneshot service)
::notice::release image reconcile passed for all declared images requiring running containers
```

两边都是旧 tag 时同样 rc=0，两条 skip + `passed for all declared`。走的是 `:462-465` skip 分支——第 1 轮 OCR 提过的「未引用 → 跳过 running 校验」假绿，在 **stub 输入**下复现。

真实 producer：成功 `up` 之后 `.Image` 已是新引用，这条输入喂不进去（见下节实测）。故不升 P1，记 P2-1。

### 3. 旧容器名 / 多副本：最后一行覆盖

`while IFS=$'\t' read` 后写 `service_images_map["$map_svc"]="$map_img"`（`:372-375`），同 Service 多行是最后一行胜出。

PATH stub 同一服务「旧 exited + 新 running」两种顺序：

```
CASE B3-old-then-new  rc=0
[release][evidence] service_images: frontend=frontend:abc123456789,backend=backend:abc123456789
::notice::release image reconcile passed for backend: expected_id=sha256:backend

CASE B3-new-then-old  rc=0
[release][evidence] service_images: frontend=frontend:abc123456789,backend=backend:oldoldoldold
::notice::release image reconcile skipped running check for backend (not referenced by any non-oneshot service)
::notice::release image reconcile passed for all declared images requiring running containers
```

**判定随顺序漂移**（卡面：漂移即 finding）→ P2-1 同一根因。

本机 v5.5.0：`up --scale backend=2` 两行同 Image；scale 2→1 时 replica-2 被 **Removed**（不是 exited 残留）；image Recreate 后只留新 tag 一行。混合 Image 的同服务两行不是成功路径的 producer 输出。唯一消费者一服务一容器。

### 4. 契约测试变异

注入确认：`scripts/release_deploy.sh:363` 由

`ps -a --format '{{.Service}}\t{{.Image}}'`

改为 `config --format json`（`sed` 前 `grep` 命中；替换后 `grep` 只见 json 行）。然后：

```
FAILED tests/test_release_workflow_contract.py::test_release_image_reconciliation_uses_per_image_two_stage_contract
E       assert "ps -a --format '{{.Service}}\\t{{.Image}}'" in script
tests/test_release_workflow_contract.py:591: AssertionError
1 failed in 0.04s
```

转红为 AssertionError，锁的是活命令串。已 `git checkout --` 还原。H1 全量 `uv run --with pytest,pyyaml,jsonschema python -m pytest -q tests/test_release_deploy.py tests/test_release_workflow_contract.py` → **108 passed in 34.86s**。

该测试不锁「旧 `.Image` 不得 skip」；那条行为缺口并入 P2-1。

## Findings

### P1

无。增量四问通过。

### P2

#### P2-1 `ps -a` 映射把「`.Image` ≠ 本 SHA tag」收成 skip，且同服务多行最后一行覆盖

- 位置：`scripts/release_deploy.sh:372-375` 覆盖写 map；`:422` 精确相等；`:462-465` skip。
- 违反：PR 正文「不再有未引用 → skipped 的静默路径」（映射错位假绿）；本轮 stub 用另一种错位（实际容器 tag ≠ 声明 SHA）再次打进同一 skip。
- 工具标注 / 本仓判定 / 两问：本轮 stub + compose v5.5.0 实测 / **P2** / ①真实使用下会触发吗？**否**——唯一消费者无 scale/profiles；v5.5.0 在 image 变更时 Recreate 并删旧容器，scale-down 也 Removed 多余副本，成功 `up` 后进对账时 `.Image` 已是新引用。②若触发后果能否接受？不能（rc=0 假绿），但第一问不过，不升 P1。
- 建议（不阻塞）：映射改回声明源（无 python3：例如逐服务 `config --images` 已否决；需别的无 python 声明抽取），或 skip 仅保留「oneshot 专用镜像」；同服务多行应 fail-loud / 只收 running。

### P3

#### P3-1 缺映射错误仍写「compose config」

- 位置：`:379` `service ${svc} has no image defined in compose config`；触发源已是 `ps -a`。
- 工具标注 / 本仓判定 / 两问：本轮 / P3 / ①会触发（`scale: 0` 或未创建容器）② fail-loud 可接受，文案不准。
- 建议：改成「ps -a 未给出该服务的镜像」。

#### P3-2 PR 正文未改到 H1 形态

- PR #37 仍描述 python3 / json。工具标注 / 本仓判定 / 两问：本轮 / P3 / ①下一轮主审会按旧正文审 ②不改行为。建议 `gh pr edit`。

第 1 轮 P3-1 已删；P3-2/P3-3 不重审。

## 收敛判定

- 本轮新增 P1：**0**。第 1 轮（Cursor）0 P1 + 本轮（Grok）0 P1 → 连续 2 轮无新增 P1，saas 提档收敛条件满足。
- 增量四问通过；B.2/B.3 的 skip 假绿在 stub 成立、在实测 producer 不触发，按 P1 两问落 P2，不阻断。
- verdict 结构：首行 `verdict: pass`；A 四问各一节；B 四项各一节且 2/3/4 含 stub/红验原文；每条 finding 含工具标注 / 本仓判定 / 两问。
