#!/bin/bash
# Host-side deferred-deploy converger (ci-templates #95 C2).
# Zero deploy logic: replay ~/.d3-converge/scripts/<script_ref> with the
# intent's deploy_env whitelist. JSON I/O uses bash/coreutils/sed/flock/curl
# only (same tools as C1). Never prints webhook URLs or curl response bodies.
set -euo pipefail

: "${HOME:?HOME required}"

CURL_BIN="${CURL_BIN:-curl}"
INTENT_LOCK_TIMEOUT="${INTENT_LOCK_TIMEOUT:-30}"
ALERT_AFTER_S=86400

CONVERGE_DIR="${HOME}/.d3-converge"
INTENTS_DIR="${CONVERGE_DIR}/intents"
SCRIPTS_DIR="${CONVERGE_DIR}/scripts"
EVENTS_FILE="${CONVERGE_DIR}/events.jsonl"
INTENT_LOCK="${CONVERGE_DIR}/intents.lock"
CONVERGE_LOCK="${CONVERGE_DIR}/converge.lock"

DEPLOY_ENV_KEYS=(
  IMAGE_NAME
  ACR_IMAGE
  LOCAL_IMAGE
  GIT_SHA
  DEPLOY_DIR
  HOST_LOCK
  HEALTHCHECK_URL
  HEALTHCHECK_EXPECT_STATUS
  HEALTHCHECK_RETRIES
  HEALTHCHECK_INTERVAL
  HEALTHCHECK_WARMUP
  ROLLBACK_SAFETY
  BUSY_LOCK_FILE
  BUSY_LOCK_TIMEOUT
  ONESHOT_SERVICES
)

json_quote() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '"%s"' "$value"
}

# Print the JSON value for KEY. Missing/null → empty. Always exit 0.
json_get() {
  local json="$1" key="$2" rest c out i
  rest="${json#*\"${key}\":}"
  if [ "$rest" = "$json" ]; then
    return 0
  fi
  case "$rest" in
    null[,\}]*)
      return 0
      ;;
    true[,\}]*)
      printf 'true'
      return 0
      ;;
    false[,\}]*)
      printf 'false'
      return 0
      ;;
    [0-9]*)
      printf '%s' "${rest%%[!0-9]*}"
      return 0
      ;;
    \"*)
      rest="${rest#\"}"
      out=""
      i=0
      while [ "$i" -lt "${#rest}" ]; do
        c="${rest:$i:1}"
        if [ "$c" = '\' ]; then
          i=$((i + 1))
          [ "$i" -lt "${#rest}" ] || return 0
          c="${rest:$i:1}"
          case "$c" in
            n) out+=$'\n' ;;
            r) out+=$'\r' ;;
            t) out+=$'\t' ;;
            *) out+="$c" ;;
          esac
        elif [ "$c" = '"' ]; then
          printf '%s' "$out"
          return 0
        else
          out+="$c"
        fi
        i=$((i + 1))
      done
      return 0
      ;;
  esac
}

now_epoch() {
  if [ -n "${CONVERGE_NOW:-}" ]; then
    printf '%s' "$CONVERGE_NOW"
  else
    date -u +%s
  fi
}

now_iso() {
  date -u -d "@$(now_epoch)" '+%Y-%m-%dT%H:%M:%SZ'
}

iso_to_epoch() {
  date -u -d "$1" +%s
}

webhook_host() {
  local url="$1" rest host
  case "$url" in
    https://*) rest="${url#https://}" ;;
    http://*) rest="${url#http://}" ;;
    *) rest="$url" ;;
  esac
  host="${rest%%/*}"
  host="${host%%:*}"
  host="${host##*@}"
  printf '%s' "$host"
}

acquire_intents_lock() {
  exec 7>>"$INTENT_LOCK"
  if ! flock -w "$INTENT_LOCK_TIMEOUT" -x 7; then
    exec 7>&-
    echo "[converge] intents.lock timeout" >&2
    return 1
  fi
}

release_intents_lock() {
  flock -u 7
  exec 7>&-
}

read_intent() {
  cat "$1"
}

# Apply sed expressions to the intent file (atomic, 0600). Caller holds lock.
patch_intent() {
  local file="$1" tmp
  shift
  tmp="$(mktemp "$(dirname "$file")/.intent.XXXXXX")"
  sed "$@" "$file" > "$tmp"
  chmod 0600 "$tmp"
  mv -f -- "$tmp" "$file"
}

append_event() {
  local line="$1"
  if [ ! -e "$EVENTS_FILE" ]; then
    touch "$EVENTS_FILE"
    chmod 0600 "$EVENTS_FILE"
  fi
  printf '%s\n' "$line" >> "$EVENTS_FILE"
}

