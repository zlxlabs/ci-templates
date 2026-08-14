# ci-templates

舰队级（50+ 自托管服务）的**版本化 GitHub Actions 复用流水线** + **服务清单（registry）单一真相源**。

每个服务的 CI 缩成 ~10 行调用本仓的 `build-deploy.yml`；两条原有 shell 脚本
（`push_to_acr.sh` / `pull_and_deploy.sh`）变成这条 reusable workflow 的内脏。

> **不做 GitOps / K8s** —— 与 compose + SSH 舰队不匹配，过度建设。
> 这是 Layer A 底座 D3，与冻结的 Layer B（web-api 重工作流）解耦。

## 仓库结构

```
.github/workflows/
  build-deploy.yml      # 复用流水线 (workflow_call)，每服务 ~10 行调它
  build-deploy-release.yml # 多镜像原子发布（独立接口，不改变单镜像 lane）
  ci.yml                # 本仓自测：registry 校验 + pytest
scripts/
  push_to_acr.sh        # build + 打 git-SHA 不可变 tag + push ACR
  pull_and_deploy.sh    # SSH 端：flock 锁 + 健康探针 + 失败自动回滚
  validate_registry.py  # registry.yaml schema + 唯一性 + DSN 明文校验
registry.schema.json    # registry 契约（JSON Schema draft 2020-12）
registry.yaml           # 舰队 host→service 单一真相源
examples/
  caller-workflow.yml   # 服务仓 caller 模板（钉 @v1）
  canary-workflow.yml   # canary 服务模板（钉 @main）
tests/                  # pytest（schema + 部署逻辑 + workflow 契约）
```

## 多镜像原子发布（release lane）

需要让 frontend、backend 等镜像一起切换的服务调用
`build-deploy-release.yml`。`images_json` 是严格 JSON 数组，每项至少包含
`image_name`、`build_context`、`dockerfile`；可选 `build_alias` 让 worker 别名共享一次
构建（同一 alias 的 context 和 Dockerfile 必须一致）。未知字段、重复 image 名、控制字符、
绝对路径和 `..` 路径都会在构建前拒绝。`probes_json` 是必填的严格 JSON 数组；生产发布
至少配置 frontend 入口和 backend API 两个探针，例如：

```json
[{"url":"http://localhost:8080/","expect_status":200},
 {"url":"http://localhost:8000/healthz","expect_status":200}]
```

Compose 必须使用不可变发布变量（Docker Compose v2，`config --images` 需可用）；公共的
nginx 等额外镜像可以继续存在：

```yaml
services:
  transcribe-backend:
    image: transcribe-backend:${D3_RELEASE_TAG:?D3_RELEASE_TAG is required}
  transcribe-frontend:
    image: transcribe-frontend:${D3_RELEASE_TAG:?D3_RELEASE_TAG is required}
```

`busy_lock_file` / `busy_lock_timeout` 是可选服务 admission 门（默认空路径关闭，兼容旧行为）。
启用后远端按服务忙锁再主机锁的顺序进入整组切换；忙时返回 rc=3，caller 只发黄色延后卡，
不会 SSH 重试或提升 last-good。

构建仍使用不可变 `${GITHUB_SHA::12}`。远端脚本分两个阶段运行：先在锁外拉取并校验这次发布的
不可变 SHA 镜像并 retag 为 `<image_name>:<sha>`，完成本地 staging；随后才按忙锁 → host `flock`
顺序进入临界区，在双锁内仅写入统一的 `D3_RELEASE_TAG` compose 环境文件，运行
`config --images` 身份门禁、`docker compose up -d`、探针和 promote/rollback。这样等待服务忙锁时
不会写 compose/env/state，也不会在拿到锁后重复拉取或 retag。全部探针通过后才原子提升唯一权威
状态 `.deploy-state/release/last_good_release`（首行是 SHA，其余是完整 manifest 内容，同目录
rename 保证原子性）；`last_good_sha` / `last_good_manifest` 是提交后尽力而为写入的兼容视图，
供人工排查读取，可能滞后于（甚至在极端情况下缺失于）canonical 文件——排查以
`last_good_release` 为准。失败时按旧 manifest 整组回滚；回滚 compose 后还必须用同预算的
`probe_release` 证明旧版本在应答，证明通过才返回 `rc=1`。首次发布没有旧版本时拒绝伪回滚，
直接返回 `rc=4`，不会伪造 last-good。同一 commit 重跑会在新 runner 上重建镜像，Dockerfile 应钉死
基镜像、锁定依赖，才能保证同 SHA 产物可复现；两次发布之间新增/删除/改名了镜像后，旧版本回滚不受
支持——脚本会显式拒绝并保持容器现状，返回 `rc=4`，绝不做部分回滚，需要立即人工介入。

