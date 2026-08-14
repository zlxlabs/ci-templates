#!/bin/bash
# SSH-side deploy internals for the ci-templates reusable workflow (T4).
#
# Hardened over the docker-package skill's original pull_and_deploy.sh:
#   - per-host flock      : concurrent deploys to the same host serialize
#   - immutable SHA tag   : deploys ${ACR_IMAGE}:${GIT_SHA}, never :latest
#   - last-good tracking  : records the last healthy tag for rollback
#   - health probe gate   : warmup + retries + expected status
#   - auto rollback       : probe failure -> redeploy previous good tag
#   - busy-lock gate      : opt-in (BUSY_LOCK_FILE); exit codes: 0=healthy,
#                           1=probe failed (rolled back), 3=deferred (service
#                           busy, busy lock not acquired in time, old
#                           container untouched)
#
# All inputs come from the environment so the build-deploy.yml job can export
# them and so tests can inject mocks (DOCKER_BIN / CURL_BIN).
set -euo pipefail

# --- required inputs ---------------------------------------------------------
: "${IMAGE_NAME:?IMAGE_NAME required}"     # local tag, e.g. ops-dispatcher
: "${ACR_IMAGE:?ACR_IMAGE required}"       # full registry path
: "${GIT_SHA:?GIT_SHA required}"           # immutable image tag to deploy
: "${DEPLOY_DIR:?DEPLOY_DIR required}"     # project root holding the compose file

# --- tunables (sane defaults) ------------------------------------------------
STATE_DIR="${STATE_DIR:-${DEPLOY_DIR}/.deploy-state}"
HOST_LOCK="${HOST_LOCK:-/var/lock/fleet-deploy.lock}"   # ONE lock per host
DOCKER_BIN="${DOCKER_BIN:-docker}"
CURL_BIN="${CURL_BIN:-curl}"

BUSY_LOCK_FILE="${BUSY_LOCK_FILE:-}"        # opt-in deploy gate; empty = off
BUSY_LOCK_TIMEOUT="${BUSY_LOCK_TIMEOUT:-600}"

PULL_RETRIES="${PULL_RETRIES:-6}"
PULL_RETRY_DELAY="${PULL_RETRY_DELAY:-10}"  # base seconds; backoff = delay * attempt

# opt-in 本地网络 registry 镜像全路径(如 host:port/namespace/image);留空(默认)=
# 禁用,pull_image() 退化为改动前的纯 ACR 单路径,行为逐字节不变。不是 caller 可传
# 的 workflow input——由 build-deploy.yml 的 deploy 步骤按 LOCAL_REGISTRY(若非空)
# 现场拼出,与 ACR_IMAGE 的组装方式完全对称。
LOCAL_IMAGE="${LOCAL_IMAGE:-}"
LOCAL_PULL_RETRIES="${LOCAL_PULL_RETRIES:-2}"
LOCAL_PULL_RETRY_DELAY="${LOCAL_PULL_RETRY_DELAY:-1}"  # base seconds; backoff = delay * attempt

HEALTHCHECK_URL="${HEALTHCHECK_URL:-}"
HEALTHCHECK_EXPECT_STATUS="${HEALTHCHECK_EXPECT_STATUS:-200}"
HEALTHCHECK_RETRIES="${HEALTHCHECK_RETRIES:-5}"
HEALTHCHECK_INTERVAL="${HEALTHCHECK_INTERVAL:-3}"   # seconds between probes
HEALTHCHECK_WARMUP="${HEALTHCHECK_WARMUP:-5}"       # seconds before first probe
HEALTHCHECK_TIMEOUT="${HEALTHCHECK_TIMEOUT:-5}"     # per-probe curl timeout

# optional test/observability hooks
DEPLOY_EVENT_LOG="${DEPLOY_EVENT_LOG:-}"
DEPLOY_ID="${DEPLOY_ID:-$$}"

GOOD_TAG_FILE="${STATE_DIR}/last_good_tag"

log()   { echo "[deploy] $*"; }
event() { [ -n "$DEPLOY_EVENT_LOG" ] && echo "$1:${DEPLOY_ID}" >> "$DEPLOY_EVENT_LOG" || true; }

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

is_non_negative_integer() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

