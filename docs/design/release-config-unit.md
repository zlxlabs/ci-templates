# release 发布单元：镜像 + compose + 声明的配置文件

**状态**：设计待审（ci-templates#71）。本文不改运行时。
**取代**：无。issue 建议的「只回读哈希、不下发」已被否决。
**范围**：只针对 `build-deploy-release.yml` / `scripts/release_deploy.sh`。普通模板同形态缺口见第 6 节，另议。

## 已拍板（2026-11-08；2026-11-09 修正「插值先于合并」一条，见下表与第 4 节）

| 决策点 | 结论 |
| --- | --- |
| 发布单元 | 镜像 + compose + 声明的配置文件，一起下发、一起回滚 |
| 哈希 | 只作下发后的读回验证，不能代替下发 |
| 主机差异 | 只能放在主机持有的显式 override；部署不覆盖它 |
| 插值先于合并（2026-11-09 修正） | 逐文件插值发生在合并之前；override 只覆盖插值后的结构字段，合并前的插值报错（必需变量缺失、被引用文件不存在）它吸收不了——插值所需值必须由主机 `.env` 提供（证据：[研究文档 §3](../sessions/261108-gate-triage/research/R-CT71D-recon.md)，v2.40.3 / v5.5.0 双实测） |
| `.env` / 凭据 | 永不由发布下发 |
| 模板 | 只改 release lane；两模板刻意分叉，禁止把补丁搬到 `build-deploy.yml` |

## 现状（实现前基线，文件:行均相对本仓 `ad2185fa`）

- 主机侧只 scp 两样：`release_deploy.sh` 与 `d3-release.manifest`（`.github/workflows/build-deploy-release.yml:277`、`:283-295`）。无 compose / 配置传输，远端清理也只删这两个路径（`:277`）。
- `scripts/release_deploy.sh` 全程只读 `$DEPLOY_DIR` 已有文件：`:262-268`（`compose config --services`）、`:354-358`（对账 `--env-file`）、`:502-516`（身份门禁 `config --images`）、`:575`（`compose up -d`）。`.env` 读取点 `:262` / `:354` / `:502`。脚本不写 compose 或 bind-mount 配置。
- manifest 只有 `image` / `probe` 两种记录（`load_manifest` `:168-203`）；未知 `kind` 直接拒绝（`:200`）。生成器 `scripts/normalize_release.py` 只吃 `--images-json` / `--probes-json`（`:205-208`），`_write_manifest` `:179-187` 只写 image/probe。
- 唯一 release 模板使用者：`zlxlabs/web_transcibe_translate`（远端 `deploy.yml:30` `uses: .../build-deploy-release.yml@v2`，`ci_templates_ref: v2`）。其远端 `docker-compose.yml:8` bind `./nginx/nginx.conf`；`env_file` 为 `${ISEC_ENV_FILE:?...}`（backend/worker/frontend 三处）。issue #71：主机 compose 的 `env_file` 用 `.env`，与仓库不同。该仓 #48 安全修复合并后部署 run 39638463886 判绿，主机仍是旧 compose / 旧 nginx.conf。
- `last_good_release` 权威记录 = 一行 SHA + 完整 manifest（`promote` `:624-633`，同目录 `mv` 原子提交）。回滚重放上一份 manifest 的镜像组（`do_release` `:733-748`），但 `compose up` 作用的是**此刻主机上的** compose，所以配置漂移会让「整组回滚」只回镜像、不回配置。

---

## 1. 调用方如何声明随发布下发的文件集

新增 `workflow_call` 输入，**默认空 = 行为与现状逐字节一致**（唯一使用者钉 `@v2`，release 模板没有 `@main` canary，见第 7 节；默认开启会在移动 `v2` 时直接打到生产）。

```yaml
release_files_json:
  description: "Strict JSON array of repo-relative paths to ship with the release (compose + bind-mounted configs). Empty = do not ship files."
  required: false
  default: "[]"
  type: string
```

形状：JSON 数组，元素为字符串路径。非空时**必须**恰好包含一个 compose 文件（basename ∈ `docker-compose.yml` / `docker-compose.yaml` / `compose.yml` / `compose.yaml`）。示例：

