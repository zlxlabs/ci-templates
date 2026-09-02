verdict: pass

# 本轮方向声明

第 2 轮，换家换视角：真实 curl 行为 + 线上日志形态。不重复第 1 轮正向全量 / 降层三问 / stub 组合 / 熵增。

- 审查对象冻结：`af8c6d592a361aa7f8a8b82f5c16deee455d6f7e..22426491ab1b86ca6fc3ad397d545bf0c7473976`（H0）。
- spec = PR #40 正文 + issue #19（含 09-02 评论：jobs 日志 grep `probe-attempts`）。
- 风险档 **internal**；infra 提档按 saas 收敛（连续 2 轮无新增 P1）。
- 本轮新证据（开启本轮的外部证据）：① 本机真实 curl 对「发 header 后挂 body」与完整 200；② H0 脚本 + stub 成功 / 200+rc=28 耗尽回滚 的 stdout 全文；③ 慢但传完 vs 无 header 对照。第 1 轮 verdict 只作对照，不当证据。
- 执行器：cursor / `cursor-grok-4.6-high`。OCR 本轮未跑（同一 diff 再扫不算新证据；r1 已扫 1 条 low）。
- 临时树：`git worktree add /tmp/review-ci40-r2 2242649`（用完已删）。

# 1. 真实 curl：发 header 后挂住不发 body

脚本同款命令行：`curl -s -o /dev/null -w '%{http_code}' --max-time <n>`。本机 `python3` socket、随机端口 `22145`，`Content-Length: 1000` 发完 header 后 sleep，不发 body。

## 挂 body

```
PORT=22145
curl -s -o /dev/null -w '%{http_code}' --max-time 2 http://127.0.0.1:22145/health
http_code=200
curl_rc=28
```

## 正常应答（同端口、完整 200 + body `ok`）

```
curl -s -o /dev/null -w '%{http_code}' --max-time 2 http://127.0.0.1:22145/health
http_code=200
curl_rc=0
```

这就是 #19 假绿的原始形态：`%{http_code}` 已是 200，curl 因 `--max-time` 以 rc=28 退出。修前只比 code 会判健康；修后 `code==200 && curl_rc==0` 才会过闸。第 1 轮 stub 预设了这对数字，本轮用真 curl 钉死。

对照（同一命令行，证明「慢但传完」不是这条路径）：

```
SLOW_COMPLETE (sleep 1s 再完整 200, max-time 2):     http_code=200 curl_rc=0
HEADER_THEN_SLOW_BODY_COMPLETE (header 后 1s 才发完 body): http_code=200 curl_rc=0
NO_HEADERS (2s 内不发 status line):                  http_code=000 curl_rc=28
```

# 2. 线上日志形态：H0 脚本 + stub

工作树 `/tmp/review-ci40-r2` @ `2242649`。`CURL_BIN`/`DOCKER_BIN` stub，`HEALTHCHECK_WARMUP=0`、`HEALTHCHECK_INTERVAL=0`、`HEALTHCHECK_RETRIES=3`。命令等价于测试助手 `_run(env)`：`bash scripts/pull_and_deploy.sh`。

## 成功路径（curl stub 打印 `200`、exit 0）

```
===== success =====
returncode=0
----- stdout -----
[deploy] deploying registry.example.com/ns/demo:abc1234
[deploy] warmup 0s before probing http://localhost/health
[deploy] health probe OK (attempt 1, status 200)
[deploy][evidence] probe-attempts: 200(curl=0)
[deploy] deploy of abc1234 healthy; recorded as last good
[deploy] image reconcile starting (host lock still held)
image reconcile values:
  expected_id=sha256:deadbeef
  latest_id=sha256:deadbeef
  running_ids=cid-app=sha256:deadbeef

::notice::image reconcile passed: abc1234 is the image ID used by latest and at least one running container
----- stderr -----
```

