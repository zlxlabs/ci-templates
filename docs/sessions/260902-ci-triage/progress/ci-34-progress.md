# ci-templates#34 进度存档

## 2026-09-02 对齐清单

- **当前阶段**：implementing（契约测试先红）
- **本段结论**：#35 定稿不是「对账塞进 `do_release()`」，而是 `do_release`/`do_deploy` 返回 0 之后、`flock -u 9` 之前调用对账函数，失败映射 rc=5；workflow 保留同名 Reconcile step 作薄壳（`failure() && reconcile_failed`），Deploy step 在 rc=5 分支写 output 并 `::error::`。pull lane 照抄该形态，expected 仍是单镜像 `${ACR_IMAGE}:${GIT_SHA}`，不抄 `config --images`。
- **关键决策与已否决方案**：
  - 选薄壳、不删 step：与 `build-deploy-release.yml:387-394` 一致；飞书卡已按「Reconcile 步骤红」分流，删 step 会让 on-call 文案对不上。
  - 函数名 `reconcile_deployed_image`（单镜像，避免与 `reconcile_release_images` 撞名）；取证行保持既有 `image reconcile values:` / `expected_id` / `latest_id` / `running_ids`。
  - 已否决：workflow 第二条 ssh 再 flock；对账只比 expected 与 latest。
- **下一步唯一动作**：把 `reconcile_docker` / `reconcile_deployed_image` 写入 `pull_and_deploy.sh`，在 `do_deploy` 返回 0 之后、`flock -u 9` 之前调用。

## 2026-09-02 脚本对账函数

- **当前阶段**：implementing（脚本对账下沉锁内）
- **本段结论**：对账在 `do_deploy` 成功返回后、`flock -u 9` 前执行；失败映射 rc=5；`compose ps` 带非 oneshot 服务列表；docker 调用走 `reconcile_docker` 超时包装。
- **关键决策与已否决方案**：不把对账塞进 `do_deploy()` 函数体（对齐 #35 的 `do_release` 后再对账）；`compose_list_services` 不改，对账自行 `reconcile_docker compose config --services`，避免给部署/回滚路径套上对账超时语义。
- **下一步唯一动作**：workflow Deploy step 增加 rc=5 分支，Reconcile step 改薄壳。

## 2026-09-02 workflow 薄壳

- **当前阶段**：implementing（workflow 薄壳）
- **本段结论**：Deploy step 在 rc=5 写 `reconcile_failed` 并 fail-loud；Reconcile step 改为 `failure() && reconcile_failed` 薄壳，不再开第二条 ssh。
- **关键决策与已否决方案**：保留 step 名以便飞书卡「Reconcile 步骤红」仍对得上；不新增 required input。
- **下一步唯一动作**：补超时用例、锁序红验、同步 README 对账位置一句，然后全量 pytest。

## 2026-09-02 超时用例与锁序红验

- **当前阶段**：implementing（收尾）
- **本段结论**：`RECONCILE_CMD_TIMEOUT=1` 时 mock `sleep 8` 以 rc=5 返回且输出含 timeout，未挂死；锁序红验把 `reconcile_deployed_image` 挪到 `flock -u 9` 之后，契约测试断言失败 `115 < 16`，已还原。
- **关键决策与已否决方案**：红验只改调用与 `flock -u 9` 的相对位置一行块，未整文件 checkout。
- **下一步唯一动作**：全量 pytest + shellcheck，提交后停在干净 card 分支。