## 调用方（每服务 ~10 行）

把 `examples/caller-workflow.yml` 放进服务仓 `.github/workflows/deploy.yml`：

```yaml
jobs:
  ship:
    uses: zlxlabs/ci-templates/.github/workflows/build-deploy.yml@v1
    with:
      image_name: web-api
      host: 100.64.0.1           # Tailscale 可达 IP/MagicDNS（runner 无 ~/.ssh/config，不能用别名 host-1）
      ssh_user: deploy
      deploy_dir: /srv/automation/web-api
      healthcheck_url: http://localhost:8001/healthz
    secrets:                       # 6 个显式传，不用 inherit
      ACR_USERNAME: ${{ secrets.ACR_USERNAME }}
      ACR_PASSWORD: ${{ secrets.ACR_PASSWORD }}
      SSH_DEPLOY_KEY: ${{ secrets.SSH_DEPLOY_KEY }}
      KNOWN_HOSTS: ${{ secrets.KNOWN_HOSTS }}
      TS_AUTHKEY: ${{ secrets.TS_AUTHKEY }}             # runner 临时入 tailnet 连内网目标机
      CI_TEMPLATES_PAT: ${{ secrets.CI_TEMPLATES_PAT }} # 仓库已公开，当前版本此 secret 保留但未使用，传任意占位值即可
```

> `host` 自 D3 激活起是 **Tailscale 可达地址**（IP/MagicDNS），不再是 `~/.ssh/config` 别名 ——
> GitHub runner 上没有用户的 ssh config，临时入 tailnet 后只能按 IP 连。别名 `host-1` 仍用于 registry 与人读。

## 本地网络 registry 拉取（可选）

D3 部署链路现状是「VM201 构建 → push 公网阿里云 ACR → 目标机从公网 ACR 拉取」，
2026-07-27/28 两天 4 次部署失败均由「目标机→ACR」这条公网链路超时导致（实测约
20% 失败率 + 偶发分钟级完全中断）。已在 VM201 上搭一个可写的本地网络 registry
（同网段拉取实测 0.458 秒），本仓支持把它接为部署的主拉取源，ACR 降级为异地存档
+ 拉取回退。

opt-in 用法，在 caller 的 `with:` 块加一个 input（不加则行为与现状逐字节一致——
纯 ACR 构建/推送/拉取，不受本特性影响）：

```yaml
    with:
      # ...其余 input 照常...
      local_registry: zlx-vm-work-i7-ci-runner.taile9071.ts.net:5001
```

开启后：
- **构建阶段**双推:本地 registry(部署关键路径)+ ACR(异地存档 + 拉取回退)。只有
  **两处都推失败**才判致命(镜像哪都不在,必须报错);单边失败允许降级继续,并打
  醒目 `::warning::`——本地失败会退化为"这次纯 ACR",ACR 失败会警示"这次没有异地
  备份/拉取回退",但不会静默放行。
- **部署阶段**拉取时先试本地 registry(默认 2 次快速失败,基础延迟 1 秒,总预算
  ~3 秒——同网段链路真出问题大概率是"整个不可达",给它套 ACR 的 150 秒预算纯属
  浪费),不通就无缝回退到未改动的 ACR 拉取预算(`PULL_RETRIES`,默认 6 次、线性
  退避、累计 150 秒)。本地拉到的字节会 retag 成规范 `ACR_IMAGE:tag` 名字——
  `last_good_tag` / 回滚逻辑只认这个名字,不关心字节来自哪个 registry。
