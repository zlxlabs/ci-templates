# R-CT71D：web_transcibe_translate 生产主机只读取证与接入执行单

- 卡：ci-templates#71 卡 D 接入前取证（dispatch dlg-20261109-052354-17104d）
- 日期：2026-11-09；全部命令只读，未写主机任何文件、未读任何 `.env` 内容
- 基线：ci-templates main `7ad449717`（`release_files_json` 已具备，`build-deploy-release.yml:17`、`:149`）
- 业务仓基线：`zlxlabs/web_transcibe_translate` main `afa87d37e5eb`（gh api 实时取，非本地 checkout）

## 0. 结论摘要

1. **权限前置全部满足**：`ssh_user=lixing` 对 `$DEPLOY_DIR`、`nginx/`、`docker-compose.yml`、`nginx/nginx.conf` 全部属主可写，无符号链接。
2. **发现一个设计未预见的 P1 阻断**：仓库 compose 的 `env_file: ${ISEC_ENV_FILE:?…}` 在 compose **合并前**的逐文件插值阶段就报错；**主机 override 覆盖 `env_file` 救不了**（生产版本 v2.40.3 与本地 v5.5.0 双实测）。接入前必须让 `ISEC_ENV_FILE` 在插值时解析为已存在的文件（主机 `.env` 加一行 `ISEC_ENV_FILE=.env`），否则下发仓库 compose 后所有 compose 命令全部拒绝（fail-loud，生产停摆于下一次部署）。
3. **主机差异不止 env_file**（卡面要求全部列出）：worker 少 `LOG_DIR=/app/data/logs` 环境行（行为差异，需主脑确认）；frontend 注释与 `:?` 报错文案差异（无运行时影响）。
4. **#71 的「主机 nginx.conf 停在旧版」已不成立**：主机 `nginx.conf` sha256 与仓库 main 逐字节一致（同日手工同步痕迹），这改变了验证步骤的预期判据（见 §4.5）。
5. **主机无 override**，postgres 只用命名卷不 bind 声明文件；唯一 bind 声明文件的是 nginx → force-recreate 集合只含 nginx。
6. 运行镜像 `afa87d37e5eb` = main HEAD = `last_good_sha`，skip-forward 生效中；`last_good_manifest` 无 `file` 记录 → 首次 opt-in 失败时回滚只回镜像不回配置，**部署前手工备份主机 compose/nginx.conf 是必须步骤**。

## 1. 主机与凭据定位（含出处）

| 项 | 值 | 出处 |
| --- | --- | --- |
| 生产主机 | vars `D3_DEPLOY_HOST` 所指 tailscale 地址（hostname 实测 `fordealubt`；IP 字面值不落盘，经上列 vars API 可查） | 该仓远端 `.github/workflows/deploy.yml`：`host: ${{ vars.D3_DEPLOY_HOST }}`；vars 经 `gh api repos/zlxlabs/web_transcibe_translate/actions/variables/D3_DEPLOY_HOST` 读得（vars 不打码） |
| ssh_user | `lixing`（uid 1001，组：lixing, sudo, docker） | 同上 `D3_DEPLOY_USER`；主机 `id` 实测一致 |
| DEPLOY_DIR | vars `D3_DEPLOY_DIR` 所指路径（字面值不落盘，同经 vars API 与业务仓 `docker/deploy_targets.json` 的 `fordeal.dir` 可查，两处一致已交叉验证） | 同上 `D3_DEPLOY_DIR` |
| ssh 通道 | 本机 `~/.ssh/config` 别名 `fordeal`（同机 LAN 侧地址与端口见该别名定义，ControlMaster 常驻链） | 与 `deploy_targets.json` 的 target key `fordeal` 一致 |

未读取、未回显任何 secret 值；`.env`（3322 B）、`ops.env`（124 B）、`.d3-release.env`（28 B）只报存在与字节数。

## 2. 主机只读取证（命令与结构化结果）

以下命令均经 `ssh -o BatchMode=yes fordeal` 执行；每条标注出处命令。

### 2.1 属主/权限/符号链接

