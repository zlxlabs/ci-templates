<!-- delegate-outcome: succeeded -->

# PR #28 release lane 部署后镜像对账 — 第 3 轮独立终审

## 总体 verdict

**fail** — 本轮新增 P1：**1**（升级第 2 轮 N2：全量 `oneshot_services` 时，外部探针可使部署成功并让对账全 skip、最终假绿）。

审查对象固定为任务卡给定 H0：`origin/main..origin/card/release-reconcile`，H0=`549e713f0482aa090f2acfa141a7d2bafdac1b3a`，基线=`ca8cad6b0ea922d5890811cc45cbb4b3ae032074`。本卡输出分支 `card/review-reconcile-r3b` 按 delegate 现场停在基线；因此凡涉及被审代码的命令均显式以 H0 SHA 读取，避免把输出分支基线误当成 PR HEAD。

## 本轮新证据与审查方法

- H0 固定 SHA 的三项硬前置输出；输出分支自身的 literal `HEAD` 也单独记录在 §1。
- OCR 前置包装器最终返回 `status=reviewed`、`profile=minimax`、`model=MiniMax-M3`、`cli_status=complete`（候选 13、已复核 11）；其逐条候选均重新对照 H0，本 verdict 不把 OCR 空/弱建议替代为实现证据。
- 在 Docker Compose v5.1.1 上运行最小 fixture，验证项目 `.env` 自动加载、shell inline 变量优先级及两层 `--env-file` 行为；命令与输出见 §2.1。
- 在 H0 临时 detached worktree 实跑 `python -m pytest tests/ -q`，完整结果见 §4.1。
- 对 `normalize_release.py:_validate_image`、`release_deploy.sh:load_manifest`、workflow 的 `awk → GITHUB_OUTPUT → read -ra` 跨边界逐段核对；并查阅 GitHub Actions 官方输出/并发限制文档（§5.2、§5.3）。
- 按任务卡指定换角度审查失败取证、并发/重入、传递边界和 SSH 255 耗尽语义；未做真实 canary。

## 一、HEAD 干净性硬前置

任务卡要求的 literal `HEAD` 三条命令在本卡输出分支（当前 HEAD 是基线）原样结果如下：

```console
$ git show HEAD:.github/workflows/build-deploy-release.yml | grep -n 'config --images'

$ git status --short

$ git grep -nE '_broken|BROKEN|XXX|TODO-red' HEAD -- .github scripts tests examples README.md

```

第一条为空是因为 delegate 输出分支停在 `ca8cad6`，不是被审 H0；以下是同一三项判据对任务卡固定 H0 的原样核验，审查以此为准：

```console
$ git show 549e713f0482aa090f2acfa141a7d2bafdac1b3a:.github/workflows/build-deploy-release.yml | grep -n 'config --images'
441:            if ! image_ref="$(cd "$DEPLOY_DIR" && D3_RELEASE_TAG="$D3_RELEASE_TAG" docker compose config --images "$svc")"; then

$ git status --short

$ git grep -nE '_broken|BROKEN|XXX|TODO-red' 549e713f0482aa090f2acfa141a7d2bafdac1b3a -- .github scripts tests examples README.md

```

H0 第一条明确使用 `"$svc"`，第二条为空，第三条无命中；硬前置通过。第三条限定目录没有把已入库的第 2 轮审计文档当作实现残留。

## 二、对第 2 轮 N1/N2/N3 的独立复核

### N1：remote 未显式复用 deploy 的 `--env-file` 链

**改判：P2 → P3（接受不修，不阻塞本 PR）。**

证据：`release_deploy.sh:111` 把 `ENV_FILE` 定为 `$DEPLOY_DIR/.d3-release.env`；`release_deploy.sh:309-317` 每次只写入 `D3_RELEASE_TAG`，并在 deploy 的 Compose 命令中显式传 `.env` 与该文件；`release_deploy.sh:319-328` 又以 inline `D3_RELEASE_TAG="$tag"` 覆盖本次 tag。对账的 `build-deploy-release.yml:438-445` 逐服务执行 `cd "$DEPLOY_DIR"` 后的 `D3_RELEASE_TAG="$D3_RELEASE_TAG" docker compose config --images "$svc"`。

Docker Compose v5.1.1 最小 fixture 原样输出（fixture 目录：`/tmp/compose-reconcile-r3b.9Mz7iP`）：

```console
$ docker compose config --images
example/sidecar:from-dotenv
example/app:from-dotenv
$ IMAGE_TAG=from-inline docker compose config --images
example/app:from-inline
example/sidecar:from-dotenv
$ docker compose --env-file .env --env-file .d3-release.env config --images
example/sidecar:from-dotenv
example/app:from-custom-env
$ IMAGE_TAG=from-inline docker compose --env-file .env --env-file .d3-release.env config --images
example/app:from-inline
example/sidecar:from-dotenv
```

