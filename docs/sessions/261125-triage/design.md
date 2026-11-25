# DESIGN-note：部署版本单调接纳（#104）与配置定向重建（#105）

用户 2026-11-25 在分诊会话认可分批方案，并裁决：开启 deferred_deploy 的仓，手工重跑旧 run 会被拒，回滚改用新提交或新触发的 run。

## 目标

延期的旧版本不会再盖掉已上线的新版本；随发布下发的配置文件改动后，挂载它的容器一定会被重建，否则部署判红。

## 非目标

- 不改 release lane 的 run 排序（另行处理）；不做 #106 代码（第 2 批）；不移 v2、不动生产主机。
- 不改未开启 deferred_deploy 的部署路径。

## 为什么不是分区 / 删除 / 约定

- **分区**：同一 DEPLOY_DIR 只能跑一个版本，在线部署与重放必然竞争同一目标。
- **删除**：#105 已删除整机容器扫描；#104 的延期重放是已上线能力（#95），不能删。
- **约定**：靠人工不重跑、不插队无法约束 2 分钟一次的 timer。

## 方案要点与已否决方案

### #104
- 现有 `~/.d3-converge/intents/<stem>.json` 升级为该目录「最新已接纳请求」的唯一记录；在线部署即使没有 pending 也写入。只有延期请求带可重放载荷。
- 在线路径：拿齐忙锁与主机锁后、首次线上变更前，短持 intents.lock 比较 `(run_number, run_attempt)`；记录中的更大则以专用 rc 拒绝，不做任何变更；否则原子写入自己。
- 延期写入走同一比较，所以新版本上线后才到的旧请求会被拒。
- 重放：子脚本拿齐部署锁后，核对记录仍是自己这条且可执行；不是则以同一专用 rc 退出，converge 记 superseded 事件，且不改动已属于别人的记录。
- 已接纳的新版本部署失败并回滚后，旧意图不复活；记录不倒退。接纳之前（构建、预拉）失败则旧意图仍可执行。
- 同一身份重送幂等。rc=3（忙锁）、superseded、部署失败三者的 rc 与事件可区分；workflow 对 superseded 不报成功。
- 锁序：忙锁 → 主机锁 → intents.lock；部署进行中不持 intents.lock；converge 等部署锁前先释放 intents.lock。
- 接纳逻辑放 `scripts/deferred_intent.sh`；`pull_and_deploy.sh` 只接线且不超过 800 行。重放工件必须自带校验：固化时一并安装相容的 deferred_intent.sh，并在重放环境中启用。
- 缺少锁内校验的旧固化脚本：converge 不执行，置为终态并告警（P1），人工用新 run 重建。
- **已否决**：另设 deployed_run_number 水位（第二份状态；只记成功会让新版本失败后旧版本复活）；converge.sh 部署前复核（等锁期间可被插队）；只作废眼前的旧意图（拦不住之后写入的旧请求）。

### #105
- 只枚举当前 compose 项目的常驻容器，只检查 `Type=bind`，只重建命中的服务；字段边界无歧义；相关 bind 源无法确认时报错。
- 同 SHA 快捷路径：声明了受管 bind 配置时，重跑仍走完整应用，不只凭磁盘哈希跳过。
- 回滚装回旧 tar 后按同样规则重建。
- **已否决**：只改换行（会激活对整机挂载的 realpath，任一不可达即失败）；整项目 force-recreate（连带重启有状态服务）；compose config JSON（主机无 JSON 解析器）。

## 关键不变式

1. [待实现] 已快照并在等锁的旧重放，在新版本接纳后不触发任何 compose 变更；`scripts/deferred_intent.sh`，由 `tests/test_deferred_admission.py` 用真实 flock 锁死。
2. [待实现] 记录的 `(run_number, run_attempt)` 在同一 workflow 序列内不倒退，含新版本失败回滚后；同上。
3. [待实现] 未开启 deferred_deploy 时不读写 `~/.d3-converge`；`tests/test_deferred_intent.py`。
4. [待实现] 项目外容器、命名卷不影响重建判定；`tests/test_release_config_files.py`。
5. [待实现] 探测模板经真实 Docker 模板引擎渲染后能被解析出全部 bind 源；同上。

## 共享资源清单

| 资源 | 写入方 | 读取方 | 守卫 | 文件:行 |
| --- | --- | --- | --- | --- |
| intents/<stem>.json | 延期写入；本批新增在线接纳 | converge | intents.lock | scripts/deferred_intent.sh:108 |
| intents/<stem>.json | converge 状态回写 | converge、人工 | intents.lock | scripts/converge/converge.sh:341 |
| 忙锁与主机锁 | pull_and_deploy | pull_and_deploy | flock 8 → 9 | scripts/pull_and_deploy.sh:735 |
| last_good_tag | do_deploy | do_deploy、回滚 | 主机锁 | scripts/pull_and_deploy.sh:650 |
| 配置文件与 last_good_release | release_deploy | release_files_skip_forward | 主机锁 | scripts/release_deploy.sh:623 |

## 待验证前提

1. [推断] 生产主机上部署用户无法遍历部分命名卷路径；第 2 批真机只读核对。
2. [推断] environment 传空串等价于不传；第 1 批 GitHub Actions 实验。

## 验收路径

1. 入口：真实 pull_and_deploy.sh 与 converge.sh，真实 flock，假 docker。
2. 步骤：A 延期 → converge 取 A 快照并等锁 → B 先上线 → 释放锁。
3. 预期：A 无 compose 调用；last_good_tag 与镜像为 B；事件有 superseded。
