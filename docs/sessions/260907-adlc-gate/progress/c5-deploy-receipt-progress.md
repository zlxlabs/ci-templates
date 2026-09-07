## 里程碑 1：digest 回滚锚点

- 当前阶段：implementing
- 本段结论：部署成功后同时记录 last_good_tag 与 last_good_digest。回滚优先按 digest 拉取并复用 :latest，digest 缺失或为空时沿用原 tag 形态。
- 关键决策与已否决方案：digest 只保存 sha256:*，local registry 的 RepoDigests 视为空值；未引入额外回退层。
- 下一步唯一动作：实现 last_deploy_result.json 与 stdout result evidence。
