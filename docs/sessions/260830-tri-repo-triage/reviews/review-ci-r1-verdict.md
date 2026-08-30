# review r1：release 对账入锁 + readarray

审查对象固定为 `edcb68ff36f785455534572b6bb271c2a28c97f9..a0ce605e33ed229aec72e3fb40a8fbe530df6ea2`，不随分支名漂移。风险档按本仓项目约定为 `internal`；本仓没有机器可读的单行 `risk-tier: internal` 声明，仓主应补上。

## 完成条件·行为验收

本轮新增证据：

- `python -m pytest -q tests/test_release_deploy.py tests/test_release_workflow_contract.py`：`105 passed in 21.41s`。
- 在 base `edcb68f` 临时 worktree 仅拷入当前 `tests/test_release_deploy.py`，抽查的两条新增行为测试均以 `AssertionError` 转红，见「红验抽查」；没有 ImportError、SyntaxError 或注入未生效。
- `bash -n scripts/release_deploy.sh` 与 `git diff --check` 已执行；后者仅报告新增进度文档 EOF 多余空行。
- `readarray` 语义实测：`readarray -t a <<< ""` 得到 `declare -a a=([0]="")`，旧式 `< <(printf "")` 得到 `declare -a a=()`；三个消费方均过滤空元素或在空服务集上 fail-loud，未发现本次改动造成的行为差异。
- `ocr-review` 已按要求启动，但主腿约 236 秒仍未返回完整 envelope，安全中止后退出 130；没有把这次工具未完成误报为「扫过且干净」。

行为结论：

| 验收项 | 结论 | 证据 |
|---|---|---|
| 1. 对账在 `do_release` 返回后、fd9 解锁前执行；失败映射 rc=5 | 结构成立；但 rc=255 的重试边界引入 P1，见 Finding P1-1 | `scripts/release_deploy.sh:853-862`；`scripts/release_deploy.sh:856-860` |
| 2. workflow 保留同名紧邻薄壳，rc=5 置 flag、deploy 不假绿 | 通过 | `.github/workflows/build-deploy-release.yml:348-394`；测试 `tests/test_release_workflow_contract.py:558-583` |
| 3. `compose config --services` 失败如实上抛 | 通过 | `scripts/release_deploy.sh:248-304`、`:326-330`；新增归因测试 |
| 4. 锁序、释放顺序、信号/忙锁路径、pull lane 不变 | 锁序与忙锁路径通过；transport retry 等价性不通过，见 Finding P1-1；无超时见 Finding P2-1 | `scripts/release_deploy.sh:781-868`；workflow base 对照 `edcb68f:.github/workflows/build-deploy-release.yml:381-576` |
| 5. 持锁顺序断言与锁序红验 | 通过 | `tests/test_release_deploy.py:1789-1811`；base 红验原文见后 |

## Finding P1-1：对账期间的 rc=255 会重放整次 release，可能重复执行 one-shot migration

- 严重度：P1（internal 红线：可能损坏他人数据）。
- 违反溯源：spec 4 的失败/状态迁移与信号路径不变约束；同时直接命中任务卡已登记且要求独立核实的 c 轴——旧 workflow 对账的 3 次 rc=255 重试语义必须等价保留。
- 代码证据：当前 workflow 把 `release_deploy.sh`（包含对账）放入 `deploy_once`，外层在 `.github/workflows/build-deploy-release.yml:338-385` 对任意 rc=255 重跑整个 `deploy_once`。对账已在 `scripts/release_deploy.sh:856-860` 放入这条 SSH 命令，并在 `:862` 后才解锁。base 的独立对账 step 在 `.github/workflows/build-deploy-release.yml:381-576` 自己有 `reconcile_once` 的 3 次 rc=255 循环；它失败时只重试读操作，不重跑部署。
- 触发链：第一次 SSH/scp 成功后，远端已完成 `do_release` 并进入新对账；此时连接因网络/SSH 传输返回 255。当前外层把它当作整个 release 的 255，最多再发起两次 `release_deploy.sh`。`ONESHOT_SERVICES` 的 forward 路径在 `scripts/release_deploy.sh:523-535` 仍执行不带服务排除的 `compose up -d`；one-shot/migration 服务只在 rollback 分支排除。因此重试可能再次执行迁移。旧独立对账 retry 不会产生这次新增的「对账传输失败→重新部署」边界。
- P1 两问：
  1. 真实使用下会触发吗？会。release lane 明确支持 `oneshot_services`（`.github/workflows/build-deploy-release.yml:71-75`），而 SSH 传输 255 是已有真实重试路径；只需断线落在新增的对账窗口即可。
  2. 后果能接受吗？不能。对任意非幂等 migration，重复 `compose up -d` 可能重复写数据库、重复迁移或损坏共享生产数据，超过 internal 的可接受范围。无 one-shot 时主要是重复部署，不能以此降低含 migration 路径的 P1 判定。
