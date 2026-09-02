verdict: pass

# 本轮方向声明

第 1 轮，正向全量 + 降层三问 + 反向抽查。

- 审查对象冻结：`af8c6d592a361aa7f8a8b82f5c16deee455d6f7e..22426491ab1b86ca6fc3ad397d545bf0c7473976`（H0）。
- spec = PR #40 正文 + issue #19（含 09-02 评论：判据在目标形态下不可观测）。
- 风险档 **internal**；改动核心是健康门/失败路径（探针失败 → 回滚），按 infra 例外用 saas 收敛条件审。
- 本轮新证据（开启本轮的外部证据，不是同一份 diff 再读一遍）：H0 临时 worktree `/tmp/review-ci40` 上 `uv run --with pytest,pyyaml,jsonschema python -m pytest -q tests/test_pull_and_deploy.py tests/test_release_deploy.py` → **150 passed in 45.28s**；以及下文降层/反向 stub 实测输出。
- 非目标：探针以外的存量逻辑（对账、回滚编排、锁）只记 backlog，不作为本轮 finding。

# 降层三问

## ① 新失败路径终态；回滚探针是否用新判据

判据收紧后，code 对且 `curl_rc≠0` 不再 `return 0`/`break`，走既有探针失败出口。

| 场景 | 终态（实测） | 代码 |
|---|---|---|
| pull：新版 200/28，旧版 200/0 | `rc=1`，`last_good` 停在旧 tag，日志 `old version passed the same-budget health probe` | `health_probe` 失败 → `do_deploy` 回滚后再调同一 `health_probe`（`scripts/pull_and_deploy.sh:446-498`） |
| pull：新版 200/28，回滚后旧版仍 200/28 | `rc=4`，`last_good` 仍是旧 tag，`rollback health probe FAILED … production state is uncertain` | 同上：回滚探针是同一函数，新判据生效 |
| release：无 last_good，首探 200/28 | 探针失败后无前回滚 → `rc=4`（`MUTATED=1 && rollback_healthy=0`） | `probe_release:598-606` → `do_release:725-727,815-819` |
| release：有 last_good，新版 200/28，旧版 200/0 | `rc=1`，`rollback to … healthy` | 回滚后再调同一 `probe_release`（`do_release:800-807`） |
| release：新版与回滚均为 200/28 | `rc=4`，`rollback compose succeeded but probes still fail` | 同上 |

是否 spec 想要的：**是**。

- issue #19：旧判据会把「已回滚且旧版本自证健康」（`rc=1`）变成假绿；README A3 明文「回滚本身同样受探针门约束，未通过则升级为 `rc=4`」。
- 旧版若同样 transport incomplete，升 `rc=4` 比声称 `rc=1` 更符合「不得假绿」；不是误伤。
- 已否决方案（继续观察 / 只改打印不改判据 / 改重试超时）不作为相反结论。

实测摘录：

```
--- pull: new=200/28, rollback=200/0 ---
rc=1
last_good=old1111
[deploy] health probe attempt 1/1 got '200' but curl rc=28 (transport incomplete)
[deploy][evidence] probe-attempts: 200(curl=28)
[deploy] rolling back to previous good tag old1111
[deploy] health probe OK (attempt 1, status 200)
[deploy][evidence] probe-attempts: 200(curl=0)
[deploy] rollback to old1111 complete; old version passed the same-budget health probe

--- pull: new=200/28, rollback=200/28 ---
rc=4
last_good=old1111
[deploy] rollback health probe FAILED for old1111; production state is uncertain

--- release: first deploy 200/28, no previous good ---
rc=4
[release] probe http://localhost/frontend got '200' but curl rc=28 (transport incomplete) (1/1)
[release] no previous good release available; refusing pseudo-rollback

--- release: new=200/28, rollback healthy ---
rc=1
[release] rollback to abc123456789 healthy

--- release: new=200/28 and rollback=200/28 ---
rc=4
[release] rollback compose succeeded but probes still fail
```

## ② `curl_rc` 取值来源；`set -e`/`pipefail` 下非零是否落到变量

