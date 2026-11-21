# 设计稿：build-deploy 忙锁推迟后的部署意图持久化与主机侧收敛（ci-templates #95）

状态：已认可，实施中（C2）
基线：7c18471f8954ba35ec6dec60f69693f49b0cbd64。

写侧 opt-in 开关（C1 落地名）：环境变量 `INTENT_FILE_ENABLED=true` 才走意图路径；未设、空、或其他值则意图代码整体跳过，现有 deferred 行为逐字节不变。意图文件路径由 `DEPLOY_DIR` 派生，caller 不传路径。下文草稿中的 `INTENT_FILE` 均指本开关（C3 的 workflow input 仍是 `deferred_deploy`）。README 推广步骤归 C4。

写侧函数因 `pull_and_deploy.sh` file-size 闸（800 行）落在同目录 `scripts/deferred_intent.sh`，由主脚本在 `INTENT_FILE_ENABLED=true` 时 source。启用但该文件缺失 → 配置校验阶段 `exit 1`（fail loud）；未启用不要求该文件存在。C3 scp 部署脚本时须把 `deferred_intent.sh` 拷到同一目录。

## 问题陈述与事实基线

`scripts/pull_and_deploy.sh` 的忙锁门禁（opt-in）在 `BUSY_LOCK_TIMEOUT` 预算内拿不到「忙锁 EX + 整机锁 EX」双锁时 `exit 3`（DEFERRED），旧容器原样保留（[实测] `scripts/pull_and_deploy.sh:726,731` 两处 exit 3：`flock -w` rc=1 分支与预算耗尽分支）。workflow 侧 rc=3 分支只写 `deferred=true` output、打一条 `::warning::... re-run this workflow when the service is idle` 后 `exit 3`（[实测] `.github/workflows/build-deploy.yml` rc=3 分支）。此后：不写部署回执（README 明文「忙锁 deferred 不写回执」）、不发任何通知（通知 step 的 `if:` 条件全部排除 deferred）、没有任何机制会重跑这个部署。「要部署这个版本」的意图在 deferred 瞬间即丢失。

实测佐证（gh api 只读，zlxlabs/live-recorder）：

- run 44108250779（run#130，2026-11-17 14:54，PR#112）与 run 44111383925（run#131，2026-11-17 15:11，PR#111）失败步骤均为 `Deploy over SSH`——两个版本相隔 17 分钟先后撞上录制，均 deferred。
- 2026-10-15 至 10-19 该仓没有任何部署 run；10-14 PR#61 的部署 run 38538345067（run#97）failure 之后，靠 10:49 PR#62 的下一次 push 补上线；10-20 当天 run#100–#104 连续 5 个 failure（持续撞录制约 4.5 小时），直到 17:56 run#105（PR#70）才 success。卡面所述「#63 生产停在旧代码 4 天」「共 4 次 deferred」「靠人工设 systemd 一次性定时器补救」与 run 时间线大方向一致（需求方陈述，run 级结论无法从 API 区分 deferred 与其他失败，不逐条复核）。

d3-busy-lock-gate 设计（`docs/design/d3-busy-lock-gate.md`）2026-07-23 拍板「延期后补上线=手动 re-run；跑一两个月看频率，烦了再加自动重试」。该触发条件已命中（约 4 个月内 ≥4 次 deferred，最长停摆以天计）。本设计稿是该拍板的后续，但**不走「自动重跑 workflow」路线**（锁定决策：不以 schedule/定时重跑触发部署；不为部署阻断录制）。

第一性：部署是命令式一次性动作，deferred 后意图即丢。解法是把意图**持久化为主机上的期望版本**，由主机侧收敛器（忙锁的属主本来就在主机上）在拿到锁后应用**最新**意图，并留下可回读回执。workflow 定时重跑整条流水线是补丁（重建镜像、占 runner、GitHub concurrency 队列还会顶掉排队 run），不在考虑范围。

## 1. 意图记录

### 位置

`${HOME}/.d3-converge/intents/<sanitized-deploy-dir>-<hash8>.json`（ssh_user 的家目录，部署用户可写）。

