# 进度存档 — 回滚安全消费端复核（#53，verifying，只读取证）

## 阶段 1：现场与台账核验

- 结论：`main` = `9d6c6bf`；远端 `v2` = `1290586`（不是本地 tag 记的 `c837cd6`，本地那份是陈旧克隆，
  `git fetch refs/tags/v2:refs/tags/v2` 因已存在被拒，必须以 `git ls-remote` 为准）；
  `v1` = `83b231b`。v1/v2 上 `rollback_safety` 命中 0 次，main 上 14 次且有 4 个契约测试锁死。
- 决策：一切模板版本判断以 `git ls-remote` 为准，不看本地 tag、不照 README 里的 `@v1`。
- 下一步：核真实 caller 与 run。

## 阶段 2：caller 与真实 run

- 结论：台账有 **4 个** `conditional`（不是 3 个），其中只有 2 个是真实 D3 caller
  （`youtube_download_api`、`live-recorder`），两者都钉 `@v2` 且都不传策略；
  另 2 个（`llm-compat`、`tg-archiver`）仓内根本没有 `deploy.yml`，属未接线。
  真实仓名是下划线 `youtube_download_api`，卡面给的连字符名会 404。
- 决策：仓名一律先从台账 `git_url` 取，不盲换仓；404 区分仓名/权限/文件不存在。
- 下一步：查 run 是否真的发生过错误回滚。

## 阶段 3：run 结果与定性

- 结论：14 天窗口内两个服务的失败 run 共 2 次，**都是服务忙锁 DEFERRED（rc=3）、旧容器保留**，
  不是回滚。**已发生错误回滚 0 次；潜在错误回滚存在**（v2 无守卫 + caller 不传）。
  失败 run 的 env 组里没有 `ROLLBACK_SAFETY`，独立印证跑的是无守卫的 v2。
- 决策：Actions 侧 `referenced_workflows` 的确切模板 SHA 取不到（REST jobs 只给 caller 仓
  head_sha，GraphQL 字段不存在），记 **unknown**，用「caller 钉 v2 + ls-remote 解引用 + env 无
  ROLLBACK_SAFETY」三条间接证据，不拿推断冒充观测。
- 下一步：逐断点定性 + 写最小 canary/发布/caller 协调计划（只写不执行）。

## 阶段 4：交付

- 结论：证据与计划已落 `docs/sessions/261002-first-wave/rollback-evidence.md`。
  断点定性：已有源码待发布（v1/v2 无守卫）、业务 caller 待接线（4 个 conditional 中 2 个无 deploy.yml）、
  代码缺陷（release 车道不消费）、无需修复（paths-ignore、已发生回滚）、未知（Rss2Wechat 等）。
- 决策：不关闭 #53（release 车道与 caller 接线仍有待办），不动 tag、不改 caller、不触发部署、
  不在远端 issue/PR 发言。父单 #53 保持打开。
- 下一步：由主脑裁决是否派发「build 车道守卫发布到 v2」的实现卡，以及 release 车道的契约设计卡。

---

# 勘误（第二轮，2026-10-02 续修）——以下各阶段旧结论按本节失效

**总原则**：第一版把「样本」当「总体」、把「grep 次数」当「语义证据」、把「取不到」当「不存在」。

## 勘误 1：conditional 数量（作废「4 条」）

- **旧结论（失效）**：「conditional 4 条」「表列 5 行」「3 + 新增 2 = 4」自相矛盾。
- **实测**：对已获取的台账 blob 做结构解析（`services` 列表枚举，非 grep）：
  顶层 service 条目 **22** 条；`safe` **17**、`conditional` **5**、`unsafe` **0**、缺失 0。
- **三个分母**：声明条目 **5** / 唯一仓 **4**（`live-recorder-compress` 是 `component_of: live-recorder`
  的宿主 cron 任务）/ 实测存在本车道 caller 的仓 **2**。三者不可互换。

## 勘误 2：时间窗（作废「近 14 天内已发生错误回滚 0 次」）

- **旧结论（失效）**：「最近 14 天内这两个服务只失败过 2 次」「已发生错误回滚 0 次」。
  两个 failure 实际发生在 09-16 / 09-17，而采集时刻是 10-02，**不在 14 天窗内**。
- **实测**：采集时刻 `2026-10-02T04:44Z`，窗口 `2026-09-18T04:44Z` 起 14 天。
  窗内 run 共 3 个（live-recorder 2 个 success、youtube_download_api 1 个 success）。
  两个 failure 在窗外，单独标注为窗外证据。
- **选取规则与样本量**：每仓 `per_page=5` 取最近 5 个，不翻页。
  live-recorder 列表已跨过窗口起点 → 该仓窗口内采集**完整**；youtube_download_api 列表只回溯到 09-05
  → 该仓窗口内采集**不完整**，真实 run 数未知。
- **修正后的表述**：只能说「所查样本未见错误回滚」；总体是否发生过错误回滚 **unknown**。
  窗口内 3 个 success run 的日志本卡未下载，其回滚标记 unknown；success 也不自动证明未走回滚路径。

## 勘误 3：referenced_workflows（作废「取不到，unknown」）

- **旧结论（失效）**：「Actions 侧 referenced_workflows 的确切 SHA：unknown（REST/GraphQL 均未暴露）」。
- **实测**：该字段就在 `GET /repos/{owner}/{repo}/actions/runs/{run_id}` 的 **run 对象**上，
  不在 jobs 上，GraphQL 也确实没有该字段。两次 failure run 均返回
  `sha = 129058694a19ddf6e6b44525f2168c128ef40e46`（`refs/tags/v2`），两次一致。
- **同时补齐**：caller 文件身份改用 contents API 的 blob sha 锁定（两个 caller 各自一个 blob sha + 读取 ref）。

## 勘误 4：定性收紧

- **作废**「release 车道 = 代码缺陷 / 应立即开发」——无任何消费侧证据，属**待设计范围**。
- **作废**「为 `llm-compat` / `tg-archiver` 新建部署车道的建议」——仓内无 `deploy.yml`
  只能证明没接入本车道，**不证明需要接入**；实际部署入口 unknown，本卡不提扩范围建议。
- **作废**「`workflow_dispatch` 存在 → `paths-ignore` 无需修复」：前者不证明后者，本卡未复验 item 1。
- **作废**「main 命中 12/14 次」作为证据：改用布尔事实「该 ref 上是否声明该输入」，行号只作源码定位。

## 勘误 5：测试与发布计划口径

- **作废**「四个契约测试能红 / 改动会让其变红」——本卡**未运行**任何测试，只能声称源码引用存在。
- **作废**发布停止条件「tag 上 grep 次数与 main 一致」——次数不是语义验收。
  新停止条件改为「具体 SHA + 该 SHA 上消费路径的可观测行为」，并按既有契约
  （`conditional` → 跳过回滚 + `rollback_skipped_conditional` 结局；`unsafe` → 显式失败结局）判定；
  同时明确「不得为赶进度回退已有 tag」。

## 勘误 6：缓存与脱敏偏差处置

- **旧做法（不合规）**：原始 API 响应与 run 日志 zip 放在通用临时目录，公开文档还写了该绝对路径。
- **处置**：按本人创建文件清单归因 → 全部迁移到本卡私有 state 目录，文件 `0600`、目录 `0700`；
  生成白名单脱敏证据 JSON（`redacted-evidence.json`），公开文档不再出现任何本机路径。
- **边界**：通用临时目录**未删除**（可能属于其他会话），只搬走本人文件。
