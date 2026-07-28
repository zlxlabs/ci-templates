#!/bin/bash
# Build + push to ACR with an IMMUTABLE git-SHA tag (build-deploy.yml internals).
#
# Hardened over the docker-package skill's push_to_acr.sh: the image is tagged
# with the git SHA so deploys and rollbacks pin an exact, immutable artifact.
set -euo pipefail

: "${ACR_REGISTRY:?ACR_REGISTRY required}"
: "${ACR_NAMESPACE:?ACR_NAMESPACE required}"
: "${IMAGE_NAME:?IMAGE_NAME required}"
: "${GIT_SHA:?GIT_SHA required}"

BUILD_CONTEXT="${BUILD_CONTEXT:-.}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
ACR_IMAGE="${ACR_REGISTRY}/${ACR_NAMESPACE}/${IMAGE_NAME}"
PUSH_TIMEOUT_SECONDS="${PUSH_TIMEOUT_SECONDS:-300}"
PUSH_TIMEOUT_KILL_AFTER_SECONDS="${PUSH_TIMEOUT_KILL_AFTER_SECONDS:-15}"
PUSH_MAX_ATTEMPTS="${PUSH_MAX_ATTEMPTS:-3}"
PUSH_RETRY_DELAY_SECONDS="${PUSH_RETRY_DELAY_SECONDS:-10}"

# opt-in 本地网络 registry 镜像(host:port,如 tailnet MagicDNS 名):部署关键路径。
# 留空(默认)= 禁用,下面的双推逻辑整体退化成改动前的纯 ACR 单推,行为逐字节不变。
LOCAL_REGISTRY="${LOCAL_REGISTRY:-}"

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

is_non_negative_integer() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

if ! is_positive_integer "$PUSH_TIMEOUT_SECONDS"; then
  echo "PUSH_TIMEOUT_SECONDS must be a positive integer, got: $PUSH_TIMEOUT_SECONDS" >&2
  exit 2
fi
if [ "$PUSH_TIMEOUT_SECONDS" -gt 300 ]; then
  echo "PUSH_TIMEOUT_SECONDS must not exceed 300, got: $PUSH_TIMEOUT_SECONDS" >&2
  exit 2
fi
if ! is_positive_integer "$PUSH_TIMEOUT_KILL_AFTER_SECONDS"; then
  echo "PUSH_TIMEOUT_KILL_AFTER_SECONDS must be a positive integer, got: $PUSH_TIMEOUT_KILL_AFTER_SECONDS" >&2
  exit 2
fi
if [ "$PUSH_TIMEOUT_KILL_AFTER_SECONDS" -gt 15 ]; then
  echo "PUSH_TIMEOUT_KILL_AFTER_SECONDS must not exceed 15, got: $PUSH_TIMEOUT_KILL_AFTER_SECONDS" >&2
  exit 2
fi
if ! is_positive_integer "$PUSH_MAX_ATTEMPTS"; then
  echo "PUSH_MAX_ATTEMPTS must be a positive integer, got: $PUSH_MAX_ATTEMPTS" >&2
  exit 2
fi
if [ "$PUSH_MAX_ATTEMPTS" -gt 3 ]; then
  echo "PUSH_MAX_ATTEMPTS must not exceed 3, got: $PUSH_MAX_ATTEMPTS" >&2
  exit 2
fi
if ! is_non_negative_integer "$PUSH_RETRY_DELAY_SECONDS"; then
  echo "PUSH_RETRY_DELAY_SECONDS must be a non-negative integer, got: $PUSH_RETRY_DELAY_SECONDS" >&2
  exit 2
fi

