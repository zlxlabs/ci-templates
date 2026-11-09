#!/usr/bin/env bash
# Release config-file host logic. This file only defines functions; the caller
# sources it from the nonce-scoped path shipped with every release invocation.

release_files_init() {
  FILE_PATHS=()
  FILE_HASHES=()
  FILE_STAGE_DIR=""
  CURRENT_FILE_STAGE_DIR=""
  FORCE_RECREATE_SERVICES=()
  REPLACED_FILE_PATHS=()
  CURRENT_FILE_STAGE_FAILED=0
  ROLLBACK_CONFIG_FAILED=0
  declare -gA SEEN_FILE_PATHS=()
}

release_file_path_safe() {
  local value="$1" part basename
  [[ "$value" =~ ^[A-Za-z0-9._/-]+$ && "$value" != /* && "$value" != */ && "$value" != -* ]] || return 1
  local parts=()
  IFS='/' read -r -a parts <<< "$value"
  for part in "${parts[@]}"; do
    [[ -n "$part" && "$part" != "." && "$part" != ".." ]] || return 1
    [[ "$part" != ".deploy-state" && "$part" != ".git" ]] || return 1
  done
  basename="${parts[${#parts[@]}-1]}"
  case "$basename" in
    .env|ops.env|.d3-release.env|*.env|docker-compose.override.yml|docker-compose.override.yaml|compose.override.yml|compose.override.yaml) return 1 ;;
  esac
  return 0
}

is_compose_file_path() {
  case "${1##*/}" in
    docker-compose.yml|docker-compose.yaml|compose.yml|compose.yaml) return 0 ;;
    *) return 1 ;;
  esac
}

sha256_file() {
  local output
  output="$(sha256sum < "$1")" || return 1
  printf '%s\n' "${output%% *}"
}