- **否决：放 `${DEPLOY_DIR}/.deploy-state/`（与 last_good_tag、last_deploy_result.json 同目录）**。`.deploy-state` 被 compose bind mount 进容器（d3 设计明文约定，live-recorder 已挂载）；容器内 root 可写 bind mount。意图 JSON 若含「从哪个 ACR_IMAGE 拉镜像」等执行参数，容器内被攻破的进程可伪造意图，诱导宿主收敛器以攻击者选定的参数执行官方部署脚本 = 把「容器内写一个锁文件」的 DoS 面升级为「替换生产镜像」的接管面。收敛器目录树不在任何容器挂载路径内（[推断] live-recorder compose 只挂 `.deploy-state` 与媒体目录，不挂部署用户整个家目录；实施卡须在该仓复核一次挂载清单），伪造面消除。同时消除「DEPLOY_DIR 本身可能被某服务挂进容器」的不确定性——不依赖任何关于 caller compose 的假设。
- sanitize 规则：部署目录路径剥去前导 `/`、非 `[A-Za-z0-9._-]` 替换为 `_`，追加 deploy_dir 的 sha256 前 8 位十六进制防歧义碰撞（`/opt/a_b` 与 `/opt/a/b` sanitize 后同名，hash 区分）。规则只在写侧（`scripts/deferred_intent.sh` 的 `sanitize_deploy_dir`）实现；收敛器不反推文件名、不需要 sanitize，部署目录从意图文件的 `deploy_env.DEPLOY_DIR` 读取（C2 拍板：两侧各一份 sanitize 的要求作废）。

### 格式

JSON，schema_version 1，与 `last_deploy_result.json` 同风格（原子写：mktemp + mv）：

```json
{
  "schema_version": 1,
  "status": "pending",
  "repo": "zlxlabs/live-recorder",
  "run_number": 131,
  "run_attempt": 1,
  "run_id": 44111383925,
  "run_url": "https://github.com/zlxlabs/live-recorder/actions/runs/44111383925",
  "git_sha": "abc123def456",
  "script_ref": "pull_and_deploy.live_recorder.sh",
  "created_at": "2026-11-17T15:11:04Z",
  "updated_at": "2026-11-17T15:21:04Z",
  "attempts": 0,
  "last_attempt_at": null,
  "last_alert_at": null,
  "notify_on_success": false,
  "webhook": "https://open.feishu.cn/...",
  "deploy_env": {
    "IMAGE_NAME": "live-recorder",
    "ACR_IMAGE": "crpi-....aliyuncs.com/zlx-personal/live-recorder",
    "LOCAL_IMAGE": "",
    "GIT_SHA": "abc123def456",
    "DEPLOY_DIR": "/opt/media/live_recorder",
    "HOST_LOCK": "/var/lock/fleet-deploy.lock",
    "HEALTHCHECK_URL": "http://localhost:8080/healthz",
    "HEALTHCHECK_EXPECT_STATUS": "200",
    "HEALTHCHECK_RETRIES": "5",
    "HEALTHCHECK_INTERVAL": "3",
    "HEALTHCHECK_WARMUP": "10",
    "ROLLBACK_SAFETY": "safe",
    "BUSY_LOCK_FILE": "/opt/media/live_recorder/.deploy-state/busy.lock",
    "BUSY_LOCK_TIMEOUT": "600",
    "ONESHOT_SERVICES": ""
  }
}
```

要点：

- `deploy_env` 是**逐键枚举的白名单快照**（与 `build-deploy.yml` deploy step 传给远端脚本的 env 一一对应），不是任意 env 透传。收敛器只读白名单键。`GITHUB_RUN_NUMBER` / `GITHUB_RUN_ATTEMPT` 由 workflow 显式以 `D3_RUN_NUMBER` / `D3_RUN_ATTEMPT` 传入远端（[实测] 当前 `deploy_once()` 的 ssh env 未传这两个值，需新增两行）。
- `webhook` 是通知凭据，见第 4 节的落盘决策。
- 文件权限 `0600`，属主 = ssh_user（部署用户），与收敛器同用户。

### 单调判定：`(run_number, run_attempt)` 字典序

写侧（deferred 时）规则：已存在意图且其 `(run_number, run_attempt)` **严格大于**本次 → 拒绝覆盖，stdout 打 `[deploy][evidence] intent: superseded`，照常 `exit 3`；否则（大于等于 → 覆盖，含同 run 高 attempt 的 re-run）原子替换，打 `intent: accepted`。旧意图被覆盖前先追加一行到 `${HOME}/.d3-converge/events.jsonl`（append-only 历史：谁被谁顶掉）。