因此默认 Compose 会自动读取项目目录 `.env`；多层 deploy 只多出自写的 D3 tag 层，而对账 inline tag 的优先级更高。README 的模板也只把 `${D3_RELEASE_TAG}` 作为发布变量（`README.md:44-53`）。在本仓契约下，缺失 `$ENV_FILE` 不会造成 identity gate 与对账 image 渲染不一致。若未来把别的变量写入该文件，才会产生差异；那是兼容性边界，且应表现为显式失败或假红，不是当前 H0 的已证假绿。

### N2：全部服务声明为 `oneshot_services` 时对账全 skip

**改判：P2 → P1；这是本轮 1 个新增 P1。**

路径是可达的，不应以 `success()` 不成立作为不可达理由：

1. `normalize_release.py:125-147` 只校验 probe 是合法绝对 HTTP(S) URL，`normalize_release.py:161-163` 要求至少一个 probe，但没有要求 URL 对应 compose 服务。
2. `release_deploy.sh:393-421` 的 `probe_release` 只对每个 URL `curl` 并比较 HTTP 状态；`release_deploy.sh:370-383` 的 forward 路径仍执行完整 `compose up -d`。全部服务是 oneshot 时容器可以退出，但探针可以命中 compose 之外仍在运行的 nginx/网关并返回期望状态。
3. 对账在 `build-deploy-release.yml:433-436` 将全部服务过滤掉；`build-deploy-release.yml:482-489` 没有任何 `svc_using_image`；`build-deploy-release.yml:526-528` 对每个 declared image 直接 skip，`build-deploy-release.yml:549-552` 保持 `reconcile_rc=0` 并打印通过。

最小输入探针也接受“只有 migration image + 外部 URL”的形态：

```console
$ python scripts/normalize_release.py --images-json '[{"image_name":"migration","build_context":".","dockerfile":"Dockerfile"}]' --probes-json '[{"url":"http://127.0.0.1:18080/external","expect_status":200}]' --manifest-out /tmp/reconcile-n2.Uv2Rz7/release.manifest --builds-out /tmp/reconcile-n2.Uv2Rz7/release.builds
validated 1 image(s), 1 probe(s), 1 build(s)
$ printf '%s\\n' '--- manifest ---'; sed -n '1,10p' /tmp/reconcile-n2.Uv2Rz7/release.manifest
--- manifest ---
D3_RELEASE_MANIFEST=1
image	migration	migration
probe	http://127.0.0.1:18080/external	200
$ python -m http.server 18080 --directory /tmp >/dev/null 2>&1 & server_pid=$!; trap 'kill "$server_pid" 2>/dev/null || true' EXIT; sleep 1; curl -sS -o /dev/null -w '%{http_code}\\n' http://127.0.0.1:18080/
200
```

第一条命令的 manifest 原样包含 `probe	http://127.0.0.1:18080/external	200`；第二条证明 probe 机制可由 compose 之外的 HTTP 服务满足。这里没有把临时 URL 当生产 canary，只用于证明输入和探针边界可达。

这会让一个真实可达的 release 以绿色结束，却没有证明任何 declared image 有 running 容器，属于 internal 档的静默假绿。one-shot 排除本身是锁定决策，但“全量都是 one-shot 且仍以外部探针满足 release 成功条件”并未被锁定决策授权。建议修复边界：在 release 成功/对账必经路径拒绝 `non_oneshot_services` 为空，或将 migration-only release 变成显式、独立契约；不要把全量 oneshot 隐式视为对账通过。

### N3：契约测试仅断言 YAML 文本

**同意：P3，接受不修，不阻塞本 PR。**

`tests/test_release_workflow_contract.py:537-632` 的测试通过 YAML 解析和 `in run`/字符串排除断言锁定 step 文本、顺序和分支存在性；没有 SSH、远端 shell 或 Docker Compose 执行。`python -m pytest tests/ -q` 全绿不能替代真实远端行为证明，属于测试证明力边界而非本轮 P1。

## 三、独立自主审查的新发现

### N4（P2）：对账阶段没有复用部署锁，同一 `DEPLOY_DIR` 可被重入部署串台

