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