```json
["docker-compose.yml", "nginx/nginx.conf"]
```

未知字段、非数组、非字符串元素、重复路径 → 在 `normalize_release.py` fail-loud（与 image 未知字段同一闸，`test_release_input.py` 已有模式）。

路径约束（复用并锁死 `normalize_release.py` `_relative_path` `:69-83` 的规则，禁止另起一套）：

- 相对路径；禁止绝对路径、反斜杠、控制字符、空组件、`.`、`..`、以 `-` 开头。
- 字符集 `^[A-Za-z0-9._/-]+$`（于是不能含 tab，才能进 TSV manifest）。
- runner 上源路径必须是普通文件：符号链接、目录、缺失一律拒绝（打包跟随后会逃逸出仓库）。
- 单文件 ≤ 1 MiB，合计 ≤ 4 MiB，最多 32 个。超出 fail-loud。nginx.conf / compose 远低于此；这是防 scp 把 runner 凭据目录打进发布包，不是业务配额。

**禁止声明（路径 denylist，normalize 与 host 双侧执行）**：

| 路径 | 原因 |
| --- | --- |
| `.env`、`ops.env`、`.d3-release.env`、任何 `*.env` | 凭据；永不下发 |
| `docker-compose.override.yml` / `.yaml`、`compose.override.yml` / `.yaml` | 主机专属；部署不触碰 |
| 任一分量为 `.deploy-state` 或 `.git` | 状态目录 / VCS |

输入必须走 step `env:`，禁止 `${{ inputs.release_files_json }}` 写进 `run:` 正文（`test_release_workflow_contract.py:250-273` 已锁）。

调用方声明的是**仓库内要随 SHA 冻结的文件**，不是主机最终合并结果。主机差异见第 4 节。

---

## 2. manifest 如何扩展

**不升 header。** 继续 `D3_RELEASE_MANIFEST=1`。升到 `=2` 会让升级后的脚本读不懂旧 `last_good_release`，跨版本回滚直接废掉。

新增可选记录（仍是 tab 分隔三列，host 无 jq/Python）：

```
file<TAB><relpath><TAB><sha256-hex>
```

- `relpath` 与输入逐字相同（posix 相对路径，无前导 `./`）。
- `sha256-hex` 为文件内容的 SHA-256 小写 64 hex（对空文件也写 `e3b0c442…`）。哈希对象是内容字节，不是 tar 成员元数据。
- 零条 `file` 记录 = 旧 manifest。新 `load_manifest` 把 `FILE_PATHS`/`FILE_HASHES` 置空，跳过下发与读回——**旧 last_good 行为不变**。
- 今日 `:200` 对未知 kind 拒绝。新记录只随**同一次** checkout 的新 `release_deploy.sh` 一起 scp（workflow `:123` `ref: ${{ inputs.ci_templates_ref }}` 取脚本，与 manifest 同源），不存在「新 manifest + 旧脚本」的合法组合。若有人把新 workflow 钉到旧 `ci_templates_ref`，旧脚本会 `unknown manifest record: file` 拒跑，fail-loud，正确。

内容本体不进 manifest。runner 在 normalize 时按声明列表打包 **ustar**：

- `tar --owner=0 --group=0 --numeric-owner --no-acls --no-selinux --no-xattrs`
- 成员名 = relpath，禁止绝对名 / `..`
- 产物 `$RUNNER_TEMP/d3-release.files.tar`，随 manifest 第三趟 scp 到 `/tmp/d3-release-${nonce}.files.tar`

零条 `file` 时不打 tar、不 scp，保持今日两趟 scp。

`last_good_release` 仍是「一行 SHA + 整份 manifest」。file 记录（哈希，不是内容）因此自动进入权威状态。内容 tar 按 SHA 缓存到 `$STATE_DIR/files/${SHA}.tar`（staging 时写入，不作为第二份 canonical；见第 3 节，避免双文件 promote 撕裂）。

---

## 3. 主机侧落地与原子性

