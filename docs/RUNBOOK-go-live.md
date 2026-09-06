# D3 上线手册（RUNBOOK）

给要把服务接入 D3（`build-deploy.yml` / `build-deploy-release.yml`）、要把 `v1`
tag 往前挪、或要给服务开可选门禁 / release lane 的人看。按阶段推进，每阶段做完
再进下一阶段；出问题按该阶段的「回滚点」退回去，不要带着不确定状态往下走。

背景文档：

- 复用流水线接口、registry 字段清单、门禁用法、release lane 用法 —— 见
  [README.md](../README.md)。
- 忙锁门禁机制细节、锁文件挂载约定、退出码表、验收矩阵 —— 见
  [docs/design/d3-busy-lock-gate.md](design/d3-busy-lock-gate.md)。
- 已知的接受不修事项与重评触发条件 —— 见 [docs/BACKLOG.md](BACKLOG.md)。

## 阶段 0：前置 —— 仓库引用已统一到组织路径

**动作**：确认本仓（以及各服务仓的 caller）里 `uses:` / `repository:` 指向的是
`zlxlabs/ci-templates`，不是旧个人路径 `zj1123581321/ci-templates`（见
README「调用方」一节与 `examples/*.yml`）。旧路径目前靠 GitHub 仓库转移重定向
还能工作，但那是个劫持面，不是可以长期依赖的东西——舰队里存量服务仓的 caller
如果还写着旧路径，不需要专门起一轮迁移，**趁下次要改动那个服务仓的机会顺手把
`uses:` 改成新路径**即可；新接入的服务仓由 `zlxlabs/gate-hub` 的 onboard 脚本
生成，天然就是新路径，不需要人工操心。

**验证点**：

```bash
grep -rn "zj1123581321" --include="*.yml" --include="*.md" --include="*.py" .
```

本仓应为空（本手册这类历史说明性文字除外）。

**回滚点**：纯文本改动，`git revert` 对应 commit 即可，不影响任何已在跑的部署。