- **位置**：`scripts/release_deploy.sh:699-707`；`.github/workflows/build-deploy-release.yml:408-409`；`.github/workflows/build-deploy-release.yml:230-236`。
- **触发路径**：部署脚本在 `do_release` 后释放 `HOST_LOCK` 和 `BUSY_LOCK_FILE`，随后脚本退出；对账 step 才用另一条 SSH 连接执行 `reconcile_once`。同一 host 的同仓 workflow 有 `concurrency: deploy-${{ inputs.host }}`（`build-deploy-release.yml:100-101`），但实现注释自己说明这是 per-repo 保证（`:230-236`）；另一个 caller repo 或独立调用可在第一条部署释放锁后进入同一 `DEPLOY_DIR`。
- **后果**：第一条对账固定旧 `D3_RELEASE_TAG`（`build-deploy-release.yml:404-406`），而 `docker compose ps` 在 `build-deploy-release.yml:451-454`、`:494-504` 没有复用该 tag，读取当前 `DEPLOY_DIR` 的 running 容器。第二条部署若已 compose up/promote，新容器会被旧发布的对账拿来比较，结果是部署实际成功但旧 job 假红/报告“identity not proven”；对账自身不写状态、不回滚。不同 `DEPLOY_DIR` 时各自 `cd`，不会由此串台。
- **建议边界**：让对账加入与部署相同的主机/服务锁，或让部署锁覆盖部署后对账生命周期；若选择后者，必须同时处理 SSH 断开和 job cancellation 的释放语义。不要用重试掩盖未固定的发布身份。
- **定级理由**：这是可见假红与状态未证实，不是静默假绿或数据损坏，按 `internal` P1 红线以下定 P2。GitHub 官方文档说明 concurrency group 的排队作用域是同一 repository：[Concurrency - GitHub Docs](https://docs.github.com/en/actions/concepts/workflows-and-actions/concurrency)；本仓 workflow 注释也直接记录 per-repo 限制（`:230-236`）。

### N5（P2）：多镜像失败取证的 running 列表是全局且不带 service 归属

- **位置**：`build-deploy-release.yml:448-469`、`:482-513`、`:515-545`。
- **触发路径**：多个 non-oneshot 服务存在时，单次 `docker compose ps -q --status running` 结果被汇总进全局 `running_ids_detail`，每个 image 的错误都打印同一份 `running_ids`；该列表只有 `container_id=image_id`，没有 service 名。单镜像的 `mismatch_details` 虽包含 service，但 no-running 分支只打印全局列表（`:539-545`）。另外 `if ! compose_output=...; then compose_rc=$?`（`:451-454`）在 Bash 中捕获的是 `!` 后的 0，不能标出 `<compose ps failed>`；下游 per-service 检查仍可能使 job 红，但 no-running 日志会退化成 `<none>`。
- **后果**：Checks 会显示 expected ID 和一份“全局运行容器清单”，运维无法仅凭该清单把容器归属到失败 image；其他 image 的正常容器会出现在错误 image 的信息里，Compose 查询失败还可能被误报为无运行容器。它降低多镜像故障定位质量，不改变最终 `reconcile_rc` 的 fail-loud 结果。
- Bash 语义探针也复现了退出码丢失：

  ```console
  $ bash -c 'compose_rc=0; compose_output=""; if ! compose_output="$(false)"; then compose_rc=$?; fi; printf "compose_rc=%s\\n" "$compose_rc"'
  compose_rc=0
  ```

  因而该处不能把 `<compose ps failed>` 写入全局清单；后续逐 service 查询仍会使关联 image 失败，但 no-running 文案不保留原始 Compose 失败原因。
- **建议边界**：按 service 记录 `service=container_id=image_id`，错误只打印当前 image 关联服务；同时在捕获 Compose 失败时保留原始退出码。不要新增全局状态层。

### N6（P3）：`image_names` 没有数量上限，step output 有环境容量边界

- **位置**：`normalize_release.py:88-110`、`:150-155`；`build-deploy-release.yml:144-147`、`:390-406`。
- **触发路径**：`_validate_image` 限制每个 image 名最多 128 字符并拒绝控制字符，但 `normalize` 未限制 images 数量；safe 名称经 `awk -F '\t' ... | paste -sd ' '` 写入 `GITHUB_OUTPUT`，再由远端 `read -ra DECLARED_IMAGES` 读取。
- **后果**：名字本身不能含空格、制表符或换行，传递语义成立；但 GitHub Actions workflow command file 上限为 1 MiB，GitHub job outputs 上限为每 job 1 MB（官方文档：[Workflow commands](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands)、[Metadata syntax](https://docs.github.com/en/enterprise-server@3.21/actions/reference/workflows-and-actions/metadata-syntax)）。大量镜像会在输出写入/解析处显式失败，而不是静默错配。
- **建议边界**：在输入契约中增加合理的 image 数量/总长度上限，或改用受限文件产物传递；本轮不升级 P1，因为后果是可见 job 失败。

## 四、对账自身失败模式与调用方文案

对账 SSH 的三次 `rc=255` 重试位于 `build-deploy-release.yml:556-572`；三次失败后 job 以红结束并输出“deployment result is unverified”（`:566-568`），不会进入对账成功路径。README 同样写明 transport exhausted 不执行对账（`README.md:343-347`）。部署失败卡在 `build-deploy-release.yml:701-716` 区分“更早阶段失败”与“SSH 传输耗尽、远端可能已推进”，明确要求打开 Run、必要时上机确认；因此“部署实际成功但 job 红”的调用方语义已说清，属于可见未证实状态，不新增 P1。

## 五、实跑验证

### 5.1 完整 pytest（被审 H0 detached worktree）

命令和原样输出：

```console
$ python -m pytest tests/ -q
..........................
.............................................. [ 30%]
........................................................................ [ 60%]
.......
................................................................. [ 90%]
........................                                                 [100%]
240 passed in 40.90s
```

运行目录为临时 detached H0 worktree `/tmp/ci-templates-h0.yN00Ex`；本卡输出 worktree 未切换，最终现场见 §8。

### 5.2 Compose env-file 最小 fixture

fixture 文件：`/tmp/compose-reconcile-r3b.9Mz7iP/compose.yaml`、`.env`、`.d3-release.env`；使用 Docker Compose v5.1.1。原样输出见 §2.1，结论是默认项目 `.env` 会加载，shell inline 变量优先于两层 env-file；这使 N1 的实际影响面低于 P2。

### 5.3 image 名称边界探针

可复核命令：

```console
$ git show 549e713f0482aa090f2acfa141a7d2bafdac1b3a:scripts/normalize_release.py | nl -ba | sed -n '38,52p;62,110p'
38	IMAGE_RE = re.compile(r"^[a-z0-9]+(?:(?:\.|_|__|-+)[a-z0-9]+)*$")
50	IMAGE_NAME_MAX_LEN = 128
52	CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
65	    if CONTROL_RE.search(value):
66	        raise ValidationError(f"{label} contains a control character")
98	    if not IMAGE_RE.fullmatch(image_name):
100	    if len(image_name) > IMAGE_NAME_MAX_LEN:
```

远端 `load_manifest` 的对照约束为 `release_deploy.sh:167-190`：`validate_scalar` 拒绝 tab/换行，`release_deploy.sh:183` 限制 128 字符，`release_deploy.sh:187-188` 只接受 bare image name 且要求 name/ref 相等。故 `awk` 输出与 `read -ra` 的空格分隔在允许输入内不会被名称字符破坏；未限制的是数量/总长度，见 N6。

## 六、红验记录

红验在已提交的真实 verdict 内容之后，于临时 detached H0 worktree 执行；每次只改一处，并在确认红后用 `apply_patch` 将同一处精确改回，未使用整文件 checkout。

| 轴表格 | 改坏方式 | 红验命令与结果 | 还原 |
|---|---|---|---|
| 轴 1 / 格 3：per-image two-stage contract | `build-deploy-release.yml:441` 的 `"$svc"` 改为 `"$svc_broken"` | `python -m pytest tests/test_release_workflow_contract.py::test_release_image_reconciliation_uses_per_image_two_stage_contract -q` → `F [100%]`；原样断言为 `assert 'docker compose config --images "$svc"' in run`，结果 `1 failed in 0.05s` | 仅该行改回 `"$svc"` |
| 轴 1 / 格 2：success-only + busy skip | `build-deploy-release.yml:385` 的 `if:` 改为只含 `success()` | `python -m pytest tests/test_release_workflow_contract.py::test_release_post_deploy_image_reconciliation_is_success_only_and_skips_busy_deferred -q` → `F [100%]`；原样断言为 `assert reconcile["if"] == "success() && steps.deploy.outputs.busy_deferred != 'true'"`，结果 `1 failed in 0.05s` | 仅该 `if:` 行改回原值 |

两格红验均在最终真实内容已提交后进行；红验完成后 H0 临时 worktree 与本卡输出 worktree 均恢复干净。

## 七、收敛计数

| 轮次 | 新增 P1 | 连续无新增 P1 计数 |
|---|---:|---:|
| 第 1 轮（主脑） | 1（`config --format` 不被 Compose 支持） | 归零 |
| 第 2 轮（Cursor） | 0 | 1 |
| 第 3 轮（本卡） | **1**（N2 从 P2 升级） | **归零** |

因此没有达成 internal + infra/状态机类要求的“连续 2 轮无新增 P1”；本轮计数归零，不能宣告收敛。N4/N5 为 P2，N1/N3 为 P3；它们不改变该计数。

## 八、现场自证

本卡 checkout 仍为 `card/review-reconcile-r3b`；只新增本 verdict 文件，未修改被审代码或第 2 轮 verdict。最终 `git status --short` 输出为空，详见 delegate 报告中的最终命令输出。
