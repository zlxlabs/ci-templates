# GitHub Actions 对 stderr `::error::` 的真实注解行为

日期：2026-10-07。此记录订正 `docs/sessions/260907-adlc-gate/reviews/c5-receipt-closure-verdict.md:19-33` 中 F-2 的未实跑推断；不修改历史文档。

## 实测结论

**GitHub Actions runner 会把 stdout 和 stderr 上的 `::error::` 都解析为 check-run annotation。** 本次真实 Actions run 的 check-run annotations API 返回中，A、B、C、D 四条消息均以 `failure` level 出现。因此，ci-templates#56 中“stderr 上的 `::error::` 不会可靠产生注解”这一前提不成立；**本次不改 `scripts/pull_and_deploy.sh`，建议 #56 按「不做」关单**（关单由主脑执行）。

| 探针 | 输出形态 | annotations API 中是否出现 | 返回 level |
|---|---|---:|---|
| A | 当前 step 直接向 stdout 输出 `::error::probe-A-stdout` | 是 | `failure` |
| B | 当前 step 直接向 stderr 输出 `::error::probe-B-stderr` | 是 | `failure` |
| C | 子进程向 stderr 输出 `::error::probe-C-child-stderr` | 是 | `failure` |
| D | 命令替换捕获 stdout 时，子进程向 stderr 输出 `::error::probe-D-captured-stderr` | 是 | `failure` |

`failure` 是 annotation level；它没有令本探针 job 失败。Actions run 与 check-run 的结论均为 `success`，符合探针末尾显式 `exit 0` 的预期。API 还返回了一条与探针无关的 Ubuntu runner 生命周期 notice，不计入 A-D。

## 真实 Actions 证据

- Run：<https://github.com/zlxlabs/ci-templates/actions/runs/37623955751>
- Job/check-run：`probe`，check-run ID `112800869472`；<https://github.com/zlxlabs/ci-templates/actions/runs/37623955751/job/112800869472>
- Run 状态：`completed` / `success`；head SHA：`981d3dcc991b6365b1c739a05d42f3e644c1921b`。
- 探针分支：`probe/stderr-annotation-20261007`；只在该临时分支添加限定此分支 push 的单一探针 workflow，不含 secrets；探针完成后已删除该分支。
- 权威判据：`gh api repos/zlxlabs/ci-templates/check-runs/112800869472/annotations` 的原始 JSON 返回如下（未筛选、未改写）：

```json
[{"path":".github","blob_href":"https://github.com/zlxlabs/ci-templates/blob/981d3dcc991b6365b1c739a05d42f3e644c1921b/.github","start_line":14,"start_column":null,"end_line":14,"end_column":null,"annotation_level":"failure","title":"","message":"probe-D-captured-stderr","raw_details":""},{"path":".github","blob_href":"https://github.com/zlxlabs/ci-templates/blob/981d3dcc991b6365b1c739a05d42f3e644c1921b/.github","start_line":13,"start_column":null,"end_line":13,"end_column":null,"annotation_level":"failure","title":"","message":"probe-A-stdout","raw_details":""},{"path":".github","blob_href":"https://github.com/zlxlabs/ci-templates/blob/981d3dcc991b6365b1c739a05d42f3e644c1921b/.github","start_line":12,"start_column":null,"end_line":12,"end_column":null,"annotation_level":"failure","title":"","message":"probe-C-child-stderr","raw_details":""},{"path":".github","blob_href":"https://github.com/zlxlabs/ci-templates/blob/981d3dcc991b6365b1c739a05d42f3e644c1921b/.github","start_line":11,"start_column":null,"end_line":11,"end_column":null,"annotation_level":"failure","title":"","message":"probe-B-stderr","raw_details":""},{"path":".github","blob_href":"https://github.com/zlxlabs/ci-templates/blob/981d3dcc991b6365b1c739a05d42f3e644c1921b/.github","start_line":1,"start_column":null,"end_line":1,"end_column":null,"annotation_level":"notice","title":"","message":"\"The ubuntu-latest label will migrate to Ubuntu 26 beginning October 19, 2026. For more information, see https://github.com/actions/runner-images/issues/14748\"","raw_details":""}]
```

## 与生产调用形态的对应

[`.github/workflows/build-deploy.yml:383-395`](../../../.github/workflows/build-deploy.yml) 的 `deploy_once()` 执行 `remote_output="$(ssh ... "bash '${REMOTE_SCRIPT}' ...")"`，再将捕获的 stdout 打印出来。SSH 子进程的 stderr 不被 `$(...)` 捕获，会继续到 runner stderr；这与探针 D 的 `out="$(bash -c '... >&2; echo payload')"` 结构对应。故 `pull_and_deploy.sh` 中走 stderr 的 `::error::` 到达 runner 后仍会被解析为注解。

## 收尾

探针 workflow 只存在于一次性分支，未进入卡分支；分支删除后的远端核查 `git ls-remote --heads origin probe/stderr-annotation-20261007` 无输出。未改部署脚本、测试、build/deploy workflow、tag 或生产部署行为。
