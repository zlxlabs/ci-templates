## 里程碑 1：digest 回滚锚点

- 当前阶段：complete
- 本段结论：部署成功后同时记录 last_good_tag 与 last_good_digest。回滚优先按 digest 拉取并复用 :latest，digest 缺失或为空时沿用原 tag 形态。
- 关键决策与已否决方案：digest 只保存 sha256:*，local registry 的 RepoDigests 视为空值；未引入额外回退层。
- 下一步唯一动作：无；进入主脑验收。

## 里程碑 2：部署回执与 evidence

- 当前阶段：complete
- 本段结论：脚本现在为五种受支持结局写入固定键集合的 last_deploy_result.json，并在 stdout 恰好输出一条 result-json evidence。回执中的探针状态、最终状态码、尝试次数和耗时来自真实脚本运行状态。
- 关键决策与已否决方案：使用纯 Bash JSON 拼接与最小字符串转义，不依赖目标机 Python；回滚结局沿用原有退出码 1/4，reconcile 失败沿用 rc=5。
- 下一步唯一动作：无；进入主脑验收。

## 里程碑 3：workflow outputs 与成功回执

- 当前阶段：complete
- 本段结论：Deploy 步骤精确提取最后一条 result-json evidence 并写入 image_digest、probe_status、probe_attempts、probe_elapsed_s、outcome 五个输出。新增 notify_on_success 布尔输入默认关闭，并在 reconcile 之后接入 fail-open 成功回执卡。
- 关键决策与已否决方案：成功卡只在显式开启时运行，缺 digest 或探针字段直接 warning 跳过；webhook 沿用 FEISHU_CI_WEBHOOK，标题前缀沿用 FEISHU_CI_TITLE_PREFIX。
- 下一步唯一动作：无；README 契约、测试防线与验证均已完成。

## 里程碑 4：失败路径与测试防线收口

- 当前阶段：complete
- 本段结论：前向 pull/tag/compose 失败统一写入 `deploy_failed`；回滚动作失败时清空未被验证的镜像身份；回执使用同目录临时文件加原子 `mv` 发布；成功回执卡不再回显响应体。成功卡六个字段逐一精确映射，malformed evidence 行为测试覆盖远端 rc=0/1/255，Docker fake 对未知调用显式失败。
- 关键决策与已否决方案：只修单镜像 lane 的既有路径与对应测试，不引入新状态或重试；通用 Docker mock 对 RepoDigest 查询返回空值，非空 digest 由专用 fixture 验证，避免改变既有 tag 回滚场景。
- 验证结果：`python3 scripts/validate_registry.py registry.yaml` 通过；全量 `pytest` 为 287 passed；第 1/3/5/6 项单侧破坏红验均由行为断言捕获；`actionlint` 仅剩既有 SC2086/SC2029 信息级提示。
- 下一步唯一动作：无；保留报告并交主脑验收。