# --- local registry input validation (opt-in; only runs when LOCAL_IMAGE is set) ---
# 校验 LOCAL_IMAGE 的 host[:port] 前缀部分(第一个 "/" 之前),拒绝协议前缀
# (http://)、凭据(user:pass@host)、多余路径——这些一旦混进镜像引用,会连同下面
# 的 docker pull/tag 命令行和 log 一起被打进部署日志,凭据因此发生泄漏。
# 错误信息里绝不回显原值,只说期望格式,否则校验本身就成了泄漏点。
#
# 首尾字符必须是字母/数字(OCR round-2 finding #7,high):不锚定首字符的话,
# 形如 "--evil-flag"(不含 "/")的值能通过 `[A-Za-z0-9.-]+`,而
# `"$DOCKER_BIN" pull "${LOCAL_IMAGE}:${tag}"` 会把它解析成
# `docker pull --evil-flag:tag`——docker CLI 把它当 flag 而不是镜像名,是真实的
# CLI flag 注入面。与 scripts/push_to_acr.sh 里同一个正则同步修。
is_valid_registry_host() {
  [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]+)?$ ]]
}

if [ -n "$LOCAL_IMAGE" ]; then
  local_registry_host="${LOCAL_IMAGE%%/*}"
  if ! is_valid_registry_host "$local_registry_host"; then
    log "LOCAL_IMAGE has an invalid registry host — expected host[:port] with no scheme prefix, credentials, or path"
    exit 1
  fi
  # LOCAL_PULL_RETRIES=0 会让本地路径的 while 循环一次都不执行 —— 表现和"本地
  # registry 不可达"完全一样:每次都静默回退 ACR,而没有任何信号说明新链路根本
  # 没生效。这正是要防的静默降级,必须是显式配置错误,不能被 bash 算术悄悄当 0
  # 处理后伪装成"永远走 ACR 兜底"。
  if ! is_positive_integer "$LOCAL_PULL_RETRIES"; then
    log "LOCAL_PULL_RETRIES must be a positive integer, got: ${LOCAL_PULL_RETRIES}"
    exit 1
  fi
  # 上限同样是防静默降级的另一半(OCR round-3 finding #3):这条快速路径存在的
  # 唯一理由就是"本地是同网段直连,预算必须远小于 ACR 的 150s"——不设上限的话,
  # 一次误配置(比如抄错成三位数)就会让"本地优先"在实际效果上退化成"本地几乎
  # 总是先拖住部署很久才轮到 ACR",违背这条快速路径本身的设计意图。上限选取
  # 与 push_to_acr.sh 的 PUSH_MAX_ATTEMPTS(<=3)同精神:给个足够宽松、但确实
  # 挡住"整数量级"误配置的硬顶。
  if [ "$LOCAL_PULL_RETRIES" -gt 5 ]; then
    log "LOCAL_PULL_RETRIES must not exceed 5, got: ${LOCAL_PULL_RETRIES}"
    exit 1
  fi
  if ! is_non_negative_integer "$LOCAL_PULL_RETRY_DELAY"; then
    log "LOCAL_PULL_RETRY_DELAY must be a non-negative integer, got: ${LOCAL_PULL_RETRY_DELAY}"
    exit 1
  fi
  if [ "$LOCAL_PULL_RETRY_DELAY" -gt 5 ]; then
    log "LOCAL_PULL_RETRY_DELAY must not exceed 5, got: ${LOCAL_PULL_RETRY_DELAY}"
    exit 1
  fi
fi