```
id
→ uid=1001(lixing) gid=1001(lixing) groups=1001(lixing),27(sudo),999(docker)

stat -c "%A %U:%G %s %Y %n" $DEPLOY_DIR $DEPLOY_DIR/nginx
→ drwxrwxr-x lixing:lixing …  docker-compose.yml 所在目录
→ drwxr-xr-x lixing:lixing …  nginx/

stat -c "%A %U:%G %s %Y %n" docker-compose.yml nginx/nginx.conf
→ -rw-r--r-- lixing:lixing 5343 … docker-compose.yml
→ -rw-r--r-- lixing:lixing 3030 … nginx/nginx.conf

ls -la（目录全量）
→ 两文件均为普通文件（- 开头），非符号链接
```

判定：设计第 3 节「ssh_user 对声明文件父目录可写」前置 **满足**（目录与文件均 lixing 属主；`mv` 只需父目录写权限）。

### 2.2 override 现状

```
for f in docker-compose.override.yml …; do [ -e "$f" ] && …
→ docker-compose.override.yml: absent
```

主机当前**无 override**，Compose 实际生效的就是主机 `docker-compose.yml` 单文件（`release_deploy.sh:12` 已 `unset COMPOSE_FILE`，设计第 4 节的 unset 推断在现 main 已实现）。

### 2.3 哈希对照（跨发布边界验收：主机侧 = 主机 `sha256sum` 实际输出；仓库侧 = `gh api contents` 取 main 字节本地计算）

| 文件 | 主机 sha256 | 仓库 main sha256 | 一致性 |
| --- | --- | --- | --- |
| `docker-compose.yml` | `3b3c7ff8ba6d8c3f7b926a8515538c5d4c4332363678a66a8ea83f8d44658c9e` | `d50a33998c061cee9f8324704c40002761dfad4847e6bec28b0617a6118581a7` | **不一致** |
| `nginx/nginx.conf` | `270e86d1a730aa8f5b2916e0320f0ee9215e3d38f8e5136bf23c215b2ffff651` | `270e86d1a730aa8f5b2916e0320f0ee9215e3d38f8e5136bf23c215b2ffff651` | **一致** |

nginx.conf 变更史（gh api，path 过滤）：main 上最后一次改动 = `61ebb8ac`（2026-10-21T12:53:59Z，#48/#56 trust-boundary 修复）。主机文件 mtime 2026-10-21 17:01 (+0800)，与修复同日、内容逐字节等于 main —— **#71 事故时「主机停在旧版」的事实已被后续同日手工同步覆盖**。

### 2.4 主机 compose 与仓库 main 的结构差异（diff 摘要，只列结构性项）

```
diff host-compose.yml wt-compose.yml（host=- / main=+）
1) backend.env_file:      主机 [.env, ops.env(required:false)] / main [${ISEC_ENV_FILE:?…}, ops.env(required:false)]
2) worker.env_file:       同上
3) worker.environment:    main 多一行 LOG_DIR=/app/data/logs（主机无）   ← 超出 env_file 的行为差异
4) frontend.env_file:     主机 [.env] / main [${ISEC_ENV_FILE:?…}]
5) frontend 注释与 INTERNAL_API_KEY 的 :? 报错文案不同（无运行时影响）
volumes / ports / image 行 / 服务集合 / depends_on：完全一致（diff 无其余输出）
```

主机侧 `env_file` / 变量引用方式：主机 compose 用 `env_file: [.env]` + `path: ops.env, required: false`；仓库 main 用 `${ISEC_ENV_FILE:?这份配置由 isec 提供，请用 isec docker compose ... 启动}`。插值必需变量（`D3_RELEASE_TAG`、`POSTGRES_PASSWORD`、`FRONTEND_API_KEY`）两版相同，主机 `.env`/`.d3-release.env` 均在提供（现网容器在跑即证明）；仓库版**新增**的插值必需变量只有 `ISEC_ENV_FILE`。

### 2.5 正在运行的容器与 bind 关系