- 其它部署目标机尚未验证 tailnet 可达性 / 域名解析,**逐仓手动 opt-in**,不要
  批量打开。

## 部署门禁（可选）

有的服务在跑不可打断任务（如录制、转写）时不希望被替换容器打断。开启后：
服务侧对一个锁文件持**共享锁**（表示"有任务在跑，别打断我"）；部署脚本替换容器前
对同一个文件申请**排他锁**——拿到即证明当前无任务在跑、且新任务也进不来，随即
完成 `compose up -d` + 探针 +（如需）回滚；等待超过预算仍拿不到锁，则本次**延期**：
旧容器原样保留，不做任何替换。

opt-in 用法，在 caller 的 `with:` 块加两个 input（不加则行为与现状完全一致）：

```yaml
    with:
      # ...其余 input 照常...
      busy_lock_file: /srv/automation/web-api/.deploy-state/busy.lock
      busy_lock_timeout: "600"   # 可选，默认 600s（10 分钟）
```

延期时的表现：GitHub job 仍是**失败态**（标红，诚实反映"新 SHA 未上线"），但通知
分流成**飞书黄卡**（非红色 P0 卡、不 `@全员`）——因为这是正常的延期，不是故障。
新镜像已经推到 ACR（不可变 SHA tag），空闲后点黄卡上的按钮手动 **Re-run** 该
workflow 即可补上线；re-run 会在新 runner 上完整重建同一 SHA 镜像（产物逐字节相同、
push 幂等极快），代价是多花几分钟构建时间，不是"跳过 build"。

> ⚠️ **上线顺序**：必须先让服务侧完成持锁实现并挂载好 `.deploy-state` 目录，
> 用现有 D3 流程验证过一次正常部署之后，再给 caller 打开 `busy_lock_file`。
> 顺序反了 = 锁文件没人持有 = 形同没有保护——部署脚本发现锁文件缺失时只会创建
> 它并打一条显著 `WARN`，**不会**拦住部署（warn-and-proceed，不 fail-closed）。

机制细节、锁文件挂载约定、退出码表、验收矩阵见
[`docs/design/d3-busy-lock-gate.md`](docs/design/d3-busy-lock-gate.md)。

### 部署退出码与处置

两条 lane 共用以下退出码语义。每次部署的生产状态和 on-call 处置以这张表为准：

| 退出码 | 语义 | 生产状态 | on-call 处置 |
|---|---|---|---|
| `rc=0` | 新版本已上线且通过健康探针 | 新版本，已验证在应答 | 无需动作 |
| `rc=1` | 新版本探针失败，已回滚，且**回滚后的同预算探针已通过** | `last_good`（单镜像 lane 为 `last_good_tag`，release lane 为 `last_good_release`），已验证在应答 | 不需要紧急上机 |
| `rc=3` | busy-lock 门禁超时，本次延期，未替换容器 | 上一版本，完全未动 | 空闲后点黄卡按钮重跑 |
| `rc=4` | 新版本不健康，且脚本未能证明生产停在一个健康版本上 | 不确定，可能不可用 | **立即上机** |
| `rc=130` | 收到 `INT` / `TERM` / `HUP` | 仅 release lane 有此码；发布可能被中断，状态需确认 | 立即确认远端状态 |
| `rc=255` | SSH 传输层失败 | 未知；若远端已推进，不能据此假设未上线 | 重跑；若本 run 早前出过 `255`，存疑就上机确认 |

`rc=1` 的含义只有表中这一种：它证明回滚后的旧版本已在**同一探针预算**内通过探针。
这是本次新增的证明步骤；在此之前，`rc=1` 只证明回滚 compose 被执行过，并不证明旧版本真的在应答。
因此 `rc=1` 与 `rc=4` 的处置严格互斥：前者不需要紧急上机，后者必须立即上机。

`rc=4` 的具体来源因 lane 略有不同。单镜像 lane 包括：无 `last_good_tag` 可回滚（含首次部署、
`last_good` 等于本次 SHA）、回滚的 pull/compose 执行失败、回滚 compose 成功但回滚后探针仍失败。
release lane 包括：无 `last_good_release` 可回滚（refusing pseudo-rollback）、镜像集较上次发布已变化
而不支持回滚（rollback impossible）、回滚的 `deploy_group` 执行失败、回滚 compose 成功但
`probe_release` 仍失败。无论来源是哪一项，`rc=4` 都表示生产可能不可用，不能按「未上线、无需处理」理解。

