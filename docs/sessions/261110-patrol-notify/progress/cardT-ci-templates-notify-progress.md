# cardT-ci-templates-notify 进度存档

## 2026-11-10 里程碑 1：7 处通知改造完成，测试全绿

- **当前阶段**：implementing（实现与测试完成，待提交/PR/报告）
- **本段结论**：两模板 7 处内联飞书发送全部改为「组装 step（写 GITHUB_OUTPUT: severity/title/body/links）+ `uses: zlxlabs/gate/.github/actions/notify@v2`」两段式；`if` 逐字保留，`continue-on-error: true` 两 step 各自携带。本地 pytest 346 全绿。
- **关键决策与已否决方案**：
  - severity 经组装 step 的 GITHUB_OUTPUT 传出（而非 `with:` 字面量）：唯一可保「成功回执证据缺失→不发」语义的机制——action 不校验空 title，但空 severity 触发 `notify-invalid-severity` 失败被 continue-on-error 吸收，等效原来的 skip；7 处统一此结构。否决「改 notify step 的 if 加 outputs 条件」（违反 if 逐字相同的验收）。
  - step 名改渠道中性（部署失败通知 (P0, fail-open) 等），测试同步；原「卡/Feishu」名不再符合迁移后语义。
  - @全员标记全部移除：企微 markdown 不支持 mention，gate action 无对应输入；唤醒强度由 heading 的 P0 级别词承担。已在模板注释与报告留痕。
  - 原卡 body 尾部「点按钮看详情。/Run:」行移到 links 输出（企微渲染为链接行、飞书渲染为按钮）。
- **下一步唯一动作**：commit + push + draft PR，写 report.md 并跑 scope check 与 report contract。