verify_file_set() {
  local root="$1" relpath expected actual path i
  for ((i = 0; i < ${#FILE_PATHS[@]}; i++)); do
    relpath="${FILE_PATHS[$i]}"
    expected="${FILE_HASHES[$i]}"
    path="$root/$relpath"
    actual="missing"
    if [[ -f "$path" && ! -L "$path" ]]; then
      actual="$(sha256_file "$path")" || actual="unreadable"
    fi
    if [[ "$actual" == "$expected" ]]; then
      echo "[release][config-verify] path=$relpath sha256=$expected result=ok"
    else
      echo "[release][config-verify] path=$relpath expected=$expected actual=$actual result=mismatch" >&2
      echo "::error::release config verify mismatch path=$relpath expected=$expected actual=$actual" >&2
      return 1
    fi
  done
  return 0
}

prepare_release_files() {
  local sha="$1" tar_path="$2" stage_root="$3" cache_tar="${4:-}" listing="" member relpath path component
  [[ "${#FILE_PATHS[@]}" -gt 0 ]] || { FILE_STAGE_DIR=""; return 0; }
  [[ -f "$tar_path" && ! -L "$tar_path" ]] || { log "release files tar missing or not a regular file: $tar_path" >&2; return 1; }
  STAGING_DIRS+=("$stage_root")
  mkdir -p "$stage_root" || return 1
  listing="$(tar -tf "$tar_path")" || { log "could not list release files tar" >&2; return 1; }
  local members=()
  if [[ -n "$listing" ]]; then readarray -t members <<< "$listing"; fi
  [[ "${#members[@]}" -eq "${#FILE_PATHS[@]}" ]] || { log "release files tar member count does not match manifest" >&2; return 1; }
  declare -A seen_members=()
  for member in "${members[@]}"; do
    release_file_path_safe "$member" || { log "unsafe member in release files tar: $member" >&2; return 1; }
    [[ -z "${seen_members[$member]+x}" ]] || { log "duplicate member in release files tar: $member" >&2; return 1; }
    seen_members["$member"]=1
  done
  for relpath in "${FILE_PATHS[@]}"; do
    [[ -n "${seen_members[$relpath]+x}" ]] || { log "release files tar is missing manifest member: $relpath" >&2; return 1; }
    path="$stage_root"
    IFS='/' read -r -a components <<< "$relpath"
    for component in "${components[@]::${#components[@]}-1}"; do
      path="$path/$component"
      [[ ! -L "$path" ]] || { log "symlink in release files staging path: $relpath" >&2; return 1; }
    done
    tar --no-same-owner --no-same-permissions -xf "$tar_path" -C "$stage_root" -- "$relpath" || { log "could not extract release file: $relpath" >&2; return 1; }
    path="$stage_root/$relpath"
    [[ -f "$path" && ! -L "$path" ]] || { log "release tar member is not a regular file: $relpath" >&2; return 1; }
  done
  FILE_STAGE_DIR="$stage_root"
  verify_file_set "$FILE_STAGE_DIR" || return 1
  if [[ -n "$cache_tar" ]]; then
    mkdir -p "$STATE_DIR/files" || return 1
    [[ ! -L "$cache_tar" ]] || { log "release files cache path is a symlink: $cache_tar" >&2; return 1; }
    cp -- "$tar_path" "$cache_tar" || return 1
  fi
  return 0
}

verify_live_files() {
  [[ "${#FILE_PATHS[@]}" -gt 0 ]] || return 0
  verify_file_set "$DEPLOY_DIR"
}

install_release_files() {
  local sha="$1" i relpath staged target parent parent_real deploy_real basename temp
  REPLACED_FILE_PATHS=(); FORCE_RECREATE_SERVICES=()
  [[ "${#FILE_PATHS[@]}" -gt 0 ]] || return 0
  [[ -n "$FILE_STAGE_DIR" ]] || { log "release file staging directory is unavailable" >&2; return 1; }
  deploy_real="$(realpath -e -- "$DEPLOY_DIR")" || { log "deploy directory cannot be resolved" >&2; return 1; }
  for ((i = 0; i < ${#FILE_PATHS[@]}; i++)); do
    relpath="${FILE_PATHS[$i]}"
    staged="$FILE_STAGE_DIR/$relpath"
    target="$DEPLOY_DIR/$relpath"
    parent="${target%/*}"
    basename="${target##*/}"
    if [[ -L "$target" ]]; then
      echo "[release][config-install] path=$relpath result=symlink-refused" >&2
      return 1
    fi
    [[ -d "$parent" ]] || {
      echo "[release][config-install] path=$relpath result=not-writable" >&2
      return 1
    }
    parent_real="$(realpath -e -- "$parent")" || {
      echo "[release][config-install] path=$relpath result=not-writable" >&2
      return 1
    }
    [[ "$parent_real" == "$deploy_real" || "$parent_real" == "$deploy_real/"* ]] || {
      log "release config target escapes deploy directory: $relpath" >&2
      return 1
    }
    if [[ ! -w "$parent_real" || ( -e "$target" && ! -f "$target" ) ]]; then
      echo "[release][config-install] path=$relpath result=not-writable" >&2
      return 1
    fi
    temp="$parent_real/.${basename}.${sha}.tmp"
    if [[ -e "$temp" || -L "$temp" ]]; then
      echo "[release][config-install] path=$relpath result=not-writable" >&2
      return 1
    fi
    cp -- "$staged" "$temp" || {
      rm -f -- "$temp"
      echo "[release][config-install] path=$relpath result=not-writable" >&2
      return 1
    }
    mv -f -- "$temp" "$target" || {
      rm -f -- "$temp"
      echo "[release][config-install] path=$relpath result=not-writable" >&2
      return 1
    }
    MUTATED=1
    echo "[release][config-install] path=$relpath result=replaced"
    if ! is_compose_file_path "$relpath"; then REPLACED_FILE_PATHS+=("$relpath"); fi
  done
  return 0
}

find_bound_services() {
  local running="" container output service source source_real target target_real relpath
  FORCE_RECREATE_SERVICES=()
  [[ "${#REPLACED_FILE_PATHS[@]}" -gt 0 ]] || return 0
  running="$("$DOCKER_BIN" ps -q)" || { log "could not list running containers for bind-mount detection" >&2; return 1; }
  declare -A seen_services=()
  while IFS= read -r container; do
    [[ -n "$container" ]] || continue
    output="$("$DOCKER_BIN" inspect "$container" --format '{{index .Config.Labels "com.docker.compose.service"}}{{range .Mounts}}{{printf "\\n%s" .Source}}{{end}}')" || {
      log "docker inspect failed during bind-mount detection for $container" >&2
      return 1
    }
    [[ -n "$output" ]] || { log "docker inspect returned no bind-mount metadata for $container" >&2; return 1; }
    local inspect_lines=()
    readarray -t inspect_lines <<< "$output"
    service="${inspect_lines[0]:-}"
    for source in "${inspect_lines[@]:1}"; do
      [[ -n "$source" ]] || continue
      source_real="$(realpath -e -- "$source")" || { log "could not resolve inspected mount source: $source" >&2; return 1; }
      for relpath in "${REPLACED_FILE_PATHS[@]}"; do
        target="$DEPLOY_DIR/$relpath"
        target_real="$(realpath -e -- "$target")" || { log "could not resolve replaced release file: $relpath" >&2; return 1; }
        if [[ "$source_real" == "$target_real" ]]; then
          [[ "$service" =~ ^[A-Za-z0-9_.-]+$ ]] || {
            log "running container $container has no valid docker compose service label" >&2
            return 1
          }
          if [[ -z "${seen_services[$service]+x}" ]]; then
            seen_services["$service"]=1
            FORCE_RECREATE_SERVICES+=("$service")
          fi
        fi
      done
    done
  done <<< "$running"
  return 0
}

report_compose_override() {
  if [[ -f "$DEPLOY_DIR/docker-compose.override.yml" ]]; then
    echo "[release][config-override] path=docker-compose.override.yml result=detected-untouched"
  else
    echo "[release][config-override] path=docker-compose.override.yml result=absent"
  fi
}

compose_release() {
  local tag="$1" env_tmp="${STAGING_PREFIX}.env"
  check_pending || return 130
  printf 'D3_RELEASE_TAG=%s\n' "$tag" > "$env_tmp" || return 1
  mv -f -- "$env_tmp" "$ENV_FILE" || return 1
  local compose_args=(compose)
  if [[ -f "$DEPLOY_DIR/.env" ]]; then compose_args+=(--env-file "$DEPLOY_DIR/.env"); fi
  compose_args+=(--env-file "$ENV_FILE")
  local rendered_images config_rc=0 image_ref found_any line
  rendered_images="$(cd "$DEPLOY_DIR" && D3_RELEASE_TAG="$tag" "$DOCKER_BIN" "${compose_args[@]}" config --images 2>&1)" || config_rc=$?
  if (( config_rc != 0 )); then
    log "compose config --images failed; compose up will not run" >&2
    return 1
  fi
  for image_name in "${IMAGE_NAMES[@]}"; do
    image_ref="${image_name}:${tag}"
    found_any=0
    while IFS= read -r line; do
      [[ -n "$line" ]] || continue
      if [[ "$line" == "${image_name}:"* ]]; then
        found_any=1
        if [[ "$line" != "$image_ref" ]]; then
          log "compose identity gate: ${image_name} has a stray reference (${line}), expected ${image_ref}; compose up will not run" >&2
          return 1
        fi
      fi
    done <<< "$rendered_images"
    if (( found_any == 0 )); then
      log "compose identity gate missing ${image_ref}; compose up will not run" >&2
      return 1
    fi
  done
  check_pending || return 130
  if [[ "$ROLLBACK_MODE" -eq 0 ]]; then validate_oneshot_services "$tag" || return 1; fi
  local rollback_services=() _svc_list="" _list_rc=0
  if [[ "$ROLLBACK_MODE" -eq 1 && -n "$ONESHOT_SERVICES" ]]; then
    _svc_list="$(rollback_compose_services "$tag")" || _list_rc=$?
    (( _list_rc == 0 )) || return 1
    readarray -t rollback_services <<< "$_svc_list"
  fi
  compose_args+=(up -d)
  if [[ "${#rollback_services[@]}" -gt 0 ]]; then compose_args+=("${rollback_services[@]}"); fi
  local compose_rc=0
  (cd "$DEPLOY_DIR" || exit 200; D3_RELEASE_TAG="$tag" "$DOCKER_BIN" "${compose_args[@]}") || compose_rc=$?
  (( compose_rc != 200 )) && MUTATED=1
  (( compose_rc == 0 )) || return "$compose_rc"
  check_pending || return 130
  if [[ "${#FORCE_RECREATE_SERVICES[@]}" -gt 0 ]]; then
    local selected_services=() svc allowed rollback_svc
    for svc in "${FORCE_RECREATE_SERVICES[@]}"; do
      allowed=1
      if [[ "${#rollback_services[@]}" -gt 0 ]]; then
        allowed=0
        for rollback_svc in "${rollback_services[@]}"; do [[ "$svc" == "$rollback_svc" ]] && allowed=1; done
      fi
      [[ "$allowed" -eq 1 ]] && selected_services+=("$svc")
    done
    if [[ "${#selected_services[@]}" -gt 0 ]]; then
      local force_args=(compose)
      if [[ -f "$DEPLOY_DIR/.env" ]]; then force_args+=(--env-file "$DEPLOY_DIR/.env"); fi
      force_args+=(--env-file "$ENV_FILE" up -d --no-deps --force-recreate "${selected_services[@]}")
      compose_rc=0
      (cd "$DEPLOY_DIR" || exit 200; D3_RELEASE_TAG="$tag" "$DOCKER_BIN" "${force_args[@]}") || compose_rc=$?
      (( compose_rc != 200 )) && MUTATED=1
      (( compose_rc == 0 )) || return "$compose_rc"
    fi
  fi
  check_pending || return 130
  return 0
}

deploy_group() {
  local sha="$1" manifest="$2" staged="${3:-0}" prefer_local="${4:-0}"
  load_manifest "$manifest" || return 1
  check_pending || return 130
  if [[ "$sha" == "$D3_RELEASE_TAG" && "$staged" -eq 1 ]]; then FILE_STAGE_DIR="$CURRENT_FILE_STAGE_DIR"; fi
  if [[ "${#FILE_PATHS[@]}" -gt 0 && -z "$FILE_STAGE_DIR" ]]; then
    local stage_root="$STATE_DIR/.release-${sha}-$$"
    if [[ "$ROLLBACK_MODE" -eq 1 ]]; then
      if ! prepare_release_files "$sha" "$STATE_DIR/files/${sha}.tar" "$stage_root/files"; then
        ROLLBACK_CONFIG_FAILED=1
        echo "[release][config-rollback] sha=$sha result=missing-tar" >&2
        return 4
      fi
    else
      prepare_release_files "$sha" "$REMOTE_FILES_TAR" "$stage_root/files" || return 1
    fi
  fi
  if [[ "$ROLLBACK_MODE" -eq 1 && "${#FILE_PATHS[@]}" -eq 0 ]]; then
    echo "[release][config-rollback] sha=$sha result=skipped-no-file-records"
  fi
  if [[ "$staged" -eq 0 ]]; then pull_and_retag "$sha" "$prefer_local" || return 1; fi
  install_release_files "$sha" || return 1
  verify_live_files || return 1
  if [[ "$ROLLBACK_MODE" -eq 1 && "${#FILE_PATHS[@]}" -gt 0 ]]; then echo "[release][config-rollback] sha=$sha result=restored"; fi
  find_bound_services || return 1
  report_compose_override
  compose_release "$sha" || return 1
}

stage_current_release() {
  check_pending || return 130
  load_manifest "$RELEASE_MANIFEST" || return 1
  check_pending || return 130
  pull_and_retag "$D3_RELEASE_TAG" 0 || return 1
  check_pending || return 130
  if [[ "${#FILE_PATHS[@]}" -gt 0 ]]; then
    local stage_root="$STATE_DIR/.release-${D3_RELEASE_TAG}-$$"
    if ! prepare_release_files "$D3_RELEASE_TAG" "$REMOTE_FILES_TAR" "$stage_root/files" "$STATE_DIR/files/${D3_RELEASE_TAG}.tar"; then
      CURRENT_FILE_STAGE_FAILED=1
    else
      CURRENT_FILE_STAGE_DIR="$FILE_STAGE_DIR"
    fi
  else
    FILE_STAGE_DIR=""
  fi
  CURRENT_STAGED=1
}

release_files_deploy_current() {
  if [[ "$CURRENT_FILE_STAGE_FAILED" -eq 1 ]]; then
    log "release file staging failed before live mutation" >&2
    return 1
  fi
  deploy_group "$D3_RELEASE_TAG" "$RELEASE_MANIFEST" "$CURRENT_STAGED"
}

release_files_skip_forward() {
  [[ "$CURRENT_FILE_STAGE_FAILED" -eq 0 ]] || return 1
  if verify_live_files; then return 0; fi
  log "same-SHA live release files mismatched; skip-forward is forbidden, redeploying"
  return 1
}

release_files_post_probe() { verify_live_files; }