两 lane **取值结果一致，捕获形态不一致**（形态差是 #18 存量，本次 diff 没改捕获，只改判据）。

| lane | 形态 | 行号 | `set` |
|---|---|---|---|
| pull | `if code="$(curl …)"; then curl_rc=0; else curl_rc=$?; fi` | `scripts/pull_and_deploy.sh:271-276` | `set -euo pipefail`（:19） |
| release | `curl_rc=0; code="$(curl … 2>/dev/null)" \|\| curl_rc=$?` | `scripts/release_deploy.sh:592-593` | `set -u -o pipefail`，**无 `-e`**（:10） |

stub（curl stdout=`200`、exit 28），复刻上述两段原样：

```
--- pull-lane if/then/else form (set -euo pipefail), curl prints 200 exits 28 ---
PULL_FORM code=200 curl_rc=28 script_still_alive=1
PULL_FORM after_function rc_of_script_would_continue=1
pull_form_script_exit=0

--- release-lane || curl_rc=$? form (set -u pipefail, no -e), curl prints 200 exits 28 ---
RELEASE_FORM code=200 curl_rc=28 script_still_alive=1
release_form_script_exit=0

--- release-lane form ALSO under set -e (counterfactual) ---
RELEASE_FORM_WITH_E code=200 curl_rc=28 script_still_alive=1
release_form_with_e_exit=0

--- counterfactual: bare assignment under set -e (no if/||) ---
bare_set_e_exit=28
```

结论：命令替换里 curl 非零**不会**在现行两形态下提前退出；`curl_rc` 都能落到 28。裸赋值 `code="$(curl)"` 在 `set -e` 下会以 28 退出——pull 的 `if` 与 release 的 `||` 都避开了这条。`pipefail` 对单条 curl 命令替换无额外影响。

## ③ 保护的是「写入」还是「行为」；成功路径取证是否覆盖所有成功 return

保护的是**行为**（判健康谓词），不是一次写入。`last_good_tag` / `last_good_release` 仍只在探针返回 0 之后才推进（pull `:446-447`；release `:702-717`）。不可逆动作 `compose up` 发生在探针之前，失败走既有回滚——本 diff 没改那一段。

`probe-attempts` 成功路径：

| 成功 return | 是否在 return 前打印 | 依据 |
|---|---|---|
| pull：warmup 后首次即过 | 是。`:285` echo 然后 `:286` `return 0` | 实测 warmup=1、retries=2、首次 200/0：`health probe OK (attempt 1, status 200)` 紧接 `probe-attempts: 200(curl=0)`，脚本 `rc=0` |
| pull：重试后才过 | 是。同一处 return，序列累加在 `:278-282` | 结构上无第二条成功 return |
| pull：无 `HEALTHCHECK_URL` 跳过 | 否。`:265` 直接 `return 0` | 实测：`no HEALTHCHECK_URL, skipping probe`，无 `probe-attempts`。无 curl，不是 #19 的目标形态；属存量跳过，不记本轮 finding |
| release：warmup 后首次即过 | 是。`break` 出 while 后 `:611` 对每个 URL echo，最后 `:613` `return 0` | 实测 warmup=1：两条 URL 各一行 `probe-attempts url=… 200(curl=0)` 后 `healthy; promoted atomically` |
| release：无探针声明 | 否。`:583` `return 0` | 存量跳过，同 pull 无 URL |

# 反向抽查

目标：既有失败形态判定不变；`rc=28` 且 code=`000`（连接阶段超时）仍走 code 不匹配，不进 `transport incomplete`。

curl stub 跑真实 `health_probe` / `probe_release`（H0 脚本），摘录：

