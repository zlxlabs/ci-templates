# 回滚安全声明的端到端消费取证（只读复核，2026-10-02）

范围：复核 `zlxlabs/ci-templates#53` 中「`rollback_safety` 声明了但没有消费者」这一断点，
在 2026-10-02 的真实状态下还剩哪些缺口。本文件**只做只读取证**：没有移动 tag、没有修改
workflow/scripts/registry、没有触发部署、没有在远端 issue/PR 发言。

> **勘误声明（2026-10-02 第二轮修订）**：本文档第一版存在数量口径不一致、时间窗错误、
> 把 `referenced_workflows` 记为不可得、以及把原始日志缓存在通用临时目录四处问题，
> 均已在本文重写。凡与第一版冲突之处，以本文为准；第一版的结论「已发生错误回滚 0 次」
> **已作废**（理由见 §4）。

术语约定（本文件统一使用）：

- **声明源**：ops-dispatcher 的舰队台账 `fleet/registry.yaml`（`rollback_safety` 字段的写入方）。
- **声明模板**：本仓 `.github/workflows/build-deploy*.yml`（声明 `workflow_call.inputs.rollback_safety` 的方）。
- **业务 caller**：服务仓 `.github/workflows/deploy.yml`（实际传 `uses:` 与 `with:` 的方）。
- **消费端生效**：某个 SHA 上的模板代码真的被一次真实 run 执行到（源码合并不等于生效）。

---

## 0. 有界结论（只覆盖实测到的对象）

1. 台账 `rollback_safety: conditional` **条目 5 / 唯一仓 4 / 实测有本车道 caller 的仓 2**；
   两个 caller 都钉 `@v2`、都不传该输入。
2. 声明模板的该输入只存在于 `main`；`refs/tags/v2` 与 `refs/tags/v1` 上都没有。
3. 两个失败 run（均在 14 天窗口**之外**）确认运行期事实：跑的模板
   `sha = 129058694a19ddf6e6b44525f2168c128ef40e46`（v2），部署 env 无 `ROLLBACK_SAFETY`，
   失败原因是忙锁延后（`DEFERRED`、旧容器保留、退出码 3），不是回滚。
4. **窗口内只查到 3 个 run（全 success），未下载其日志，是否出现回滚标记 = unknown。
   总体是否发生过错误回滚 = unknown（样本不覆盖总体，见 §4）。本卡不宣称「已发生错误回滚 0 次」。**
5. 「标了 conditional 仍被自动回滚」在窗口内**未被观测到发生**；它作为**代码路径层面的潜在缺口**
   成立（v2 无守卫 + caller 不传），但本卡**没有观测到任何一次触发**。

---

## 1. 声明源当前状态（证据 A）

`zlxlabs/ops-dispatcher` @ `fleet/registry.yaml`，blob sha `76d5bf73f35acffb50ccd2799c1f2b4bff64b274`。
解析方式：对该 blob 的实际结构 `services` 列表做枚举统计（不是按行 grep）。

- **顶层 service 条目数 = 22**
- `rollback_safety` 分布：`safe` **17**、`conditional` **5**、`unsafe` **0**、缺失 **0**
- **conditional 条目数 = 5**，对应**唯一仓数 = 4**（`live-recorder-compress` 是
  `component_of: live-recorder` 的宿主 cron 任务，与 `live-recorder` 同仓）

| # | 条目 id | `component_of` | 台账登记的真实仓 | 默认分支 | 仓内实测有无 `deploy.yml` |
|---|---|---|---|---|---|
| 1 | `llm-compat-collector` | — | `zlxlabs/llm-compat` | main | 无（目录内只有 `gate*.yml`、`publish-ghcr.yml`、`release.yml`） |
| 2 | `tg-archiver` | — | `zlxlabs/tg-archiver` | main | 无（目录内只有 `gate*.yml`） |
| 3 | `youtube-download-api` | — | `zlxlabs/youtube_download_api`（下划线） | main | 有 |
| 4 | `live-recorder` | — | `zlxlabs/live-recorder` | master | 有 |
| 5 | `live-recorder-compress` | `live-recorder` | 同 #4 | master | 不适用（非本车道部署单元） |

三个分母口径互不相同，不能互换：**声明条目 5 / 唯一仓 4 / 实测存在本车道 caller 的仓 2**。
对 #1、#2：仓内无 `deploy.yml` 只能推出「**没有接入本车道**」，**不能推出该服务需要接入本车道**；
其它部署入口（systemd 单元、cron 直跑脚本等）本卡未查，记 **unknown**。