对照 #19 09-02 采集方式（jobs 日志 grep `probe-attempts`）：修后 pull lane **成功路径也落** `[deploy][evidence] probe-attempts: 200(curl=0)`。此前「成功率 0」对目标形态不可观测，是因为成功即 return、不打这行；现在 grep 能看到健康样本，目标形态若再出现会以 `200(curl=28)` 出现在失败路径（下一小节），不再被成功 return 吞掉。

## 失败路径（200 但 rc=28 → 重试 → 耗尽 → 回滚；旧版 200/curl=0）

curl stub 序列：`(200,28)×3` 后 `(200,0)`。

```
===== fail_retry_rollback =====
returncode=1
----- stdout -----
[deploy] deploying registry.example.com/ns/demo:new2222
[deploy] warmup 0s before probing http://localhost/health
[deploy] health probe attempt 1/3 got '200' but curl rc=28 (transport incomplete)
[deploy] health probe attempt 2/3 got '200' but curl rc=28 (transport incomplete)
[deploy] health probe attempt 3/3 got '200' but curl rc=28 (transport incomplete)
[deploy][evidence] probe-attempts: 200(curl=28),200(curl=28),200(curl=28)
[deploy] health probe FAILED for new2222
[deploy][evidence] compose-ps:
[deploy][evidence] new-container running
[deploy][evidence] container-logs:
[deploy][evidence] new-container last-line
[deploy] rolling back to previous good tag old1111
[deploy] deploying registry.example.com/ns/demo:old1111
[deploy] warmup 0s before probing http://localhost/health
[deploy] health probe OK (attempt 1, status 200)
[deploy][evidence] probe-attempts: 200(curl=0)
[deploy] rollback to old1111 complete; old version passed the same-budget health probe
----- stderr -----
```

on-call 一眼能看出「不是状态码问题是传输不完整」：每轮重试都有 `got '200' but curl rc=28 (transport incomplete)`，取证行是 `200(curl=28)` 而不是 `000` 或 `5xx`。jobs grep `probe-attempts` 能命中目标形态。

# 3. 回滚探针同判据：200/rc=28 → rc=4

问：新版 200/rc=28 回滚后，旧版若也 200/rc=28（例如上游代理慢），是否把「网络慢但服务其实健康」从 rc=0 推到 rc=4？

本轮加跑 stub（序列 `(200,28)×6`，新版 3 次 + 回滚 3 次）：

```
===== fail_rollback_also_28 =====
returncode=4
----- stdout -----
[deploy] deploying registry.example.com/ns/demo:new2222
[deploy] warmup 0s before probing http://localhost/health
[deploy] health probe attempt 1/3 got '200' but curl rc=28 (transport incomplete)
[deploy] health probe attempt 2/3 got '200' but curl rc=28 (transport incomplete)
[deploy] health probe attempt 3/3 got '200' but curl rc=28 (transport incomplete)
[deploy][evidence] probe-attempts: 200(curl=28),200(curl=28),200(curl=28)
[deploy] health probe FAILED for new2222
[deploy][evidence] compose-ps:
[deploy][evidence] new-container running
[deploy][evidence] container-logs:
[deploy][evidence] new-container last-line
[deploy] rolling back to previous good tag old1111
[deploy] deploying registry.example.com/ns/demo:old1111
[deploy] warmup 0s before probing http://localhost/health
[deploy] health probe attempt 1/3 got '200' but curl rc=28 (transport incomplete)
[deploy] health probe attempt 2/3 got '200' but curl rc=28 (transport incomplete)
[deploy] health probe attempt 3/3 got '200' but curl rc=28 (transport incomplete)
[deploy][evidence] probe-attempts: 200(curl=28),200(curl=28),200(curl=28)
[deploy] rollback health probe FAILED for old1111; production state is uncertain
----- stderr -----
```

脚本行为：是，双侧 200/28 走既有「回滚探针失败 → rc=4」。这不是新状态机。

