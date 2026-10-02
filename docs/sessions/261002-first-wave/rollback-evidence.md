# 回滚安全声明的端到端消费取证（只读复核，2026-10-02）

范围：复核 `zlxlabs/ci-templates#53` 中「`rollback_safety` 声明了但没有消费者」这一断点，
在 2026-10-02 的真实状态下还剩哪些缺口。本文件**只做只读取证**：没有移动 tag、没有修改
workflow/scripts/registry、没有触发部署、没有在远端 issue/PR 发言。

术语约定（本文件统一使用）：

- **声明源**：ops-dispatcher 的舰队台账 `fleet/registry.yaml`（`rollback_safety` 字段的写入方）。
- **声明模板**：本仓 `.github/workflows/build-deploy*.yml`（声明 `workflow_call.inputs.rollback_safety` 的方）。
- **业务 caller**：服务仓 `.github/workflows/deploy.yml`（实际传 `uses:` 与 `with:` 的方）。
- **消费端生效**：某个 SHA 上的模板代码真的被一次真实 run 执行到（源码合并不等于生效）。

---

## 0. 一句话结论

`rollback_safety` 守卫**在 main 上存在、在 v1/v2 两个 tag 上都不存在**；两个真实
`conditional` 业务 caller（youtube_download_api、live-recorder）都钉 `@v2` 且都不传该输入，
因此「台账标了 conditional 但探针失败仍自动回滚」这一失败模式**在生产上当前可触发**；
但最近 14 天内**实际发生的两次失败都不是回滚**（是服务忙锁延后，rc=3），**已发生错误回滚
= 0 次，潜在错误回滚 = 存在**。

---

## 1. 声明源当前状态（证据 A）

`zlxlabs/ops-dispatcher` @ `fleet/registry.yaml`，blob sha `76d5bf73f35acffb50ccd2799c1f2b4bff64b274`。

取值分布：`safe` 若干条，**`conditional` 4 条，`unsafe` 0 条**。

| 服务 id | 台账声明 | 真实业务仓（`git_url` 核对后） | 是否存在 `.github/workflows/deploy.yml` |
|---|---|---|---|
| `llm-compat-collector` | conditional | `zlxlabs/llm-compat` | **否**（workflows 目录只有 gate*.yml / publish-ghcr / release） |
| `tg-archiver` | conditional | `zlxlabs/tg-archiver` | **否**（workflows 目录只有 gate*.yml） |
| `youtube-download-api` | conditional | `zlxlabs/youtube_download_api`（下划线） | 是 |
| `live-recorder` | conditional | `zlxlabs/live-recorder` | 是 |
| `live-recorder-compress` | conditional | 同上（`component_of: live-recorder`，cron 宿主任务） | 不适用（非 D3 部署车道） |

> 与 #53 2026-09-29 分诊记录的差异：该记录说 3 个 conditional、且点名 `youtube-download-api`。
> 复核后是 4 个 conditional（多 `live-recorder` 与 `live-recorder-compress`），
> 且真实仓名是下划线 `youtube_download_api`——连字符仓名 404，按卡面要求**不盲换仓**，
> 先从台账 `git_url` 取真名再取证。

本仓 `registry.yaml`（ci-templates 自己的台账）只有 2 条服务，`rollback_safety` 全为 `safe`。

## 2. 声明模板当前状态（证据 B）

`git ls-remote https://github.com/zlxlabs/ci-templates.git`：

| ref | SHA | build-deploy.yml 内 `rollback_safety` 命中次数 |
|---|---|---|
| `refs/heads/main` | `9d6c6bffccbe5cded09392074d317991ebbfcf30` | 14（输入 `:53`、透传 `:259`、守卫 `:307-337`、远端变量 `:394`、结局 `:435-436`） |
| `refs/tags/v2` | `129058694a19ddf6e6b44525f2168c128ef40e46` | **0** |
| `refs/tags/v1` | `83b231bbbadc58d4f10fce345c3fa93a6533e367` | **0** |

两个 tag 都是轻量 tag（`ls-remote` 无 `^{}` 解引用行），且都是 main 的祖先，
落后 main 三个与本议题相关的提交：

- `d214586` feat(deploy): consume rollback_safety in build lane
- `4c27b59` fix(deploy): harden rollback safety guard matching
- `1d62aa2` feat(deploy): digest 回滚锚点 + 机读部署回执 + 默认关闭的成功回执卡（#54）

**release 车道**：`build-deploy-release.yml` 内 `rollback_safety` 命中 **0** 次（main 上同样为 0）。
即 #53 的「release 车道不消费」这一条在 2026-10-02 仍然成立，main 与 tag 两侧都未修。

