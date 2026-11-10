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
# 留空(默认)= 禁用本地 registry,只推送 ACR。
LOCAL_REGISTRY="${LOCAL_REGISTRY:-}"
BUILDER_NAME="ci-templates-registry-cache"

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

# 校验 LOCAL_REGISTRY 的形状:只接受 host[:port],拒绝协议前缀(http://)、凭据
# (user:pass@host)、多余路径(host:port/extra)——这些一旦混进下面的
# ${LOCAL_REGISTRY}/${ACR_NAMESPACE}/${IMAGE_NAME} 镜像引用,凭据会连同 docker
# tag/push 命令行和 warning 文本一起被打进构建日志,发生泄漏。错误信息里绝不
# 回显原值,只说期望格式,否则校验本身就成了泄漏点。
#
# 首尾字符必须是字母/数字(OCR round-2 finding #7/#4,high):不锚定首字符的话,
# "--evil-flag" 这类值能通过 `[A-Za-z0-9.-]+`,拼进 docker tag/push 的镜像引用后,
# 会被 docker CLI 当成一个以 "-" 开头的参数解析成 flag,而不是镜像名——是真实的
# CLI flag 注入面。与 scripts/pull_and_deploy.sh 里同一个正则同步修。
is_valid_registry_host() {
  [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]+)?$ ]]
}

if [ -n "$LOCAL_REGISTRY" ] && ! is_valid_registry_host "$LOCAL_REGISTRY"; then
  echo "::error::local_registry has an invalid shape — expected host[:port] only (no scheme prefix like http://, no credentials, no path)" >&2
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

print_toolchain_fingerprint() {
  local version_info docker_client=unknown docker_server=unknown buildx_version=unknown storage_driver=unknown

  if version_info="$("$DOCKER_BIN" version --format 'client={{.Client.Version}} server={{.Server.Version}}' 2>/dev/null)" \
    && [[ "$version_info" == client=*' server='* ]]; then
    docker_client="${version_info#client=}"
    docker_server="${docker_client#* server=}"
    docker_client="${docker_client%% server=*}"
    [ -n "$docker_client" ] || docker_client=unknown
    [ -n "$docker_server" ] || docker_server=unknown
  fi
  if buildx_version="$("$DOCKER_BIN" buildx version 2>/dev/null)" && [ -n "$buildx_version" ]; then
    :
  else
    buildx_version=unknown
  fi
  if storage_driver="$("$DOCKER_BIN" info --format '{{.Driver}}' 2>/dev/null)" && [ -n "$storage_driver" ]; then
    :
  else
    storage_driver=unknown
  fi

  echo "[push] toolchain: docker_client=${docker_client} docker_server=${docker_server} buildx=${buildx_version} storage_driver=${storage_driver}"
}

# docker manifest inspect exits non-zero both for an absent tag and for registry
# failures. Only Docker's explicit not-found response means safe-to-build.
probe_acr_sha() {
  local ref="$1" output rc
  if output="$("$DOCKER_BIN" manifest inspect "$ref" 2>&1)"; then
    return 0
  else
    rc=$?
  fi

  if [[ "$output" == *"no such manifest: ${ref}"* ]]; then
    return 1
  fi

  echo "::error::ACR-SHA-PROBE-FAILED ${ref} (docker manifest inspect rc=${rc})"
  return 2
}

build_classic_docker_image() {
  "$DOCKER_BIN" build \
    --provenance=false \
    --sbom=false \
    --build-arg "GIT_SHA=${GIT_SHA}" \
    -f "${BUILD_CONTEXT}/${DOCKERFILE}" \
    -t "${ACR_IMAGE}:${GIT_SHA}" \
    "${BUILD_CONTEXT}"
}

# Registry cache build uses one stable builder and falls back without cache on any buildx failure.
build_registry_cached_image() {
  CACHE_REF="${LOCAL_REGISTRY}/${ACR_NAMESPACE}/${IMAGE_NAME}:buildcache"

  if ! "$DOCKER_BIN" buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
    if ! "$DOCKER_BIN" buildx create --name "$BUILDER_NAME" --driver docker-container --bootstrap >/dev/null 2>&1; then
      echo "::warning::buildx builder initialization failed; falling back to classic docker build"
      build_classic_docker_image
      return
    fi
  fi

  if ! "$DOCKER_BIN" buildx build \
    --builder "$BUILDER_NAME" \
    --provenance=false \
    --sbom=false \
    --build-arg "GIT_SHA=${GIT_SHA}" \
    -f "${BUILD_CONTEXT}/${DOCKERFILE}" \
    -t "${ACR_IMAGE}:${GIT_SHA}" \
    --cache-from "type=registry,ref=${CACHE_REF}" \
    --cache-to "type=registry,ref=${CACHE_REF},mode=max,ignore-error=true" \
    --load \
    "${BUILD_CONTEXT}"; then
    echo "::warning::buildx build failed; falling back to classic docker build"
    build_classic_docker_image
  fi
}

print_toolchain_fingerprint
sha_ref="${ACR_IMAGE}:${GIT_SHA}"
sha_exists=0
if probe_acr_sha "$sha_ref"; then
  sha_exists=1
  echo "[push] ACR tag already exists → reuse: ${sha_ref}"
  "$DOCKER_BIN" pull "$sha_ref"
else
  probe_rc=$?
  if [ "$probe_rc" -ne 1 ]; then
    exit 1
  fi
  echo "[push] building ${sha_ref}"
  if [ -n "$LOCAL_REGISTRY" ]; then
    build_registry_cached_image
  else
    build_classic_docker_image
  fi
fi

# 本地 registry 是可选的拉取加速路径；ACR SHA tag 是必须的归档，推送失败始终致命。
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

acr_ok=1
if [ "$sha_exists" -eq 0 ]; then
  acr_ok=0
  if push_with_retry "${ACR_IMAGE}:${GIT_SHA}" "$PUSH_MAX_ATTEMPTS"; then
    acr_ok=1
  fi
fi

if [ "$acr_ok" -ne 1 ]; then
  echo "::error::push failed for ${GIT_SHA} to every configured registry — image not available anywhere"
  exit 1
fi

if [ "$local_enabled" -eq 1 ]; then
  echo "[push] done: ${ACR_IMAGE}:${GIT_SHA} (local registry $([ "$local_ok" -eq 1 ] && echo ok || echo failed), ACR $([ "$acr_ok" -eq 1 ] && echo ok || echo failed))"
else
  echo "[push] done: ${ACR_IMAGE}:${GIT_SHA}"
fi
