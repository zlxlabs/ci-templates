# Sourced by pull_and_deploy.sh when INTENT_FILE_ENABLED=true.
# Not a shared library: converge.sh (C2) copies sanitize_deploy_dir.
# Do not execute this file directly.
#
# Independent function so C2 can copy the same rule; tests lock both sides
# character-for-character. Rule: strip leading slashes; replace every char
# not in [A-Za-z0-9._-] with _; append '-' and the first 8 hex chars of
# sha256(original deploy_dir).
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
# Event append happens before replacing the intent file. Event write
# failure → write-failed, leave the published intent unchanged.
_write_deferred_intent_locked() {
  local existing_run existing_attempt now notify_json run_id_json result
  local event_line tmp

  if [ -f "$intent_path" ]; then
    existing_run="$(sed -n 's/.*"run_number":\([0-9][0-9]*\).*/\1/p' "$intent_path" 2>/dev/null)" || existing_run=""
    existing_run="${existing_run%%$'\n'*}"
    existing_attempt="$(sed -n 's/.*"run_attempt":\([0-9][0-9]*\).*/\1/p' "$intent_path" 2>/dev/null)" || existing_attempt=""
    existing_attempt="${existing_attempt%%$'\n'*}"
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
    fi
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
    "$(json_quote "pull_and_deploy.${stem}.sh")" \
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

  tmp="$(mktemp "${intents_dir}/.intent.XXXXXX")" || { _intent_write_failed; return 0; }
  if ! printf '%s\n' "$result" > "$tmp"; then
    rm -f -- "$tmp" || true
    _intent_write_failed
    return 0
  fi
  if ! chmod 0600 "$tmp"; then
    rm -f -- "$tmp" || true
    _intent_write_failed
    return 0
  fi
  printf -v event_line '{"event":"accepted","run_number":%s,"run_attempt":%s,"git_sha":%s,"at":%s}' \
    "$run_number" "$run_attempt" "$(json_quote "$GIT_SHA")" "$(json_quote "$now")"
  if ! _append_intent_event "$events_file" "$event_line"; then
    rm -f -- "$tmp" || true
    _intent_write_failed
    return 0
  fi
  if ! mv -f -- "$tmp" "$intent_path"; then
    rm -f -- "$tmp" || true
    _intent_write_failed
    return 0
  fi
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