**测试锁定**：`tests/test_workflow_contract.py:148,162,241,272` 锁死了输入枚举/默认值、
env 透传、`rollback_skipped_${rollback_safety}` 结局与 unsafe/conditional 两条实际执行断言。
守卫不是「没人发现的探测器」——改动会让这四个测试变红。

**README / examples 的 pin 仍写 `@v1`**（`README.md:93`、`examples/caller-workflow.yml:11,18`、
`examples/release-caller-workflow.yml:22,42`）。`v1` 比 `v2` 还旧，两个都没有守卫。
文档与真实消费（`@v2`）不一致这一点仍然成立。

## 3. 业务 caller 当前状态（证据 C）

| caller | `uses:` | `ci_templates_ref` | 传 `rollback_safety`？ |
|---|---|---|---|
| `zlxlabs/youtube_download_api` `.github/workflows/deploy.yml:19` | `zlxlabs/ci-templates/.github/workflows/build-deploy.yml@v2` | `v2` | **否** |
| `zlxlabs/live-recorder` `.github/workflows/deploy.yml:18` | `zlxlabs/ci-templates/.github/workflows/build-deploy.yml@v2` | `v2` | **否** |

两份 caller 都带 `workflow_dispatch`，与 #53 item 1 的历史结论一致（无需修复）。

## 4. 真实 run 的回滚结果（证据 D，只读）

窗口：最近 14 天内每个服务取最近 5 个 `deploy.yml` run。

| 服务 | run id | 起始时间 | 结论 | 实际结果 |
|---|---|---|---|---|
| youtube_download_api | 36334555497 | 2026-09-27 | success | 无回滚（未进入回滚路径） |
| live-recorder | 36906037923 | 2026-10-01 | success | 无回滚 |
| live-recorder | 35630540135 | 2026-09-21 | success | 无回滚 |
| live-recorder | **35232221772** | 2026-09-17 | **failure** | `[deploy] service busy: busy lock not acquired within budget — DEFERRED, old container kept` + `deploy DEFERRED — service busy, old container kept` + `Process completed with exit code 3` |
| live-recorder | **35124441366** | 2026-09-16 | **failure** | 同上（rc=3，DEFERRED） |

判读：

- **已发生的错误回滚：0 次。** 两次失败的回滚标记是 `DEFERRED / old container kept`，
  不是 `rollback`。旧容器被保留，没有出现「回滚到错误版本」，也没有捏造数据损坏。
- **可能发生的错误回滚：存在（latent）。** 触发条件是同一服务某次 run 走到
  健康探针失败分支；此时 v2 模板没有守卫、caller 也没传策略，会无条件自动回滚。
- **运行期佐证守卫不存在**：两次失败 run 的 deploy 步骤 env 组里逐项列出了
  `ACR_REGISTRY / IMAGE_NAME / GIT_SHA / DEPLOY_HOST / SSH_USER / DEPLOY_DIR /
  HEALTHCHECK_* / BUSY_LOCK_* / ONESHOT_SERVICES`，
  **没有 `ROLLBACK_SAFETY`**。`ROLLBACK_SAFETY` 这个 env 只在 main 的模板里被注入，
  它的缺席与「跑的是 v2、v2 无守卫」互相印证。
- **Actions 侧 referenced_workflows 的确切 SHA：unknown。** 尝试过两条路都不通：
  REST `repos/{o}/{r}/actions/runs/{id}/jobs` 只回 caller 仓的 `head_sha`
  （`d843c869d65b…` / `db8d7360e6e6…`，是服务仓 commit，不是模板 commit）；
  GraphQL `Repository.workflowRun` 字段在当前 API 版本上不存在。
  逻辑上跑的就是 `v2` → `129058694a19ddf6e6b44525f2168c128ef40e46`（`ls-remote` 权威），
  但这一条按纪律记 **unknown**，不拿推断冒充观测。

## 5. 逐断点定性