```
docker compose --env-file .d3-release.env --env-file .env config --services
→ postgres migrate backend frontend nginx worker（6 服务，现网解析通过）

docker ps（项目容器）
→ 251211_web_trans_scribe-{nginx,frontend,backend,worker}-1 Up，镜像 transcribe-{backend,frontend}:afa87d37e5eb
→ 251211_web_trans_scribe-postgres-1 postgres:16-alpine（healthy）
→ migrate 不在 ps（oneshot）

docker inspect --format …（逐服务）
→ nginx:    bind <DEPLOY_DIR>/nginx/nginx.conf → /etc/nginx/nginx.conf (ro)   ← 唯一 bind 声明文件的容器
→ backend:  bind ./data → /app/data (rw) + 命名卷 staged-inputs（data 是目录，不在声明文件集）
→ worker:   同 backend
→ postgres: 仅命名卷 postgres-data，不 bind 任何将声明的文件 ✓
→ frontend: 无 mounts
→ 全部 StartedAt=2026-11-07T04:18Z（同波次；nginx.conf mtime 10-21 早于容器启动，无「先改文件后建容器」漂移）
```

判定：force-recreate 集合 = 仅 `nginx`；postgres（有状态）不受任何声明文件影响。

### 2.6 `.env` 与状态

- 主机 `.env` 存在，3322 B，0600 lixing；`ops.env` 124 B；`.d3-release.env` 28 B。内容未读。
- `.deploy-state/release/`：`last_good_sha=afa87d37e5eb`、`last_good_manifest` 为**旧格式**（仅 image/probe 记录，无 `file` 行）。

## 3. 关键机制实验：override 能否吸收 `ISEC_ENV_FILE:?`（P1 阻断的依据）

设计第 4 节假设「先写 override 覆盖三服务 env_file」即可安全下发。实测**不成立**：

| 实验 | compose 版本 | 配置 | 结果 |
| --- | --- | --- | --- |
| 生产版本复现 | **v2.40.3（主机）** | 仓库 main compose 原字节经 stdin `docker compose -f - config --services`，无 override、无 ISEC | **exit 1**：`error while interpolating services.frontend.env_file.[]: required variable ISEC_ENV_FILE is missing a value` |
| override 合并 | v5.5.0（本地） | base 含 `${ISEC_ENV_FILE:?}` + override 覆盖 `env_file`，ISEC 未设 | **仍 exit 1**，同样插值错误 |
| full config + dummy 路径 | v5.5.0 | `ISEC_ENV_FILE=/nonexistent` + override | full `config` 报 `env file /nonexistent not found`（`config --services` 能过，但 `config`/`up` 这类真正解析 env_file 的命令不能过） |
| 真实文件 + override | v5.5.0 | `ISEC_ENV_FILE=.env` + override，full `config` | **exit 0** ✓ |
| 真实文件、无 override | v5.5.0 | `ISEC_ENV_FILE=.env`，仅 base，full `config` | **exit 0** ✓（env_file 解析为 `[.env, ops.env]` = 现主机语义） |

机理：compose 对每个文件**先插值、后合并**。base 里 `${ISEC_ENV_FILE:?}` 的插值错误与 env_file 文件存在性检查都发生在合并之前，override 替换只改变合并后的最终模型，救不了加载阶段。主机现网自己的报错格式（`error while interpolating services.worker.environment.[]: required variable POSTGRES_PASSWORD …`）证明生产 v2.40.3 同为逐文件插值引擎。

推论：**无论走 override 路线还是不走，接入前都必须让 `ISEC_ENV_FILE` 在插值时解析为一个已存在的文件**；否则下发仓库 compose 的那一刻起，主机所有 compose 命令（含 `release_deploy.sh` 的 `config --services`/`config --images`/`up`）全部拒绝。

## 4. 执行单（拟定，只写不落地；落地需用户对主机变更逐项授权）

### 4.1 前置动作（主机，需用户授权；顺序不可倒）

1. 备份（`cp -a` 加时间戳后缀，同目录，lixing 属主）：
   `docker-compose.yml`、`nginx/nginx.conf`、`.env`（`.env` 只备份不读内容）。
