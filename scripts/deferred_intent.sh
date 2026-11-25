# Sourced by pull_and_deploy.sh when INTENT_FILE_ENABLED=true.
# Do not execute this file directly.
#
# sanitize_deploy_dir is the only definition of the deploy_dir stem in
# script_ref. Rule: strip leading slashes; replace every char not in
# [A-Za-z0-9._-] with _; append '-' and the first 8 hex chars of
# sha256(original deploy_dir).
# script_ref = pull_and_deploy.<stem>.r<run_number>a<run_attempt>.sh
# so each intent binds an immutable script file and never overwrites another
# intent's bytes. The caller must set D3_DEPLOY_SCRIPT; this file does not guess.
sanitize_deploy_dir() {
  local raw="${1-}" stripped c i sanitized digest
  stripped="$raw"
  while [ "${stripped#/}" != "$stripped" ]; do
    stripped="${stripped#/}"
  done
  sanitized=""
  i=0
  while [ "$i" -lt "${#stripped}" ]; do
    c="${stripped:$i:1}"
    case "$c" in
      [A-Za-z0-9._-]) sanitized="${sanitized}${c}" ;;
      *) sanitized="${sanitized}_" ;;
    esac
    i=$((i + 1))
  done
  digest="$(printf '%s' "$raw" | sha256sum)" || return 1
  digest="${digest%% *}"
  printf '%s-%s\n' "$sanitized" "${digest:0:8}"
}

_intent_write_failed() {
  echo "[deploy][evidence] intent: write-failed"
}

_append_intent_event() {
  local events_file="$1" line="$2"
  printf '%s\n' "$line" >> "$events_file" || return 1
}

# Install the executing deploy script bytes to ~/.d3-converge/scripts/<script_ref>
# (0700, tmp + mv). src must be an explicit path from the caller.
_install_current_deploy_script() {
  local scripts_dir="$1" dest="$2" src="$3" companion_dest="$4" companion_src="$5" tmp
  mkdir -p "$scripts_dir" || return 1
  chmod 0700 "$scripts_dir" || return 1
  [ -n "$src" ] && [ -f "$src" ] && [ -r "$src" ] || return 1
  [ -n "$companion_src" ] && [ -f "$companion_src" ] && [ -r "$companion_src" ] || return 1
  tmp="$(mktemp "${scripts_dir}/.script.XXXXXX")" || return 1
  if ! cp -- "$src" "$tmp"; then
    rm -f -- "$tmp" || true
    return 1
  fi
  if ! chmod 0700 "$tmp"; then
    rm -f -- "$tmp" || true
    return 1
  fi
  if ! mv -f -- "$tmp" "$dest"; then
    rm -f -- "$tmp" || true
    return 1
  fi
  tmp="$(mktemp "${scripts_dir}/.script.XXXXXX")" || { rm -f -- "$dest" || true; return 1; }
  if ! cp -- "$companion_src" "$tmp" || ! chmod 0700 "$tmp" || ! mv -f -- "$tmp" "$companion_dest"; then
    rm -f -- "$tmp" "$dest" || true
    return 1
  fi
  return 0
}