# --- pull with retry + local fallback ----------------------------------------
# 单发 docker pull 碰上 registry 网络抖动(EOF/reset/timeout)会整场判死;而 SHA tag
# 不可变,本地已有的同 tag 镜像(回滚残留/预热)与远端逐字节一致 —— registry 单独挂
# 不应该拦下部署(2026-07-09 n305→ACR 间歇抖,imflow 因此 9 连败)。
#
# 为什么 PULL_RETRIES 默认是 6(而不是 3):2026-07-28 10:20 CST 从部署目标机 n305
# 对 crpi-0vsre5argteykh9m.cn-guangzhou.personal.cr.aliyuncs.com/v2/ 连续发 20 次
# HTTPS 探测,结果 OK=16 FAIL=4(失败率 20%),且失败成簇出现(第 11、12 次连续失
# 败),表现为 TCP connect 挂死到超时 —— 与 2026-07-27 晚间部署日志里的
# "dial tcp 8.134.34.201:443: i/o timeout" 一致。旧预算 PULL_RETRIES=3 配线性退避
# delay*attempt(10+20=30 秒)小于观测到的坏窗口,导致 2026-07-27 当晚三次部署
# (13:00 / 15:12 / 16:24 UTC)全部因 3/3 pull 失败而中止。retries=6 把总等待预算
# 提到 10+20+30+40+50=150 秒(线性退避算法不变,只调次数),足以跨过实测到的成簇
# 失败窗口。该链路问题在 hosted runner 时代就存在,不是 self-hosted 迁移引入的。
#
# 本地 registry 快速路径(opt-in,LOCAL_IMAGE 非空才生效):2026-07-27 实测同网段
# 端到端拉取只要 0.458 秒,真出问题大概率是"整个不可达"而不是"抖一下就好"——
# 照搬上面 ACR 的 150 秒累计预算纯属浪费等待。默认 2 次、基础延迟 1 秒(线性退避
# 1+2=3 秒)就足够分辨"能连"与"连不上",连不上就快速切到下面完全未改动的 ACR
# 预算,不会把总部署时长拖长。拉到的字节 retag 成规范名 ${ACR_IMAGE}:${tag} ——
# 下游 deploy_tag()/last_good_tag/回滚只认这个名字,不关心字节来自哪个 registry
# (两个 registry 存的是同一个不可变 SHA tag 的逐字节相同内容)。
pull_from_local_registry() {
  local tag="$1" attempt=1
  while [ "$attempt" -le "$LOCAL_PULL_RETRIES" ]; do
    if "$DOCKER_BIN" pull "${LOCAL_IMAGE}:${tag}"; then
      if "$DOCKER_BIN" tag "${LOCAL_IMAGE}:${tag}" "${ACR_IMAGE}:${tag}"; then
        log "pulled ${LOCAL_IMAGE}:${tag} from local registry, retagged as ${ACR_IMAGE}:${tag}"
        return 0
      fi
      log "local registry pull succeeded but retag to ${ACR_IMAGE}:${tag} failed — falling back to ACR"
      return 1
    fi
    log "local registry pull attempt ${attempt}/${LOCAL_PULL_RETRIES} failed for ${LOCAL_IMAGE}:${tag}"
    [ "$attempt" -lt "$LOCAL_PULL_RETRIES" ] && sleep $((LOCAL_PULL_RETRY_DELAY * attempt))
    attempt=$((attempt + 1))
  done
  log "local registry unreachable after ${LOCAL_PULL_RETRIES} attempts — falling back to ACR"
  return 1
}

pull_image() {
  local ref="$1" attempt=1
  local tag="${ref##*:}"
  if [ -n "$LOCAL_IMAGE" ] && pull_from_local_registry "$tag"; then
    return 0
  fi
  while [ "$attempt" -le "$PULL_RETRIES" ]; do
    if "$DOCKER_BIN" pull "$ref"; then return 0; fi
    log "pull attempt ${attempt}/${PULL_RETRIES} failed for ${ref}"
    [ "$attempt" -lt "$PULL_RETRIES" ] && sleep $((PULL_RETRY_DELAY * attempt))
    attempt=$((attempt + 1))
  done
  if "$DOCKER_BIN" image inspect "$ref" >/dev/null 2>&1; then
    log "registry unreachable but ${ref} already local — proceeding"
    return 0
  fi
  log "pull failed ${PULL_RETRIES}x and ${ref} not local — aborting"
  return 1
}

# --- deploy a specific tag: pull (if remote) + retag + compose up ------------
deploy_tag() {
  local tag="$1"
  log "deploying ${ACR_IMAGE}:${tag}"
  pull_image "${ACR_IMAGE}:${tag}" || return $?
  "$DOCKER_BIN" tag "${ACR_IMAGE}:${tag}" "${IMAGE_NAME}:latest" || return $?
  ( cd "$DEPLOY_DIR" && "$DOCKER_BIN" compose up -d )
}

# --- health probe: returns 0 if the service answers as expected --------------
health_probe() {
  [ -z "$HEALTHCHECK_URL" ] && { log "no HEALTHCHECK_URL, skipping probe"; return 0; }
  log "warmup ${HEALTHCHECK_WARMUP}s before probing ${HEALTHCHECK_URL}"
  sleep "$HEALTHCHECK_WARMUP"
  local attempt=1 probe_attempts=""
  while [ "$attempt" -le "$HEALTHCHECK_RETRIES" ]; do
    local code curl_rc
    if code="$("$CURL_BIN" -s -o /dev/null -w '%{http_code}' \
             --max-time "$HEALTHCHECK_TIMEOUT" "$HEALTHCHECK_URL")"; then
      curl_rc=0
    else
      curl_rc=$?
    fi
    [ -n "$code" ] || code="000"
    if [ -n "$probe_attempts" ]; then
      probe_attempts+=",${code}(curl=${curl_rc})"
    else
      probe_attempts="${code}(curl=${curl_rc})"
    fi
    if [ "$code" = "$HEALTHCHECK_EXPECT_STATUS" ]; then
      log "health probe OK (attempt ${attempt}, status ${code})"
      return 0
    fi
    log "health probe attempt ${attempt}/${HEALTHCHECK_RETRIES} got '${code}', want ${HEALTHCHECK_EXPECT_STATUS}"
    attempt=$((attempt + 1))
    [ "$attempt" -le "$HEALTHCHECK_RETRIES" ] && sleep "$HEALTHCHECK_INTERVAL"
  done
  echo "[deploy][evidence] probe-attempts: ${probe_attempts}"
  return 1
}

