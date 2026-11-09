# ci-templates#71 卡 A 进度

## 里程碑 1：先写契约测试
- 当前阶段：TDD 红测完成，尚未实现
- 本段结论：扩展 `tests/test_release_input.py` 覆盖空数组逐字节兼容、文件声明校验、文件系统边界、大小/数量限制，以及真实子进程写出的 manifest 与可复现 tar。基线运行得到 8 failed、9 passed，失败均来自尚不识别新参数或未生成所需产物。
- 关键决策与已否决方案：空数组输出使用硬编码字节期望，不读取基线版本；tar 验收直接读取生产子进程实际生成的归档及成员。
- 下一步唯一动作：实现 `scripts/normalize_release.py` 的声明校验、manifest file 记录与原子 ustar 生成。

## 里程碑 2：实现 schema 与打包
- 当前阶段：实现已完成，针对性测试通过，待全仓验证
- 本段结论：新增 `--files-json`、`--repo-root`、`--files-tar-out`；按声明顺序校验普通文件和额度、追加 SHA-256 file 记录，并生成可复现的原子 USTAR。`python -m pytest -q tests/test_release_input.py` 当前为 17 passed。
- 关键决策与已否决方案：空文件集直接走旧输出路径，不读取 repo-root、不创建 tar；文件路径校验继续调用 `_relative_path`，不新增路径语法。
- 下一步唯一动作：提交实现后运行完整 `python -m pytest -q` 并核对范围与验收契约。
