# ci-templates#71 卡 B 进度

## 里程碑 1：基线与红测
- 当前阶段：实现中
- 本段结论：工作树干净，HEAD 与卡面 base commit 一致（`308800cbf6ace7b6d7d0c4aadda8a5d1dcaaf034`）；已读设计全文并补入传输、主机安装/验证/回滚的验收测试。
- 关键决策与已否决方案：默认无文件的旧行为测试在 base 已通过，归类为基线已有行为；其他新增功能测试以 `unknown manifest record: file` 或缺少 workflow input/传输契约在 base 失败。
- 下一步唯一动作：完成 workflow + release_deploy.sh 实现并运行新增测试。

## 里程碑 2：文件大小闸与测试归位
- 当前阶段：实现中
- 本段结论：提交预检拒绝向 2,076 行的 `tests/test_release_deploy.py` 追加测试；范围申请 seq=1 获批新增 `tests/test_release_config_files.py`，新增部署行为测试已迁入该文件，原测试文件恢复未修改。
- 关键决策与已否决方案：不绕过文件大小闸、不压缩断言；按批准范围把新文件行为测试单独放入同级模块，复用现有部署测试夹具。
- 下一步唯一动作：提交测试与进度里程碑，然后提交实现和报告主体。