- **拍板 run 序号，否决 git 提交时间**：commit 时间可伪造、rebase/force-push 后旧 commit 的 author/commit time 可晚于新 commit；deferred 的 re-run 还是同一个 commit。GitHub run 序列才是「发布意图」的真实顺序。
- **否决 run 侧 created_at 时间戳比较**：runner/主机时钟漂移可在数秒级乱序，恰好落在两个连续 deferred run 的窗口内（#130/#131 相隔 17 分钟尚安全，但不能依赖时钟）。
- **否决收敛器本地自增序号**：主机重装、多 run 并发写时本地计数漂移，且 GitHub 已提供免维护的单调数。
- [推断] GitHub 保证同一 workflow 文件在同一 repo 内 `run_number` 按触发顺序单调递增；`re-run` 保留 run_number、attempt 递增。已知的失效场景：caller **重建/重命名 workflow 文件**可能重置 run_number，导致新部署意图永远小于旧意图而被静默拒绝。该场景以年计低频，后果可用 24h 周期告警暴露（收敛器持续 pending 的告警照发），处置 = 人工删除意图文件（RUNBOOK 记载）。不建证明机器自动甄别（BACKLOG「拆除 255 后自动判成功证明机器」同款理由：证明条件可被历史污染，维护面大于价值）。
- 这个比较键自动给出正确行为的两个既有习惯场景：(a) 旧黄卡的手动 re-run（attempt+1，run_number 不变）在有更新意图 pending 时被拒——旧版本 re-run 不会回退掉新版本；(b) GitHub concurrency `deploy-<host>`（`cancel-in-progress: false`）已保证同主机 run 按 run_number 串行执行，写侧天然按序。

## 2. 执行契约一致

**原则：收敛器零部署逻辑。** 它不重实现探针、回滚、对账、忙锁循环中的任何一个；它只做「按意图快照的 env，重放同一次部署所用的同一个脚本字节」。

- **脚本固化**：`build-deploy.yml` 在 `intent_file` opt-in 时，把本次 run 实际执行的那个部署脚本（`safe` 时是仓内 `pull_and_deploy.sh` 原件；`unsafe`/`conditional` 时是 awk 注入守卫后生成的脚本，[实测] 该生成发生在 workflow 侧 deploy step 内）以 `scp` 安装到 `${HOME}/.d3-converge/scripts/<script_ref>`（`script_ref` = `pull_and_deploy.<sanitized-deploy-dir>.sh`，per caller 隔离，两个 caller 的 rollback_safety 不同互不影响）。意图记录 `script_ref` 字段指向它。**执行契约由此字节级一致**：守卫、探针预算、回滚语义、对账、回执写入（`last_deploy_result.json`）全部是同一段代码。每次部署 run（无论成败）都刷新安装（哈希相同则跳过写），收敛器与模板的版本漂移窗口收敛到一个 run 内部。
- **否决：脚本快照写进 STATE_DIR/意图文件同目录**——即第 1 节否决过的容器可写位置，不赘述。
- **否决：收敛器运行时从 GitHub 拉脚本**——主机无 GitHub 凭据（CI_TEMPLATES_PAT 已废，runbook 明载），离线不可用，且把「部署脚本内容」这一执行契约绑到收敛时刻的网络与权限状态上。
- **否决：意图记录存脚本内容或哈希供收敛器校验**——校验基准与被校验对象在同一可写位置时校验无意义；脚本由部署 run 直接安装到收敛器目录，安装通道本身即信任锚（与今天 scp 部署脚本到 /tmp 同一信任模型，收敛器目录只是 /tmp 换成了持久且容器不可见的路径）。
- **收尾清理差异**：在线部署 ssh 命令里 `rm -f` 临时脚本；收敛器目录的脚本持久保留（下个意图可能仍引用同一字节），清理随模板 major 升级的 RUNBOOK 步骤走。
- 回执复用：`do_deploy()` 成功/失败路径写的 `last_deploy_result.json`（`write_deploy_result`）与 `DEPLOY_EVENT_LOG` 钩子在收敛器重放时照常工作——**已上线的详细回执格式零新增**。deferred 时写意图失败是唯一新增的 fail-loud 分支：意图目录不可写/磁盘满时打 `[deploy][evidence] intent: write-failed` 到 stdout 并照常 `exit 3`（deferred 语义不变，workflow 侧凭标记把黄卡升级为 P1 卡）。
- 配置校验新增一条：`INTENT_FILE` 机制启用但 `BUSY_LOCK_FILE` 为空 → `exit 1` fail fast（没有忙锁就没有 deferred，意图永不触发，纯误导配置）。

## 3. 锁模型相容