「网络慢但服务健康」是否被误伤：真实 curl 对照说明 **不是**。慢但在 `--max-time` 内传完 body → 仍 `200/rc=0`（过闸）。2s 内连 status line 都没有 → `000/rc=28`（修前已失败）。只有「header 已是 200、body 传不完」才从修前假绿翻成失败。健康检查 body 通常很小；默认 `HEALTHCHECK_TIMEOUT=5` 传不完，更接近 #19 要抓的传输不完整，而不是「稍微有点慢」。

## P 等级（本条）

| 项 | 内容 |
|---|---|
| 工具标注 | 无（本轮自提，非 OCR/主审） |
| 本仓判定 | **不构成缺陷**；文档提示记 backlog P3，不阻塞 |
| 两问 ① 真实使用会被触发吗？ | #19 09-02 已在真实消费环境量过：12 仓 60 deploy job，release 成功路径 26 条全 `curl=0`，pull 仅 1 条失败 `000(curl=56)`，目标形态 `200(curl=28)` 发生率 0。本轮真 curl 能复现该形态，但是故意挂 body 的实验室 server，不是舰队健康检查。双侧同时 200/28 在舰队未观测到。 |
| 两问 ② 触发了后果能否接受？ | 若真发生：rc=4 `@全员`、`last_good` 不推进。这是既有升级卡，fail-loud。内部档 P1 是静默出错 / 损坏他人数据——本条是假绿的反面。比修前「body 没传完却 rc=0」可接受。已否决「改重试/超时参数」，不新开机制。 |

若仍要处理：只允许文档提示，例如 README 探针预算节加一句「`HEALTHCHECK_TIMEOUT`（默认 5s、无 workflow input）覆盖**含 body 的完整响应**，不是 TTFB；健康检查传不完 body 会按探针失败回滚，双侧传不完升 rc=4」。不改超时值、不加 input。

# 4. README / 文档一致性

A3 已补「判健康需 HTTP code 匹配且 curl rc=0」（本 diff 唯一 README 行）。退出码表 `rc=1`/`rc=4` 写的是探针**结果之后**的生产状态与处置（回滚探针过→1，过不了→4），不定义探针谓词。失败原因是 5xx 还是 `curl rc≠0`，分流不变。再在表里写「curl rc≠0 也算失败」是重复 A3，还可能让人以为 rc=1/4 的处置因 curl rc 而变。

**判定：退出码表不必改。** 取证节（`000` vs `5xx`）没点名 `200(curl=28)=传输不完整`，与 §2 日志比是文档滞后，≤P3、接受不修（失败路径已有 `transport incomplete` 原文，on-call 不依赖 README 才能读懂）。

# Findings

无新增 P1 / P2。无阻塞项。

# Backlog（本轮不阻塞）

- **P3** README 探针预算节可补一句 timeout 覆盖完整响应（含 body），见 §3。不改参数、不新 input。与已否决「改重试/超时参数」不冲突。
- 第 1 轮 backlog（回滚侧 200/28→rc=4 无专项测试等）不重提；本轮 stub 只作 §3 取证，不升为新 finding。

# 收敛判定

- 本轮新证据齐：真 curl 钉死 #19 原始形态；成功路径 `probe-attempts` 可被 09-02 同款 grep 采到；失败路径 `transport incomplete` 可读；双侧 200/28 升 rc=4 是既有门，未把「慢但传完」从 rc=0 推走。
- 本轮 **无新增 P1**。
- 与第 1 轮（pass、0 P1）合计连续 2 轮无新增 P1；本轮换了证据源（真 curl + 日志形态，不是同 diff 复读），计入 saas 提档收敛。是否收口由主脑确认。
- 结构检查：首行 `verdict: pass`；四项各一节，1/2 贴命令与输出原文；§3 含工具标注 / 本仓判定 / 两问；本节为收敛判定。
