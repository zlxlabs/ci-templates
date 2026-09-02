verdict: pass

# PR #37 第 1 轮独立全量审查

审查对象冻结 `9131e7b30b32cb7266e4eb6be89c3fb24de294ea..48a67f9cd245aee42b63dd24808f0332c687507d`（H0=`48a67f9`）。风险档 **internal**，infra/失败路径例外按 saas 收敛条件（本轮是第 1 轮，不计数收敛）。执行器 cursor / cursor-grok-4.6-high。只审本次 diff（`scripts/release_deploy.sh` + `tests/test_release_deploy.py`）；pull lane 与 #35 存量对账逻辑不占本轮。

**本轮方向**：正向全量（PR 正文每句是否兑现）+ 降层三问。本轮新证据：H0 临时 worktree pytest、本机 Compose v5.5.0 `config --format json` / `ps -q` 实测、唯一消费者 `web_transcibe_translate` compose 抽查、`ocr-review` status=`reviewed`。

已覆盖问题清单：

| 问题 | 结论 |
|---|---|
| 单服务精确取值（`services.<svc>.image`） | 兑现；复现测试锁死 mapping 不再吃 depends_on 多行 |
| 四种失败均 `return 1`（→ rc=5） | 兑现：无 python3 / 渲染失败（含 timeout）/ JSON 解析失败 / 非 oneshot 无 image |
| 取证行 `[release][evidence] service_images: …` | 兑现；缺 image 提前 return 时不打印，见 P3-2 |
| 无「未引用 → skipped」静默假绿（映射错位） | 兑现；oneshot 专用镜像 skip 是 #28 既有契约，未改 |
| 降层① promote 后 rc=5 世界状态 | 文案与 README 一致，本 diff 未改锁/rc=5/超时形状 |
| 降层② `image` 字段唯一性 / 归一化 | 本机 v5.5.0 对消费者形态原样保留，见生产者抽查 |
| 降层③ `ps -q` 多服务只返回 1 容器时判定 | 判定走逐服务 `ps -q`；批量 ps 只写取证文本 |

## 正向全量对照

PR 正文「改了什么」三条均在 H0 兑现：一次 `config --format json`（`:372`）、`python3 -c` 抽 `services.<svc>.image` 建 `service_images_map`（`:378-412`）、判定循环按 map 精确反查（`:457-461`）。四种失败均 `return 1`，测试 `test_reconcile_missing_python3_returns_rc5` / `test_reconcile_docker_timeout_during_json_config_returns_rc5` / `test_reconcile_compose_json_parse_failure_returns_rc5` / `test_reconcile_service_missing_image_field_returns_rc5_with_error` 分别锁死。无「json 失败退回旧逻辑」兜底（已否决方案未重现）。持锁 / rc=5 / 超时仍走 `reconcile_docker` + `:934-936` 映射，未改 pull lane。

`::notice:: skipped running check`（`:499-501`）仍在：这是 declared image **仅**被 oneshot 引用时的 README 契约，不是映射错位假绿。复现用例断言 frontend/backend **不会**再被 skip。

## 降层三问

### ① 终态写入前的不可逆动作

对账在 `do_release` 成功返回之后、`flock -u 9` 之前（`:930-939`）。rc=5 时 pull/retag、`compose up -d`、探针、`last_good_release` promote 都已发生；脚本不自动回滚。错误串写明「last_good 已推进到本 SHA、本次不自动回滚、需上机核验」（`:494/:510/:515`），与 README「部署后镜像事实对账」段一致。本 diff 只换取值器，不改变这本账。python3 缺失会在已 promote 后 rc=5——fail-loud，不是静默错。

### ② 守卫值 `services.<svc>.image` 是否唯一

比较式是精确相等：`service_images_map[$svc] == "${image_name}:${D3_RELEASE_TAG}"`（`:459`）。唯一消费者 compose 写的是 `transcribe-backend:${D3_RELEASE_TAG}` / `transcribe-frontend:${D3_RELEASE_TAG}`（无 registry 前缀）。本机 Compose v5.5.0：带 tag 的短名原样保留、不补 `:latest`、不自动加 `docker.io/library/`；`config --services` 与 json `services` 键集合一致（profiles 开/关两侧对齐，顺序可不同：`--services` 按依赖序，json 按对象键）。身份门禁仍用无服务参数的 `config --images` 集合匹配（本 diff 未改），declared image 若不是 `name:tag` 会在 up 前被拦住，到不了对账。

### ③ 保护覆盖的是写入还是行为

对账是只读观察。批量 `ps -q --status running "${non_oneshot_services[@]}"`（`:429`）只填 `running_ids_detail` 取证文本；**判定**对每个命中服务再跑一次 `ps -q --status running "$svc"`（`:468`）。N5 即使在某些形态下批量只吐 1 个容器，也不改判定。本机 v5.5.0 三服务 running 批量实测返回 3 个 id。

## 真实 producer 抽查（Compose v5.5.0，≤20 行）

三服务 + `depends_on`（frontend→backend→nginx），`config --format json`：