收敛器**不含部署锁逻辑**（不重实现忙锁/整机锁循环）。每次尝试 = 以意图 env 调用固化的 `pull_and_deploy.sh`，锁循环（先忙锁 EX 带剩余预算 → 非阻塞探整机锁 → 被占立即放忙锁、sleep 5s clamp 重试 → 双锁在手才进替换窗口 → 预算耗尽 exit 3）原样执行。由此免费继承全部既有语义。

**意图文件互斥**：写侧与 C2 对意图 JSON 的读-比较-替换（含 `status` 更新、events.jsonl 追加）必须持有 `${HOME}/.d3-converge/intents.lock`（`flock -w 30 -x`，阻塞等待、默认 30s 上限；拿不到 → `[deploy][evidence] intent: write-failed` 并仍 `exit 3`）。C2 **必须用同一把锁**，否则收敛器改 status 与 deferred 写入会竞态。这把锁只管意图记录，不是部署锁。


- **等待期间不互持**（[实测] `scripts/pull_and_deploy.sh` 等锁循环 + `docs/design/d3-busy-lock-gate.md` 时序图）：收敛器等忙锁时不攥整机锁（不堵同主机其他服务部署），整机锁被占时不攥忙锁（admission 保持打开，录制任务照常接单）。**正在录制的任务不可能被收敛器打断**；唯一受影响窗口是替换窗口本身（`compose up` + warmup + 探针，约 30–60s，期间新 LOCK_SH 申请失败、任务不启动）——这正是忙锁门禁的既有契约，不是新增暴露。
- 收敛器每轮尝试的时长 ≈ `BUSY_LOCK_TIMEOUT`（600s，沿用意图快照值）——忙锁预算内等到了就部署，等不到 exit 3 返回。systemd timer 周期（2min）小于忙锁预算，实际尝试节奏由收敛锁串行化，约 10–12 分钟一轮。
- **应用前核对最新意图**：`converge.sh` 读意图 → 快照 `(run_number, run_attempt)` 与全部 env → 调脚本。执行期间新 deferred run 原子替换意图文件不影响进行中的进程（env 已入进程）。rc=0 后 re-read：文件仍是自己应用的意图 → 置 `status=deployed`；已被更新意图替换 → 新意图保持 pending，下一轮 timer 收敛（最终一致收敛到最新版本）。短暂中间态（文件已显示新意图 pending、生产还在跑旧意图版本）由 events.jsonl 与下一轮收敛消除。
- **与手动部署并发**：真人/Agent 手动跑 workflow 部署时，与收敛器在主机侧由 HOST_LOCK（fd 9）串行化、服务侧由忙锁竞争；双方锁申请顺序固定「先忙锁后整机锁」，不构成环。GitHub concurrency 域（workflow 之间）与收敛器域（主机侧）互不感知，靠主机锁兜底——与今天「GitHub concurrency + SSH flock 双层」的既有设计同一结构。
- **收敛器自身的并发保护**：`flock -n ${HOME}/.d3-converge/converge.lock`（防 timer 触发重叠实例），非阻塞拿不到即退出（上一实例还在跑）。

## 4. 回执与通知

### 主机回执（状态机，可回读）

意图文件 `status` 字段单向演进，回执四态对卡面的映射：

| status | 卡面四态 | 谁写 | 语义 |
|---|---|---|---|
| `pending` | 已接收 | deferred 的 `pull_and_deploy.sh` | 意图已持久化，等待空闲窗口 |
| `running` | 已接收（执行中） | converge.sh 执行前置位 | 收敛器正持有/等待锁执行该意图 |
| `deployed` | 已上线 | converge.sh rc=0 后置位 | 详细回执在既有 `last_deploy_result.json`（零新格式） |
| `failed` | 失败 | converge.sh rc∈{1,4,5,其他非 0 非 3} 后置位 | 自动重试停止，需人工 |
| `interrupted` | 失败（变体） | converge.sh 启动时发现上次 running 无终态 | 上次执行被中断（kill/主机重启），生产状态未证 |
| `abandoned` | 放弃 | **仅人工显式动作** | 见第 5 节 |

历史追加 `events.jsonl`：accepted / superseded / attempt(rc=N) / deployed / failed / interrupted / alert-throttled 每事件一行。回读 = `cat` 两个文件；journald 另有 converge 单元日志。

### workflow 侧结论语义（DEFERRED 不判绿）

