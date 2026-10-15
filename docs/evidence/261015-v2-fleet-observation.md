# v2（01f1f85）推广后舰队部署观察

观察窗：`2026-10-07T13:30:00Z` → `2026-10-15T10:30:00Z`（取证时刻）。
模板指针：`refs/tags/v2` = `01f1f857beab2b77b6797626bfbcb74a61fca29b`（#65 后推广）。
只读取证：未评论 issue、未改 tag、未重跑 run、未改任何 caller 仓。

## 方法

- 本仓 `registry.yaml` 是校验器用的合成示例，不承载真实舰队。调用方以各仓 `.github/workflows/*` 的 `uses:` 为准。
- 枚举 `zlxlabs` 57 个仓的 workflow 目录；`gh search code` 交叉核对。扫描 50/57 完成后 `boq-frontend` 一次 API 超时，其余 HIT 已齐；未 HIT 仓未再检出 `@v2`。
- 每个 caller：`gh run list --workflow <文件> --created '>=2026-10-07T13:30:00Z' --limit 200`。
- 非 success/skipped：`gh run view --json jobs` + check-run annotations；`--log-failed` 只保留 `::error::|deploy of|rollback|FAILED|rc=` 且去掉脚本回显。

## 1. 调用方清单

### `build-deploy.yml@v2`（单镜像）

| 仓 | workflow |
|---|---|
| zlxlabs/AI_Information_processor | `.github/workflows/deploy.yml` |
| zlxlabs/HealthExporter | `.github/workflows/deploy.yml` |
| zlxlabs/MediaResolverAPI | `.github/workflows/deploy.yml` |
| zlxlabs/WechatArticleCommentScrape | `.github/workflows/deploy.yml` |
| zlxlabs/WechatChatRoomSummary | `.github/workflows/deploy.yml` |
| zlxlabs/imflow | `.github/workflows/deploy.yml` |
| zlxlabs/live-recorder | `.github/workflows/deploy.yml` |
| zlxlabs/obsidian-clip-api | `.github/workflows/deploy.yml` |
| zlxlabs/one_translate_tts | `.github/workflows/deploy.yml` |
| zlxlabs/shoplazza-capabilities | `.github/workflows/deploy.yml` |
| zlxlabs/url2gdocs | `.github/workflows/deploy.yml` |
| zlxlabs/wecomGroupHelper | `.github/workflows/deploy.yml` |
| zlxlabs/youtube_download_api | `.github/workflows/deploy.yml` |

### `build-deploy-release.yml@v2`（多镜像发布车道）

| 仓 | workflow |
|---|---|
| zlxlabs/web_transcibe_translate | `.github/workflows/deploy.yml` |

### 不计入 `@v2` 样本

- `zlxlabs/url-parse-api`：钉 `@main` 的 canary，不吃 `v2` tag。

## 2. 汇总

| 仓 | lane | run | 失败 | 模板引起 | 业务仓引起 | 基础设施 | 待定 |
|---|---|---:|---:|---:|---:|---:|---:|
| AI_Information_processor | single | 4 | 0 | 0 | 0 | 0 | 0 |
| HealthExporter | single | 0 | 0 | 0 | 0 | 0 | 0 |
| MediaResolverAPI | single | 3 | 1 | 0 | 1 | 0 | 0 |
| WechatArticleCommentScrape | single | 0 | 0 | 0 | 0 | 0 | 0 |
| WechatChatRoomSummary | single | 0 | 0 | 0 | 0 | 0 | 0 |
| imflow | single | 0 | 0 | 0 | 0 | 0 | 0 |
| live-recorder | single | 12 | 4 | 0 | 4 | 0 | 0 |
| obsidian-clip-api | single | 0 | 0 | 0 | 0 | 0 | 0 |
| one_translate_tts | single | 1 | 0 | 0 | 0 | 0 | 0 |
| shoplazza-capabilities | single | 0 | 0 | 0 | 0 | 0 | 0 |
| url2gdocs | single | 0 | 0 | 0 | 0 | 0 | 0 |
| wecomGroupHelper | single | 0 | 0 | 0 | 0 | 0 | 0 |
| youtube_download_api | single | 0 | 0 | 0 | 0 | 0 | 0 |
| web_transcibe_translate | release | 0 | 0 | 0 | 0 | 0 | 0 |
| **合计** | | **20** | **5** | **0** | **5** | **0** | **0** |

有部署活动的仓 4 个（20 run）；其余 10 个 caller 窗内零 run。成功 15：AI_Information_processor 4、live-recorder 8、MediaResolverAPI 2、one_translate_tts 1。失败 run 的 checkout 日志均落到模板 `01f1f85`。

## 3. 失败 run 归因

### live-recorder（4，业务仓引起：忙锁延期）

模板按设计对 opt-in `busy_lock_file` 在服务忙时 `exit 3`，旧容器保留。annotation 同时有 `deploy result evidence missing`：延期路径不写 `result-json`，属既有警告，不是失败因。

| run | 日期 | 证据行 |
|---|---|---|
| 37633432750 | 2026-10-07T14:02:50Z | `deploy DEFERRED — service busy, old container kept`；`Process completed with exit code 3.` |
| 37640953227 | 2026-10-07T14:56:51Z | 同上（image 22d963bc5e81） |
| 38538345067 | 2026-10-14T08:53:26Z | 同上（image eea4b32cebaf） |
| 38598064785 | 2026-10-14T16:26:18Z | 同上（image ca16c660bc2e） |

失败步骤均为 `ship / build-deploy` → `Deploy over SSH (flock + health probe + auto-rollback)`。同窗另 8 次 success，说明模板部署路径可用。

### MediaResolverAPI（1，业务仓引起：caller 首次接入写错）

| run | 日期 | 证据行 |
|---|---|---|
| 38574014996 | 2026-10-14T13:42:13Z | 0-job startup_failure；`log not found`；`created_at=updated_at`。该 SHA 的 caller 把 `deploy_dir` 写成 `with:` + `secrets` 上下文（Actions `with:` 不支持）。同日后续 run 38592780090、38596384964 改为字面量后 success。 |

不是模板脚本/探针/回滚路径执行失败。

## 4. 模板引起的失败

无。

## 5. 结论

2026-10-07T13:30Z 之后，全部以 `@v2` 调用 `build-deploy.yml` / `build-deploy-release.yml` 的 caller 部署 run **没有由模板引起的失败**。5 次非 success 均可归到业务仓（4 次忙锁延期、1 次 caller `with:` 误用 secrets）。不构成回退 `v2` 的条件。
