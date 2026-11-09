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

## 里程碑 3：实现完成、提交前验收
- 当前阶段：实现完成，待提交与全量验证
- 本段结论：workflow opt-in tar 传输与主机 staging/install/read-back/rollback 已实现；真实 normalize→真实脚本的跨进程测试和 workflow 合同测试共 52 项通过，`bash -n` 与 `git diff --check` 通过。
- 关键决策与已否决方案：只对 Mount.Source 命中的服务做 `--no-deps --force-recreate`；完整 image 组先普通 up，避免 service operands 漏部署其他 image，也避免全局强制重建数据库。
- 下一步唯一动作：提交实现、进度与报告主体，然后执行提交后的完整 pytest 和范围检查。

## 里程碑 4：生产脚本体积闸阻断
- 当前阶段：blocked，等待主脑拆分后续任务
- 本段结论：实现提交被 `file_size_gate_block` 拒绝：`scripts/release_deploy.sh` 从 base 的 912 行增长到 1,210 行，限制 800；守卫明确要求超限生产文件交由主脑拆分卡。本地重点测试已通过，但实现未入 commit。
- 关键决策与已否决方案：不压行、不删测试、不跳过提交钩子，也不擅自增加远端 helper 脚本/额外 scp 路径绕过该闸；等拆分任务明确授权。
- 下一步唯一动作：由主脑拆分生产脚本任务后继续本卡实现与交付。

## 里程碑 5：按主脑裁决抽离函数库
- 当前阶段：实现完成，待提交与验收
- 本段结论：按新派发 Scope-Add 新建 `scripts/release_files.sh`，将全部配置文件 host 逻辑及 compose 部署包装移出；主脚本降至 842 行（base 912），函数库 344 行。workflow 每次都传函数库；空 input 为脚本+函数库+manifest 三次 scp，tar 是可选第四次。
- 关键决策与已否决方案：函数库仅声明函数，source 时不执行动作；缺失/不可读 fail-loud；遵从主脑本反馈覆盖设计第 7 节第 5 条，合同注释固定该裁决出处。
- 下一步唯一动作：写完整报告并提交实现/进度，随后运行指定四文件验收与全量 pytest。