```
image[backend]='transcribe-backend:def567890123'
image[frontend]='transcribe-frontend:def567890123'
image[nginx]='nginx:alpine'
--services: nginx backend frontend
json keys: backend frontend nginx  （同集合，序不同）
untagged redis → 'redis'（不补 :latest）
redis:latest → 'redis:latest'
docker.io/library/nginx:alpine → 原样
--profile extra: --services 键 == json 键
ps -q --status running svc-a svc-b svc-c → 3 个容器 id
```

与测试 stub `"image":"transcribe-backend:<tag>"` 同构。唯一消费者 `web_transcibe_translate/docker-compose.yml` 同形态（另有 worker 共用 backend 镜像、nginx/postgres 公共镜像、migrate oneshot）。

## 测试

`git worktree add /tmp/review-ci37 48a67f9` 后：

`uv run --with pytest,pyyaml,jsonschema python -m pytest -q tests/test_release_deploy.py` → **84 passed in 34.20s**（与 ci.yml 同款依赖）。

OCR：`ocr-review` status=`reviewed`（minimax / MiniMax-M3），4 条；落地判定见下，无升级为 P1。

## Findings

### P1

无。

### P2

无。

### P3

#### P3-1 把映射事故证据写进脚本注释

- 位置：`scripts/release_deploy.sh:366-370`「Historical contract note」。
- 违反：review-discipline 熵增/「把证据写进注释」；不是行为不变式。
- 推理：五行使 git blame 变成事故备忘录；#25 评论与本 verdict 才是证据落点。
- 工具标注 / 本仓判定 / 两问：OCR 未提 / P3 / ①会触发（每次读脚本）②后果可接受（不影响判定）。
- 建议：删注释，指针留 #25。

#### P3-2 缺 image 的失败路径不打取证行

- 位置：`:414-418` `return 1` 在 `:421-427` 取证行之前。
- 违反：PR 正文要求新增取证行；未要求失败路径也打，故不升 P2。
- 推理：缺字段时 map 不完整；`::error::` 已点名服务。python3/渲染/解析失败时尚无可用 map。
- 工具标注 / 本仓判定 / 两问：OCR finding 3 severity=low（codex-sub confirmed） / P3 / ①缺 image 会触发 ②fail-loud，可接受。
- 建议：若要修，把取证行挪到缺 image 校验之前（空槽也打印）。

#### P3-3（OCR，接受不修）测试 mock 用 f-string 拼 JSON

- 位置：`tests/test_release_deploy.py` `_reconcile_mock_bash`。
- 违反：无生产不变式；测试硬编码服务名。
- 工具标注 / 本仓判定 / 两问：OCR finding 1 low / P3 / ①真实生产路径不走该 helper ②不可接受性不成立。
- 建议：不必为本轮修。

OCR finding 0（medium，要求断言 `passed for nginx`）**驳回**：对账按 `IMAGE_NAMES` 循环，nginx 是 README 允许的公共额外镜像，不是 declared image；按 OCR 去锁「nginx passed」会反着契约。

## 熵增审查

| 新增 | 熵 +1？ | 理由 |
|---|---|---|
| `python3 -c` 内联解析 | 否 | 替换错误的 per-service `config --images`，stdin 进 `json.loads`，无转发包装、无第二实现 |
| `service_images_map` | 否 | 替换 `service_images_output`，净零；单函数内局部表 |
| 取证行 | 否 | spec 要求的可观察输出 |
| `command -v python3` | 否 | fail-loud 归因；缺解释器本来也会炸，这里只是把 rc 收成 5 |
| Historical contract note | 是（P3-1） | 把事故证据写进注释 |

未新增 fallback/双路径；测试里残留的 `config --images` 分支服务的是未改的身份门禁 mock，不是对账双路径。

## 查过什么、为什么没 P1

- 比较式若撞上 registry 前缀 / `:latest` 补全会假 skip → 假绿。本机 v5.5.0 + 消费者短名:tag **不归一化**；身份门禁仍在 up 前拦非 `name:tag`。第一问在真实形态下不触发。
- `python3` 新宿主依赖：缺则已 promote 后 rc=5。fail-loud，文案已交代；不是静默错/假绿。
- `backend`+`worker` 共用 `transcribe-backend`：map 循环会把两个都放进 `svc_using_image`；「至少一个 running 命中即 passed」是 README 既有「至少一个非 oneshot」契约（#28），不是本 diff 引入。
- `IFS='=' read` 解析 `svc=img`：镜像引用不含 `=`；JSON 走 stdin 不进 `python3 -c` 单引号源码。

## Backlog（不占本轮）

- pull lane 对账同样不持锁（issue #34）。
- #35 存量：`ps -q` 多副本时 `container_id` 可能多行（N5 取证文本；判定已逐服务）。
- oneshot 专用 declared image 的 skip notice（#28 README）。
- 「至少一个命中即 passed」：worker 旧、backend 新时整镜通过——契约如此，要改需先改 README。
- release lane 线上证伪实验（#25 维持 open；本 PR 明确不做）。
- README `:44/:71` 身份门禁仍写 `config --images`（正确，非本 diff）。
