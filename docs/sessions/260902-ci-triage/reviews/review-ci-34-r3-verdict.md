verdict: pass

# review r3：PR #38 rc=5 世界状态账本（换家：on-call 顺序读 + workflow 重试记账）

- 审查对象冻结：H1 = `6f395585bfb27a3b2d1c8740714ecb6832c1de1b`。审查中分支再有新提交不改变本轮对象。
- spec = PR #38 正文 + 第 1/2 轮 verdict（`origin/card/ci-templates-20260902-04` 的 `review-ci-34-r1-verdict.md`，`origin/card/ci-templates-20260902-08` 的 `review-ci-34-r2-verdict.md`）。
- 风险档 **internal**（`AGENTS.md`「风险等级」）；失败路径 / 锁临界区 / `last_good` 账本，infra 提档按 **saas 收敛条件**（连续 2 轮无新增 P1）。saas 收敛目前 r2 过后 1/2，本轮是换第三家、换视角的第 2 个无新增 P1 候选。
- 本轮方向 = on-call 跳点账本 + `GITHUB_OUTPUT` 写失败独立复判 + skip-forward×忙锁 stub + README 两条 P3 处置。**不重复**：正向兑现、降层三问、255 重放窗口、矩阵格语义、飞书三条分流文案字面。
- 本轮新证据（换家必须换证据源，不是同一份 diff 重读）：
  1. 临时 worktree `/tmp/review-ci38-r3` @ H1，按值班顺序读飞书卡 → Run → Deploy 日志末尾 → Reconcile 薄壳 → README 退出码表；并对 `.github/workflows/build-deploy.yml:308-391` 重试循环逐行记账（`max_attempts` 与 `exit 5` 行号见 §1）。
  2. 真实消费环境量 `GITHUB_OUTPUT`：本会话 `printenv GITHUB_OUTPUT` 长度为 0（不是 Actions runner）；消费者 `zlxlabs/WechatChatRoomSummary` run `33371207720` 的 Deploy 跑在 self-hosted `gatehub-4e96105df3cb-slot-1`（labels `self-hosted,linux,x64,codex`），同 job 里 `echo git_sha=… >> "$GITHUB_OUTPUT"` 成功且 job conclusion=success。三份消费者 log 无 `::warning::failed to write … output` 运行时注解（只命中脚本原文回显）。本机用 `/dev/full` 与不可写文件复现写失败签名。
  3. 同 H1 脚本、复用 `tests/test_pull_and_deploy.py` 的 PATH stub：`BUSY_LOCK_FILE` 设定 + `last_good_tag` 已是本次 SHA 重入一次；另测 `flock -u 8` 对未打开 / 已打开未加锁 fd 的退出码。原文见 §3。
- OCR：本轮未重跑（r1 已对 H0 扫过并消化 OCR F5；本轮证据是值班跳点与 runner 实测，不是再扫同一 diff）。
- 已否决方案（不作为 finding 重提）：让 workflow 对 255 不重试；对账改回第二条 ssh；workflow 层再加 flock；skip-forward 重跑探针。

## 1. rc=5 之后的世界状态账本

目标三元组：**新版本在跑、身份未证明、不要重跑**。按 on-call 打开顺序走。

### 飞书卡（第一跳）

正常 rc=5 且 `GITHUB_OUTPUT` 写成功时：Deploy step 红 **且** Reconcile 薄壳红。卡上第三条（`:445`）写「同时红 = 对账失败（rc=5），按 Reconcile 那条处置，**不要**按已回滚处置」；第二条（`:442-444`）写 last_good 已推进、无自动回滚、必须上机。能唯一定位「新版本很可能在跑、身份未证明」。

本跳**没有**写出「不要重跑」。会把人引向「重跑」的文案在**另一条卡**：延期黄卡（`:560`「空闲后点按钮 **重跑部署**」）和 Deploy 日志里 rc=3 的 `re-run this workflow`（`:329`）。那是 busy-lock 延期，不是 rc=5。GitHub UI 的 Re-run 按钮对任何红 job 都在，但 H1 的 skip-forward 让同 SHA 重跑只对账、不再 `compose up`（r2 已测；本轮忙锁下再测一次见 §3），所以「点重跑」不再构成损坏他人数据。不把缺「不要重跑」升格为 P1/P2——README 表（第五跳）补了这句。

### 打开 Run（第二跳）

两红 step：`Deploy over SSH`（`id: deploy`，`:226-227`）+ `Reconcile deployed image` 薄壳（`:393-400`，`if: failure() && steps.deploy.outputs.reconcile_failed == 'true'`）。两红结构本身就能从 rc=1（只 Deploy 红）里分出来。单红会被第一条飞书文案读成「已回滚」——只在 `reconcile_failed` 没写成 output 时发生，见 §2。

