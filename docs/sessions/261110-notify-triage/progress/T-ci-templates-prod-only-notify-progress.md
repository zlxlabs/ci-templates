# T-ci-templates-prod-only-notify 进度存档

## 里程碑 1：通知路由实现
- **当前阶段**：implementing
- **本段结论**：已完成两个刻意分叉部署模板的通知路由调整：新增 Reconcile 失败 P0，删除普通失败、延期与 release busy defer 通知，并限制 release failure 通知在远端状态未证时发送。受影响测试与 README 已同步，尚未运行测试。
- **关键决策与已否决方案**：release failure 条件用已发布的 `steps.deploy.outputs.deploy_rc` 排除 `rc=1`；同时排除 `reconcile_failed=true`，防止 Reconcile P0 与通用 release failure 重复通知。保留成功回执 opt-in 与既有 rollback unhealthy 条件。提交闸发现 `tests/test_workflow_contract.py` 由 1322 增至 1346 行，按要求申请 sibling 测试文件；scope-request seq=1 已批准 `tests/test_notify_routing_contract.py`，新增路由测试移入该文件，无绕过闸。
- **下一步唯一动作**：提交实现、测试、文档与本进度文件，再开始验证。