set_status() {
  local file="$1" status="$2" now="$3"
  patch_intent "$file" \
    -e "s|\"status\":\"[^\"]*\"|\"status\":\"${status}\"|" \
    -e "s|\"updated_at\":\"[^\"]*\"|\"updated_at\":\"${now}\"|"
}

bump_running() {
  local file="$1" attempts="$2" now="$3"
  patch_intent "$file" \
    -e "s|\"status\":\"[^\"]*\"|\"status\":\"running\"|" \
    -e "s|\"attempts\":[0-9][0-9]*|\"attempts\":${attempts}|" \
    -e "s|\"last_attempt_at\":null|\"last_attempt_at\":\"${now}\"|" \
    -e "s|\"last_attempt_at\":\"[^\"]*\"|\"last_attempt_at\":\"${now}\"|" \
    -e "s|\"updated_at\":\"[^\"]*\"|\"updated_at\":\"${now}\"|"
}

set_last_alert_at() {
  local file="$1" ts="$2"
  patch_intent "$file" \
    -e "s|\"last_alert_at\":null|\"last_alert_at\":\"${ts}\"|" \
    -e "s|\"last_alert_at\":\"[^\"]*\"|\"last_alert_at\":\"${ts}\"|" \
    -e "s|\"updated_at\":\"[^\"]*\"|\"updated_at\":\"${ts}\"|"
}

notify() {
  local severity="$1" title="$2" body="$3" webhook="$4"
  local host kind icon template heading payload http_code curl_rc content

  if [ -z "$webhook" ]; then
    echo "[converge] notify: empty-webhook" >&2
    return 1
  fi

  host="$(webhook_host "$webhook")"
  case "$host" in
    qyapi.weixin.qq.com) kind=wecom ;;
    open.feishu.cn|open.larksuite.com) kind=feishu ;;
    *)
      echo "[converge] notify: unsupported-webhook-host host=${host}" >&2
      return 1
      ;;
  esac

  case "$severity" in
    P0) icon="🔴"; template="red" ;;
    P1) icon="🟠"; template="orange" ;;
    P2) icon="🟠"; template="orange" ;;
    info) icon="🔵"; template="blue" ;;
    *)
      echo "[converge] notify: invalid-severity" >&2
      return 1
      ;;
  esac

  heading="${icon} ${severity} ${title}"
  if [ "$kind" = wecom ]; then
    content="$(printf '%s\n%s' "**${heading}**" "$body")"
    payload="$(printf '{"msgtype":"markdown","markdown":{"content":%s}}' "$(json_quote "$content")")"
  else
    payload="$(printf '{"msg_type":"interactive","card":{"header":{"template":%s,"title":{"tag":"plain_text","content":%s}},"elements":[{"tag":"div","text":{"tag":"lark_md","content":%s}}]}}' \
      "$(json_quote "$template")" \
      "$(json_quote "$heading")" \
      "$(json_quote "$body")")"
  fi

  http_code="000"
  set +e
  http_code="$("$CURL_BIN" -sS -o /dev/null -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    -d "$payload" \
    "$webhook")"
  curl_rc=$?
  set -e
  http_code="${http_code:-000}"

  if [ "$curl_rc" -ne 0 ]; then
    echo "[converge] notify: send-failed http=${http_code}" >&2
    return 1
  fi
  case "$http_code" in
    2[0-9][0-9]) return 0 ;;
    *)
      echo "[converge] notify: send-failed http=${http_code}" >&2
      return 1
      ;;
  esac
}

# rc of the last deploy-script invocation (set -e cannot see it).
LAST_DEPLOY_RC=0

run_deploy_script_capture() {
  local script="$1" json="$2"
  local key val script_ref companion_ref
  local -a env_cmd
  env_cmd=(env -i "PATH=${PATH}" "HOME=${HOME}")
  for key in "${DEPLOY_ENV_KEYS[@]}"; do
    val="$(json_get "$json" "$key")"
    env_cmd+=("${key}=${val}")
  done
  script_ref="$(json_get "$json" script_ref)"
  companion_ref="deferred_intent.${script_ref#pull_and_deploy.}"
  env_cmd+=("INTENT_FILE_ENABLED=true" "D3_DEFERRED_REPLAY=true")
  env_cmd+=("D3_RUN_NUMBER=$(json_get "$json" run_number)" "D3_RUN_ATTEMPT=$(json_get "$json" run_attempt)")
  env_cmd+=("D3_DEPLOY_SCRIPT=${script}" "D3_DEFERRED_INTENT_SCRIPT=${SCRIPTS_DIR}/${companion_ref}")
  set +e
  "${env_cmd[@]}" bash "$script"
  LAST_DEPLOY_RC=$?
  set -e
}