### Deploy step 日志末尾（第三跳）

`:341-344`：

```
if [ "$rc" -eq 5 ]; then
  echo "reconcile_failed=true" >> "$GITHUB_OUTPUT" || echo "::warning::failed to write reconcile_failed output"
  echo "::error::image reconcile assertion failed (rc=5); deployment may have succeeded, but production image identity is not proven"
  exit 5
fi
```

文案定位「可能已部署成功、身份未证明」，**不**说已回滚、**不**说重跑。`:347-349` 的「host 已按探针门自动回滚,不重试」在 rc=5 分支之后，且被 `exit 5` 截断，rc=5 **到不了** 那句。

**重试循环是否真的 `exit 5`、不再进入下一次 attempt：**

| 行号 | 作用 |
|---|---|
| `:308` | `attempt=1; max_attempts=3; had_transport_failure=0` |
| `:309` | `while true; do` |
| `:341-344` | rc=5 → 写 output → `::error::` → **`exit 5`**（退出整个 Deploy step 脚本，不是 `break`、不是 `continue`） |
| `:346-349` | 非 255 的其余码（典型 rc=1）才走「已回滚,不重试」 |
| `:351-390` | 只对 rc=255：`attempt=$((attempt + 1))` + `sleep 10` 再循环 |
| `:352-386` | `attempt >= max_attempts` 时清远程脚本 `exit 1` |

`exit 5` 在 `while true` **体内、且在 `attempt++` 之前**。rc=5 不会把 `attempt` 从 1 加到 2，也不会 sleep。`max_attempts=3` 只服务传输层 255。job 级无 `retry`/`max-attempts`（全文件除注释外，重试字样只在 `:388` 的 255 警告）。workflow 记账与「不要重跑」一致。

### Reconcile 薄壳（第四跳）

`:399` 重复同一句 `::error::… identity is not proven` 后 `exit 1`。不 SSH、不对账。打开这一步不会看到「已回滚」或「请重跑」。step 名仍像真对账，但日志第一句就是断言失败——值班不会从这里得出「已经回滚」。

### README 退出码表（第五跳）

`:219` rc=5 行三列写满：语义（探针已过、`last_good` 已推进、对账失败）/ 生产状态（新版本在跑但身份未证明）/ 处置（**立即上机核对**，不自动回滚，**不要重跑**）。同表里明确说重跑的是 `rc=3`（空闲后点黄卡重跑）和 `rc=255`（重跑）。这一跳是三元组最完整的一跳。

滞后句在表**下面**的 rc=4 叙述（`:237-238`「`last_good` 等于本次 SHA」），不在 rc=5 行。处置见 §4。

### 上机（第六跳）

脚本侧：`last_good_tag` 已是本次 SHA（对账前已 promote），host flock 已在 `:587` 释放。上机看到的是「新 tag 在账本里、容器身份对不上或 docker 当时超时」——与「不要重跑、不要当已回滚」同向。

### 本节省论

主路径六跳都能定位「新版本在跑、身份未证明」；「不要重跑」在第三跳（循环 `exit 5`）和第五跳（表）硬锁，第一跳靠「不要按已回滚」+ 上机，不靠「别点 Re-run」。会把人引向「已回滚」的结构只有 §2 的 output 写失败；会把人引向「重跑」的文案属于 rc=3/255，不是 rc=5。

## 2. `GITHUB_OUTPUT` 写失败路径（独立复判 r1 P3-5）

路径：`:342` 写失败被 `|| ::warning::` 吞掉 → 仍 `exit 5` → Deploy 红；`:396` 条件 `steps.deploy.outputs.reconcile_failed == 'true'` 为假 → 薄壳不跑 → 飞书卡只看见 Deploy 单红 → 第一条（P2-2 修后，`:440-441`）读成「未部署成功……**自动回滚到 last_good**」。世界状态实际是新版本在跑、身份未证明。r1 判 P3-5「接受现状（deferred/rc=4 同一写法）」；本轮独立再判一次。

**真实环境量一次（P1 第一问，不能从代码形态推断）：**