- 建议处置方向：重新划定 transport retry 边界，使对账 rc=255 仍只重试只读对账，或让已完成 promote 的远端运行不再进入整次 forward deploy 重放；不要用无依据的 fallback/兼容分支掩盖状态不确定性。

## Finding P2-1：持锁期内的 Docker/Compose 调用没有 per-call timeout

- 严重度：P2（internal 两问未同时命中 P1 红线）。
- 违反溯源：spec 1/4 的「对账持 fd9」资源账本约束与任务卡降层三问③；保护覆盖了读取，但没有给读取行为设置边界。
- 代码证据：`reconcile_release_images` 在 `scripts/release_deploy.sh:348-450` 依次调用 `compose config --images`、批量 `compose ps`、逐服务 `compose ps`、`docker inspect` 和 `docker image inspect`，均未套 `timeout`。fd9 在 `scripts/release_deploy.sh:829-850` 获取，直到 `:862` 才释放；fd8 存在时还会一起占用。workflow 没有任何 `timeout-minutes`（可由 `rg` 得到 `no timeout-minutes in build-deploy-release.yml`），因此 GitHub Actions job 采用官方文档所述默认 360 分钟上限：[GitHub Actions workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)。
- 降层两问：
  1. 真实使用下会触发吗？会。Docker daemon 无响应、Compose 项目解析卡住或逐容器 inspect 卡住都能让这些本地外部调用不返回；镜像数×服务数还放大调用次数。
  2. 后果能接受吗？不能接受其长时间占住同主机全部 release admission，但它表现为显式 job/锁阻塞而非本仓 internal 定义的静默错、数据损坏、崩溃或越权，因此本轮定 P2，不作为 fail 的 P1 来源。
- 处置：为对账单次 Docker 行为设置有限且与 workflow job budget 对齐的超时，并保留 fail-loud 归因；超时应释放 host lock，而不是无限等到 job 默认上限。

## 降层三问

### ① 终态写入前的不可逆动作与 rc=5 世界状态

在 `do_release` 成功返回前，目标镜像已经 pull/retag（`scripts/release_deploy.sh:206-245`），forward `compose up -d` 已执行（`:523-543`，其中 one-shot migration 也在这一侧执行），探针通过后 `promote` 在 `:577-606` 原子推进 `last_good_release`，并刷新两个兼容视图。对账从 `:856` 才开始，因此 rc=5 时：运行时替换已发生、one-shot 已发生、`last_good_release` 已推进到本 SHA，但 expected image ID 与运行容器集合尚未被证明一致；脚本不会自动 rollback。

workflow 的 rc=5 文案在 `.github/workflows/build-deploy-release.yml:527-528` 明确说「部署已成功但对账失败，last_good 已推进，本次不自动回滚，需上机核验」，与该世界状态如实相符。注意「未验证的 SHA 已进入 last_good，后续失败回滚可能以它为 previous」是 base 独立对账 step 已存在的状态账本风险：base 同样在远端 promote 后才运行对账（旧 workflow `:539-541`）。本轮记录为存量 backlog，不把它重复算作本次新增 P1；本次新增 P1 是 rc=255 导致 forward deploy 重放并可能重复 migration。

### ② HOST_LOCK / BUSY_LOCK_FILE 的值是否唯一、rc=5 读谁的容器

workflow 固定将 `HOST_LOCK` 设为 `/var/lock/fleet-deploy.lock`（`.github/workflows/build-deploy-release.yml:201-216`），因此同一主机跨仓、不同 `DEPLOY_DIR` 的部署仍由同一 host lock 串行；对账里的 `cd "$DEPLOY_DIR"` 与 Compose 参数绑定当前 caller 的 compose 目录。相同 `DEPLOY_DIR` 的多个 caller 也会被 host lock 串行，但它们本质上共享一个部署目标；仓库 README 的 `(host, deploy_dir)` 唯一约束禁止把两个服务声明成同一目标。`BUSY_LOCK_FILE` 是可选 admission 锁，不是 rc=5 对账的身份来源，且即使不同 caller 传入不同 busy path，fd9 仍覆盖行为临界区。

因此在文档化部署形态下，rc=5 读的是当前持 fd9 的 `DEPLOY_DIR` Compose 项目，不是另一个同时部署者的集合；若人工绕过 workflow 改写 HOST_LOCK、复用同名 Compose project 或把不同服务故意指向同一目录，属于未满足既有目标唯一性契约的存量风险，不作为本次新增 P1。