caller 的触发条件与 `paths-ignore` 取舍见 [README「触发条件与 `paths-ignore`（可选）」](../README.md#触发条件与-paths-ignore可选)。

## 阶段 1：canary 试跑 —— 验证流水线本身没坏

**动作**：

1. 找 canary 仓（唯一钉 `@main` 的服务，见 `examples/canary-workflow.yml`）——常驻
   canary 仓是 `zlxlabs/url-parse-api`，部署主机 n305。
   **前置**：先确认 canary 自己的构建是绿的。它 2026-08-31 至 09-05 一直红
   （camoufox 撞 GitHub API 未认证限流），期间本阶段整个跑不了；
   构建红的时候别往下走，先修构建。
   推一个低风险变更（哪怕只是加一行注释）触发一次真实 build + deploy。
2. 等 GitHub Actions 跑完，确认 build → push ACR → SSH 部署 → 健康探针全绿。
3. 临时把该服务 caller 的 `healthcheck_expect_status` 改成一个明显错误的值
   （如 `599`），推一次变更，人为制造探针失败。
4. 观察：job 应标红，飞书应收到红色 P0 卡；上主机确认容器已**自动回滚**到上一个
   `last_good` 版本（单镜像看 `.deploy-state/last_good_tag`）。
5. 把 `healthcheck_expect_status` 改回正确值，推一次变更，确认恢复正常部署。

**验证点**：两次推送（正常 / 探针故意失败）在 Actions 里都能看到期望结果；故意
失败那次，飞书红卡到达，主机上跑的容器版本没有变成故障版本。

> **这个注入方法的两个坑，读之前先知道**（2026-09-06 实跑得出）：
>
> 1. **`healthcheck_expect_status: 599` 会同时污染回滚自己的健康探针。** 回滚把
>    `last_good` 镜像放回去之后，它用同一个 input 去探，探到 200 仍判不健康。所以
>    这次演练**必然**停在 `rc=4 / rollback health was not proven`，job 红。这是本方法
>    的预期终点，不是「回滚坏了」。要判回滚有没有真发生，看日志里
>    `rollback health probe FAILED for <tag>` 里的那个 tag 是不是 `last_good`。
> 2. **「服务返 200」不能当回滚成功的证据。** 注入的是错误的期望值，服务从头到尾
>    是健康的——不管回没回滚都返 200。用它当判据等于用一个恒真的东西下结论。

**回滚点**：只动了 canary 一个服务仓的一个 input 值，改回原值即可；全程未触及
`v1` tag，舰队其余 49+ 服务不受影响。

## 现状（2026-09-06 更新，先读这节再读阶段 2）

**阶段 2 描述的「移动 `v1`」方案没有被执行。** 2026-09-05 实际发生的是另一套做法，
本节记录现状，阶段 2 的原文保留作为设计意图与备选方案。

| | 阶段 2 的原设计 | 2026-09-05 实际做法 |
|---|---|---|
| 版本指针 | `v1` 一个可变 tag，`git tag -f v1 main` 推平 | 新建 `v2` tag，`v1` **不动** |
| 舰队 caller | 一行不改，被动升级 | 改了 10 个仓的 `uses:` 与 `ci_templates_ref:` |
| 当前 tag | — | `v1` = `83b231b`（2026-08-14）；`v2` = `c837cd6` |

**为什么停在这里没有回到原设计**：`v1..main` 之间横着 93 个 commit，其中 27 个动了
`scripts/` 或 `.github/workflows/`，`scripts/release_deploy.sh` 一个文件就 +289 行，
改的是对账语义（走 rc=5）、探针判健康的判据、飞书红卡分类、`oneshot_services` 处理。
一旦移动 `v1`，**所有还钉 `@v1` 的仓会在同一时刻被动吃下这 93 个 commit**，
而阶段 1 的失败注入验证当时跑不了（canary 仓构建从 2026-08-31 起一直红，
见 zlxlabs/url-parse-api#28）。

所以现在 `v2` 承担的角色是**隔离带**：钉 `@v2` 的 = 已经吃下新流水线的，
钉 `@v1` 的 = 还在老流水线上的，两拨泾渭分明。

**恢复原设计的前提**（三条都满足才动 `v1`）：

1. canary 仓构建修好（url-parse-api#28 合并且自建 runner 上真实构建绿）。
2. ~~阶段 1 的失败注入在 canary 上跑通，拿到自动回滚 + 飞书红卡的实证。~~
   **2026-09-06 已满足**，见下方「阶段 1 执行记录」。
3. 决定是「把 `v1` 移到 main 并让 caller 退回 `@v1`」还是「保留 v2 方案并改写阶段 2」。

**当前钉法**（2026-09-06 逐仓核实远端字节）：

- `zlxlabs/url-parse-api` —— `@main`，常驻 canary，不参与版本 tag。
- `@v2`（9 个）—— AI_Information_processor、HealthExporter、WechatArticleCommentScrape、
  WechatChatRoomSummary、imflow、obsidian-clip-api、url2gdocs、web_transcibe_translate、
  youtube_download_api。
- `@v1`（2 个）—— **one_translate_tts、shoplazza-capabilities**。
- 裸 commit `597641a2`（2026-07-27，比 `v1` 还老 68 个 commit）—— **live-recorder**。

隔离带必须写出成员名单才有意义。此前本节只写「尚未迁移的仓」，读的人得到的印象是
「大家都在 v2」——ci-templates#45 就是这么产生的。

## 阶段 1 执行记录（2026-09-06）

四项判据全部命中，canary 为 `zlxlabs/url-parse-api`：

| 判据 | 证据 |
|---|---|
| 正常部署全绿 | run `33982274468`（09-05，url-parse-api#28 修完构建后的首次真实构建） |
| 故意失败被识别 | run `34009339238`，`probe-attempts: 200(curl=0)×5` —— 探到 200 但期望 599，判死 |
| 自动回滚到 `last_good` | 同 run，`rollback health probe FAILED for 2a037286ce9b`；`2a037286` 正是上一次成功部署（run 33982274468）的 commit |
| 飞书红卡送达 | 同 run，卡片标题 `P0 回滚健康未证`，接口返回 `StatusCode:0 / success` |
| 还原后恢复正常 | commit `a3d89302` 撤回注入，重新部署 |

演练顺带发现的问题另开单：ci-templates#46（飞书 webhook 存成 `vars` 而非 `secrets`，
每次部署都把完整 URL 明文打进 Actions 日志）。

---

## 阶段 2：移动 `v1` tag —— 让舰队吃到新流水线

**动作**：

```bash
git tag -f v1 main
git push -f origin v1
```

推之前记下**发布 SHA**（`git rev-parse main`）和**移动前 `v1` 指向的旧 SHA**
（移动前先跑一次 `git rev-parse v1`），写进变更记录或飞书群消息里，回滚要用。

**验证点**：全舰队钉 `@v1` 的 caller 是**被动**升级的——下次它们各自触发部署时
才会拉到新版本，`v1` tag 移动本身不会立刻触发任何服务重新部署。可选门禁
（`busy_lock_file` / `busy_lock_timeout`）在未 opt-in 的 caller 上默认关闭，
行为应与移动前逐字节一致（见 README「锁死的核心契约」表的「爆炸半径」一行）。
挑 1-2 个非 canary 服务观察它们下一次自然部署，确认无异常。

**回滚点**：

```bash
git tag -f v1 <移动前记下的旧 SHA>
git push -f origin v1
```

把 `v1` 指回旧 SHA 即可；已经用新版本部署过的服务不会自动回退，需要针对性地
再触发一次部署。

## 阶段 3：live-recorder 接入忙锁门禁（busy-lock gate）

**动作**：严格按顺序，顺序反了等于没有保护（见
[docs/design/d3-busy-lock-gate.md](design/d3-busy-lock-gate.md)「上线顺序」）：

1. live-recorder 服务侧先落地：compose 挂载 `.deploy-state` 目录、任务生命周期
   包 `LOCK_SH`、顺带补 `stop_grace_period`（60–120s）。
2. 用现有 D3 流程（未开门禁）正常部署一次，确认这套挂载 / 持锁改动本身没有把
   服务跑挂。
3. live-recorder 的 caller `with:` 块加上 `busy_lock_file` / `busy_lock_timeout`
   （见 README「部署门禁（可选）」一节）。
4. 在录制任务进行中（服务持着 `LOCK_SH`）推一次部署，确认：job 标红但通知是
   **飞书黄卡**（不 @全员）、旧容器原样保留、镜像已经推到 ACR。
5. 录制结束后，点黄卡上的按钮手动 re-run，确认这次能正常替换成功。
6. 用这套已验证的机制替换掉 live-recorder 仓里过时的
   `docs/handoff/d3-safe-deploy-drain.md` 草案（该草案的 HTTP prepare 协议方案
   已被 flock 方案取代；问题定义 / 验收矩阵部分若仍有参考价值可保留引用）。

**验证点**：录制中推送 → 黄卡到达且旧容器未被打断；录制结束后 re-run → 成功
替换；整个过程 last_good 状态没有被污染成「部署了一半」的中间态。

**回滚点**：`busy_lock_file` 是纯 opt-in input，live-recorder caller 把这一行
删掉（或留空）即可退回未开门禁前的行为，不影响其他服务；服务侧的挂载 / `LOCK_SH`
改动如需回退，按该仓自己的部署流程走（不是 D3 流水线负责的范围）。

## 阶段 4：web_transcibe_translate 接入 release lane（多镜像原子发布）

**动作**：

1. 根目录 `docker-compose.yml` 里 backend/frontend 两个服务的 `image:` 改成
   `${D3_RELEASE_TAG:?D3_RELEASE_TAG is required}` 这类不可变发布变量（见
   README「多镜像原子发布（release lane）」一节）。
2. 新增 `.github/workflows/deploy.yml`，内容照抄
   [`examples/release-caller-workflow.yml`](../examples/release-caller-workflow.yml)，
   按该服务真实的 `host` / `deploy_dir` 改 `with:` 块；`uses:` 先钉 `@main`
   （和阶段 1 canary 同样的爆炸半径纪律，不要第一次接入就吃 `@v1` 的既有产线）。
3. 真实推一次发布，确认两个镜像一起 build、一起 push、一起原子切换。
4. 人为制造一次探针失败（临时改错 `probes_json` 里某条 URL 或
   `expect_status`），确认**整组**回滚到旧版本（不是只回滚一个镜像）。
5. 全部验证通过后，把 `uses:` 从 `@main` 改成 `@v1`，正式接入既有产线。

**验证点**：正常发布时两个镜像的 tag 一致且都是本次 `GIT_SHA`；探针故意失败时
`.deploy-state/release/last_good_release` 没有被更新，旧的两个镜像都还在跑，
不存在「前端新版本 + 后端旧版本」这种半新半旧组合。

**回滚点**：探针失败已经触发脚本自动整组回滚，一般不需要人工介入；如需人工
回退到接入 release lane 之前的状态，把 `deploy.yml` 删掉、compose 的 `image:`
字段改回接入前的写法即可，不影响该服务在此之前的运行方式。

## 阶段 5：收尾 —— BACKLOG 重评触发条件巡检

**动作**：过一遍 [docs/BACKLOG.md](BACKLOG.md) 里每条「接受不修」事项的重评
触发条件，对照阶段 1-4 实际观察到的情况（canary 探针失败频率、忙锁延期频率、
release lane 是否出现同 SHA 重跑漂移等），逐条判断触发条件是否已经命中。

**验证点**：每条 BACKLOG 事项要么「未触发，继续接受不修」，要么「已触发，已拆
出新任务」——不允许停留在「不确定」状态。

**回滚点**：纯巡检，不改代码，没有回滚问题。