- job 结论保持 **failure**（`exit 3`，现状不动）——诚实反映「新 SHA 未上线」，README 既有表述不变。
- 新增解析远端 stdout 的 `intent:` 标记 → `intent_status` output（accepted / superseded / write-failed），与 `capture_deploy_result` 同款解析模式。
- 黄卡（新增 step，复用 `zlxlabs/gate/.github/actions/notify@v2`，severity=`P2`，`FEISHU_CI_WEBHOOK` secret，`if: failure() && outputs.deferred == 'true'`）：
  - accepted：「部署延期（服务忙，录制中）。部署意图已持久化到主机，录制结束后将**自动上线**，无需手动 re-run。结果查询：主机 `~/.d3-converge/intents/...` 或 `events.jsonl`。」
  - superseded：「部署延期。已有更新的部署意图（run #N）排队，本次（run #M）不会上线（被覆盖），re-run 本 run 无效。」
  - write-failed：升级 **P1**：「部署延期且意图持久化失败——自动收敛不会发生，需人工空闲后 re-run 或上机。」（意图写失败若只打 warning，事件流静默退回「没人管」现状，违背失败可见性。）

### 收敛器侧通知（复用既有通道：同一 webhook、同一卡面语义）

收敛器直接按意图里的 `webhook` 发卡（按 webhook 主机名分派企微 markdown / 飞书卡，与 notify@v2 同一判断逻辑的最简实现，内置在 converge 工件里；不跨仓 import gate 的 notify.py，避免 ci-templates→gate 的文件同步负担——两仓各自演进，拷贝会静默漂移）：

- deployed → `info` 卡（仅当意图 `notify_on_success=true`，与在线部署的 opt-in 语义对齐）：「run #N 的部署意图已由主机自动上线，探针+对账通过。」
- 持续 deferred → `P2` 卡，**节流 ≥24h 重发一次**（`last_alert_at` 存意图文件；全局约定「持续故障周期重发」的静默期）：「部署意图 #N 已排队 X 小时仍被忙锁占用（已尝试 M 次）。录制优先，继续等待；如需放弃见 RUNBOOK。」
- failed → `P1`：「自动部署失败（rc=N，已自动回滚/已停止），不再自动重试，需人工处置。」
- interrupted → `P0`：「收敛器上次执行被中断，生产状态未证——按 rc=4 同款处置上机核对。」

**凭据落盘决策（owner 已于 2026-11-21 认可 webhook 落盘）**：`FEISHU_CI_WEBHOOK` 值随意图写入 `${HOME}/.d3-converge/intents/`（0600、部署用户属主、不在任何容器挂载内、收敛器发送时 `curl -o /dev/null -s` 不回显 body 与 URL——#148 的企微 webhook 进启动日志教训）。理由：泄漏后果上限 = 向 CI 通知群发消息（骚扰/钓鱼），显著低于该主机上已存在的部署凭据（`~/.ssh/id_deploy` 部署私钥）；而「failed/interrupted 无强通知」正是 #63 式 4 天静默的根源，强通知的价值大于低敏感凭据落盘的代价。**否决**：收敛器经 GitHub API 回写 run 结论——需要 PAT 落主机，敏感度更高；收敛器经另一生产服务代理通知——新增跨服务耦合。owner 已认可本决策，C1 按落盘路径实施，不再走降级路径。

## 5. 放弃条件

**自动永不放弃。** 理由：

1. 需求方锁定决策「不为部署阻断录制」的直接推论：录制可以无限期持续（连续排期），意图过期 = 系统单方面决定「永不部署这个版本」，与需求矛盾，且把「停在新版本不可得」变成无告警的稳态——#63 的教训恰是静默停摆以天计。
2. 放弃的上限参数（次数/天数）无法从任何第一性推导：录制时长分布没有先验，任何上限都是猜测，猜小了回到意图丢失，猜大了等于没有上限。
3. 「只告警不放弃」的持续成本有界：每轮尝试是忙锁预算内的等待循环，等待不持任何锁，对录制与同主机其他服务零影响；告警有 24h 节流。

配套：人工放弃 = 人工将 `status` 置 `abandoned`（或删除意图文件），RUNBOOK 记载确切命令；这是**人的显式决定**，不是系统的猜测。`interrupted` 也不自动重试（生产状态未证，与 rc=4 同语义，人工核对后重置）。

## 6. 范围与爆炸半径

先读 README「版本与爆炸半径」与 RUNBOOK「版本推广模型」后的结论：