与 #53 历史记录的差异（订正）：2026-09-29 分诊说 3 个 conditional 并点名 `youtube-download-api`；
实测 5 条目 / 4 仓，真实仓名是下划线 `youtube_download_api`（连字符名取文件 404，先从台账 `git_url` 取真名）。
本仓 `registry.yaml`：2 条服务，`rollback_safety` 全为 `safe`。

## 2. 声明模板当前状态（证据 B）

`git ls-remote`（远端权威，不使用本地 tag 引用）：

| ref | SHA | `build-deploy.yml` 是否声明 `rollback_safety` 输入 | `build-deploy-release.yml` 是否声明 |
|---|---|---|---|
| `refs/heads/main` | `9d6c6bffccbe5cded09392074d317991ebbfcf30` | **是**（输入 `:53`、透传 `:259`、守卫 `:307-337`、远端变量 `:394`、跳过回滚的结局分支 `:435-436`） | 否 |
| `refs/tags/v2` | `129058694a19ddf6e6b44525f2168c128ef40e46` | **否** | 否 |
| `refs/tags/v1` | `83b231bbbadc58d4f10fce345c3fa93a6533e367` | **否** | 否 |

两个 tag 均为轻量 tag（`ls-remote` 无 `^{}` 解引用行），且都是 main 的祖先，
落后 main 三个相关提交：`d214586`（build 车道消费 `rollback_safety`）、
`4c27b59`（加固守卫匹配）、`1d62aa2`（digest 回滚锚点 + 机读回执 + 默认关闭的成功回执卡）。

**关于「次数」的纠正**：第一版把 `grep -c` 命中次数（正文写 12、后改 14）当证据，自相矛盾且指标不合适。
本版一律用**布尔事实**「该 ref 上是否声明该输入」，行号仅作源码定位。

**测试**：`tests/test_workflow_contract.py:148,162,241,272` 覆盖了输入枚举与默认值、
env 透传、`rollback_skipped_${rollback_safety}` 结局、以及 unsafe/conditional 两条执行断言。
**本卡未运行这些测试**（只读取证、不跑全量套件），因此只能声称「源码引用存在」，
不能声称「这四个测试当前为绿」或「改动会让它们变红」。

**release 车道**：`build-deploy-release.yml` 在 main 与两个 tag 上都没有该输入。**待设计范围**——
是否存在 release 车道消费者、`conditional` 在「整组镜像回滚」下如何解释，本卡无消费侧证据，
不定性为「代码缺陷」，不建议现在动手。
**README / examples 的 pin 仍写 `@v1`**（`README.md:93`、`examples/caller-workflow.yml:11,18`、
`examples/release-caller-workflow.yml:22,42`）而实测消费者用 `@v2`；两个 tag 都没有该输入，
故这是同一条断点的文档面，不是独立缺陷。

## 3. 业务 caller 当前状态（证据 C）

caller 文件身份用 contents API 的 blob sha 锁定，不只记路径：

| caller 仓 | 文件 | 读取 ref | blob sha | `uses:` | `ci_templates_ref` | 传 `rollback_safety`？ |
|---|---|---|---|---|---|---|
| `zlxlabs/youtube_download_api` | `.github/workflows/deploy.yml` | `main` | `230502519c8b97a67eeba1284542022dd96a0142` | `...build-deploy.yml@v2` | `v2` | **否** |
| `zlxlabs/live-recorder` | `.github/workflows/deploy.yml` | `master` | `736c19ce87421dddcfb96c252e2f159a984e9f44` | `...build-deploy.yml@v2` | `v2` | **否** |

两份 caller 都有 `workflow_dispatch`。**注意**：`workflow_dispatch` 的存在只说明有手动触发入口，
**不证明** `paths-ignore` 配置正确；本卡没有核 `paths-ignore` 的具体模式，
所以 #53 item 1 在本卡范围内**不重新判定**（沿用历史结论，未复验）。

## 4. 真实 run 的采集口径与结果（证据 D）

采集口径（明确记录，避免把样本当总体）：

