"""Producer-boundary tests for scripts/set-fleet-secrets.sh."""
from __future__ import annotations

import json
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "set-fleet-secrets.sh"
ENV_USER = "ENV_SENTINEL_ACR_USER_should_never_leak"
ENV_PASS = "ENV_SENTINEL_ACR_PASS_should_never_leak"
ENV_PAT = "ENV_SENTINEL_GH_PAT_should_never_leak"
ENV_TS = "ENV_SENTINEL_TS_AUTHKEY_should_never_leak"
ACR_USER = "INFISICAL_ACR_USER_s3ntinel"
ACR_PASS = "INFISICAL_ACR_PASS_s3ntinel"
KNOWN = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEKNOWNHOSTS_s3ntinel"
SSH_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "FAKE_SSH_KEY_s3ntinel\n-----END OPENSSH PRIVATE KEY-----\n"
)
UA_ID = "fake-ua-client-id-s3ntinel"
UA_SECRET = "fake-ua-client-secret-s3ntinel"
UA_TOKEN = "fake-ua-login-token-s3ntinel"
ENV_SENTS = (ENV_USER, ENV_PASS, ENV_PAT, ENV_TS)
VALUE_SENTS = ENV_SENTS + (ACR_USER, ACR_PASS, KNOWN, "FAKE_SSH_KEY_s3ntinel", UA_TOKEN)
INFISICAL_FLAGS = (
    "--projectId", "b0467d60-36b6-49a2-858c-c2bb82cc633e",
    "--env", "dev", "--path", "/ci-templates", "--format", "json",
)


def _secret_item(key: str, value: str) -> dict:
    return {
        "_id": f"id-{key}",
        "key": key,
        "value": value,
        "type": "shared",
        "secretPath": "/ci-templates",
    }


def _export_list(*pairs: tuple[str, str]) -> list[dict]:
    return [_secret_item(k, v) for k, v in pairs]


def _exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def sentinel_dotenv():
    path = REPO_ROOT / ".env"
    existed, previous = path.exists(), path.read_bytes() if path.exists() else None
    path.write_text(
        f"ACR_USERNAME={ENV_USER}\nACR_PASSWORD={ENV_PASS}\n"
        f"GH_PAT_TOKEN={ENV_PAT}\nTS_AUTHKEY={ENV_TS}\n"
    )
    try:
        yield path
    finally:
        path.write_bytes(previous) if existed else path.unlink(missing_ok=True)


def _invoke(tmp_path: Path, payload, extra: dict[str, str] | None = None):
    bindir, cap = tmp_path / "bin", tmp_path / "capture"
    bindir.mkdir()
    cap.mkdir()
    body = json.dumps(payload, separators=(",", ":"))
    _exec(bindir / "infisical", (
        "#!/bin/bash\nset -euo pipefail\n"
        f"cap={shlex.quote(str(cap))}\n"
        'n=$(($(cat "$cap/inf-n" 2>/dev/null || echo 0)+1)); echo "$n" > "$cap/inf-n"\n'
        'python3 -c \'import json,sys; json.dump(sys.argv[2:], open(sys.argv[1],"w"))\' '
        '"$cap/inf-argv-$n.json" "$@"\n'
        'if [ "${1:-}" = login ]; then printf "%s" '
        f"{shlex.quote(UA_TOKEN)}; exit 0; fi\n"
        f"printf '%s' {shlex.quote(body)}\n"
    ))
    _exec(bindir / "gh", (
        "#!/bin/bash\nset -euo pipefail\n"
        f"cap={shlex.quote(str(cap))}\n"
        'n=$(($(cat "$cap/n" 2>/dev/null || echo 0)+1)); echo "$n" > "$cap/n"\n'
        'python3 -c \'import json,sys; json.dump(sys.argv[2:], open(sys.argv[1],"w"))\' '
        '"$cap/argv-$n.json" "$@"\ncat > "$cap/stdin-$n.bin"\n'
    ))
    _exec(bindir / "ssh-keyscan", f"#!/bin/bash\nprintf '%s\\n' {shlex.quote(KNOWN)}\n")
    key = tmp_path / "id_ed25519"
    key.write_text(SSH_KEY)
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        ["bash", str(SCRIPT), "owner/demo-repo", str(key)],
        cwd=str(REPO_ROOT), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin", "HOME": str(home),
            "DEPLOY_HOST_IP": "192.0.2.1", "LC_ALL": "C", **(extra or {}),
        }, check=False,
    )
    calls = []
    nfile = cap / "n"
    if nfile.exists():
        for i in range(1, int(nfile.read_text().strip()) + 1):
            argv = json.loads((cap / f"argv-{i}.json").read_text())
            calls.append((argv, (cap / f"stdin-{i}.bin").read_bytes()))
    inf_calls = []
    inf_n = cap / "inf-n"
    if inf_n.exists():
        for i in range(1, int(inf_n.read_text().strip()) + 1):
            inf_calls.append(json.loads((cap / f"inf-argv-{i}.json").read_text()))
    return result, calls, inf_calls