- **改动范围（本设计全部落点）**：`scripts/pull_and_deploy.sh`（deferred 两分支写意图 + INTENT_FILE 校验，约 +60 行）、`.github/workflows/build-deploy.yml`（新 input、env 透传、收敛器工件安装步骤、intent 标记解析、黄卡 step，约 +80 行）、新增 `scripts/converge/`（converge.sh + systemd 单元模板 + 安装说明，约 +150 行）、测试、README/RUNBOOK/docs/design。**不碰 `build-deploy-release.yml` / `release_deploy.sh`**（AGENTS.md：两个模板是刻意分叉，禁止直接搬补丁）。
- **opt-in**：新 input `deferred_deploy`（boolean，默认 `false`，与 `notify_on_success` 同款风格）。默认关闭时：不安装收敛器工件、不传 `D3_RUN_NUMBER`/`D3_RUN_ATTEMPT`、`pull_and_deploy.sh` 的意图代码路径整体跳过（`INTENT_FILE` 为空即返回，与 `BUSY_LOCK_FILE` 同模式），deferred 行为与现状逐字节一致（测试锁死，对应既有契约「未 opt-in 行为不变」）。意图文件路径由 deploy_dir 派生（第 1 节），caller 不传路径——`~/.d3-converge` 部署用户必可写，规避 caller 随手指一个无权限路径的误配。
- **release lane 同问题，单列不修**：[实测] `scripts/release_deploy.sh` 存在同一 rc=3 deferred 契约（`BUSY_LOCK_HELD` 循环，约 :776,789 两处 exit 3），workflow 侧同样无后续。单镜像 lane 落地并稳定后，若 release lane 使用者（web_transcibe_translate）出现同类停摆，按同一模式平移；release lane 的意图快照面更大（多镜像 manifest + `release_files_json` 配置下发），不是本设计的复制粘贴，需独立卡。**BACKLOG P3（两条 lane 忙锁逻辑各一份拷贝）接受理由继续成立，且本设计把拷贝数控制在 2（写侧意图代码），收敛器本身零部署逻辑不构成第三份。**
- **v2 标签移动的前置条件**（按 RUNBOOK 推广节奏，一步不可省）：(1) 本设计合并 main 后 canary（url-parse-api，钉 `@main`）不开 opt-in 真实部署一次，验证未 opt-in 路径零回归；(2) `git tag -f v2` 显式推广；(3) live-recorder caller 加 `deferred_deploy: true` opt-in，跑第 7 节验收场景（可在低风险时段人工持有 SH 锁模拟录制，不必等真实长录制）；(4) RUNBOOK 阶段记录实跑证据。
- 爆炸半径评估：最重的是 `pull_and_deploy.sh`（全舰队 13×@v2 caller 共享）——但非 opt-in 路径零变化由测试锁死；收敛器是新文件新目录，主机侧影响面 = opt-in 的那台主机。最坏故障 = 收敛器 bug 导致 opt-in 服务在空闲时部署了一个不该部署的版本——缓解 = 收敛器不选择版本（只应用意图记录里 workflow 已确认过的 SHA + 参数），版本决策链与在线部署完全相同。

## 7. 验收场景

卡面指定场景 + 可测映射。场景前缀 S，测试落点标注（现有测试模式：fake DOCKER_BIN/CURL_BIN + tmp_path 真实 flock，见 `tests/test_pull_and_deploy.py:1232-1426` 的 busy-lock 组）：

**S1 持续持有共享锁 + 连续两个版本**

1. 宿主进程持 busy.lock 的 `LOCK_SH`（模拟录制）。push 版本 A（run#N）→ 600s 预算耗尽 → `exit 3`、容器与 `last_good_tag` 未动、`intents/<...>.json` 存在且 `status=pending`、`run_number=N`、`deploy_env.GIT_SHA=A`、stdout 含 `intent: accepted`；workflow job 红、`deferred=true`、黄卡（P2）送达。
2. push 版本 B（run#N+1）→ 同样 deferred → 意图文件被原子替换为 B 的快照，events.jsonl 追加 `superseded run=N`；第二张黄卡文案为 superseded 变体。
3. 断言：录制进程全程存活（持有 SH 从未被阻塞超过一次 flock 尝试）；`last_good_tag` 仍是部署前值；A 的镜像存在于 ACR 但从未被 compose up。

**S2 收敛器重启**

4. 释放 SH 前，kill 收敛器（等待中/执行中各做一次）。等待中被 kill：锁由内核释放、录制不受影响、意图仍 pending，重启后（systemd timer 自动拉起）继续走锁循环。执行中（替换窗口）被 kill：重启后意图 `status=interrupted`、P0 卡发出、**不自动重试**，人工核对后处置（runbook 给重置命令）。
5. 断言：两种 kill 后 `flock` 均无残留（`lslocks` 或再次 EX 申请立即成功）。