| 断点 | 定性 | 证据 | 下一步 |
|---|---|---|---|
| 声明源（台账）有 `conditional` 但无人接线 | **业务 caller 待接线** | §1（4 个 conditional，其中 2 个连 deploy.yml 都没有）+ §3 | llm-compat / tg-archiver 先各自提单补 deploy 车道；两个已有 caller 传 `rollback_safety: conditional` |
| 守卫只在 main，v1/v2 都没有 | **已有源码待发布** | §2（v1/v2 命中 0 次；main 12 次且有 4 个测试锁定） | 按 §6 的 canary→发布计划把 main 上已验证的 build 车道守卫随发布带到 v2；本次不动 tag |
| release 车道不消费 | **代码缺陷（未开工）** | §2（release 模板命中 0，main 上也没有） | 先裁决「策略如何从 registry 到达 caller」与「整组镜像回滚的 conditional 语义」，再动代码；禁止照搬 build 车道补丁 |
| `README`/`examples` 仍写 `@v1` | **文档与真实消费不一致** | §2 | 与 tag 发布同批修；本卡不改（本卡只写证据文档） |
| 「已发生错误回滚」 | **无需修复**（未发生） | §4（两次失败均为 DEFERRED/rc=3） | 保持观察，不做数据修复 |
| 已被 #44 解决的 `paths-ignore` | **无需修复** | §3（两个 caller 均有 `workflow_dispatch`；历史 14 caller 全量结论见 #53 正文订正） | 无 |
| Rss2Wechat 自带内联脚本 | **车道外**（沿用 09-11 分诊） | 本卡未复核 → **unknown** | 若要处理需去该仓提单，不在本仓关单谓词内 |

## 6. 最小 canary + 发布 + caller 协调计划（本卡不执行）

前提：只搬运 main 上已有的 build 车道守卫，**不改语义**；release 车道不夹带。

1. **Canary**：先把 `main` 的 `build-deploy.yml` 挂到一个 canary 服务（`@main`，仓库已有
   `examples/canary-workflow.yml` 的既有车道），在 canary 上故意触发一次探针失败，
   断言日志出现 `rollback safety=conditional; skipping automatic rollback` 且
   结局是 `rollback_skipped_conditional`。停止条件：结局不是该值，或守卫行安装失败
   （模板里已有 `::error::failed to install rollback_safety guard in deploy script` 这条 fail-loud）。
2. **发布**：canary 通过后按本仓既有 v 序列流程推进 tag。停止条件：
   tag 解引用 SHA 的 `build-deploy.yml` 命中 `rollback_safety` 次数与 main 一致；
   同时确认 `build-deploy-release.yml` 未被顺带改动（两模板是刻意分叉，禁止互搬）。
3. **Caller 协调**：tag 到位后再让 `youtube_download_api` / `live-recorder` 传
   `rollback_safety: conditional`。顺序不能反：caller 先传而 tag 没有输入，
   reusable workflow 的未知 input 会直接失败。
4. **全局停止条件**：任一步出现「回滚发生但没有 `rollback_skipped_*` 结局标记」，
   或 probe 假绿被误判为成功，立即停止并回退到当前 tag。

本卡**不执行**以上任何一步：不动 tag、不改 caller、不触发部署。

## 7. 越仓待提单草案（不在本卡执行，不含任何改码）

- `zlxlabs/youtube_download_api`：`.github/workflows/deploy.yml` 的 `with:` 增加
  `rollback_safety: conditional`（依据：ops-dispatcher 台账该服务 `rollback_safety: conditional`）。
  阻塞项：本仓 v2 尚无该输入，需先完成 §6 第 1-2 步。
- `zlxlabs/live-recorder`：同上（台账亦为 `conditional`）。
- `zlxlabs/llm-compat` / `zlxlabs/tg-archiver`：台账已标 `conditional` 但仓内无
  `.github/workflows/deploy.yml`，属未接入部署车道；接入方式需先定，不在本卡范围。

## 8. 未知项清单（禁止当零）

- Actions 运行期 `referenced_workflows` 的模板 SHA：unknown（方法见 §4）。
- Rss2Wechat 内联脚本现状：unknown（本卡未取证，仓名未在台账中定位到）。
- `llm-compat` / `tg-archiver` 是否有别的部署入口（systemd/cron 直跑脚本）：unknown。
- 主干基线：派发时 `gh api` 请求失败，故本卡无「继承红 / 新红」对照基线，
  **继承红未能判定**；本卡为只读取证，未运行任何 CI 作业，因此不存在新红。

## 9. 取证方法与配额

- REST GET 共 16 次（预算 30）：issue 1、台账 3、caller 文件 4、workflow 目录 2、
  run 列表 2、run 日志 2、jobs 1、GraphQL 1。
- 时间窗：最近 14 天；每服务最多 5 run；命中 job 日志 2 份。
- 缓存：脱敏前的原始响应与日志 zip 只落 `/tmp/ck/`（不在 git 内），
  公开证据只保留本文中的 SHA / run id / ref / 结局标记。
- 脱敏：本文不含主机地址、用户名、镜像仓库名、部署目录、本机绝对路径。
