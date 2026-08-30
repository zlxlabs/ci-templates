# ci-templates #30+#29：release lane 对账入锁 + readarray 合修

## 2026-08-30 · #29 readarray 修复

- **当前阶段**：implementing · 第一个可独立过测单元
- **本段结论**：`validate_oneshot_services` 与 `rollback_compose_services` 改为命令替换捕获 + `if !` 判 rc + `readarray <<<`，对齐 workflow `:426-431`。compose config 失败时两个函数返回非零且 stderr 含 `compose config --services failed`，不再误报 unknown service / all-oneshot。
- **关键决策与已否决方案**：无（锁定决策 3 原样落地）
- **下一步唯一动作**：把对账体下沉进 `release_deploy.sh`，在 `do_release` 返回之后、`flock -u 9` 之前执行。