```
===== empty stdout rc=0 (pull) =====
rc=4 last_good=old1111
[deploy] health probe attempt 1/1 got '000', want 200
[deploy][evidence] probe-attempts: 000(curl=0)
# 说明：空 stdout 被 `:277` 归一成 000；日志是 want 200，不是 transport incomplete。
# 本 stub 对回滚探针同样返回空，故整体 rc=4（回滚探针也失败），与「判定路径」无关。

===== empty stdout rc=0 (release) =====
rc=4
[release] probe http://localhost/frontend got 000, want 200 (1/1)
[deploy][evidence] probe-attempts url=http://localhost/frontend 000(curl=0)

===== isolated judge: empty code maps to 000, mismatch log, not transport =====
mismatch got '000', want 200 curl_rc=0

===== isolated judge: 000 + rc=28 =====
mismatch got '000', want 200 curl_rc=28

--- pull reverse: code='000' rc=7 ---
rc=1 last_good=old1111
[deploy] health probe attempt 1/1 got '000', want 200
[deploy][evidence] probe-attempts: 000(curl=7)

--- release reverse: first url code='000' rc=7 ---
rc=4
[release] probe http://localhost/frontend got 000, want 200 (1/1)
[deploy][evidence] probe-attempts url=http://localhost/frontend 000(curl=7)

--- pull reverse: code='000' rc=56 ---
rc=1 last_good=old1111
[deploy] health probe attempt 1/1 got '000', want 200
[deploy][evidence] probe-attempts: 000(curl=56)

--- release reverse: first url code='000' rc=56 ---
rc=4
[release] probe http://localhost/frontend got 000, want 200 (1/1)
[deploy][evidence] probe-attempts url=http://localhost/frontend 000(curl=56)

--- pull reverse: code='000' rc=28 ---
rc=1 last_good=old1111
[deploy] health probe attempt 1/1 got '000', want 200
[deploy][evidence] probe-attempts: 000(curl=28)

--- release reverse: first url code='000' rc=28 ---
rc=4
[release] probe http://localhost/frontend got 000, want 200 (1/1)
[deploy][evidence] probe-attempts url=http://localhost/frontend 000(curl=28)

--- pull reverse: code='000' rc=0 ---
rc=1 last_good=old1111
[deploy] health probe attempt 1/1 got '000', want 200
[deploy][evidence] probe-attempts: 000(curl=0)
```

反向结论：空 code / `000` + rc∈{0,7,56,28} 全部走 `got '000', want 200`，**没有一条**打出 `transport incomplete`。`transport incomplete` 仅在 `code == expect && curl_rc != 0`（本次新增分支）出现。

# 熵增审查

对照 `templates/REFACTOR-guide.md` 坏味道词表（无消费者面 / 镜像事实 / 投机通用性 / 多余路径或转发层 / 生命周期重复 / 错位防御 / 自建基础设施 / 仅支撑残留）：

| 新增物 | 是否熵 +1 | 判定 |
|---|---|---|
| 判据 `code 匹配 && curl_rc==0` | 否 | 收紧同一谓词，不是第二套健康状态 |
| 日志分支 `got '…' but curl rc=… (transport incomplete)` | 否 | 同一失败路径的诊断文案，无新状态/配置/包装层 |
| pull 成功路径 `probe-attempts` echo | 否 | 把失败路径已有的取证行补到成功 return；填 #19 09-02「目标形态不可观测」的洞，不是第二套证据格式 |
| curl stub 的 exit code 控制 | 否 | base 已有 `_mock_curl_sequence(status, rc)` 与 `mock_curl_status_exit_sequence`；本 diff 只消费既有 helper，未新增并行 stub |

本 diff 无新文件、无新配置项、无转发-only 层。熵 +0。

# Findings

无 P1 / P2 / P3。

# Backlog（本轮不阻塞）

- **回滚侧 200/28 → rc=4 无专项测试。** 正向新测只锁「新版 200/28、旧版 200/0 → pull rc=1」和「release 首探失败」。降层 stub 已证实回滚探针走新判据且升 rc=4，但测试文件没锁这条。属覆盖缺口，不是行为错误。
- **release 新测未铺 previous good**，脚本整体 rc 是无 last_good 的既有 `4`，不是「已回滚 rc=1」。断言是 `returncode != 0` + 探针日志，对「探针过闸」足够，不覆盖 release 回滚成功/失败矩阵。
- **对账、回滚编排、锁**：按卡面非目标未审。
- **无 URL / 无探针声明的成功 return 不打 `probe-attempts`**：存量跳过，与 #19 目标形态无关。
- 已否决、不得重提：继续观察三周；只改打印不改判据；改重试/超时参数。
)
