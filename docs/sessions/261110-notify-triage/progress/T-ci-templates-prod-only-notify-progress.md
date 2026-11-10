# T-ci-templates-prod-only-notify 进度存档

## 里程碑 1：通知路由实现
- **当前阶段**：implementing
- **本段结论**：已完成两个刻意分叉部署模板的通知路由调整：新增 Reconcile 失败 P0，删除普通失败、延期与 release busy defer 通知，并限制 release failure 通知在远端状态未证时发送。受影响测试与 README 已同步，尚未运行测试。
- **关键决策与已否决方案**：release failure 条件用已发布的 `steps.deploy.outputs.deploy_rc` 排除 `rc=1`；同时排除 `reconcile_failed=true`，防止 Reconcile P0 与通用 release failure 重复通知。保留成功回执 opt-in 与既有 rollback unhealthy 条件。提交闸发现 `tests/test_workflow_contract.py` 由 1322 增至 1346 行，按要求申请 sibling 测试文件；scope-request seq=1 已批准 `tests/test_notify_routing_contract.py`，新增路由测试移入该文件，无绕过闸。
- **下一步唯一动作**：提交实现、测试、文档与本进度文件，再开始验证。

## 里程碑 2：首轮验证前修正
- **当前阶段**：implementing
- **本段结论**：首轮 Accept-Check 收集测试时发现 `tests/test_release_notify_contract.py` 新函数定义漏写冒号，已修正；失败发生在 pytest 收集阶段，尚无测试用例执行。
- **关键决策与已否决方案**：不绕过语法错误、不调整验证范围；修复函数声明后重新运行相同 Accept-Check。
- **下一步唯一动作**：提交语法修正与本段进度，再运行 Accept-Check。

## 里程碑 3：路由契约修正
- **当前阶段**：implementing
- **本段结论**：第二轮 Accept-Check 完成收集并执行 164 项，其中 162 通过、2 失败：旧测试仍要求已删除的普通失败条件，另一个新测试的消息断言与现有文案措辞不符。已更新断言以检查新路由与实际 Reconcile 文案。随后 file-size gate 拒绝主测试文件净增 2 行；已将剩余路由断言移到已批准的 sibling 文件，原测试文件现低于提交前行数。
- **关键决策与已否决方案**：不恢复普通失败通知条件；保留 rollback/reconcile P0 与普通失败条件不存在的完整断言，并迁移到 sibling 文件，不绕过 file-size gate。Reconcile 消息内容本身已覆盖事件、影响与立即核对运行中容器 SHA 的操作，不改业务文案实现。
- **下一步唯一动作**：提交测试断言调整与本段进度，再重跑 Accept-Check。

## 里程碑 4：验收通过
- **当前阶段**：implementing
- **本段结论**：Accept-Check 扩展覆盖授权新增的 sibling 测试文件后全部通过：`164 passed in 26.30s`。此前测试收集错误与两条旧断言失败均已修正。
- **关键决策与已否决方案**：Verify-Command 属于远程 CI 验证入口，本机不运行；按固定约束不等待远程 CI 终态。主干基线 run 查询派发时为 HTTP 404，继承红无法判定。
- **下一步唯一动作**：提交本段验收进度与最终报告，再按授权推送当前分支。
