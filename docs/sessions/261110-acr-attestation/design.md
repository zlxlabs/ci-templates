# DESIGN-note：部署产物从「每次现做」改为「SHA 只构建一次、形状显式写死」，runner 换底座先过真实部署链

## 目标

runner 工具链升级后，业务仓部署不再因为产物形状变化被 ACR 个人版拒收；同一个 SHA 的重跑直接复用 ACR 里已有的成品；ACR 推送失败让部署变红，不再只打 warning；runner 换底座前先在候选 runner 上跑通真实的「构建 → 推 ACR / 本地 registry → 部署主机拉取 → 身份对账」链路。

## 非目标

- 不重做 attestation / 签名 / SBOM 体系。本次直接关掉默认 attestation，以后需要供应链证明时另立设计。
- 不改部署主机的 Docker 版本，也不改回滚锚点算法：`RepoDigests[0]` 语义由只读核实卡先查清，有问题另出修复卡。
- 不改 `ci_templates_ref` 的默认值 `v1`，另开 issue 跟踪。
- 不在业务仓（url-parse-api、live-recorder 等）改代码。

## 为什么不是分区 / 删除 / 约定

- **分区**：可以把「ACR 个人版」和「本地 registry」分成两种产物形状分别构建，但这会让同一个 SHA 在两边出现两份不同的产物，正是要消除的分叉，所以不采用。真正有效的分区是时间上的：一个 SHA 只构建一次，之后都只读。
- **删除**：默认 attestation 当前没有任何消费者（全仓没有读 provenance/SBOM 的代码），直接删掉（`--provenance=false --sbom=false`）。重跑时的重新构建同样没有消费者，也删掉，改为复用成品。
- **约定**：「同一个 SHA 产物不变」原本只是写在 README 和注释里的约定，事故证明约定守不住（重建会生成新的 digest）。所以改由代码保证：tag 只写一次，存在性检查用代码实现。

## 方案要点与已否决方案

- **要点**：
  1. `push_to_acr.sh` 的两条构建路径（经典 `docker build` 和 buildx 缓存构建）都显式加 `--provenance=false --sbom=false`。用命令行参数而不用环境变量：CLI 不支持时直接报错（fail fast），环境变量会被静默忽略。
  2. 构建前先查 ACR 是否已有 `${ACR_IMAGE}:${GIT_SHA}`，三种结果分开处理：已存在 → 跳过构建，改为 `docker pull` 这份成品到本地，后面推本地 registry、release 线别名推送照常消费本地镜像，两边 digest 因此一致；明确不存在 → 构建并推送；查询出错（网络 / 鉴权等）→ 直接失败，不能当作「不存在」去重建。
  3. ACR 推送失败一律致命，不再看本地 registry 是否成功；本地 registry 失败仍允许降级（它只是拉取加速器）。用户在本会话已经拍板。
  4. 构建前在日志里打印工具链指纹：docker client/server 版本、buildx 版本、存储驱动。
  5. 删掉或改正「重跑会重建、产物逐字节相同」的错误文案（`build-deploy.yml:829`、`README.md:265`、`pull_and_deploy.sh:221/240` 一带）。
  6. gate-hub 的 runner 发布闸在分批替换前，先在候选 runner 上跑一次真实端到端 canary；探针必须先证明能抓到「未修复形状被拒」这个负例。
- **已否决**：
  - 用 `BUILDX_NO_DEFAULT_ATTESTATIONS=1` 环境变量：不支持时被静默忽略，而且不覆盖显式开启的 attestation。
  - 保留 provenance、只设 `oci-artifact=false`：目前没有消费者，多一个随版本漂移的旋钮。
  - 自建「最小镜像推送探针」代替真实 canary：只验得到 registry 接不接受，验不到部署主机拉取和身份对账；仓里已经有真实 canary 可用。
  - 回退到旧版 DinD：等于放弃 #2180 的 BuildKit 安全修复。

## 关键不变式

1. [待实现] 两条构建路径的 argv 都包含 `--provenance=false` 和 `--sbom=false`。代码：`scripts/push_to_acr.sh`；测试：`tests/test_push_to_acr.py` 的 argv 断言。
2. [待实现] ACR 已有 `:<sha>` 时不执行任何 build、不向 ACR push 该 tag，而是 pull 这份成品；存在性查询出错时以非零码退出。测试：`tests/test_push_to_acr.py` 覆盖已存在 / 不存在 / 查询出错三态。
3. [待实现] 只要 ACR push 失败，退出码就非零，不管本地 registry 结果如何。测试：替换 `test_acr_push_failure_with_local_registry_ok_is_non_fatal`。
4. [待实现] runner 发布在 batch-roll 之前要求候选 runner 的真实 canary 通过。代码与测试在 gate-hub 卡内确定。

## 共享资源清单

| 资源 | 写入方 | 读取方 | 守卫 | 文件:行 |
| --- | --- | --- | --- | --- |
| ACR tag `${ACR_IMAGE}:${GIT_SHA}`（远端 registry 状态） | `scripts/push_to_acr.sh`（两条部署线都调用） | `scripts/pull_and_deploy.sh`、`scripts/release_deploy.sh`（主机拉取与身份对账） | 改动后：已存在则只读（新增） | scripts/push_to_acr.sh:184 |
| ACR 别名 tag（release 线） | `scripts/push_alias_to_acr.sh`（消费本地镜像） | release 部署 | 本地必须已有 SOURCE_IMAGE | .github/workflows/build-deploy-release.yml:193 |

## 待验证前提

1. [推断] Docker 29.9.3 内置 BuildKit 0.34.x，默认 attestation 是 OCI artifact 形状（config 为 `application/vnd.oci.empty.v1+json`），ACR 个人版因此拒收。验证入口：只读核实卡用 `imagetools inspect --raw` 比对新旧产物。
2. [推断] 部署主机 containerd 存储下的 `RepoDigests[0]` 可能不对应 ACR 远端真实存在的 digest。验证入口：只读核实卡上机核对。
3. [推断] buildx 缓存构建用的 `docker-container` builder 的 BuildKit 镜像没有钉版本，可能没拿到 #2180 的安全修复。验证入口：只读核实卡。

## 验收路径

1. 入口：live-recorder 重跑 SHA `d5b90487a542` 的部署（ACR 里已有 attempt 1 推上去的 digest `f13108cf…`）。
2. 步骤：push 步骤应打印「已存在 → pull」，而不是重新构建。另找一个新 SHA 的部署，应构建并推送成功，日志里有工具链指纹。
3. 预期：两次 run 的 conclusion 都是 success；不再出现 `unknown manifest class`。