**S3 空闲后只部署最新版本**

6. 释放 SH → 下一轮收敛 → 固化脚本以 B 的 env 执行 → 探针+对账通过 → `last_deploy_result.json` 更新为 B、`intent.status=deployed`、events 追加 deployed、（`notify_on_success=true` 时）info 卡送达。
7. 断言：生产运行 B 的镜像 ID（三段对账已在脚本内证明）；A 从未上线；意图比较在 B 应用前被 re-read 确认（模拟：S3 执行期间并发写入 C 的 deferred 意图，断言 C 保持 pending 且下一轮收敛 C）。

**S4 单元级补充（对应实现卡的测试清单）**

- `test_pull_and_deploy.py`：两处 exit 3 分支均写意图；`deploy_env` 白名单逐键正确；`(run_number, run_attempt)` 比较三态（覆盖/拒绝/同 run 高 attempt 覆盖）；意图写失败仍 `exit 3` 且带 `intent: write-failed`；`INTENT_FILE` 启用但 `BUSY_LOCK_FILE` 空 → `exit 1`；未传 `INTENT_FILE` → 不碰文件系统（现状逐字节不变）。
- `test_converge.py`（新文件）：rc=0/3/1/4/5 → deployed/pending+attempts++/failed/failed/failed；running 残留 → interrupted 且不重试；24h 告警节流；webhook 发送不回显凭据（fake curl 断言 argv）。收敛器不实现 sanitize。
- `test_workflow_contract.py`：`deferred_deploy` input 声明与默认 false；opt-in 时才透传 env 与安装工件；`D3_RUN_NUMBER`/`D3_RUN_ATTEMPT` 传递；intent 标记解析进 output；黄卡三变体（accepted/superseded/write-failed→P1）的 step 存在与 `if:` 条件；未 opt-in 的 deploy step 输出与现状一致。

**生产验收判据**（RUNBOOK 新阶段）：live-recorder 低风险时段人工 `flock -x` 持 busy.lock 推一个注释级 commit → 黄卡+意图落盘；释放锁 → ≤15 分钟内自动上线该 SHA；`cat intent` + `events.jsonl` + 飞书卡三处一致。

## 8. 拆卡清单

按文件互不重叠，依赖单向：

| 卡 | 文件 | 内容 | 依赖 |
|---|---|---|---|
| C1 意图写入 | `scripts/pull_and_deploy.sh` + `tests/test_pull_and_deploy.py` | `write_deferred_intent()`（sanitize、白名单快照、原子写、`(run_number,run_attempt)` 比较、write-failed 标记）、INTENT_FILE/BUSY_LOCK_FILE 联动校验；两处 exit 3 接线 | 无 |
| C2 收敛器工件 | 新增 `scripts/converge/`（converge.sh、units 模板、README）+ `tests/test_converge.py` | 状态机、意图比较 re-read、通知发送（webhook 分派+节流）、events.jsonl、interrupted 语义、systemd 安装说明（unit 首次 enable 需 root 一次性引导，工件更新走部署 run scp 无需 root） | C1 的 schema 与意图 JSON 格式（不依赖读侧 sanitize） |
| C3 workflow 接线 | `.github/workflows/build-deploy.yml` + `tests/test_workflow_contract.py` | `deferred_deploy` input、D3_RUN_* env、工件安装步骤（scp 脚本变体+converge 工件，opt-in 才装）、intent 标记解析、黄卡三变体 | C1、C2 |
| C4 文档与推广 | README、`docs/RUNBOOK-go-live.md`、本设计稿入库 `docs/design/deferred-deploy-converge.md`、（完工后）BACKLOG 巡检记录 | opt-in 用法、回执查询命令、人工放弃/中断处置、v2 推广与 live-recorder 验收步骤、release lane 同问题登记 | C3 合并后 |

每个实现卡各自带红验：新增测试函数在 base（7c18471）上必须失败（现状无意图机制，天然可红验）；「未 opt-in 逐字节不变」的负向测试锁死爆炸半径。

---

## 已否决备选方案汇总（速查）