- 本会话不是 Actions runner：`GITHUB_OUTPUT` 未设置，长度 0。
- 文档：runner **生成临时文件**，路径经默认环境变量暴露（GitHub Docs「Environment files」/ `core.setOutput` → `GITHUB_OUTPUT`）。
- 真实消费形态：pull lane 默认 `runs-on` 三元（`:138`）在 caller 传 `runner=self` 时走 self-hosted。`WechatChatRoomSummary` deploy run `33371207720` job `ship / build-deploy`：`runner_name=gatehub-4e96105df3cb-slot-1`，labels `self-hosted,linux,x64,codex`，conclusion=success；同 job 早先 `echo "git_sha=…" >> "$GITHUB_OUTPUT"` 已执行。`one_translate_tts` 成功 run `32275271475` 同样写出 `git_sha` 且 job 绿。失败 run `33371702360` 亦无运行时 `failed to write … output` 注解。
- 写失败签名（本机，不是 runner）：`GITHUB_OUTPUT=/dev/full` → `echo: write error: No space left on device`、echo rc=1；`chmod 000` 目标文件 → `Permission denied`、rc=1；unset → `No such file or directory`、rc=1。套上生产的 `|| ::warning::` 后整句 rc=0，step 继续 `exit 5`——薄壳条件仍为假。这是「runner 磁盘满/权限坏」的形态，不是 rc=5 功能路径自己的形态。

**本仓判定：维持 P3，不升 P2。**

- 工具标注：OCR F5 曾标 high；r1 落 P3-5。本轮复判同级。
- P1 两问：①真实使用下会被触发吗？**不会作为 rc=5 的常驻路径触发。** 真实 runner 每次 step 都注入可写的 `GITHUB_OUTPUT` 文件；三份消费者 log 零条写失败注解。会让 echo 失败的是 runner 磁盘满或文件权限被改——那时 checkout/build 多半已经先死，Deploy 还跑到 rc=5 是复合极端。②触发了后果能否接受？飞书第一跳会错成「已回滚」，但第三跳 Deploy 日志仍是正确的 rc=5 `::error::`；打开 Run 的人能纠正。未达 internal 红线（不假绿、不损坏数据、不把坏版本判健康）。
- 不升 P2 的理由：与 `deferred`/`rollback_unhealthy` **同一写法**（`:328`、`:333`），单修 rc=5 会造出第三条不一致的失败模式；畸形 runner 状态按 review-discipline「可信配置畸形形态 ≤P2」还要再降。不为 P3 新增机制。
- 若后续有人要做（不阻塞、不新增机制）：飞书第一条加半句「若 Deploy 日志含 `image reconcile assertion failed` 则按第三条」——仍是文案，不是新 output/新 step。本轮不改文件。

## 3. skip-forward 与 busy-lock（fd 8）交互

锁序（H1 `scripts/pull_and_deploy.sh`）：

- `:516-571` `BUSY_LOCK_FILE` 非空才 `exec 8<"$BUSY_LOCK_FILE"`，循环拿 fd 8 排他锁 + 非阻塞探 fd 9；等 fd 9 时 `:562` `flock -u 8` 放忙锁再重试。超时 `exit 3`（进程退出，内核放锁），**走不到** 末尾释放。
- `:577-578` `flock 9` 后 `do_deploy`。skip-forward 在 **`:433-437`**，`deploy_tag` 之前 `return 0`——此时两把锁已经在手。
- `:580-587` `do_deploy` rc=0 仍持 fd 9 对账，然后 `flock -u 9`。
- `:593` `[ -n "$BUSY_LOCK_FILE" ] && flock -u 8`。

重入**不会**跳过忙锁获取却走到释放：获取在 `do_deploy` 外；skip 只跳 `deploy_tag`/探针。`:519` 超时配置非法是 `exit 1` 在 `exec 8` 之前，也走不到 `:593`。

`flock -u 8` 对未持有 fd 的行为（本机 util-linux）：

| 条件 | rc | stderr |
|---|---|---|
| fd 8 未打开 | 65 | `flock: 8: Bad file descriptor` |
| fd 8 只读打开、从未 `flock -x` | 0 | （空） |
| 持锁后 `flock -u` 一次 / 再 `flock -u` 一次 | 0 / 0 | （空） |

skip-forward 主路径属于「已 `flock -x 8`，末尾 `flock -u 8`」——成对。不会踩到 Bad file descriptor。二次 unlock 是空操作，不炸。

**stub 运行（H1 真实脚本 + 测试同款 docker/curl stub）：**

`BUSY_LOCK_FILE` 设为新建空闲锁文件，`last_good_tag` 预写 `abc1234`（= `GIT_SHA`），跑一次：