与今日相同的临界区模型：锁外预拉镜像 + 解包校验；**host flock（及可选忙锁）内**才改 live 文件并 `compose up`。多文件 POSIX 不能单次 rename 一整棵树；本设计接受「同目录逐文件 `mv` + 全程握锁」，窗口与今日写 `.d3-release.env`（`:496-498`）同类。不引入新的目录 symlink 发布树——那会改 bind-mount 路径，爆炸半径大于本缺陷。

### 时序

```
锁外:  pull/retag 镜像
       解包 tar → $STATE_DIR/.release-${SHA}-$$/files/
       逐文件 sha256 对 manifest；任一不符 → 不碰 live，rc 前向失败
       把 tar 拷到 $STATE_DIR/files/${SHA}.tar（缓存，供本次失败后的对照与下次回滚）
锁内:  对每个目标:
         拒绝目标是符号链接（不跟随；打 [release][config-install] result=symlink-refused）
         目标必须落在 $DEPLOY_DIR 下（realpath 前缀检查）
         ssh_user 对目标目录可写，否则 result=not-writable，fail-loud，不 chmod/chown
         写到 同目录/.${basename}.${SHA}.tmp 再 mv -f 到 live（新 inode，不 cp）
       计算需要 --force-recreate 的 compose 服务（见下「inode」）
       compose up -d （rollback 路径仍按 oneshot 收窄）
       探针
       读回验证（第 5 节）
       通过才 promote last_good_release（格式不变）
```

远端清理：今日 EXIT trap 只删 script/manifest（`:49-59`）。实现必须把 `${nonce}.files.tar` 纳入同一套 `/tmp/d3-release-<nonce>.*` 白名单；workflow 的 scp 失败清理（`:277`）同步加这一路径。禁止 glob `/tmp`。

### bind-mount inode（不处理则 #71 原事故复现）

`mv` 换的是目录项；正在跑的容器 bind-mount 按 inode 打开，仍读旧字节。`compose up -d` 只在 compose 声明或镜像 tag 变化时重建容器。`nginx:alpine` **不在** manifest 镜像组里，nginx.conf 内容变化不会触发重建——主机文件已经是新哈希，读回通过，容器仍吃旧 conf。这正是 #71 要修的假绿。

因此：安装非 compose 文件后，对**当前正在跑、Mount.Source realpath 命中刚替换文件**的容器，取其 `com.docker.compose.service` 标签，把这些服务并入本次 `compose up -d --force-recreate`。检测失败（inspect 超时/无标签）fail-loud，禁止「检测不到就当无 bind」。postgres 镜像不被 bind 到所声明文件时不会进这个集合，避免无故重启数据库。

### 回滚

`deploy_group` 重放 `previous_sha` 时：

1. 若上一份 manifest **有** `file` 记录：必须存在 `$STATE_DIR/files/${previous_sha}.tar`，解包、哈希对照旧 manifest、按上面同一套 mv 装回，再 compose up 旧镜像组。缺 tar 或哈希不符 → 视为回滚失败（`rc=4`），不要只回镜像。
2. 若上一份 manifest **无** `file` 记录（升级窗口）：跳过文件还原，打 `[release][config-rollback] result=skipped-no-file-records sha=...`。镜像回滚逻辑不变。首次 opt-in 失败时没有「上一份配置」可回，与今日无 `last_good_release` 拒绝伪回滚（`:816`）同类。
3. 文件集增减不阻断回滚（与镜像组变更相反，`:744-748`）。回滚的目标就是回到上一份发布单元；多出来的新文件留下无害（旧 compose 不再引用），缺的文件由旧 tar 补回。

`.env` / `ops.env` / `.d3-release.env` / override / `.deploy-state` 不在安装集合里，回滚也不动它们。`.d3-release.env` 仍只写 `D3_RELEASE_TAG=`（今日 `:496-498`）。

### 权限模型（可能不可行的点）

D3 约定 `deploy_dir` 是 ssh_user 的项目根，但**没有**写成「live 配置文件属主 = ssh_user」的契约。若主机 `nginx/nginx.conf` 是 root:root 0444（推断，待证；本卡未上机），`mv` 失败。