maybe_stale_alert() {
  local file="$1" json="$2" webhook="$3"
  local created last_alert attempts run_number run_attempt now_s created_s last_s queued_s queued_h now
  created="$(json_get "$json" created_at)"
  last_alert="$(json_get "$json" last_alert_at)"
  attempts="$(json_get "$json" attempts)"
  run_number="$(json_get "$json" run_number)"
  run_attempt="$(json_get "$json" run_attempt)"
  [ -n "$created" ] || return 0
  now_s="$(now_epoch)"
  set +e
  created_s="$(iso_to_epoch "$created")"
  set -e
  [ -n "$created_s" ] || return 0
  queued_s=$((now_s - created_s))
  [ "$queued_s" -ge "$ALERT_AFTER_S" ] || return 0
  if [ -n "$last_alert" ]; then
    set +e
    last_s="$(iso_to_epoch "$last_alert")"
    set -e
    [ -n "$last_s" ] || return 0
    if [ $((now_s - last_s)) -lt "$ALERT_AFTER_S" ]; then
      return 0
    fi
  fi
  queued_h=$((queued_s / 3600))
  if ! notify P2 \
    "部署意图持续推迟" \
    "部署意图 #${run_number} 已排队 ${queued_h} 小时仍被忙锁占用（已尝试 ${attempts:-0} 次）。录制优先，继续等待；如需放弃见 RUNBOOK。" \
    "$webhook"; then
    return 0
  fi
  now="$(now_iso)"
  if ! acquire_intents_lock; then
    return 0
  fi
  json="$(read_intent "$file")"
  if [ "$(json_get "$json" run_number)" = "$run_number" ] \
    && [ "$(json_get "$json" run_attempt)" = "$run_attempt" ] \
    && [ "$(json_get "$json" status)" = pending ]; then
    set_last_alert_at "$file" "$now"
    append_event "$(printf '{"event":"alert-throttled","run_number":%s,"run_attempt":%s,"queued_hours":%s,"attempts":%s,"at":%s}' \
      "$run_number" "$run_attempt" "$queued_h" "${attempts:-0}" "$(json_quote "$now")")"
  fi
  release_intents_lock
}