`rc=4` 优先于 `rc=130`：回滚未自证健康时返回 `4`，因为生产状态的坏消息比「被中断」更需要人知道。
飞书通知分流为：`rc=3` 发不 `@全员` 的延期黄卡；`rc=4` 发 `@全员` 的紧急卡，明确提示「生产可能不可用，
必须立即上机」；其余失败发普通 P0 卡，并按失败 step 处置。

### 回滚前现场取证

回滚 compose 会永久销毁新版本容器的日志，因此脚本在**回滚之前**先把现场写入 workflow 日志：
`docker compose ps`、容器尾部日志，以及探针每次尝试的 HTTP code 与 curl 退出码。三段证据统一使用
`[deploy][evidence]` 前缀，格式分别是 `compose-ps`、`container-logs`、`probe-attempts`；例如：

```
[deploy][evidence] probe-attempts: 000(curl=28),000(curl=28),000(curl=7)
```

证据用于判断回滚是否可能是误判：探针序列全为 `000` 说明连不上（很可能只是服务还没起来），而
`5xx` 说明服务活着但返回了坏结果。回滚前保留的这组信号，是 on-call 判断「刚才那次回滚是不是误判」
的关键信息。

### 探针预算与冷启动调参

当前默认探针预算为：

| 参数 | 默认值 |
|---|---:|
| `HEALTHCHECK_WARMUP` | `5` 秒 |
| `HEALTHCHECK_RETRIES` | `5` 次 |
| `HEALTHCHECK_INTERVAL` | `3` 秒 |
| `HEALTHCHECK_TIMEOUT` | `5` 秒 |

最坏耗时约为 `HEALTHCHECK_WARMUP + HEALTHCHECK_RETRIES × (HEALTHCHECK_TIMEOUT + HEALTHCHECK_INTERVAL)`，
即 `5 + 5×(5+3) = 45` 秒；最好是 warmup 结束后首探立即通过，也就是 5 秒后判定成功。服务方应把
这组值当作一次部署和回滚共同使用的预算：回滚后的旧版本也必须在同一预算内通过，才可以返回 `rc=1`。

Python、Node 等冷启动较慢，或启动时还需连接外部依赖的服务，应在 caller 中显式调大预算，尤其是
warmup、retries 或 timeout；否则服务可能只是尚未完成启动，就被判定为不健康并触发**误回滚**。
误回滚会在生产中连续做两次容器替换，可用性抖动是不回滚时的两倍。默认值本次刻意不改，因为静默
调大它会改变 50+ 个存量服务的部署行为；请服务方根据自己的启动时间显式调参。

### 部署后镜像事实对账（单镜像 lane）

`build-deploy.yml` 的健康探针通过后，还会在同一目标机上无条件对账三段事实：本次
`${GIT_SHA}` 镜像 tag 可 inspect、`<image_name>:latest` 与它拥有相同 image ID、以及
`docker compose ps -q --status running` 找到的至少一个运行容器通过 `docker inspect` 使用该
image ID（显式限定 `running` 而不依赖 `compose ps` 的版本默认过滤）。
三段中任一不成立，workflow 判红并打印 expected / latest / running 的实际值；SSH
对账连接只对传输层 `rc=255` 做有限重试，最终不可达也判红，不会降级为绿灯。`rc=3`
延期、`rc=1` 已回滚和 `rc=4` 的部署不会执行对账。

部署失败通知读取调用方 repo variables:
- `FEISHU_CI_WEBHOOK`: 目标飞书自定义机器人 webhook。
- `FEISHU_CI_TITLE_PREFIX`: 机器人关键词标题前缀；未配置时默认 `[zlxlabs·CI]`。

这两个变量通常由 `zlxlabs/gate-hub` 的 `scripts/onboard-repo.sh` 按 `registry.yaml`
里的 `notify_category` 写入，避免个人 / fordeal / 合伙人项目的 CI 卡混到同一群。