- 采集时刻 `collected_at` = **2026-10-02T04:44Z**
- 窗口 = 最近 14 天 = **2026-09-18T04:44Z ~ 2026-10-02T04:44Z**
- 选取规则：每个仓 `GET /actions/workflows/deploy.yml/runs?per_page=5`，取**最近 5 个** run，
  不再翻页；窗口归属按 `run_started_at` 判断。
- 样本量：live-recorder 5 个（其中窗口内 2 个）、youtube_download_api 5 个（窗口内 1 个）。
- **覆盖率**：live-recorder 的 5 条已跨过窗口起点，故该仓窗口内 run **采集完整**（=2）；
  youtube_download_api 的 5 条只回溯到 2026-09-05，其窗口内 run 可能多于所采 1 个，
  **采集不完整**。
- 本卡只下载了 2 个失败 run 的日志；**窗口内 3 个 success run 的日志没有下载**。

| 仓 | run id | run_started_at | conclusion | 在窗口内 | 日志已查 | 回滚标记 |
|---|---|---|---|---|---|---|
| live-recorder | 36906037923 | 2026-10-01T18:34:36Z | success | 是 | 否 | **unknown** |
| live-recorder | 35630540135 | 2026-09-21T17:12:40Z | success | 是 | 否 | **unknown** |
| live-recorder | 35232221772 | 2026-09-17T14:14:15Z | failure | **否**（窗外，早于窗口 1 天） | 是 | `DEFERRED`（非回滚） |
| live-recorder | 35126639261 | 2026-09-16T23:18:45Z | success | 否 | 否 | unknown |
| live-recorder | 35124441366 | 2026-09-16T16:50:29Z | failure | **否**（窗外，早于窗口 2 天） | 是 | `DEFERRED`（非回滚） |
| youtube_download_api | 36334555497 | 2026-09-27T16:47:47Z | success | 是 | 否 | **unknown** |
| youtube_download_api | 33981605665 / 33974881695 / 33970390769 | 2026-09-05 | success ×3 | 否 | 否 | unknown |
| youtube_download_api | 33970389566 | 2026-09-05T13:57:36Z | cancelled | 否 | 否 | unknown |

两个失败 run 的运行期事实（这是本卡最硬的一条证据）：

- `GET /repos/zlxlabs/live-recorder/actions/runs/{run_id}` 的 **`referenced_workflows` 字段**
  明确给出：`path = zlxlabs/ci-templates/.github/workflows/build-deploy.yml@v2`、
  `ref = refs/tags/v2`、**`sha = 129058694a19ddf6e6b44525f2168c128ef40e46`**。
  两次 run 一致。→ 第一版把此项记为「取不到、unknown」是**错的**，字段就在 run 对象上，
  不在 jobs 上。
- 部署步骤的 env 组逐项列出了全部输入，**没有 `ROLLBACK_SAFETY`**。该 env 只在 main 模板里注入，
  其缺席与 `referenced_workflows` 的 v2 SHA 互相印证。
- 失败原因为 `[deploy] service busy: busy lock not acquired within budget — DEFERRED,
  old container kept`，`Process completed with exit code 3`。**没有回滚**。

因此可以说的与不能说的：

- ✅ 可说：**所查样本（窗口内 3 个 success + 窗外 2 个 failure）内未观测到错误回滚**。
- ❌ 不可说：「近 14 天已发生错误回滚 0 次」——样本不覆盖总体，且 3 个 success run 的回滚标记
  本卡根本没查（success 也不自动证明没走过回滚路径）；也不可说「不存在错误回滚」。

## 5. 逐断点定性

| 断点 | 定性 | 证据 | 下一步（不含本卡执行） |
|---|---|---|---|
| 守卫只在 `main`，`v1`/`v2` 都没有 | **已有源码待发布** | §2（布尔事实 + 行号引用） | 走 §6 的发布计划；本卡不动 tag |
| 实测的 2 个 caller 不传策略 | **业务 caller 待接线** | §3（blob sha 锁定） | 待 §6 第 3 步 tag 到位后再提单接线；顺序不可反 |
| `llm-compat` / `tg-archiver` 仓内无 `deploy.yml` | **入口未知** | §1 目录列表 | 仅记未知；是否需要接入本车道本卡无证据，不提扩范围建议 |
| release 车道无该输入 | **待设计** | §2（无消费侧证据） | 先裁决策略来源与整组回滚语义；禁止照搬 build 车道补丁 |
| README/examples 写 `@v1` | 与「守卫未发布」同源的文档面 | §2 | 与 tag 发布同批修（本卡不改） |
| 窗口内 3 个 success run 的回滚行为 | **未知** | §4 | 如需定论，须下载这 3 个 run 的部署步骤日志 |
| 错误回滚是否曾发生 | **未知**（不是「0 次」） | §4 | 需按 release lane + 更长窗口补采，不在本卡范围 |
| #53 item 1（`paths-ignore`） | **本卡未复验** | §3 注 | 沿用历史结论，不在本卡判定 |
| Rss2Wechat 内带内联脚本 | **未知**（沿用 09-11 分诊） | 本卡未取证 | 需另行取证 |

