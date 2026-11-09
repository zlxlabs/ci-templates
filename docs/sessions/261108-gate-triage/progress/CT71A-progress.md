# ci-templates#71 卡 A 进度

## 里程碑 1：先写契约测试
- 当前阶段：TDD 红测完成，尚未实现
- 本段结论：扩展 `tests/test_release_input.py` 覆盖空数组逐字节兼容、文件声明校验、文件系统边界、大小/数量限制，以及真实子进程写出的 manifest 与可复现 tar。基线运行得到 8 failed、9 passed，失败均来自尚不识别新参数或未生成所需产物。
- 关键决策与已否决方案：空数组输出使用硬编码字节期望，不读取基线版本；tar 验收直接读取生产子进程实际生成的归档及成员。
- 下一步唯一动作：实现 `scripts/normalize_release.py` 的声明校验、manifest file 记录与原子 ustar 生成。