2. **`.env` 追加一行 `ISEC_ENV_FILE=.env`**（P1 阻断的解除；plain `KEY=value`，无引号无 `$`，跨消费方安全）。副作用说明：`.env` 同时是 backend/worker/frontend 的 env_file 内容源，该行会以环境变量形式进入容器（惰性，应用不读此键）。
3. 写主机 override `$DEPLOY_DIR/docker-compose.override.yml`（见 4.2）。设计第 4 节路线；在 4.1-2 就位后，override 变为「显式声明主机差异」的防御层而非必需品——若 repo compose 未来再改 env_file 形状，主机语义不被带跑。
4. 只读预检（模拟下发后解析，零写盘）：把仓库 main compose 字节经 stdin 送主机 compose：
   `ssh fordeal 'cd <DEPLOY_DIR> && docker compose -f - config --services' < repo-main-compose.yml`
   → 期望 exit 0、列 6 服务；再 `… config backend frontend | sed -n '/env_file/,+4p'` 人工核对 env_file 解析结果。
   注意 stdin 测试不含 override 合并；override 合并核对用 4.3。

### 4.2 主机 override 拟定内容（复刻主机现行 env_file 语义；不带 `build:`）

```yaml
# 主机持有；部署只读不写（[release][config-override] result=detected-untouched）。
# 本文件把「主机用 .env，仓库用 ISEC_ENV_FILE」这一主机差异显式化。
services:
  backend:
    env_file:
      - .env
      - path: ops.env
        required: false
  worker:
    env_file:
      - .env
      - path: ops.env
        required: false
  frontend:
    env_file:
      - .env
```

（Compose 合并：override 的 `env_file` 序列**替换** base 序列；不重复 `ISEC_ENV_FILE`。）

### 4.3 `docker compose config` 核对点（主机、写完 override 后）

```
cd "$DEPLOY_DIR"   # 值见 §1 定位表
docker compose --env-file .d3-release.env --env-file .env config --services
  → 恰好 postgres migrate backend frontend nginx worker，exit 0
docker compose --env-file .d3-release.env --env-file .env config backend   （worker、frontend 各一遍）
  → env_file 解析为 .env（+ ops.env required:false，backend/worker）
  → environment 仍含 D3_RELEASE_TAG 插值（nginx 服务）、POSTGRES_DSN 等
docker compose --env-file .d3-release.env --env-file .env config --images
  → transcribe-backend / transcribe-frontend 带 ${D3_RELEASE_TAG}（身份门禁输入不变形）
```

### 4.4 caller `deploy.yml` 拟定 diff（业务仓 PR；`deploy.yml` 不在 paths 白名单，合并后需 `workflow_dispatch` 补发）

```diff
   release:
-    uses: zlxlabs/ci-templates/.github/workflows/build-deploy-release.yml@v2
+    uses: zlxlabs/ci-templates/.github/workflows/build-deploy-release.yml@main   # 验证窗口后按设计§7 拨回 @v2
     with:
       images_json: …（不变）
       probes_json: …（不变）
       host: … / ssh_user: … / deploy_dir: …（不变）
+      release_files_json: '["docker-compose.yml", "nginx/nginx.conf"]'
       oneshot_services: migrate
-      ci_templates_ref: v2
+      ci_templates_ref: main
```

两文件均满足 normalize 约束：普通文件、相对路径、字符集合法、5343+3030 B ≪ 1 MiB/4 MiB 限额、含恰好一个 compose 文件、不触 denylist。仓库自身的 `docker-compose.override.yml`（纯 `build:` 配方）**不进发布集**（denylist 双侧拦截），证实不会把现场 build 带到主机。

### 4.5 验证步骤（设计§4 步骤 4，按 §2.3 修正预期）