- **本设计选择 fail-loud**，不 sudo、不 chmod。标记 `[release][config-install] result=not-writable path=...`。
- **否决的替代**：改 bind-mount 到 `$DEPLOY_DIR/.release-files/<sha>/` 再切 symlink。要先改仓库 compose 的 volumes，且 symlink 切 inode 对已打开的 bind 同样无效，还是得 force-recreate，复杂度更高。
- 接入前一条主机前置：`ssh_user` 对声明文件的父目录可写。不满足就不要把该路径写进 `release_files_json`。

---

## 4. 主机 override 机制与插值边界

Compose V2 在**未传 `-f`** 时自动合并项目目录下的 `docker-compose.yml` + `docker-compose.override.yml`。今日 `compose_release` `:499` 正是无 `-f` 的 `compose_args=(compose)`，主机若已有 override，已经在生效。

实现约束（2026-11-09 修正后原样保留）：

- **禁止**写成 `-f docker-compose.yml` 单独一项——那会丢掉 override，主机 `env_file` 差异立刻暴露为生产故障。
- 允许显式 `-f docker-compose.yml -f docker-compose.override.yml`，但仅当后者存在；不存在时不要把 override 写进 `-f` 列表（compose 会找不到文件）。
- 部署**读** override 是否存在，**写/删/chmod 一律禁止**。存在则打 `[release][config-override] path=docker-compose.override.yml result=detected-untouched`。
- 脚本启动时 `unset COMPOSE_FILE`（推断：今日未 unset；若主机环境漏进该变量，会静默改搜索路径）。
- denylist 保证仓库里的 `docker-compose.override.yml` 不能进发布集。该仓仓库内 override 是**本地 build:** 配方（远端文件：backend/worker/migrate/frontend 四段 `build.context`），一旦误发到主机，生产会改去现场 build，属 P1。

### 插值先于合并：override 的能力边界（2026-11-09 修正）

Compose 对每个文件**先逐文件插值、后合并**。仓库 compose 里由主机提供值的变量——`${VAR}` / `${VAR:?}`，含 `env_file` 的路径插值——在合并前的插值阶段就已求值：必需变量缺失在那一刻报错，`env_file` 指向的文件存在性检查同样早于合并；主机 override 只改写合并后的最终模型，救不了加载阶段。因此 override 机制之上有一条硬约束：这些变量必须在主机的插值来源（`release_deploy.sh` 注入的 `--env-file`：主机 `.env` / `.d3-release.env`，`scripts/release_deploy.sh:299-302`、`:391-394`）里有值，override **不能**替代；override 只适合覆盖插值后的结构字段（volumes、ports、environment 字面量等）。

证据（Compose v2.40.3 主机版 + v5.5.0 本地版双实测，[研究文档 §3](../sessions/261108-gate-triage/research/R-CT71D-recon.md)）：仓库 compose 的 `${ISEC_ENV_FILE:?}` 即使配有覆盖 `env_file` 的 override，compose 仍在插值阶段 `exit 1`（`required variable ISEC_ENV_FILE is missing a value`）；`env_file` 指向不存在路径时 `config --services` 能过、真正解析 env_file 的 `config` / `up` 拒绝；指向真实文件后仅 base、不带 override 也 `exit 0`。本文 2026-11-08 版曾把「主机 override 先行覆盖 `env_file`」当作下发安全前置，前提不成立，该迁移步骤已废止。

### 把 web_transcibe_translate 主机差异接入下发（只写步骤，不执行）

前提事实（[研究文档 §2、§3](../sessions/261108-gate-triage/research/R-CT71D-recon.md)，2026-11-09 只读取证）：

| | 仓库 `docker-compose.yml`（main） | 主机现状 |
| --- | --- | --- |
| `env_file` | `${ISEC_ENV_FILE:?这份配置由 isec 提供，...}`（backend/worker/frontend 三处） | `[.env]`（backend/worker 另有 `ops.env` required: false） |
| `worker.environment` | 多一行 `LOG_DIR=/app/data/logs` | 无（真实行为差异，另行显式确认） |
| nginx bind | `./nginx/nginx.conf` | 同路径；内容已于 2026-10-21（与 #48 修复同日）手工同步，与 main 逐字节一致（§2.3） |
| 主机 override | 仓库内 override 是本地 `build:` 配方，不拷主机 | 不存在（§2.2） |