_owned_script_ref() {
  local name="$1"
  [[ "$name" != */* ]] && [[ "$name" =~ ^pull_and_deploy\.[A-Za-z0-9._-]+\.r[0-9]+a[0-9]+\.sh$ ]]
}

_remove_replaced_script() {
  local scripts_dir="$1" old_ref="$2" new_ref="$3"
  [ -n "$old_ref" ] || return 0
  [ "$old_ref" != "$new_ref" ] || return 0
  _owned_script_ref "$old_ref" || return 0
  rm -f -- "${scripts_dir}/${old_ref}" || true
  local companion_ref="deferred_intent.${old_ref#pull_and_deploy.}"
  rm -f -- "${scripts_dir}/${companion_ref}" || true
}

_fail_after_install() {
  local dest="$1" companion_dest="$2"
  rm -f -- "$dest" "$companion_dest" || true
  _intent_write_failed
}

validate_deferred_intent_config() {
  if [ -z "$BUSY_LOCK_FILE" ]; then
    log "INTENT_FILE_ENABLED=true requires BUSY_LOCK_FILE (no busy lock means no deferred path)"
    exit 1
  fi
  if ! is_positive_integer "${D3_RUN_NUMBER:-}"; then
    log "INTENT_FILE_ENABLED=true requires D3_RUN_NUMBER to be a positive integer"
    exit 1
  fi
  if ! is_positive_integer "${D3_RUN_ATTEMPT:-}"; then
    log "INTENT_FILE_ENABLED=true requires D3_RUN_ATTEMPT to be a positive integer"
    exit 1
  fi
}

_release_intent_lock() {
  flock -u 7
  exec 7>&-
}

# Critical section: caller already holds intents.lock (fd 7).
# Install this intent's script (unique script_ref), then append the event,
# then replace the intent. Any step failure deletes only the new script and
# leaves the published intent + its script unchanged. superseded does not install.
# After a successful replace, delete the displaced intent's script_ref in-lock.
_write_deferred_intent_locked() {
  local existing_run existing_attempt now notify_json run_id_json result
  local event_line tmp script_ref companion_ref scripts_dir dest companion_dest old_script_ref=""

  if [ -f "$intent_path" ]; then
    existing_run="$(sed -n 's/.*"run_number":\([0-9][0-9]*\).*/\1/p' "$intent_path" 2>/dev/null)" || existing_run=""
    existing_run="${existing_run%%$'\n'*}"
    existing_attempt="$(sed -n 's/.*"run_attempt":\([0-9][0-9]*\).*/\1/p' "$intent_path" 2>/dev/null)" || existing_attempt=""
    existing_attempt="${existing_attempt%%$'\n'*}"
    old_script_ref="$(sed -n 's/.*"script_ref":"\([^"]*\)".*/\1/p' "$intent_path" 2>/dev/null)" || old_script_ref=""
    old_script_ref="${old_script_ref%%$'\n'*}"
    if is_positive_integer "$existing_run" && is_positive_integer "$existing_attempt"; then
      if [ "$existing_run" -gt "$run_number" ] || { [ "$existing_run" -eq "$run_number" ] && [ "$existing_attempt" -gt "$run_attempt" ]; }; then
        now="$(date -u '+%Y-%m-%dT%H:%M:%SZ')" || now=""
        printf -v event_line '{"event":"superseded","run_number":%s,"run_attempt":%s,"kept_run_number":%s,"kept_run_attempt":%s,"at":%s}' \
          "$run_number" "$run_attempt" "$existing_run" "$existing_attempt" "$(json_quote "$now")"
        if ! _append_intent_event "$events_file" "$event_line"; then
          _intent_write_failed
          return 0
        fi
        echo "[deploy][evidence] intent: superseded"
        return 0
      fi
      if [ "$existing_run" -eq "$run_number" ] && [ "$existing_attempt" -eq "$run_attempt" ]; then
        echo "[deploy][evidence] intent: accepted"
        return 0
      fi
    fi
  fi

  script_ref="pull_and_deploy.${stem}.r${run_number}a${run_attempt}.sh"
  companion_ref="deferred_intent.${stem}.r${run_number}a${run_attempt}.sh"
  scripts_dir="${converge_dir}/scripts"
  dest="${scripts_dir}/${script_ref}"
  companion_dest="${scripts_dir}/${companion_ref}"
  if ! _install_current_deploy_script "$scripts_dir" "$dest" "${D3_DEPLOY_SCRIPT-}" "$companion_dest" "${BASH_SOURCE[0]}"; then
    _intent_write_failed
    return 0
  fi

  now="$(date -u '+%Y-%m-%dT%H:%M:%SZ')" || now=""
  if [ "${D3_NOTIFY_ON_SUCCESS:-}" = "true" ]; then
    notify_json=true
  else
    notify_json=false
  fi
  if is_non_negative_integer "${D3_RUN_ID:-}"; then
    run_id_json="$D3_RUN_ID"
  else
    run_id_json="$(json_quote "${D3_RUN_ID-}")"
  fi

  printf -v result '{"schema_version":1,"status":"pending","repo":%s,"run_number":%s,"run_attempt":%s,"run_id":%s,"run_url":%s,"git_sha":%s,"script_ref":%s,"created_at":%s,"updated_at":%s,"attempts":0,"last_attempt_at":null,"last_alert_at":null,"notify_on_success":%s,"webhook":%s,"deploy_env":{"IMAGE_NAME":%s,"ACR_IMAGE":%s,"LOCAL_IMAGE":%s,"GIT_SHA":%s,"DEPLOY_DIR":%s,"HOST_LOCK":%s,"HEALTHCHECK_URL":%s,"HEALTHCHECK_EXPECT_STATUS":%s,"HEALTHCHECK_RETRIES":%s,"HEALTHCHECK_INTERVAL":%s,"HEALTHCHECK_WARMUP":%s,"ROLLBACK_SAFETY":%s,"BUSY_LOCK_FILE":%s,"BUSY_LOCK_TIMEOUT":%s,"ONESHOT_SERVICES":%s}}' \
    "$(json_quote "${D3_REPO-}")" \
    "$run_number" \
    "$run_attempt" \
    "$run_id_json" \
    "$(json_quote "${D3_RUN_URL-}")" \
    "$(json_quote "$GIT_SHA")" \
    "$(json_quote "$script_ref")" \
    "$(json_quote "$now")" \
    "$(json_quote "$now")" \
    "$notify_json" \
    "$(json_quote "${D3_INTENT_WEBHOOK-}")" \
    "$(json_quote "${IMAGE_NAME-}")" \
    "$(json_quote "${ACR_IMAGE-}")" \
    "$(json_quote "${LOCAL_IMAGE-}")" \
    "$(json_quote "${GIT_SHA-}")" \
    "$(json_quote "${DEPLOY_DIR-}")" \
    "$(json_quote "${HOST_LOCK-}")" \
    "$(json_quote "${HEALTHCHECK_URL-}")" \
    "$(json_quote "${HEALTHCHECK_EXPECT_STATUS-}")" \
    "$(json_quote "${HEALTHCHECK_RETRIES-}")" \
    "$(json_quote "${HEALTHCHECK_INTERVAL-}")" \
    "$(json_quote "${HEALTHCHECK_WARMUP-}")" \
    "$(json_quote "${ROLLBACK_SAFETY-}")" \
    "$(json_quote "${BUSY_LOCK_FILE-}")" \
    "$(json_quote "${BUSY_LOCK_TIMEOUT-}")" \
    "$(json_quote "${ONESHOT_SERVICES-}")"

  tmp="$(mktemp "${intents_dir}/.intent.XXXXXX")" || { _fail_after_install "$dest" "$companion_dest"; return 0; }
  if ! printf '%s\n' "$result" > "$tmp"; then
    rm -f -- "$tmp" || true
    _fail_after_install "$dest" "$companion_dest"
    return 0
  fi
  if ! chmod 0600 "$tmp"; then
    rm -f -- "$tmp" || true
    _fail_after_install "$dest" "$companion_dest"
    return 0
  fi
  printf -v event_line '{"event":"accepted","run_number":%s,"run_attempt":%s,"git_sha":%s,"at":%s}' \
    "$run_number" "$run_attempt" "$(json_quote "$GIT_SHA")" "$(json_quote "$now")"
  if ! _append_intent_event "$events_file" "$event_line"; then
    rm -f -- "$tmp" || true
    _fail_after_install "$dest" "$companion_dest"
    return 0
  fi
  if ! mv -f -- "$tmp" "$intent_path"; then
    rm -f -- "$tmp" || true
    _fail_after_install "$dest" "$companion_dest"
    return 0
  fi
  _remove_replaced_script "$scripts_dir" "$old_script_ref" "$script_ref"
  echo "[deploy][evidence] intent: accepted"
  return 0
}

# Persist the current deploy as the host's latest deferred intent.
# No-op unless INTENT_FILE_ENABLED=true. Never prints webhook values.
# On any write failure: emit intent: write-failed and return 0 so the
# caller still exit 3 (deferred semantics unchanged).
write_deferred_intent() {
  [ "${INTENT_FILE_ENABLED:-}" = "true" ] || return 0

  local converge_dir intents_dir intent_path events_file intent_lock stem
  local run_number run_attempt

  run_number="${D3_RUN_NUMBER-}"
  run_attempt="${D3_RUN_ATTEMPT-}"

  converge_dir="${HOME}/.d3-converge"
  intents_dir="${converge_dir}/intents"
  events_file="${converge_dir}/events.jsonl"
  intent_lock="${converge_dir}/intents.lock"

  stem="$(sanitize_deploy_dir "$DEPLOY_DIR")" || { _intent_write_failed; return 0; }
  intent_path="${intents_dir}/${stem}.json"

  mkdir -p "$intents_dir" || { _intent_write_failed; return 0; }
  chmod 0700 "$converge_dir" || { _intent_write_failed; return 0; }
  chmod 0700 "$intents_dir" || { _intent_write_failed; return 0; }

  exec 7>>"$intent_lock" || { _intent_write_failed; return 0; }
  if ! flock -w "${INTENT_LOCK_TIMEOUT:-30}" -x 7; then
    exec 7>&-
    _intent_write_failed
    return 0
  fi
  _write_deferred_intent_locked
  _release_intent_lock
  return 0
}

# Online deployments reserve the same monotonic identity before any state or
# compose mutation. Caller holds busy and host locks; fd 7 is held only here.
_admit_online_locked() {
  local existing_run existing_attempt old_script_ref="" now event_line result tmp
  local scripts_dir="${converge_dir}/scripts"

  if [ -f "$intent_path" ]; then
    existing_run="$(sed -n 's/.*"run_number":\([0-9][0-9]*\).*/\1/p' "$intent_path" 2>/dev/null)" || existing_run=""
    existing_run="${existing_run%%$'\n'*}"
    existing_attempt="$(sed -n 's/.*"run_attempt":\([0-9][0-9]*\).*/\1/p' "$intent_path" 2>/dev/null)" || existing_attempt=""
    existing_attempt="${existing_attempt%%$'\n'*}"
    old_script_ref="$(sed -n 's/.*"script_ref":"\([^"]*\)".*/\1/p' "$intent_path" 2>/dev/null)" || old_script_ref=""
    old_script_ref="${old_script_ref%%$'\n'*}"
    if is_positive_integer "$existing_run" && is_positive_integer "$existing_attempt"; then
      if [ "$existing_run" -gt "$run_number" ] || { [ "$existing_run" -eq "$run_number" ] && [ "$existing_attempt" -gt "$run_attempt" ]; }; then
        now="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        printf -v event_line '{"event":"superseded","run_number":%s,"run_attempt":%s,"kept_run_number":%s,"kept_run_attempt":%s,"at":%s}' \
          "$run_number" "$run_attempt" "$existing_run" "$existing_attempt" "$(json_quote "$now")"
        _append_intent_event "$events_file" "$event_line" || { _intent_write_failed; return 7; }
        echo "[deploy][evidence] intent: superseded"
        return 6
      fi
      if [ "$existing_run" -eq "$run_number" ] && [ "$existing_attempt" -eq "$run_attempt" ] \
        && [ "$(sed -n 's/.*\"status\":\"\([^\"]*\)\".*/\1/p' "$intent_path")" = "admitted" ]; then
        echo "[deploy][evidence] intent: admitted"
        return 0
      fi
    fi
  fi

  now="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf -v result '{"schema_version":1,"status":"admitted","repo":%s,"run_number":%s,"run_attempt":%s,"git_sha":%s,"created_at":%s,"updated_at":%s}' \
    "$(json_quote "${D3_REPO-}")" "$run_number" "$run_attempt" \
    "$(json_quote "$GIT_SHA")" "$(json_quote "$now")" "$(json_quote "$now")"
  tmp="$(mktemp "${intents_dir}/.intent.XXXXXX")" || { _intent_write_failed; return 7; }
  if ! printf '%s\n' "$result" > "$tmp" || ! chmod 0600 "$tmp"; then
    rm -f -- "$tmp"
    _intent_write_failed
    return 7
  fi
  printf -v event_line '{"event":"admitted","run_number":%s,"run_attempt":%s,"git_sha":%s,"at":%s}' \
    "$run_number" "$run_attempt" "$(json_quote "$GIT_SHA")" "$(json_quote "$now")"
  if ! _append_intent_event "$events_file" "$event_line" || ! mv -f -- "$tmp" "$intent_path"; then
    rm -f -- "$tmp"
    _intent_write_failed
    return 7
  fi
  _remove_replaced_script "$scripts_dir" "$old_script_ref" ""
  echo "[deploy][evidence] intent: admitted"
}

admit_online_deploy() {
  [ "${INTENT_FILE_ENABLED:-}" = "true" ] || return 0

  local converge_dir intents_dir intent_path events_file intent_lock stem rc=0
  local run_number="${D3_RUN_NUMBER-}" run_attempt="${D3_RUN_ATTEMPT-}"
  converge_dir="${HOME}/.d3-converge"
  intents_dir="${converge_dir}/intents"
  events_file="${converge_dir}/events.jsonl"
  intent_lock="${converge_dir}/intents.lock"
  stem="$(sanitize_deploy_dir "$DEPLOY_DIR")" || { _intent_write_failed; return 7; }
  intent_path="${intents_dir}/${stem}.json"
  mkdir -p "$intents_dir" || { _intent_write_failed; return 7; }
  chmod 0700 "$converge_dir" "$intents_dir" || { _intent_write_failed; return 7; }
  exec 7>>"$intent_lock" || { _intent_write_failed; return 7; }
  if ! flock -w "${INTENT_LOCK_TIMEOUT:-30}" -x 7; then
    exec 7>&-
    _intent_write_failed
    return 7
  fi
  _admit_online_locked || rc=$?
  _release_intent_lock
  return "$rc"
}

# Called only by the replayed script after acquiring busy lock then host lock.
# It must not deploy if the snapshot no longer owns the latest admission.
validate_deferred_replay_admission() {
  local converge_dir intents_dir intent_path events_file intent_lock stem
  local existing_run existing_attempt existing_status now event_line rc=0
  [ "${INTENT_FILE_ENABLED:-}" = "true" ] || return 0
  converge_dir="${HOME}/.d3-converge"
  intents_dir="${converge_dir}/intents"
  events_file="${converge_dir}/events.jsonl"
  intent_lock="${converge_dir}/intents.lock"
  stem="$(sanitize_deploy_dir "$DEPLOY_DIR")" || return 7
  intent_path="${intents_dir}/${stem}.json"
  exec 7>>"$intent_lock" || { echo "[deploy][evidence] intent: write-failed"; return 7; }
  if ! flock -w "${INTENT_LOCK_TIMEOUT:-30}" -x 7; then
    exec 7>&-
    echo "[deploy][evidence] intent: write-failed"
    return 7
  fi
  existing_run=""
  existing_attempt=""
  existing_status=""
  if [ -f "$intent_path" ]; then
    existing_run="$(sed -n 's/.*"run_number":\([0-9][0-9]*\).*/\1/p' "$intent_path")"
    existing_attempt="$(sed -n 's/.*"run_attempt":\([0-9][0-9]*\).*/\1/p' "$intent_path")"
    existing_status="$(sed -n 's/.*"status":"\([^"]*\)".*/\1/p' "$intent_path")"
  fi
  if [ "$existing_run" = "$D3_RUN_NUMBER" ] && [ "$existing_attempt" = "$D3_RUN_ATTEMPT" ] \
    && [ "$existing_status" = "running" ]; then
    _release_intent_lock
    return 0
  fi
  now="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  if ! is_positive_integer "$existing_run" || ! is_positive_integer "$existing_attempt"; then
    existing_run=null
    existing_attempt=null
  fi
  printf -v event_line '{"event":"superseded","run_number":%s,"run_attempt":%s,"kept_run_number":%s,"kept_run_attempt":%s,"at":%s}' \
    "$D3_RUN_NUMBER" "$D3_RUN_ATTEMPT" "$existing_run" "$existing_attempt" "$(json_quote "$now")"
  _append_intent_event "$events_file" "$event_line" || rc=7
  echo "[deploy][evidence] intent: superseded"
  _release_intent_lock
  [ "$rc" -eq 0 ] && return 6
  return "$rc"
}