| 备选 | 否决理由 |
|---|---|
| workflow schedule/定时 re-run | 锁定决策排除；重建镜像占 runner；concurrency 队列顶替；治标 |
| 忙锁预算内无限等待（拉长 timeout） | 占 GitHub runner 与 concurrency 队列；录制以小时计，600s→∞ 不现实 |
| 意图/脚本快照存 `.deploy-state` | 容器 bind mount 可写；伪造意图 = 生产镜像接管面 |
| 收敛器运行时从 GitHub 拉脚本 | 主机无凭据、离线不可用、执行契约绑网络状态 |
| 收敛器自实现部署/锁逻辑 | 双实现漂移（BACKLOG P3 既有教训）；收敛器必须零部署逻辑 |
| 提交时间/时钟戳判意图新旧 | commit 时间可伪造可乱序；主机时钟漂移秒级即误判 |
| 收敛器本地自增序号 | 重装漂移；GitHub run_number 免费提供单调序 |
| 收敛器经 GitHub API 回写结果 | PAT 落主机，敏感度高于 webhook |
| 通知经另一生产服务代理 | 跨服务耦合，第二个服务故障拖垮通知 |
| 自动放弃（次数/天数上限） | 上限无第一性依据；过期=静默停摆回归（#63 教训）；人工显式放弃替代 |
| 意图写入失败静默降级 | 违背失败可见性；事件流退回「没人管」现状，必须 P1 卡 fail-loud |
| run_number 重置自动甄别 | 证明机器可被历史污染（BACKLOG 拆除先例）；24h pending 告警已可暴露，人工清理意图文件 |

## 收尾必答

### 踩到的坑

- 卡面给的「4 次 deferred」与 run 数据不完全一一对齐：PR#63 的部署 run#99（38598064785）结论是 success，PR#61 deferred 后 2 小时即被 PR#62 的 push 顶上；「停 4 天」与 10-15~10-19 零部署 run 的时间线吻合但精确归因需主机侧证据（本卡无 SSH 授权）。报告按「需求方陈述 + run 级大方向佐证」分开表述，未把未证实细节写成实测。
- registry.yaml 是合成示例（真实舰队台账在私有运维仓留底），无法从本仓实测 rollback_safety 的 fleet 分布——因此设计不依赖该分布：脚本变体按「随意图走」的 per-caller 固化方案，天然覆盖非 safe。
- `deploy_once()` 的 ssh env 没有传 `GITHUB_RUN_NUMBER`/`GITHUB_RUN_ATTEMPT`，意图单调判定需要新增两行 env 透传——读代码前以为现成可用。

### 闸与绕过

无。未触发任何闸；只读任务无写操作，未申请 scope 扩展。

### 与卡面的偏差

无实质偏差。两点说明：(1)「若需要看 live-recorder 侧持锁行为，只读 gh api」已遵守，未 SSH 主机核实录制持锁现状，相关结论标为推断/需求方陈述；(2) 完成条件「产物入库」按提交授权「完工不提交」执行——report.md 落在 $DELEGATE_REPORT_PATH，由主脑复核后入库到 docs/design/。

### 最贵的一步

核实 live-recorder 的 deferred 时间线（三轮 gh api 翻 run/jobs/创建区间）：它把「意图丢失」从卡面断言变成了有 run 号、时间戳、失败步骤名的证据链，并直接给了第 7 节验收场景的真实雏形（run#130/#131 相隔 17 分钟连续 deferred = 「连续提交两个版本」场景）。此后所有设计决策（run_number 单调序、黄卡文案、永不放弃的理由）都锚在这条证据链上，没有返工。

### 新增测试判据

不适用：本卡 Exec-Profile=readonly、Verify-Mode=not_applicable，未改任何生产文件，无新增测试函数。设计稿第 7/8 节给出了实现卡的完整测试清单与红验要求（每个新增测试在 base 7c18471 上必须失败——现状无意图机制，天然满足），由后续 C1–C4 实现卡各自落地。

## 遗留与实施前必核验项

1. [推断→实施卡核验] live-recorder compose 的挂载清单确不含部署用户家目录（第 1 节安全假设）。
2. [推断→C2 核验] 部署目标主机 systemd 版本支持 timer+oneshot 惯用法；unit 首次 enable 的 root 引导步骤与 gate-hub onboard 流程的衔接。
3. [评审确认] webhook 落盘决策（第 4 节）；若否决，通知降级路径已给出。
4. [推断] GitHub `run_number` per-workflow-file 语义（GitHub 文档）；workflow 文件重建重置的运维处置已写入 RUNBOOK 责任。
5. 收敛器 timer 周期（2min）与告警静默期（24h）是初值，可调；无配置面（不做 input，反过度设计：第二个消费者出现前不参数化）。