仓库版相对主机**新增**的插值必需变量只有 `ISEC_ENV_FILE`（`D3_RELEASE_TAG` / `POSTGRES_PASSWORD` / `FRONTEND_API_KEY` 主机已在提供）。主机 `.env` 加一行 `ISEC_ENV_FILE=.env` 后，仓库 compose 的插值结果与主机现行 env_file 语义一致（研究文档 §3 末行实测：`env_file` 解析为 `[.env, ops.env]`）——**该仓在此方案下不需要 override**；override 机制保留给未来真正的主机结构差异。

迁移顺序（顺序不可倒，依据见研究文档 §4 执行单）：

1. 备份主机 `docker-compose.yml`、`nginx/nginx.conf`、`.env`（`cp -a` 加时间戳后缀；`.env` 只备份不读内容）。首份 `last_good_manifest` 无 `file` 记录时回滚只回镜像（§3 回滚第 2 条、研究文档 B5），配置回退靠这份手工备份。
2. 主机 `.env` 追加一行 `ISEC_ENV_FILE=.env`（plain `KEY=value`，无引号无 `$`；解除上述 P1 插值阻断——没有这一步，下发仓库 compose 后主机所有 compose 命令在插值阶段全部拒绝）。
3. 只读预检（零写盘）：把仓库 main compose 字节经 stdin 送主机 compose——`docker compose -f - config --services` 期望 exit 0、6 服务；再 `config` 全量核对 `env_file` 解析与 `D3_RELEASE_TAG` 插值仍在。
4. caller `deploy.yml` 仍钉 `@main`（第 7 节），加 `release_files_json: '["docker-compose.yml","nginx/nginx.conf"]'`。
5. 新 SHA 验证：推一个只动 nginx 注释的 SHA（`last_good_sha` == main HEAD 时 skip-forward 生效，重跑同 SHA 不会下发文件）。**判据**：`[release][config-install] path=nginx/nginx.conf result=replaced` + nginx 容器 `StartedAt` 晚于部署起点（force-recreate 真发生）——**不是**「哈希从旧变新」，主机 nginx.conf 已与 main 一致（研究文档 §2.3、§4.5-3）。
6. 改坏重跑：人为改坏主机 `nginx.conf` 一字节 → 重跑同一 SHA → skip-forward 必须被禁（第 5 节），日志出 `[release][config-verify] … result=mismatch` 并完整下发装回正确文件；然后恢复。

生产**不**演练探针失败回滚（2026-11-09 用户决定）：整组回滚把配置文件与镜像一起装回旧版本的路径由 `tests/test_release_config_files.py` 覆盖（`result=restored` / `skipped-no-file-records` / `missing-tar` 均有断言）。

将来主机真要写 override 时仍适用：override 给出的序列字段**替换**基文件的序列，不是追加。

---

## 5. 读回验证

哈希是验证，不是下发的替代。三个点都要比对 manifest：

| 点 | 何时 | 失败动作 |
| --- | --- | --- |
| staging | 解包后、改 live 前 | 不碰 live；前向失败（有 last_good 则走既有回滚） |
| post-install | mv 之后、compose up 之前 | 回滚文件+镜像 |
| post-probe | 探针通过后、promote 之前 | 回滚文件+镜像；**不 promote** |

读回对象是 live 路径上的普通文件字节（`sha256sum`，不跟随符号链接）。来源必须是 compose 实际 bind 的那个路径，不是 staging 副本。

可 grep 标记（前缀固定，实现不得改字面量）：

```
[release][config-verify] path=nginx/nginx.conf sha256=<64hex> result=ok
[release][config-verify] path=nginx/nginx.conf expected=<64hex> actual=<64hex> result=mismatch
[release][config-install] path=nginx/nginx.conf result=replaced|not-writable|symlink-refused
[release][config-override] path=docker-compose.override.yml result=detected-untouched|absent
[release][config-rollback] sha=<12hex> result=restored|skipped-no-file-records|missing-tar
```

