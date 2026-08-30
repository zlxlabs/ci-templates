# ci-templates #30+#29：release 对账入锁 + r1
- #29：`compose_list_services` 退出码经命令替换上抛，`readarray <<<` 不再吞 rc。
- 对账在 `do_release` 返回 0 后、fd9 解锁前；失败 rc=5。workflow 薄壳无第二条 SSH。
- r1 P1：`last_good_release` 已是本 SHA 则 skip forward（含 oneshot），只对账。promote 前 255 仍整次重放。
- r1 P2：`reconcile_docker` 单次 60s timeout，超时 fail-loud → rc=5 放锁。
- r1 P3 backlog：契约 `.index` 首次命中/精确计数锚点脆弱，不修。