def _no_values(*texts: str) -> None:
    blob = "\n".join(texts)
    for sentinel in VALUE_SENTS:
        assert sentinel not in blob


def _assert_secrets_via_stdin(calls):
    sets = {argv[2]: (argv, stdin) for argv, stdin in calls
            if len(argv) >= 3 and argv[:2] == ["secret", "set"]}
    expected = {
        "ACR_USERNAME": ACR_USER.encode(), "ACR_PASSWORD": ACR_PASS.encode(),
        "KNOWN_HOSTS": KNOWN.encode(), "SSH_DEPLOY_KEY": SSH_KEY.encode(),
    }
    for name, value in expected.items():
        argv, stdin = sets[name]
        assert "--body" not in argv
        joined = "\0".join(argv)
        for sentinel in VALUE_SENTS:
            assert sentinel not in joined
        assert stdin == value
    assert "CI_TEMPLATES_PAT" not in sets and "TS_AUTHKEY" not in sets
    for argv, stdin in calls:
        for sentinel in ENV_SENTS:
            assert sentinel.encode() not in stdin
            assert sentinel not in " ".join(argv)


def test_stdin_not_argv_and_dotenv_ignored(tmp_path, sentinel_dotenv):
    result, calls, inf_calls = _invoke(
        tmp_path,
        _export_list(("ACR_USERNAME", ACR_USER), ("ACR_PASSWORD", ACR_PASS)),
        {"INFISICAL_TOKEN": "fake-infisical-token-s3ntinel"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(inf_calls) == 1
    inf_argv = inf_calls[0]
    assert inf_argv[0] == "export"
    for flag in INFISICAL_FLAGS:
        assert flag in inf_argv
    assert ACR_PASS not in inf_argv and ACR_USER not in inf_argv
    _assert_secrets_via_stdin(calls)


def test_missing_infisical_auth_fails_without_values(tmp_path, sentinel_dotenv):
    result, calls, inf_calls = _invoke(
        tmp_path,
        _export_list(("ACR_USERNAME", ACR_USER), ("ACR_PASSWORD", ACR_PASS)),
    )
    assert result.returncode != 0
    assert "ACR_USERNAME" in result.stderr and "INFISICAL_TOKEN" in result.stderr
    _no_values(result.stderr, result.stdout)
    assert calls == []
    assert inf_calls == []


def test_missing_acr_key_fails_without_values(tmp_path, sentinel_dotenv):
    result, calls, _ = _invoke(
        tmp_path,
        _export_list(("ACR_USERNAME", ACR_USER)),
        {"INFISICAL_TOKEN": "fake-infisical-token-s3ntinel"},
    )
    assert result.returncode != 0
    assert "ACR_PASSWORD" in result.stderr and "INFISICAL_TOKEN" in result.stderr
    for sentinel in (ACR_USER, ACR_PASS, ENV_USER, ENV_PASS):
        assert sentinel not in result.stderr and sentinel not in result.stdout
    assert calls == []


def test_dict_export_shape_is_rejected(tmp_path, sentinel_dotenv):
    result, calls, _ = _invoke(
        tmp_path,
        {"ACR_USERNAME": ACR_USER, "ACR_PASSWORD": ACR_PASS},
        {"INFISICAL_TOKEN": "fake-infisical-token-s3ntinel"},
    )
    assert result.returncode != 0
    assert "JSON 数组" in result.stderr
    _no_values(result.stderr, result.stdout)
    assert calls == []


def test_universal_auth_logs_in_then_exports(tmp_path, sentinel_dotenv):
    result, calls, inf_calls = _invoke(
        tmp_path,
        _export_list(("ACR_USERNAME", ACR_USER), ("ACR_PASSWORD", ACR_PASS)),
        {
            "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID": UA_ID,
            "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET": UA_SECRET,
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(inf_calls) == 2
    login_argv, export_argv = inf_calls
    assert login_argv[0] == "login"
    assert "--method=universal-auth" in login_argv
    assert f"--client-id={UA_ID}" in login_argv
    assert f"--client-secret={UA_SECRET}" in login_argv
    assert ACR_PASS not in login_argv and UA_TOKEN not in login_argv
    assert export_argv[0] == "export"
    for flag in INFISICAL_FLAGS:
        assert flag in export_argv
    assert "--token" not in export_argv
    assert UA_TOKEN not in export_argv
    assert UA_SECRET not in export_argv
    _assert_secrets_via_stdin(calls)
    for argv, stdin in calls:
        assert UA_SECRET.encode() not in stdin
        assert UA_TOKEN.encode() not in stdin
        assert UA_SECRET not in " ".join(argv)
        assert UA_TOKEN not in " ".join(argv)
    assert UA_SECRET not in result.stderr and UA_TOKEN not in result.stderr
    assert UA_SECRET not in result.stdout and UA_TOKEN not in result.stdout