`result=` 取值封闭。mismatch 必须同时打 `::error::`，避免只靠脚本日志。

**skip-forward**（今日 `:691-694`，同 SHA 已在 last_good）：先读回 live 哈希。全部匹配才允许 skip；任一 mismatch 则**禁止 skip**，落入完整 `deploy_group` 重新下发。否则「哈希当替代」会在重跑时把漂移当成成功。

零条 `file` 记录时不打 verify 行、不改变 skip-forward——旧 caller 零回归。

---

## 6. 与 `build-deploy.yml`（普通模板）的关系

两模板刻意分叉（`AGENTS.md`「改动纪律」：release 有多镜像顺序构建、原子发布、飞书卡片、约 170 行内联 SSH，不调 `pull_and_deploy.sh`）。给本设计打的补丁禁止搬到普通模板。

普通模板**同类缺口**（另议，不在本设计处理）：

- `.github/workflows/build-deploy.yml:379` 只 scp `$DEPLOY_SCRIPT`（`pull_and_deploy.sh`），不传 compose / 配置。
- `scripts/pull_and_deploy.sh:350-352` `cd "$DEPLOY_DIR" && docker compose up -d`，只消费主机已有文件。
- 舰队里抽样仓（R2：MediaResolverAPI / AI_Information_processor / youtube_download_api）走 `@v2` 普通模板。普通模板 canary 是 `url-parse-api`，与本缺陷无关。

另议时不要复用本设计的 manifest 记录类型——普通模板没有 `d3-release.manifest`。那是另一张卡。

---

## 7. 发布与爆炸半径

README「版本与爆炸半径」：稳定 caller 钉 `@v2` + `ci_templates_ref: v2`；**唯一 canary** 是 `url-parse-api` 钉 `@main`。`@v2` 会动：合入 main → canary 真实部署 → 再手动移动 `v2`。依据 `docs/RUNBOOK-go-live.md`，不要逐仓改 caller 来推广。

**release 模板的 canary：缺失。**

| 车道 | canary | 稳定使用者 |
| --- | --- | --- |
| 普通 `build-deploy.yml` | `zlxlabs/url-parse-api` `@main` | 其余钉 `@v2` |
| release `build-deploy-release.yml` | **无**钉 `@main` 的使用者 | 唯一使用者 `web_transcibe_translate` 钉 **`@v2`**（远端 `deploy.yml:30`） |

`examples/release-caller-workflow.yml:23` 仍写 `@v1`，与舰队事实不符；不在本卡改 examples 以外的路径，实现卡再对齐。RUNBOOK 阶段 4 曾要求该仓先钉 `@main` 再拨 `@v1`，实际停在 `@v2`，从未建立 release canary。

因此本特性必须 **opt-in 默认关**：`release_files_json` 默认 `[]`，合入 main 并移动 `v2` 时，未改 caller 的唯一使用者行为不变。

验证与回退：

1. 实现合入 ci-templates `main`。**先不移动 `v2`。**
2. `web_transcibe_translate` 临时把 `uses:` / `ci_templates_ref` 改为 `@main`，加上 `release_files_json`，主机按第 4 节补齐插值变量（`.env` 追加 `ISEC_ENV_FILE=.env`）。生产验证 = 真发一次 + 改坏重跑（第 4 节步骤 5–6）；**不**在生产演练探针失败回滚（2026-11-09 用户决定），整组回滚路径由 `tests/test_release_config_files.py` 覆盖。
3. 观察窗口过后再移动 `v2`（按 RUNBOOK：canary 绿 ≠ 已推广）。该仓拨回 `@v2`。
4. **回退**：未移 `v2` 时，该仓拨回 `@v2` 即离开新代码；ci-templates `git revert` 那组实现 commit。已移 `v2` 则按 RUNBOOK 把 `v2` 移回上一锚点，而不是逐仓改文件。
5. 空 `release_files_json` 的契约测试必须在实现卡锁死：旧 fixture manifest 无 `file` 行时 `release_deploy.sh` 不写 `$DEPLOY_DIR` 下任何 compose/配置。

