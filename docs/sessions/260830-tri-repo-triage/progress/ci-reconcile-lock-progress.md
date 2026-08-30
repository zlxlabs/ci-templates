# ci-templates #30+#29：release lane 对账入锁 + readarray 合修

## 2026-08-30 · #29 readarray 修复

- **当前阶段**：implementing · 第一个可独立过测单元
- **本段结论**：`validate_oneshot_services` 与 `rollback_compose_services` 改为命令替换捕获 + `if !` 判 rc + `readarray <<<`，对齐 workflow `:426-431`。compose config 失败时两个函数返回非零且 stderr 含 `compose config --services failed`，不再误报 unknown service / all-oneshot。
- **关键决策与已否决方案**：无（锁定决策 3 原样落地）
- **下一步唯一动作**：把对账体下沉进 `release_deploy.sh`，在 `do_release` 返回之后、`flock -u 9` 之前执行。

## 2026-08-30 · 对账下沉进持锁进程

- **当前阶段**：implementing · 脚本侧对账入锁
- **本段结论**：`reconcile_release_images` 在 `do_release` 返回 0 之后、`flock -u 9` 之前执行；失败映射为 rc=5（与 rc=1 回滚健康 / rc=4 不健康 / rc=3 忙锁让位可区分）。busy_deferred 与部署失败路径不跑对账。锁探测用例确认 `compose ps` 时 host 锁仍被持有。
- **关键决策与已否决方案**：选「do_release 之后的独立函数」而非塞进 do_release 收尾——do_release 的返回值语义（含回滚 rc）保持不动，对账失败不会被并进部署失败。否决「对账 step 自行重新拿锁」（卡面已否决）。全-oneshot 第一次部署现为 rc=5（promote 已完成、对账拒绝），与旧独立对账 step 失败同结果，只是退出码从「部署 0 + 对账 step 1」收敛到脚本 rc=5。
- **下一步唯一动作**：改造 `build-deploy-release.yml`：部署 step 识别 rc=5 写 `reconcile_failed=true`，对账 step 瘦成转发失败通知的薄壳。

