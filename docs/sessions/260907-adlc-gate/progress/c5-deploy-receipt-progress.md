## 里程碑 1：digest 回滚锚点

- 当前阶段：implementing
- 本段结论：部署成功后同时记录 last_good_tag 与 last_good_digest。回滚优先按 digest 拉取并复用 :latest，digest 缺失或为空时沿用原 tag 形态。
- 关键决策与已否决方案：digest 只保存 sha256:*，local registry 的 RepoDigests 视为空值；未引入额外回退层。
- 下一步唯一动作：实现 last_deploy_result.json 与 stdout result evidence。

## 里程碑 2：部署回执与 evidence

- 当前阶段：implementing
- 本段结论：脚本现在为五种受支持结局写入固定键集合的 last_deploy_result.json，并在 stdout 恰好输出一条 result-json evidence。回执中的探针状态、最终状态码、尝试次数和耗时来自真实脚本运行状态。
- 关键决策与已否决方案：使用纯 Bash JSON 拼接与最小字符串转义，不依赖目标机 Python；回滚结局沿用原有退出码 1/4，reconcile 失败沿用 rc=5。
- 下一步唯一动作：把 result-json evidence 解析成 deploy step outputs，并声明 notify_on_success 输入。

## 里程碑 3：workflow outputs 与成功回执

- 当前阶段：implementing
- 本段结论：Deploy 步骤精确提取最后一条 result-json evidence 并写入 image_digest、probe_status、probe_attempts、probe_elapsed_s、outcome 五个输出。新增 notify_on_success 布尔输入默认关闭，并在 reconcile 之后接入 fail-open 成功回执卡。
- 关键决策与已否决方案：成功卡只在显式开启时运行，缺 digest 或探针字段直接 warning 跳过；webhook 沿用 FEISHU_CI_WEBHOOK，标题前缀沿用 FEISHU_CI_TITLE_PREFIX。
- 下一步唯一动作：补齐 README 契约说明并做全量验证。