1. 业务仓推一个**只动 nginx 注释**的 SHA（`nginx/**` 在 deploy paths 白名单，自动触发）。注意当前 `last_good_sha == main HEAD` 且 skip-forward 生效，**必须用新 SHA**，重跑同 SHA 不会下发文件。
2. 看 Actions 日志标记（grep 用）：`[release][config-install] path=nginx/nginx.conf result=replaced`、`[release][config-override] path=docker-compose.override.yml result=detected-untouched`、`[release][config-verify] … result=ok`。
3. 主机核实 force-recreate 真发生：`docker inspect 251211_web_trans_scribe-nginx-1 --format '{{.State.StartedAt}}'` 晚于部署起点；`sha256sum nginx/nginx.conf` == manifest 期望值。**预期判据修正**：由于主机 nginx.conf 现已等于 main，首次下发前后哈希不变——判据是 `result=replaced` + 容器重建时间戳，不是「哈希从旧变新」。
4. 负向测试（skip-forward 不得放行漂移）：人为改坏主机 `nginx.conf` 一字节 → `workflow_dispatch` 重跑同一 SHA → 日志必须出现 `[release][config-verify] … result=mismatch`（含 `::error::`）并落完整下发而非 skip；然后恢复。
5. 回滚演练：正常发布一个 SHA 后，人为让探针失败整组回滚（设计§7 步骤 2）。已知边界：首份 `last_good_manifest` 无 `file` 记录，日志应打 `[release][config-rollback] result=skipped-no-file-records`，只回镜像——配置回退靠 4.1-1 的手工备份，此为升级窗口预期行为，不算故障。

### 4.6 回退步骤

1. caller 层：业务仓 revert 4.4 的 PR（`uses:`/`ci_templates_ref` 回 `@v2`、去掉 `release_files_json`）→ 完全离开新代码路径。
2. 主机层（若需恢复文件）：用 4.1-1 备份 `mv` 回原文件（同目录同属主），`docker compose --env-file .d3-release.env --env-file .env up -d`。
3. `.env` 的 `ISEC_ENV_FILE=.env` 行与 override 可留在主机：旧 compose 不引用 `ISEC_ENV_FILE`，override 在旧 compose 下合并后语义不变（env_file 序列相同），二者对 `@v2` 老路径无行为影响。
4. ci-templates 层：main 的实现是 opt-in（默认 `[]`），无需 revert；`v2` 未移动。

### 4.7 阻断项清单（全部列出，不在 override 里顺手吸收，由主脑决定）

| # | 级别 | 项 | 处置 |
| --- | --- | --- | --- |
| B1 | **P1 阻断** | 仓库 compose `${ISEC_ENV_FILE:?}` 插值在合并前失败，override 无法吸收（§3 双版本实测） | 接入前主机 `.env` 加 `ISEC_ENV_FILE=.env`（§4.1-2，用户授权）。替代方案 C：业务仓 compose 改 `env_file: [{path: ${ISEC_ENV_FILE:-.env}, required: true}, …]`——免去 `.env` 改动，但失去 isec 未运行时的响亮报错语义，属业务仓语义决定，不在本卡 |
| B2 | P2 差异 | worker 缺 `LOG_DIR=/app/data/logs`：下发后 worker 新增该环境变量，日志写入已 bind 的 `./data/logs`，与 backend 对齐；低风险但为真实行为变化 | 主脑显式确认接受，或先在主机 override 给 worker 补 `environment` 差异（不推荐：把仓库修复永久挡在主机外） |
| B3 | P3 差异 | frontend 注释与 `INTERNAL_API_KEY` 的 `:?` 报错文案不同 | 无运行时影响（仅变量缺失时的报错文本），随下发自然对齐，无需动作 |
| B4 | 信息 | 主机 nginx.conf 已与 main 一致，#71「停在旧版」不再成立 | 验证判据按 §4.5-3 修正 |
| B5 | 信息 | `last_good_manifest` 无 `file` 记录：首次 opt-in 的整组回滚不回配置 | §4.1-1 手工备份为必须步骤；§4.5-5 已纳入演练预期 |

非阻断确认：属主/权限/符号链接全部满足（§2.1）；主机无既有 override 需要尊重（§2.2）；postgres 不 bind 声明文件（§2.5）；发布集两文件通过 normalize 约束（§4.4）；`.env` 系文件未读内容（本卡红线）。