# --- critical section, serialized per host via flock -------------------------
do_deploy() {
  event enter
  mkdir -p "$STATE_DIR"

  local prev_good="" compose_ps container_logs rollback_rc=0
  [ -f "$GOOD_TAG_FILE" ] && prev_good="$(cat "$GOOD_TAG_FILE")"

  deploy_tag "$GIT_SHA"

  if health_probe; then
    echo "$GIT_SHA" > "$GOOD_TAG_FILE"
    log "deploy of ${GIT_SHA} healthy; recorded as last good"
    event exit
    return 0
  fi

  log "health probe FAILED for ${GIT_SHA}"
  echo "[deploy][evidence] compose-ps:"
  compose_ps="$(
    cd "$DEPLOY_DIR" && "$DOCKER_BIN" compose ps 2>&1 || true
  )"
  printf '%s\n' "$compose_ps" | sed 's/^/[deploy][evidence] /' || true
  echo "[deploy][evidence] container-logs:"
  container_logs="$(
    cd "$DEPLOY_DIR" && "$DOCKER_BIN" compose logs --tail 100 --no-color 2>&1 || true
  )"
  printf '%s\n' "$container_logs" | sed 's/^/[deploy][evidence] /' || true
  if [ -n "$prev_good" ] && [ "$prev_good" != "$GIT_SHA" ]; then
    log "rolling back to previous good tag ${prev_good}"
    rollback_rc=0
    deploy_tag "$prev_good" || rollback_rc=$?
    if [ "$rollback_rc" -ne 0 ]; then
      log "rollback to ${prev_good} failed (rc=${rollback_rc}); production state is uncertain"
      event exit
      return 4
    fi
    if health_probe; then
      # last_good_tag intentionally left at ${prev_good}; the bad tag is NOT promoted
      log "rollback to ${prev_good} complete; old version passed the same-budget health probe"
      event exit
      return 1
    fi
    log "rollback health probe FAILED for ${prev_good}; production state is uncertain"
    event exit
    return 4
  else
    log "no previous good tag to roll back to"
    event exit
    return 4
  fi
}

# --- busy-lock deploy gate (opt-in; BUSY_LOCK_FILE empty = skip entirely) ----
# 服务侧每个不可打断任务存续期间对这个文件持共享锁(LOCK_SH);这里在替换容器前
# 申请排他锁(LOCK_EX) —— 拿到即证明"无进行中任务,且新任务进不来"。
# 锁申请顺序固定:先忙锁(fd 8,服务级) 后 HOST_LOCK(fd 9,整机级)——每个服务的忙锁
# 文件互不相同,HOST_LOCK 全局只有一把,顺序固定的两级锁不构成环,不会死锁。
# 但"申请顺序固定"不等于"等待期间可以互相攥着":两把锁只应该在真正的替换窗口
# (即将 compose up 之前)同时持有;在等待阶段,任何时刻最多只持有正在等的那一把,
# 绝不允许"因为在等 HOST_LOCK,所以顺手一直攥着已经到手的忙锁"——那会让 admission
# 在纯排队等待、尚未开始替换容器的时间里被误关,反而伤到这套门禁本该保护的对象。
# 做法是一个循环:申请忙锁(带预算)→ 非阻塞探 HOST_LOCK → HOST_LOCK 被占就立即
# 放掉忙锁、sleep 5s 后重试整对锁 → 总预算(BUSY_LOCK_TIMEOUT)耗尽仍未能同时拿到
# 两把锁,则本次 deferred。
mkdir -p "$(dirname "$HOST_LOCK")" 2>/dev/null || true
exec 9>"$HOST_LOCK"