## 门禁（pre-merge）在哪

pre-merge 门禁的 reusable workflow（gate.yml）曾于 2026-07-09 短暂迁入本仓，同日再迁至
**[`zlxlabs/gate`](https://github.com/zlxlabs/gate)**（org 公开仓）：org runner group 的
`restricted_to_workflows` 白名单只接受 **org 内仓库**的 workflow（实测个人账号公开仓
不行），这道硬闸必须配上。本仓回归只管部署 lane（build-deploy）。

## 锁死的核心契约（来自 plan-eng-review）

| # | 契约 | 落点 |
|---|------|------|
| **A4** | secrets **显式声明，不 `inherit`** —— 只 6 个 secret 可见，最小权限 | `build-deploy.yml` `secrets:` 块 + `test_workflow_contract.py` |
| 爆炸半径 | caller 钉 `@v1` 不钉 `@main`；canary 仓先吃 `@main`，验证后移 v1 tag | `examples/*` |
| **A3** | 每主机 **flock** 串行化 + GitHub **concurrency group** `deploy-<host>` | `pull_and_deploy.sh` + `build-deploy.yml` |
| **A3** | git SHA **不可变 image tag**；记录"上一个 good"；回滚不覆盖并发部署 | `push_to_acr.sh` / `pull_and_deploy.sh` |
| 上传边界 | 只发布不可变 SHA 镜像；每次 ACR `docker push` 最多 **5 分钟**（TERM 后 15 秒强杀）、最多 **3 次**、间隔 10 秒；不重建、不重复部署。部署机再将已验证的 SHA 本地 retag 为短名 `latest` 供 compose 使用 | `push_to_acr.sh` / `pull_and_deploy.sh` |
| **A3** | 健康探针真定义（endpoint/超时/重试/期望状态/warmup），失败 → **自动回滚**；回滚本身同样受探针门约束，未通过则升级为 `rc=4` | `pull_and_deploy.sh` `health_probe()` |
| **A3** | 健康探针通过后仍需证明本次 SHA 已实际运行：expected SHA image → `latest` → running container image ID 三段对账 | `build-deploy.yml` 镜像对账 step |
| **A1** | registry **JSON Schema** + 唯一性约束 + **只存 DSN 引用** → CI fail fast | `registry.schema.json` / `validate_registry.py` |

## registry.yaml 字段清单（D4/D5/D7 共用）

每服务一条，全部必填（校验器强制）：

| 字段 | 说明 | 约束 |
|------|------|------|
| `id` | 服务 id（kebab-case），即 ACR image 名 | **唯一**，`^[a-z0-9][a-z0-9-]*$` |
| `git_url` | 源码仓 URL | `http(s)://` 或 `git@` |
| `default_branch` | 默认分支 | 非空 |
| `host` | 部署主机（`~/.ssh/config` 别名） | 非空 |
| `deploy_dir` | 主机上绝对路径（含 compose） | 绝对路径；`(host, deploy_dir)` **唯一** |
| `port` | 对外端口 | 1–65535，**唯一** |
| `glitchtip_project` | GlitchTip 项目名 | 非空 |
| `sentry_dsn_secret` | DSN 的 **secret 名**（如 `SENTRY_DSN_FOO`） | `^[A-Z][A-Z0-9_]*$`，**绝不存 DSN 明文** |
| `monitor_slug` | GlitchTip cron monitor slug | **唯一**，kebab-case |
| `heartbeat_url_secret` | （cron 服务才需）GlitchTip Heartbeat check-in URL 的 **secret 名**（如 `ZLX_HEARTBEAT_URL_FOO`） | `^[A-Z][A-Z0-9_]*$`，**绝不存 URL 明文**；运行时经 `ZLX_HEARTBEAT_URL` 注入 |
| `tier` | 环境/爆炸半径分层（D7） | enum `dev`/`staging`/`prod` |
| `rollback_safety` | 回滚是否安全 | enum `safe`/`unsafe`/`conditional` |
| `healthcheck_url` | （可选）覆盖默认探针 URL | — |

校验本地跑：

```bash
python scripts/validate_registry.py registry.yaml
```

CI 在 PR 时自动跑 —— 漏字段 / 重复 slug / 重复端口 / DSN 明文 一律 **fail fast**。

### 多镜像（release lane）服务怎么登记

registry 仍然**一服务一条**，schema 不变。`id` 的语义是「服务标识」（对多镜像
服务而言就是 compose 项目本身），**不是** ACR 镜像名——release lane 服务的镜像名
不来自 registry，而来自 caller 自己声明的 `images_json`（见
[`examples/release-caller-workflow.yml`](examples/release-caller-workflow.yml)），
一个服务对应多个镜像，例如 `<id>-backend` / `<id>-frontend`。这是上表
「`id` 即 ACR image 名」这条假设的**显式例外**：该假设只对单镜像（`build-deploy.yml`）
服务成立；`healthcheck_url` 同理不适用（release lane 的探针配置在 caller 的
`probes_json` 里，registry 不重复登记）。`deploy_dir` 仍指向该服务 compose 文件
所在目录，两条 lane 共用同一套 host/deploy_dir 唯一性约束。

## 测试

```bash
pip install pytest pyyaml jsonschema
python -m pytest -q
```

- `test_registry_schema.py` —— 8 个坏 fixture（缺字段/重复 id/port/slug/路径、DSN 明文、heartbeat 明文、坏 enum）全部报错；好 registry 通过。
- `test_pull_and_deploy.py` —— flock 并发串行化、不可变 SHA tag、探针失败自动回滚、回滚后探针、`rc=4` 分流、回滚不提升坏 tag、本地 registry 快速路径 + 不可达时回退 ACR（docker/curl mock，无需真实守护进程）。
- `test_release_deploy.py` —— release lane 多镜像发布与整组回滚、回滚后探针、`rc=4` 分流（docker/curl mock，无需真实守护进程）。
- `test_push_to_acr.py` —— ACR 推送有界重试、本地 registry 双推的致命/非致命语义（单边失败降级继续、双边失败才致命）。
- `test_workflow_contract.py` —— workflow 只声明 6 个 secret、无 `inherit`、per-host concurrency、`local_registry` input 安全默认值。
- `test_caller_examples.py` —— `examples/*.yml` 与真实接口对齐:6 secret、`ssh_user`、host 是 Tailscale IP、caller 钉 `@v1` / canary 钉 `@main`。

## 端到端 / canary（需真实凭证与主机，未在本机执行）

`workflow_call` 本体 + 真机 SSH 部署属**集成**，本地无法纯单测。推荐流程：

1. 配 6 个 secrets（个人账号无 org secrets，每 repo `gh secret set --repo` 配）：
   `ACR_USERNAME` / `ACR_PASSWORD` / `SSH_DEPLOY_KEY` / `KNOWN_HOSTS` / `TS_AUTHKEY` / `CI_TEMPLATES_PAT`。
2. 建一个低风险 canary 服务仓，用 `examples/canary-workflow.yml`（钉 `@main`）。
3. push → 观察 build→ACR→SSH 部署→探针→（人为让探针失败）→自动回滚。
4. 全绿后再 `git tag -f v1 && git push -f origin v1`，存量服务的 `@v1` caller 才吃到新流水线。

> ⚠️ 真实 canary 会向生产主机 `host-1` 部署并推 ACR，属对外不可逆操作，需人工授权后执行。

## 版本与爆炸半径

- caller **必须钉 `@v1`**（主版本 tag），不钉 `@main`。
- 只有 canary 仓吃 `@main`。验证通过后移动 `v1` tag 推平舰队。
- 一个坏 commit 进 `@main` 只炸 canary 一个，不会一次炸 50 个部署。

### 发布 `v1`（单人维护版）

1. 合并修复到 `main`；让唯一 canary 服务继续引用 `@main`，推一次低风险变更。
2. 在 Actions 确认 build、ACR、SSH、健康探针和回滚门均通过。
3. `git tag -f v1 main && git push -f origin v1`；记录发布 SHA。引用 `@v1` 的服务在下次部署自动采用该版本。

不要为单个服务的紧急修复移动 `v1`；该服务可临时钉具体 commit，待 canary 验证后再统一发布。
