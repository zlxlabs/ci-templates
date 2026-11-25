# d3-converge：主机侧部署意图收敛器

忙锁 deferred 后，写侧（C1）把最新意图落到 `${HOME}/.d3-converge/intents/`。
本目录的 `converge.sh` **零部署逻辑**：按意图快照里的 `deploy_env` 白名单键，
重放 `${HOME}/.d3-converge/scripts/<script_ref>`（由 C3 随部署 run scp 安装）。

## 首次安装（需要 root 一次）

把 `__DEPLOY_USER__` 换成实际 `ssh_user`（部署用户，家目录可写 `~/.d3-converge`）。

```bash
# 在 ci-templates 仓根，以 root 执行一次
ssh_user=实际ssh用户
home=$(getent passwd "$ssh_user" | cut -d: -f6)
install -m 644 scripts/converge/systemd/d3-converge.service /etc/systemd/system/d3-converge.service
install -m 644 scripts/converge/systemd/d3-converge.timer   /etc/systemd/system/d3-converge.timer
sed -i -e "s/__DEPLOY_USER__/${ssh_user}/g" \
       -e "s#__CONVERGE_SH__#${home}/.d3-converge/bin/converge.sh#" \
       /etc/systemd/system/d3-converge.service
install -o "$ssh_user" -g "$ssh_user" -m 755 -D \
  scripts/converge/converge.sh "${home}/.d3-converge/bin/converge.sh"
systemctl daemon-reload
systemctl enable --now d3-converge.timer
```

确认 timer 已挂上：

```bash
systemctl list-timers d3-converge.timer
systemctl cat d3-converge.service
```

`User=` 与 `ExecStart=` 必须指向同一部署用户；unit 本身是 system 级，所以 enable 需要 root。

## 工件更新（无需 root）

C3 的部署 run 会 scp：

- `converge.sh` → `~/.d3-converge/bin/converge.sh`（与 unit `ExecStart` 同一路径，覆盖后下一轮 timer 即用新字节）
- 本次实际执行的部署脚本 → `~/.d3-converge/scripts/<script_ref>`
- 与脚本配对的 `deferred_intent.sh` → 同目录的 `deferred_intent.<stem>.r<run_number>a<run_attempt>.sh`；收敛器先核实伴侣存在，重放脚本在忙锁与主机锁内再次核对 latest admission

覆盖后下一轮 timer（`OnUnitActiveSec=2min`）即用新字节，不必 `daemon-reload`，也不必 root。

## 回读

以部署用户：

```bash
ls -l ~/.d3-converge/intents/
cat ~/.d3-converge/intents/*.json
cat ~/.d3-converge/events.jsonl
journalctl -u d3-converge.service -n 50 --no-pager
```

意图 JSON 含 webhook，权限是 `0600`。不要把文件内容贴进工单或群。

## 人工放弃

自动永不放弃。人要停止某个 pending 意图的自动重放时，持 `intents.lock` 把 `status` 置 `abandoned`：

```bash
lock=~/.d3-converge/intents.lock
file=~/.d3-converge/intents/<stem>.json   # stem 即文件名，ls intents/ 可见

# 下一轮收敛器跳过；记录保留为 run_number/run_attempt 单调水位
flock -w 30 -x "$lock" sed -i 's/"status":"[^"]*"/"status":"abandoned"/' "$file"
```

`abandoned` 不会放低身份水位；不得靠删除记录让低编号 run 接管。删除前必须证明旧 workflow run 已不可再触发，否则会重新放行旧请求。新版本 B 接纳后，手工重跑旧 run A 会被锁内拒绝（rc=6），且不执行部署脚本。

## interrupted 重置

收敛器启动时若发现 `status=running`（上一轮没写终态），会改成 `interrupted`、发 P0 卡、**不自动重试**。
上机核对生产（镜像 / 容器 / `last_good_tag`）之后：

```bash
lock=~/.d3-converge/intents.lock
file=~/.d3-converge/intents/<stem>.json

# 生产已是目标版本：置 deployed，避免再跑
flock -w 30 -x "$lock" sed -i 's/"status":"interrupted"/"status":"deployed"/' "$file"

# 生产仍是旧版本、需要再试：置 pending，下一轮 timer 会重放脚本
flock -w 30 -x "$lock" sed -i 's/"status":"interrupted"/"status":"pending"/' "$file"
```

不要在未核对生产时直接改回 pending。
