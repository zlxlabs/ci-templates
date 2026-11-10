## 里程碑 1：失败测试与设计入库

- 当前阶段：implementing
- 本段结论：新增了 ACR tag 三态探测、复用 pull、工具链指纹顺序、显式 build 参数和 ACR push 致命语义的契约测试。基线脚本在这些新增判据上失败，确认覆盖到了待实现行为。
- 关键决策与已否决方案：存在性探测将以 `docker manifest inspect` 的明确 `no such manifest: <ref>` 文本判为不存在；其他非零结果统一视为探测故障，不转入重建。
- 下一步唯一动作：实现脚本行为并完成文案更新。

## 里程碑 2：实现与 targeted 测试

- 当前阶段：implementing
- 本段结论：脚本现先打印可观测性指纹、用 `docker manifest inspect` 的明确 not-found 输出分流三态，存在时 pull 复用且不重推 ACR；两条构建命令显式关闭 provenance/SBOM，ACR push 失败无论本地结果均致命。目标测试文件 24 项通过。
- 关键决策与已否决方案：仅输出含精确 SHA 引用的 `no such manifest: <ref>` 判为不存在；鉴权、网络等其他非零响应输出 `ACR-SHA-PROBE-FAILED` 并停止，不回退重建。
- 下一步唯一动作：提交实现与报告主体，再执行整套验收和范围检查。

## 里程碑 3：保持成功路径 stdout 契约

- 当前阶段：implementing
- 本段结论：工具链指纹改写到 stderr 日志流，仍与构建/pull 前顺序一致，但不改变原有成功路径 stdout 收尾契约；新增 fail-open 测试同样通过。
- 关键决策与已否决方案：不扩改 workflow 输入说明行（卡面只授权 DEFERRED 提示字符串），通过将诊断指纹写入 stderr 保持 stdout 约定。
- 下一步唯一动作：提交这处调整，写入完整报告主体。

## 里程碑 4：红测判据闭环

- 当前阶段：implementing
- 本段结论：将工具链字段不可用的 fail-open 场景并入首轮已红的指纹测试函数，避免新加一条未经过测试先行的独立测试函数；该函数同时锁住正常指纹顺序和全 unknown 时继续构建/推送。
- 关键决策与已否决方案：不把 fail-open 判据标成不适用；保留独立断言，并与首轮 base 行为已证明会失败的同一测试函数绑定。
- 下一步唯一动作：提交测试闭环后再写最终报告主体。