if [ -n "$BUSY_LOCK_FILE" ]; then
  if ! is_positive_integer "$BUSY_LOCK_TIMEOUT"; then
    log "BUSY_LOCK_TIMEOUT must be a positive integer, got: ${BUSY_LOCK_TIMEOUT}"
    exit 1
  fi
  # pre-pull outside all locks: shrinks the admission-closed window to seconds
  pull_image "${ACR_IMAGE}:${GIT_SHA}"
  if [ ! -e "$BUSY_LOCK_FILE" ]; then
    log "WARN: busy lock file ${BUSY_LOCK_FILE} missing — service side may not hold locks yet; creating it, proceeding WITHOUT drain protection"
    mkdir -p "$(dirname "$BUSY_LOCK_FILE")"
    : >> "$BUSY_LOCK_FILE"   # append-open 的空操作：绝不 truncate，只是把文件创建出来
  fi
  # 只读打开：服务侧(容器内进程)创建的锁文件通常属主是容器内用户/root、权限较窄
  # (如 0644),宿主上跑部署脚本的用户往往只有读权限、没有写权限。flock(2) 的互斥
  # 语义作用在文件的 inode 上,不要求持有该锁的 fd 具备写权限——只读 fd 一样能申请
  # LOCK_EX/LOCK_SH。这里改成只读打开以兼容"部署用户对锁文件只读"的真实场景，
  # 语义与之前的 append-open 完全一致，只是不再要求写权限。
  exec 8<"$BUSY_LOCK_FILE"

  _deadline=$(( SECONDS + BUSY_LOCK_TIMEOUT ))
  while :; do
    _remain=$(( _deadline - SECONDS ))
    if [ "$_remain" -le 0 ]; then
      log "service busy: host deploy lock busy through the whole ${BUSY_LOCK_TIMEOUT}s budget — DEFERRED, old container kept"
      exit 3
    fi
    _frc=0; flock -w "$_remain" -x 8 || _frc=$?
    if [ "$_frc" -eq 1 ]; then
      log "service busy: busy lock not acquired within budget — DEFERRED, old container kept"
      exit 3
    elif [ "$_frc" -ne 0 ]; then
      log "flock on busy lock failed with rc=${_frc} (not a lock timeout — config or host problem)"
      exit 1
    fi
    # 只有 rc=1 才是真正的锁冲突(host lock 被别的部署占着,应该走既有的放忙锁重试
    # 路径);其余非零值是 flock 命令本身出了问题(参数错误、系统调用异常等),不能
    # 被悄悄吞掉当成"服务忙"处理,必须报出真实错误——与上面忙锁的 flock rc 分流
    # 保持同一原则。
    _hrc=0; flock -n 9 || _hrc=$?
    if [ "$_hrc" -eq 0 ]; then
      break            # 两把锁同时在手 → 替换窗口开始
    fi
    if [ "$_hrc" -ne 1 ]; then
      log "flock on host lock failed (rc=${_hrc} — not lock contention)"
      exit 1
    fi
    flock -u 8         # 整机锁被别的部署占着:立即放掉忙锁,admission 重新打开
    # 重试前的休眠要 clamp 到"剩余预算"和 5s 两者中较小值:固定 sleep 5 在剩余预算
    # 不足 5 秒时(比如 BUSY_LOCK_TIMEOUT=2)会把总等待拖过用户配置的预算上限,
    # clamp 之后循环顶部的预算耗尽判断才能准时生效,deferred 不会被这一觉睡过头。
    _nap=$(( _deadline - SECONDS ))
    if [ "$_nap" -gt 5 ]; then _nap=5; fi
    if [ "$_nap" -gt 0 ]; then sleep "$_nap"; fi
  done
  log "busy lock + host deploy lock both acquired (admission closed until replace completes)"
fi

# opt-in 路径:fd 9 上面已经通过 flock -n 9 拿到锁了,这一行只是确认——同一进程对
# 同一 fd 重复 flock 是空操作,立即成功返回,不会阻塞。
# opt-out 路径:busy-lock if 块整体跳过,fd 9 尚未加锁,这一行就是原来的行为——
# 阻塞直到这台主机的部署锁空闲。
flock 9
do_deploy
rc=$?
flock -u 9
# fd 8(忙锁,若开启)必须活过整个 do_deploy()(含探针失败后的回滚),并且晚于
# fd 9 释放,才能保证 admission 在 compose up + 探针 + 回滚全程都是关闭的。
# 这里选择显式 flock -u 8(而不是依赖脚本 exit 时内核自动释放两把锁):两者都
# 安全(内核保证进程退出必然释放所有 flock),但显式释放让代码里的锁生命周期
# 一目了然,也让"9 先于 8 释放"的顺序不依赖读者去脑补 exit 的隐式行为。
[ -n "$BUSY_LOCK_FILE" ] && flock -u 8
exit $rc