```
returncode 0
[deploy] busy lock + host deploy lock both acquired (admission closed until replace completes)
[deploy] this SHA already in last_good_tag; skip forward deploy; reconcile only
[deploy] image reconcile starting (host lock still held)
image reconcile values:
  expected_id=sha256:deadbeef
  latest_id=sha256:deadbeef
  running_ids=cid-app=sha256:deadbeef
::notice::image reconcile passed: abc1234 is the image ID used by latest and at least one running container
```

docker.log：`pull registry.example.com/ns/demo:abc1234`（忙锁 opt-in 在持锁前的预拉，`:522`，**不是** `deploy_tag`）；随后只有 reconcile 的 `compose config` / `image inspect` / `compose ps` / `inspect`。**无** `compose up`。

结论：忙锁获取发生在 skip 之前（日志第一行），释放按 `:593` 成对执行；重入只对账。预拉一次是忙锁门禁既有行为，不是 skip-forward 破坏锁对。无 P1。

## 4. README 两条 P3 的处置建议（不改文件）

**P3-1**（r2：`:237-238` rc=4 叙述仍把「`last_good` 等于本次 SHA」列为来源）——**随本 PR 一并改（文档 1 行）**。理由：值班账本第五跳若往下读到这段，会把同 SHA 重入理解成「紧急、无法回滚」的 rc=4，和表里 rc=0/rc=5 行打架。skip-forward 之后该组合不再走拒回滚；改成「无 `last_good_tag` 可回滚（含首次部署）」即可，不是新机制。不阻塞本轮 pass。

**P3-2**（r2：`:215` rc=0「已验证在应答」对重入过满）——**留 backlog**。理由：处置列仍是「无需动作」，过满的是把「身份仍匹配」读成「此刻 HTTP 在应答」；对账仍挡住容器没在跑。不把人引向重跑或已回滚，不是账本分流错误。不值得为它单独再开修复卡。

## Findings

本轮 **无新增 P1**。无新增 P2。

- r1 P3-5（`GITHUB_OUTPUT` 写失败 → 薄壳不跑 → 飞书单红读成已回滚）：本轮独立复判，**维持 P3**，接受不修。
- r2 P3-1 / P3-2：本轮只给处置建议（§4），不改级别、不改文件。同一意见不换措辞重提为新 finding。

## 工具标注 / 本仓判定 / 两问对照表

| 来源 | 工具标注 | 本仓判定 | P1 两问 |
|---|---|---|---|
| §1 重试循环 `:308-391` | （无外部工具）rc=5 是否再 attempt | **不是缺陷**：`:344` `exit 5`，到不了 `:389` `attempt++` | ①会走到 rc=5；②后果可接受（不重试部署） |
| §1 飞书缺「不要重跑」 | （本轮账本跳点） | **不是缺陷**（≤ 观察）：黄卡才叫重跑；同 SHA 重跑现只对账 | ① GitHub Re-run 按钮恒在；② 现后果可接受 |
| r1 OCR F5 / P3-5 | high → r1 P3 | **维持 P3** | ①真实 runner 注入可写文件，三份消费 log 零写失败；② 飞书会错但 Deploy `::error::` 仍对，不假绿 |
| §3 忙锁×skip-forward stub | （本轮 stub） | **不是缺陷**：获取在 `do_deploy` 外，释放成对 | ①会触发（opt-in + 同 SHA）；② rc=0、无 compose up |
| r2 P3-1 README `:237-238` | r2 P3 | **P3**，建议本 PR 改 1 行文档 | ①会误读 rc=4 来源；② 不假绿 |
| r2 P3-2 README `:215` | r2 P3 | **P3**，留 backlog | ①会过读「在应答」；② 处置仍是无需动作 |

## 收敛判定

internal + infra 提档 = saas 收敛条件：**连续 2 轮无新增 P1**，且相邻两轮须换执行器或换视角。

| 轮 | 执行器 | 视角 | 新增 P1 |
|---|---|---|---|
| r1 | Cursor | 正向兑现 + 降层三问 + 137/124 | P1-1 |
| r2 | Grok | H0..H1 四问 + 255 重放实测 + 矩阵格 + 飞书文案 | **无** |
| r3（本轮） | Cursor | on-call 跳点账本 + workflow 重试记账 + `GITHUB_OUTPUT` 复判 + 忙锁×skip stub | **无** |

本轮 **无新增 P1**。r2+r3 连续 2 轮无新增 P1，且换家（Grok → Cursor）+ 换视角（255 重放 / 矩阵格 / 飞书字面 → 值班账本 / 重试循环 / output 写失败 / fd 8）。收敛计数 **2/2**。

执行器：cursor / cursor-grok-4.6-high。只写本 verdict 文件，未改被审代码。临时 worktree `/tmp/review-ci38-r3` 用完删除。