### ③ 保护覆盖「写入」还是「行为」、挂死如何解除

本次改动确实把对账读取行为放进 fd9 临界区，但 `config --images`、`ps`、`inspect` 没有 per-call timeout。它们挂死时 host lock 会一直被该远端进程占用；唯一确定的上限是 workflow job 默认 360 分钟，或外部人工杀远端进程/runner 强制终止，不能靠 `BUSY_LOCK_TIMEOUT` 解除，因为该超时只约束拿 fd8 的 admission 等待（`:797-821`），不约束已拿到锁后的对账。该问题按 P2-1 记录。

## 工具标注 / 本仓判定 / 两问对照表

| 轴 | 工具标注或线索 | 本仓判定 | P1 两问 |
|---|---|---|---|
| a 持锁 Docker 无超时 | OCR 线索；静态确认 `:348-450` 全部直接 Docker 调用 | P2-1；真实资源阻塞，未命中 internal P1 红线 | 会触发：daemon/Compose 可挂；不可接受长时间阻塞，但不是数据损坏/静默错/崩溃/越权 |
| b `readarray <<<` 空输入 | OCR 线索；Bash 实测空数组变单元素空串 | 不成立；三个消费方都跳过空串或在空集 fail-loud，compose config 失败由命令替换如实返回 | 不构成生产缺陷；后果不新增错误状态 |
| c 旧对账 3×rc=255 语义 | OCR 线索；base/new workflow 对照确认重试边界变化 | P1-1；对账 255 现在会重放整次部署，含 one-shot 时可损坏数据 | 会：新增窗口遇 255；不可接受：可能重复非幂等迁移 |
| d `.index` 首次命中/精确计数锚点 | OCR 线索；静态核对 `tests/test_release_workflow_contract.py:595-597,647-650` | P3 backlog；当前实现正确，但测试可被注释/前置同名文本或错误的首个调用伪满足 | 不会直接触发生产；会削弱未来回归检测，低于 P1 |

## 红验抽查

临时 worktree 基于 `edcb68ff36f785455534572b6bb271c2a28c97f9`，仅拷入当前 `tests/test_release_deploy.py`，运行：

```text
python -m pytest -q \
  /tmp/ci-templates-red.nJSdN5/tests/test_release_deploy.py::test_validate_oneshot_services_compose_config_failure_is_attributed \
  /tmp/ci-templates-red.nJSdN5/tests/test_release_deploy.py::test_reconcile_runs_while_host_lock_held
```

原文结果（两项均为断言失败）：

```text
=================================== FAILURES ===================================
_____ test_validate_oneshot_services_compose_config_failure_is_attributed ______
E       AssertionError: assert 'oneshot_services references unknown' not in '[release] n...o-rollback\n'
E         'oneshot_services references unknown' is contained here:
E           [release] oneshot_services references unknown compose service(s): migrate
E           [release] no previous good release available; refusing pseudo-rollback
___________________ test_reconcile_runs_while_host_lock_held ___________________
E       AssertionError: assert 'release image reconcile starting (host lock still held)' in '[deploy][evidence] probe-attempts url=http://localhost/frontend 200(curl=0)\n[deploy][evidence] probe-attempts url=http://localhost/api/health 200(curl=0)\n[release] release abc123456789 healthy; promoted atomically\n'
=========================== short test summary info ============================
2 failed in 0.74s
```

判据：第一条证明 base 仍在 process substitution 后误报 unknown，第二条证明 base 没有本次新增的持锁对账；失败类型均为 `AssertionError`，满足红验有效性要求。

## 熵增审查

新增 `reconcile_release_images` 是跨发布边界移动现有 workflow 对账逻辑的单一落点，虽然当前只有一个 caller，但它必须存在于远端持锁脚本中才能满足 spec 1/5；其单消费者必要性成立。workflow 同名薄壳是锁定的 Checks/step-index 契约所需，不是无消费者转发层。`all_services_output`、`reconcile_rc` 等局部状态分别承载真实命令退出码和多镜像汇总，不是镜像状态的第二份持久事实源。未发现为 P2/P3 顺手新增 fallback、重试或防御式 catch；P1-1 恰恰来自已有外层重试边界被新代码扩大，需修边界而非再堆机制。

新增进度文档末尾有多余空行（`git diff --check`），属不阻塞的文档卫生问题。

## 收敛判定

本仓属于 internal，且本 diff 是 infra/状态机类，按提档规则需连续 2 轮无新增 P1；本轮新增 P1-1，不能收敛。修复后下一轮必须固定审 `H0..H1` 增量，重点验证 retry 边界、one-shot 不重放与锁内超时。

verdict: fail
