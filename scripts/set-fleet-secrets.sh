#!/usr/bin/env bash
# 给一个服务仓配 D3 流水线 GitHub secret。
# ACR 从 Infisical 项目 shared-infra（id b0467d60-36b6-49a2-858c-c2bb82cc633e）
# env dev、路径 /ci-templates 读取，不读本仓 .env。SSH 私钥来自本机文件；
# KNOWN_HOSTS 由 ssh-keyscan 现扫。gh secret set 一律经 stdin，不走 --body。
# CI_TEMPLATES_PAT：模板已不消费，默认不写；占位时设环境变量 CI_TEMPLATES_PAT。
# TS_AUTHKEY：仅 hosted runner 消费（Connect to Tailscale）；默认 self 不写，
# hosted 应急设环境变量 TS_AUTHKEY。
# 用法: DEPLOY_HOST_IP='<host-ip>' scripts/set-fleet-secrets.sh <owner/repo> [ssh_key_path]
set -euo pipefail

INFISICAL_PROJECT_ID="b0467d60-36b6-49a2-858c-c2bb82cc633e"
NEED_LOGIN="需要 INFISICAL_TOKEN 或 universal-auth 登录"

REPO="${1:?用法: set-fleet-secrets.sh <owner/repo> [ssh_key_path]}"
HOST_IP="${DEPLOY_HOST_IP:?缺少环境变量 DEPLOY_HOST_IP}"
SSH_KEY="${2:-$HOME/.ssh/id_ed25519}"
[ -f "$SSH_KEY" ] || { echo "✗ 找不到 SSH 私钥 $SSH_KEY" >&2; exit 1; }

if [ -z "${INFISICAL_TOKEN:-}" ] && {
     [ -z "${INFISICAL_UNIVERSAL_AUTH_CLIENT_ID:-}" ] ||
     [ -z "${INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET:-}" ]
   }; then
  echo "✗ 缺少 Infisical 登录凭据，无法取 ACR_USERNAME / ACR_PASSWORD；$NEED_LOGIN" >&2
  exit 1
fi

export_json="$(infisical export \
  --projectId "$INFISICAL_PROJECT_ID" --env dev --path /ci-templates \
  --format json --silent 2>/dev/null)" || {
  echo "✗ infisical export 失败，缺 ACR_USERNAME / ACR_PASSWORD；$NEED_LOGIN" >&2
  exit 1
}

_acr_assign="$(printf '%s' "$export_json" | python3 -c '
import json, shlex, sys
msg = "；需要 INFISICAL_TOKEN 或 universal-auth 登录\n"
try:
    data = json.loads(sys.stdin.read())
except json.JSONDecodeError:
    sys.stderr.write("✗ infisical export 不是 JSON，缺 ACR_USERNAME / ACR_PASSWORD" + msg)
    sys.exit(1)
if not isinstance(data, dict):
    sys.stderr.write("✗ infisical export 不是 JSON 对象，缺 ACR_USERNAME / ACR_PASSWORD" + msg)
    sys.exit(1)
missing = [k for k in ("ACR_USERNAME", "ACR_PASSWORD")
           if not isinstance(data.get(k), str) or not data[k]]
if missing:
    sys.stderr.write("✗ infisical 缺 " + " / ".join(missing) + msg)
    sys.exit(1)
sys.stdout.write("ACR_USERNAME=" + shlex.quote(data["ACR_USERNAME"]) + "\n")
sys.stdout.write("ACR_PASSWORD=" + shlex.quote(data["ACR_PASSWORD"]) + "\n")
')"
eval "$_acr_assign"
unset _acr_assign export_json

echo "→ 配 secret 到 $REPO(部署目标 $HOST_IP,SSH key $SSH_KEY)"
KNOWN_HOSTS="$(ssh-keyscan -T 10 "$HOST_IP" 2>/dev/null)"
[ -n "$KNOWN_HOSTS" ] || { echo "✗ ssh-keyscan $HOST_IP 没拿到 host key(目标在线吗/入 tailnet 了吗)" >&2; exit 1; }

set_secret() { printf '%s' "$2" | gh secret set "$1" --repo "$REPO"; }
set_secret ACR_USERNAME "$ACR_USERNAME"
set_secret ACR_PASSWORD "$ACR_PASSWORD"
set_secret KNOWN_HOSTS "$KNOWN_HOSTS"
gh secret set SSH_DEPLOY_KEY --repo "$REPO" < "$SSH_KEY"
[ -n "${CI_TEMPLATES_PAT:-}" ] && set_secret CI_TEMPLATES_PAT "$CI_TEMPLATES_PAT"
[ -n "${TS_AUTHKEY:-}" ] && set_secret TS_AUTHKEY "$TS_AUTHKEY"
echo "✓ secret 已写入 $REPO:"
gh secret list --repo "$REPO"