# An immutable SHA push is safe to retry: it always points to the same image
# bytes. Bound every attempt so a stalled blob upload cannot keep a deployment
# job silent for tens of minutes. Do not publish a mutable registry `latest`:
# the deploy host retags the verified SHA locally for compose compatibility.
push_with_retry() {
  local tag="$1"
  local max_attempts="$2"
  local attempt=1 rc=0

  while true; do
    echo "[push] pushing ${tag} (attempt ${attempt}/${max_attempts}, timeout ${PUSH_TIMEOUT_SECONDS}s)"
    # Do not use timeout --foreground: it would leave docker's child processes
    # alive and holding the Actions log pipe after the client is terminated.
    if timeout --kill-after="${PUSH_TIMEOUT_KILL_AFTER_SECONDS}s" \
      "${PUSH_TIMEOUT_SECONDS}s" "$DOCKER_BIN" push "$tag"; then
      return 0
    else
      rc=$?
    fi

    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
      echo "::warning::push ${tag} timed out after ${PUSH_TIMEOUT_SECONDS}s"
    else
      echo "::warning::push ${tag} failed (rc=${rc})"
    fi

    if [ "$attempt" -ge "$max_attempts" ]; then
      echo "::error::push ${tag} failed after ${max_attempts} attempts"
      return 1
    fi

    attempt=$((attempt + 1))
    echo "[push] retrying ${tag} in ${PUSH_RETRY_DELAY_SECONDS}s"
    sleep "$PUSH_RETRY_DELAY_SECONDS"
  done
}

echo "[push] building ${ACR_IMAGE}:${GIT_SHA}"
"$DOCKER_BIN" build \
  --build-arg "GIT_SHA=${GIT_SHA}" \
  -f "${BUILD_CONTEXT}/${DOCKERFILE}" \
  -t "${ACR_IMAGE}:${GIT_SHA}" \
  "${BUILD_CONTEXT}"

# 双推:本地 registry(部署关键路径,opt-in) + ACR(异地存档 + 拉取回退,pull_and_deploy.sh
# 的 pull_from_local_registry() 失败时会退回它)。LOCAL_REGISTRY 为空(默认)时
# local_enabled 恒为 0,下面的致命判断退化为"ACR 失败就报错退出"—— 与改动前逐字节一致。
local_enabled=0
local_ok=0
if [ -n "$LOCAL_REGISTRY" ]; then
  local_enabled=1
  LOCAL_IMAGE="${LOCAL_REGISTRY}/${ACR_NAMESPACE}/${IMAGE_NAME}"
  if "$DOCKER_BIN" tag "${ACR_IMAGE}:${GIT_SHA}" "${LOCAL_IMAGE}:${GIT_SHA}"; then
    if push_with_retry "${LOCAL_IMAGE}:${GIT_SHA}" "$PUSH_MAX_ATTEMPTS"; then
      local_ok=1
    else
      echo "::warning::local registry push failed for ${LOCAL_IMAGE}:${GIT_SHA} — this deploy will fall back to ACR-only for this SHA"
    fi
  else
    echo "::warning::local retag to ${LOCAL_IMAGE}:${GIT_SHA} failed — skipping local registry push, falling back to ACR-only for this SHA"
  fi
fi

acr_ok=0
if push_with_retry "${ACR_IMAGE}:${GIT_SHA}" "$PUSH_MAX_ATTEMPTS"; then
  acr_ok=1
elif [ "$local_enabled" -eq 1 ]; then
  echo "::warning::ACR push failed for ${ACR_IMAGE}:${GIT_SHA} — offsite archive/fallback unavailable for this SHA"
fi

# 致命条件:ACR 失败,且(本地未启用 或 本地也失败)——覆盖默认路径(本地关闭时
# ACR 失败必须致命,逐字节复刻改动前行为)和 opt-in 路径(两处都失败才致命;
# 单边失败允许降级继续,只是丢掉对应那份保障,已在上面打过 warning)。
fatal=0
if [ "$acr_ok" -ne 1 ]; then
  if [ "$local_enabled" -eq 0 ] || [ "$local_ok" -ne 1 ]; then
    fatal=1
  fi
fi
if [ "$fatal" -eq 1 ]; then
  echo "::error::push failed for ${GIT_SHA} to every configured registry — image not available anywhere"
  exit 1
fi

if [ "$local_enabled" -eq 1 ]; then
  echo "[push] done: ${ACR_IMAGE}:${GIT_SHA} (local registry $([ "$local_ok" -eq 1 ] && echo ok || echo failed), ACR $([ "$acr_ok" -eq 1 ] && echo ok || echo failed))"
else
  echo "[push] done: ${ACR_IMAGE}:${GIT_SHA}"
fi