---

## 8. 实现拆卡建议

每卡独立可合并；禁止把「只 scp 不消费」或「只消费不校验」的中间态推进 `v2`。

| 卡 | 边界 | 验收 |
| --- | --- | --- |
| **A. schema / pack** | 只改 `scripts/normalize_release.py` + `tests/test_release_input.py`。`--files-json` + `--repo-root`：校验路径/denylist/compose 必含、算 sha256、写 `file` 记录、打 tar。`--files-json '[]'` 与今日输出逐字节一致。 | 空数组不产生 tar；`..` / 绝对 / symlink / `.env` / override 文件名 / 缺 compose 各一条红转绿；旧 image/probe fixture 仍过。 |
| **B. 传输 + 主机安装/回滚/读回** | `build-deploy-release.yml` 新 input（默认 `[]`）、第三趟 scp、nonce 清理；`release_deploy.sh`：`load_manifest` 认 `file`、staging、mv、force-recreate、三处读回、回滚装旧 tar、第 5 节标记。`tests/test_release_deploy.py` + `tests/test_release_workflow_contract.py`。 | 默认空 input：契约测试断言仍只 scp 两文件。非空：mock 主机目录下 compose+conf 被替换且哈希标记 `result=ok`；故意改坏 live 后读回 `result=mismatch` 且调用回滚；旧 last_good 无 file 记录时回滚打 `skipped-no-file-records` 且不改 `.env`；symlink / not-writable fail-loud。input 不得进 `run:` 正文。 |
| **C. 文档与示例** | `README.md` release 节、`docs/RUNBOOK-go-live.md` 阶段 4、`examples/release-caller-workflow.yml`（补 `release_files_json` 注释；`uses:` 钉法与第 7 节对齐）。 | 示例 JSON 含 compose + 至少一配置；写明 override 不随发布；写明 release canary 缺失与 opt-in。 |
| **D. 消费者仓（他仓）** | `zlxlabs/web_transcibe_translate`：主机 override、caller 声明文件集、临时钉 `@main`。本仓不改。 | 第 4 节步骤 3–5 的真机证据（新 nginx.conf 生效、回滚带回旧 conf）。 |

A 可单独合 main（无 workflow 入口则生产不可达）。B 依赖 A。C 可与 B 同 PR 或紧随。D 在 B 合入 **main** 且该仓钉 `@main` 之后，**先于**移动 `v2`。

契约：secrets 仍是那 6 个（+ 可选飞书），不新增 secret；`runs-on` / tag 不可变 / 探针语义不变。改 input 集合必须同步合同测试，不许绕过（`AGENTS.md` 契约纪律）。

---

## 可行性风险与否决项

| 项 | 结论 |
| --- | --- |
| 多文件非原子 | 接受。握锁 + 同目录 `mv`，与现有 `.d3-release.env` 同级。不改目录布局。 |
| live 文件不可写 | fail-loud，不提权。接入前主机前置。 |
| bind-mount inode | 必须按 Mount.Source 对 bind 了所声明文件的服务 `--force-recreate`。只读回主机哈希会再出 #71 假绿。 |
| 双 canonical（release 文件 + files tar） | 否决。tar 按 SHA 缓存，权威仍是 `last_good_release`。 |
| 只回读、不下发 | 否决（用户 2026-11-08）。 |
| 默认真下发 compose | 否决。release canary 缺失，默认开会在移 `v2` 时打到唯一生产使用者。 |
| 把补丁搬到 `build-deploy.yml` | 否决。第 6 节另议。 |
| 下发 `.env` | 否决。 |
| 下发仓库里的 `docker-compose.override.yml` | 否决。与主机 override 同名不同义。 |

本方向在「ssh_user 可写声明文件」这一前置下可行。前置不成立时不要迁就成 chmod/sudo，停在 fail-loud，改主机属主或把该文件移出发布集。