process_intent() {
  local file="$1" json status run_number run_attempt script_ref webhook
  local notify_on_success repo git_sha attempts now snapshot_run snapshot_attempt
  local script_path companion_ref companion_path rc

  if ! acquire_intents_lock; then
    return 0
  fi
  json="$(read_intent "$file")"
  status="$(json_get "$json" status)"
  run_number="$(json_get "$json" run_number)"
  run_attempt="$(json_get "$json" run_attempt)"
  script_ref="$(json_get "$json" script_ref)"
  webhook="$(json_get "$json" webhook)"
  notify_on_success="$(json_get "$json" notify_on_success)"
  repo="$(json_get "$json" repo)"
  git_sha="$(json_get "$json" git_sha)"
  attempts="$(json_get "$json" attempts)"
  now="$(now_iso)"

  case "$status" in
    admitted|deployed|failed|interrupted|abandoned)
      release_intents_lock
      return 0
      ;;
    running)
      set_status "$file" interrupted "$now"
      append_event "$(printf '{"event":"interrupted","run_number":%s,"run_attempt":%s,"at":%s}' \
        "${run_number:-0}" "${run_attempt:-0}" "$(json_quote "$now")")"
      release_intents_lock
      notify P0 \
        "收敛器执行中断" \
        "收敛器上次执行被中断，生产状态未证——按 rc=4 同款处置上机核对。repo=${repo} run=#${run_number} sha=${git_sha}" \
        "$webhook" || true
      return 0
      ;;
    pending)
      ;;
    *)
      echo "[converge] unknown-status" >&2
      release_intents_lock
      return 0
      ;;
  esac

  script_path="${SCRIPTS_DIR}/${script_ref}"
  case "$script_ref" in
    pull_and_deploy.*.sh) companion_ref="deferred_intent.${script_ref#pull_and_deploy.}" ;;
    *) companion_ref="" ;;
  esac
  companion_path="${SCRIPTS_DIR}/${companion_ref}"
  if [ -n "$companion_ref" ] && [ -f "$script_path" ] && [ ! -f "$companion_path" ]; then
    set_status "$file" failed "$now"
    append_event "$(printf '{\"event\":\"legacy-script-rejected\",\"run_number\":%s,\"run_attempt\":%s,\"at\":%s}' "${run_number:-0}" "${run_attempt:-0}" "$(json_quote "$now")")"
    release_intents_lock
    notify P1 \
      "旧版延期部署脚本已拒绝" \
      "run #${run_number} 的固化脚本缺少锁内接纳校验，未执行且已置终态。请用新触发的 run 重建意图。repo=${repo} sha=${git_sha}" \
      "$webhook" || true
    return 0
  fi

  snapshot_run="$run_number"
  snapshot_attempt="$run_attempt"
  attempts="${attempts:-0}"
  attempts=$((attempts + 1))
  bump_running "$file" "$attempts" "$now"
  append_event "$(printf '{"event":"attempt","run_number":%s,"run_attempt":%s,"attempts":%s,"at":%s}' \
    "$snapshot_run" "$snapshot_attempt" "$attempts" "$(json_quote "$now")")"
  release_intents_lock

  rc=127
  case "$script_ref" in
    ""|*/*)
      rc=127
      ;;
    *)
      if [ ! -f "$script_path" ]; then
        rc=127
      else
        run_deploy_script_capture "$script_path" "$json"
        rc="$LAST_DEPLOY_RC"
      fi
      ;;
  esac

  if ! acquire_intents_lock; then
    return 0
  fi
  json="$(read_intent "$file")"
  now="$(now_iso)"
  if [ "$(json_get "$json" run_number)" != "$snapshot_run" ] \
    || [ "$(json_get "$json" run_attempt)" != "$snapshot_attempt" ]; then
    append_event "$(printf '{"event":"attempt-superseded","run_number":%s,"run_attempt":%s,"kept_run_number":%s,"kept_run_attempt":%s,"rc":%s,"at":%s}' \
      "$snapshot_run" "$snapshot_attempt" \
      "$(json_get "$json" run_number)" "$(json_get "$json" run_attempt)" \
      "$rc" "$(json_quote "$now")")"
    release_intents_lock
    return 0
  fi

  case "$rc" in
    0)
      set_status "$file" deployed "$now"
      append_event "$(printf '{"event":"deployed","run_number":%s,"run_attempt":%s,"rc":0,"at":%s}' \
        "$snapshot_run" "$snapshot_attempt" "$(json_quote "$now")")"
      release_intents_lock
      if [ "$notify_on_success" = true ]; then
        notify info \
          "部署意图已上线" \
          "run #${snapshot_run} 的部署意图已由主机自动上线，探针+对账通过。repo=${repo} sha=${git_sha}" \
          "$webhook" || true
      fi
      ;;
    6)
      if [ "$(json_get "$json" status)" = "running" ]; then
        set_status "$file" failed "$now"
        append_event "$(printf '{\"event\":\"failed\",\"run_number\":%s,\"run_attempt\":%s,\"rc\":6,\"at\":%s}' "$snapshot_run" "$snapshot_attempt" "$(json_quote "$now")")"
        release_intents_lock
        notify P1 "自动部署失败" "自动部署失败（rc=6，部署脚本明确失败），不再自动重试，需人工处置。repo=${repo} run=#${snapshot_run} sha=${git_sha}" "$webhook" || true
      else
        release_intents_lock
      fi
      ;;
    3)
      set_status "$file" pending "$now"
      append_event "$(printf '{"event":"deferred","run_number":%s,"run_attempt":%s,"rc":3,"at":%s}' \
        "$snapshot_run" "$snapshot_attempt" "$(json_quote "$now")")"
      json="$(read_intent "$file")"
      release_intents_lock
      maybe_stale_alert "$file" "$json" "$webhook"
      ;;
    *)
      set_status "$file" failed "$now"
      append_event "$(printf '{"event":"failed","run_number":%s,"run_attempt":%s,"rc":%s,"at":%s}' \
        "$snapshot_run" "$snapshot_attempt" "$rc" "$(json_quote "$now")")"
      release_intents_lock
      notify P1 \
        "自动部署失败" \
        "自动部署失败（rc=${rc}，已自动回滚/已停止），不再自动重试，需人工处置。repo=${repo} run=#${snapshot_run} sha=${git_sha}" \
        "$webhook" || true
      ;;
  esac
}

mkdir -p "$INTENTS_DIR" "$SCRIPTS_DIR"
chmod 0700 "$CONVERGE_DIR" "$INTENTS_DIR"

exec 8>"$CONVERGE_LOCK"
if ! flock -n 8; then
  echo "[converge] another instance running"
  exit 0
fi

shopt -s nullglob
for intent_path in "$INTENTS_DIR"/*.json; do
  [ -f "$intent_path" ] || continue
  process_intent "$intent_path"
done
exit 0