本卡**不关闭 #53**：至少「守卫未发布」「caller 未接线」「release 车道待设计」三项仍在。

## 6. 最小 canary + 发布 + caller 协调计划（本卡不执行）

**发布验收口径的纠正**：停止条件不能是「grep 命中次数与 main 一致」——次数不是语义。
下表改用「具体 SHA + 该 SHA 上消费路径的可观测行为」。

1. **Canary（语义证据先行）**
   - 对象：仓库既有的 canary 车道（`examples/canary-workflow.yml` 钉 `@main`）。
   - 做法：在 canary 上人为触发一次部署失败（探针失败）。
   - **验收条件**：按 `rollback_safety` 的既有契约取值——
     `conditional` 时必须观测到「跳过自动回滚」的日志与 `rollback_skipped_conditional` 结局；
     `unsafe` 时必须观测到带错误提示的显式失败结局。**不能用 grep 次数代替行为观测。**
   - 停止条件：观测到的结局与传入策略不一致，或守卫安装失败（模板已有 fail-loud 报错分支）。
2. **发布**
   - 做法：走本仓既有 tag 流程。发布前记录候选 tag 解引用后的**具体 SHA**，
     并在该 SHA 上直接核「`build-deploy.yml` 是否声明该输入 / `build-deploy-release.yml` 是否被顺带改动」
     这两个布尔事实（两模板刻意分叉，禁止互搬）。
   - 停止条件：出现任一 —— tag 解引用 SHA 与预期不符；release 模板被意外改动；
     canary 的行为证据与契约不符。**不得为了赶进度回退已有 tag**。
3. **Caller 协调**
   - 做法：tag 到位后，让两个实测 caller 传与台账一致的 `rollback_safety` 值（当前均为 `conditional`）。
   - 顺序不可反：caller 先传而该 ref 尚无此输入，reusable workflow 会因未知 input 直接失败。
4. **全局停止条件**：出现「发生了回滚但没有对应 `rollback_skipped_*` 结局标记」的 run，
   或探针假绿被误判成功，立即停止并回到上一可用 ref。

## 7. 未知项汇总（禁止当零）

- 窗口内 3 个 success run 是否出现回滚标记：**unknown**（日志未下载）。
- 近 14 天是否有其它相关 run、是否曾发生错误回滚：**unknown**（列表截断于 5 条）。
- `llm-compat` / `tg-archiver` 的实际部署入口：**unknown**（仅知其 `.github/workflows` 无 `deploy.yml`）。
- release 车道是否存在任何消费者：**unknown**。Rss2Wechat 现状：**unknown**（未取证）。
- 契约测试当前是否全绿：**未运行**，只能给源码引用。
- 主干基线：派发时 `gh api` 失败，**继承红未能判定**；本卡只读取证、未运行 CI，不存在新红。

## 8. 取证方法、配额与缓存

- 采集时刻 `2026-10-02T04:44Z`；REST GET 累计 **20 次**（第二轮补 4 次：2 个 run 对象 + 2 个 caller
  文件 blob 身份）。分布：issue 1 / 台账 3 / caller 文件 4 / workflows 目录 2 / run 列表 2 /
  run 日志 2 / jobs 1 / GraphQL 1（失败）/ run 对象 2。
- 缓存（按原卡要求只保留脱敏结构化结果）：白名单脱敏证据 JSON 存于本卡私有 state 目录
  （权限 `0600`，目录 `0700`，不进 git）；原始响应与 run 日志 zip 已按「本人创建文件清单」
  归因后迁移至同一私有目录并设为 `0600`/`0700`。本文档不写任何本机绝对路径。
- 脱敏：本文不含主机地址、用户名、镜像仓库名、部署目录、本机绝对路径；
  只保留 SHA / run id / ref / 结局标记 / env 字段名列表。