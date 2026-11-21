"""C2: converge.sh 按意图快照重放固化脚本，零部署逻辑。

驱动真实 converge.sh 子进程（HOME 指向 tmp）。意图文件用 C1 写侧
`write_deferred_intent` 生成，不手写 JSON fixture。部署脚本与 curl 用 fake，
断言子进程写出的意图 JSON 字节、fake 脚本收到的 env、fake curl 的 argv。
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest

from test_deferred_intent import DEPLOY_ENV_KEYS, sanitize_deploy_dir
from test_pull_and_deploy import REPO_ROOT, _write_exec

CONVERGE_SH = REPO_ROOT / "scripts" / "converge" / "converge.sh"
DEFERRED_INTENT_SH = REPO_ROOT / "scripts" / "deferred_intent.sh"
SYSTEMD_DIR = REPO_ROOT / "scripts" / "converge" / "systemd"

FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/c2-feishu-secret-do-not-leak"
WECOM_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=c2-wecom-secret-do-not-leak"
LARK_WEBHOOK = "https://open.larksuite.com/open-apis/bot/v2/hook/c2-lark-secret-do-not-leak"
UNKNOWN_WEBHOOK = "https://example.invalid/hook/c2-unknown-secret-do-not-leak"

_WRITE_INTENT = r"""
set -euo pipefail
json_quote() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '"%s"' "$value"
}
is_positive_integer() { [[ "$1" =~ ^[1-9][0-9]*$ ]]; }
is_non_negative_integer() { [[ "$1" =~ ^[0-9]+$ ]]; }
source "$DEFERRED_INTENT_SH"
write_deferred_intent
"""


def _iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _base_env(tmp_path: Path, *, webhook: str = FEISHU_WEBHOOK) -> dict:
    home = tmp_path / "home"
    home.mkdir()
    curl_log = tmp_path / "curl.argv"
    curl_log.write_text("")
    curl_bin = tmp_path / "fake-curl"
    _write_exec(
        curl_bin,
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$@\" >> '{curl_log}'\n"
        "printf '%s' '200'\n"
        "exit 0\n",
    )
    deploy_dir = tmp_path / "app"
    deploy_dir.mkdir()
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        CURL_BIN=str(curl_bin),
        IMAGE_NAME="demo",
        ACR_IMAGE="registry.example.com/ns/demo",
        LOCAL_IMAGE="",
        GIT_SHA="abc1234",
        DEPLOY_DIR=str(deploy_dir),
        HOST_LOCK=str(tmp_path / "host.lock"),
        HEALTHCHECK_URL="http://localhost/health",
        HEALTHCHECK_EXPECT_STATUS="200",
        HEALTHCHECK_RETRIES="2",
        HEALTHCHECK_INTERVAL="0",
        HEALTHCHECK_WARMUP="0",
        ROLLBACK_SAFETY="safe",
        BUSY_LOCK_FILE=str(tmp_path / "busy.lock"),
        BUSY_LOCK_TIMEOUT="1",
        ONESHOT_SERVICES="",
        INTENT_FILE_ENABLED="true",
        D3_RUN_NUMBER="131",
        D3_RUN_ATTEMPT="1",
        D3_RUN_ID="44111383925",
        D3_RUN_URL="https://github.com/zlxlabs/live-recorder/actions/runs/44111383925",
        D3_REPO="zlxlabs/live-recorder",
        D3_INTENT_WEBHOOK=webhook,
        D3_NOTIFY_ON_SUCCESS="false",
        DEFERRED_INTENT_SH=str(DEFERRED_INTENT_SH),
        D3_DEPLOY_SCRIPT=str(tmp_path / "seed-deploy.sh"),
    )
    seed = Path(env["D3_DEPLOY_SCRIPT"])
    seed.write_text("#!/bin/bash\nexit 0\n")
    seed.chmod(0o700)
    env["_CURL_ARGV"] = str(curl_log)
    return env


def _home(env: dict) -> Path:
    return Path(env["HOME"])


def _intent_path(env: dict) -> Path:
    stem = sanitize_deploy_dir(env["DEPLOY_DIR"])
    return _home(env) / ".d3-converge" / "intents" / f"{stem}.json"


def _events_path(env: dict) -> Path:
    return _home(env) / ".d3-converge" / "events.jsonl"


def _script_ref(env: dict) -> str:
    return f"pull_and_deploy.{sanitize_deploy_dir(env['DEPLOY_DIR'])}.sh"


def write_intent(env: dict) -> None:
    res = subprocess.run(
        ["bash", "-c", _WRITE_INTENT],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[deploy][evidence] intent: accepted" in res.stdout, res.stdout
    assert env["D3_INTENT_WEBHOOK"] not in res.stdout + res.stderr


def _install_fake_script(
    env: dict, *, rc: int, extra: str = ""
) -> dict[str, Path]:
    home = _home(env)
    scripts = home / ".d3-converge" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    env_log = scripts / "env.log"
    argv_log = scripts / "argv.log"
    runs_log = scripts / "runs.log"
    path = scripts / _script_ref(env)
    path.write_text(
        "#!/bin/bash\n"
        f"n=0\n"
        f"if [ -f '{runs_log}' ]; then n=$(grep -c . '{runs_log}' || true); fi\n"
        f"env | sort > '{env_log}.'\"$n\"\n"
        f"env | sort > '{env_log}'\n"
        f"printf '%s\\n' \"$0\" \"$@\" > '{argv_log}'\n"
        f"echo run >> '{runs_log}'\n"
        f"{extra}\n"
        f"exit {rc}\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return {
        "script": path,
        "env_log": env_log,
        "argv_log": argv_log,
        "runs_log": runs_log,
    }


def _run_converge(env: dict, extra: dict | None = None) -> subprocess.CompletedProcess:
    merged = dict(env)
    if extra:
        merged.update(extra)
    return subprocess.run(
        ["bash", str(CONVERGE_SH)],
        env=merged,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _load_intent(env: dict) -> dict:
    path = _intent_path(env)
    assert path.is_file(), path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, oct(path.stat().st_mode)
    return json.loads(path.read_text())


def _event_kinds(env: dict) -> list[str]:
    path = _events_path(env)
    assert path.is_file(), path
    text = path.read_text()
    assert text.endswith("\n")
    return [json.loads(line)["event"] for line in text.splitlines() if line]


def _event_rows(env: dict) -> list[dict]:
    return [
        json.loads(line)
        for line in _events_path(env).read_text().splitlines()
        if line
    ]


def _curl_argv(env: dict) -> str:
    return Path(env["_CURL_ARGV"]).read_text()


def _run_count(logs: dict[str, Path]) -> int:
    path = logs["runs_log"]
    if not path.is_file():
        return 0
    return len(path.read_text().splitlines())


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


def _assert_no_webhook_leak(res: subprocess.CompletedProcess, env: dict) -> None:
    blob = res.stdout + res.stderr
    webhook = env["D3_INTENT_WEBHOOK"]
    assert webhook not in blob
    events = _events_path(env)
    if events.is_file():
        assert webhook not in events.read_text()


def _replace_json_string(path: Path, key: str, value: str) -> None:
    text = path.read_text()
    new, n = re.subn(rf'("{key}":")[^"]*(")', rf"\g<1>{value}\2", text, count=1)
    assert n == 1, (key, text)
    path.write_text(new)


def _replace_json_null_or_string(path: Path, key: str, value: str) -> None:
    text = path.read_text()
    new, n = re.subn(rf'"{key}":null', f'"{key}":"{value}"', text, count=1)
    if n == 0:
        new, n = re.subn(rf'("{key}":")[^"]*(")', rf"\g<1>{value}\2", text, count=1)
    assert n == 1, (key, text)
    path.write_text(new)


@pytest.mark.parametrize("notify_on_success,expect_notify", [(True, True), (False, False)])
def test_rc0_marks_deployed_events_and_optional_info_card(
    tmp_path, notify_on_success, expect_notify
):
    """(a) rc=0 → deployed，events 有 attempt 和 deployed；notify_on_success 控制 info 卡。"""
    env = _base_env(tmp_path)
    env["D3_NOTIFY_ON_SUCCESS"] = "true" if notify_on_success else "false"
    write_intent(env)
    logs = _install_fake_script(env, rc=0)

    res = _run_converge(env)
    assert res.returncode == 0, res.stdout + res.stderr
    intent = _load_intent(env)
    assert intent["status"] == "deployed"
    assert intent["run_number"] == 131
    assert intent["run_attempt"] == 1
    assert intent["attempts"] == 1
    kinds = _event_kinds(env)
    assert "attempt" in kinds
    assert "deployed" in kinds
    assert _run_count(logs) == 1
    argv = _curl_argv(env)
    if expect_notify:
        assert argv.strip() != ""
        assert "msg_type" in argv
        assert "info" in argv
        assert "-sS" in argv
        assert "/dev/null" in argv
        assert "%{http_code}" in argv
    else:
        assert argv.strip() == ""
    _assert_no_webhook_leak(res, env)


def test_rc3_returns_to_pending_increments_attempts_no_notify(tmp_path):
    """(b) rc=3 → 回到 pending、attempts 加 1、不发卡。"""
    env = _base_env(tmp_path)
    write_intent(env)
    logs = _install_fake_script(env, rc=3)

    res = _run_converge(env)
    assert res.returncode == 0, res.stdout + res.stderr
    intent = _load_intent(env)
    assert intent["status"] == "pending"
    assert intent["attempts"] == 1
    assert intent["last_attempt_at"] is not None
    kinds = _event_kinds(env)
    assert "attempt" in kinds
    assert "deployed" not in kinds
    assert "failed" not in kinds
    assert _run_count(logs) == 1
    assert _curl_argv(env).strip() == ""
    _assert_no_webhook_leak(res, env)


@pytest.mark.parametrize("rc", [1, 4, 5])
def test_failed_rc_marks_failed_sends_p1_and_next_round_does_not_run(tmp_path, rc):
    """(c) rc=1/4/5 → failed 并发 P1；下一轮不再执行。"""
    env = _base_env(tmp_path)
    write_intent(env)
    logs = _install_fake_script(env, rc=rc)

    first = _run_converge(env)
    assert first.returncode == 0, first.stdout + first.stderr
    intent = _load_intent(env)
    assert intent["status"] == "failed"
    assert intent["attempts"] == 1
    kinds = _event_kinds(env)
    assert "attempt" in kinds
    assert "failed" in kinds
    argv = _curl_argv(env)
    assert "P1" in argv
    assert "msg_type" in argv
    assert _run_count(logs) == 1
    _assert_no_webhook_leak(first, env)

    second = _run_converge(env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert _load_intent(env)["status"] == "failed"
    assert _run_count(logs) == 1
    _assert_no_webhook_leak(second, env)


def test_leftover_running_marks_interrupted_sends_p0_does_not_run(tmp_path):
    """(d) 遗留 running → interrupted 并发 P0，不执行脚本。"""
    env = _base_env(tmp_path)
    write_intent(env)
    logs = _install_fake_script(env, rc=0)
    _replace_json_string(_intent_path(env), "status", "running")

    res = _run_converge(env)
    assert res.returncode == 0, res.stdout + res.stderr
    intent = _load_intent(env)
    assert intent["status"] == "interrupted"
    assert intent["attempts"] == 0
    kinds = _event_kinds(env)
    assert "interrupted" in kinds
    assert "attempt" not in kinds
    assert _run_count(logs) == 0
    argv = _curl_argv(env)
    assert "P0" in argv
    _assert_no_webhook_leak(res, env)


def test_superseded_during_execute_keeps_new_pending_and_next_round_runs_it(tmp_path):
    """(e) 执行期间写更大 run → 新意图保持 pending，旧结果不覆盖；下一轮执行新意图。"""
    env = _base_env(tmp_path)
    write_intent(env)

    newer = dict(env)
    newer["D3_RUN_NUMBER"] = "200"
    newer["D3_RUN_ATTEMPT"] = "1"
    newer["GIT_SHA"] = "def5678"
    # Copy the already-installed fake replay script onto itself so accepted
    # install does not replace converge's fake with the seed stub.
    newer["D3_DEPLOY_SCRIPT"] = str(
        _home(env) / ".d3-converge" / "scripts" / _script_ref(env)
    )
    helper = tmp_path / "write_newer.sh"
    export_lines = []
    for key in (
        "HOME",
        "IMAGE_NAME",
        "ACR_IMAGE",
        "LOCAL_IMAGE",
        "GIT_SHA",
        "DEPLOY_DIR",
        "HOST_LOCK",
        "HEALTHCHECK_URL",
        "HEALTHCHECK_EXPECT_STATUS",
        "HEALTHCHECK_RETRIES",
        "HEALTHCHECK_INTERVAL",
        "HEALTHCHECK_WARMUP",
        "ROLLBACK_SAFETY",
        "BUSY_LOCK_FILE",
        "BUSY_LOCK_TIMEOUT",
        "ONESHOT_SERVICES",
        "INTENT_FILE_ENABLED",
        "D3_RUN_NUMBER",
        "D3_RUN_ATTEMPT",
        "D3_RUN_ID",
        "D3_RUN_URL",
        "D3_REPO",
        "D3_INTENT_WEBHOOK",
        "D3_NOTIFY_ON_SUCCESS",
        "DEFERRED_INTENT_SH",
        "D3_DEPLOY_SCRIPT",
        "PATH",
    ):
        export_lines.append(f"export {key}={json.dumps(newer[key])}")
    helper.write_text("#!/bin/bash\nset -euo pipefail\n" + "\n".join(export_lines) + "\n" + _WRITE_INTENT)
    helper.chmod(0o755)

    marker = tmp_path / "wrote-newer"
    logs = _install_fake_script(
        env,
        rc=0,
        extra=(
            f'if [ ! -f "{marker}" ]; then\n'
            f'  bash "{helper}"\n'
            f'  touch "{marker}"\n'
            f"fi\n"
        ),
    )

    first = _run_converge(env)
    assert first.returncode == 0, first.stdout + first.stderr
    intent = _load_intent(env)
    assert intent["status"] == "pending"
    assert intent["run_number"] == 200
    assert intent["git_sha"] == "def5678"
    kinds = _event_kinds(env)
    assert "attempt-superseded" in kinds
    assert "deployed" not in kinds
    assert _run_count(logs) == 1
    first_env = _parse_env_file(Path(str(logs["env_log"]) + ".0"))
    assert first_env["GIT_SHA"] == "abc1234"
    _assert_no_webhook_leak(first, env)

    second = _run_converge(env)
    assert second.returncode == 0, second.stdout + second.stderr
    intent = _load_intent(env)
    assert intent["status"] == "deployed"
    assert intent["run_number"] == 200
    assert intent["git_sha"] == "def5678"
    assert "deployed" in _event_kinds(env)
    assert _run_count(logs) == 2
    second_env = _parse_env_file(Path(str(logs["env_log"]) + ".1"))
    assert second_env["GIT_SHA"] == "def5678"
    _assert_no_webhook_leak(second, env)


def test_pending_alert_throttled_24h(tmp_path):
    """(f) pending 超过 24h 首轮发 P2，紧接下一轮不发，last_alert_at 拨回 24h 前再发。"""
    env = _base_env(tmp_path)
    now = 1_700_000_000
    env["CONVERGE_NOW"] = str(now)
    write_intent(env)
    _install_fake_script(env, rc=3)
    created = _iso(now - 25 * 3600)
    _replace_json_string(_intent_path(env), "created_at", created)

    first = _run_converge(env)
    assert first.returncode == 0, first.stdout + first.stderr
    intent = _load_intent(env)
    assert intent["status"] == "pending"
    assert intent["last_alert_at"] is not None
    argv1 = _curl_argv(env)
    assert "P2" in argv1
    assert "alert-throttled" in _event_kinds(env)
    _assert_no_webhook_leak(first, env)

    Path(env["_CURL_ARGV"]).write_text("")
    second = _run_converge(env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert _curl_argv(env).strip() == ""
    _assert_no_webhook_leak(second, env)

    _replace_json_null_or_string(_intent_path(env), "last_alert_at", _iso(now - 24 * 3600))
    Path(env["_CURL_ARGV"]).write_text("")
    third = _run_converge(env)
    assert third.returncode == 0, third.stdout + third.stderr
    assert "P2" in _curl_argv(env)
    _assert_no_webhook_leak(third, env)


def test_converge_lock_held_exits_0_without_touching_intent(tmp_path):
    """(g) converge.lock 被占 → exit 0，不碰意图。"""
    env = _base_env(tmp_path)
    write_intent(env)
    logs = _install_fake_script(env, rc=0)
    before = _intent_path(env).read_bytes()
    lock = _home(env) / ".d3-converge" / "converge.lock"
    lock.touch()
    holder = subprocess.Popen(["flock", "-x", str(lock), "sleep", "30"])
    try:
        time.sleep(0.2)
        res = _run_converge(env)
    finally:
        holder.terminate()
        holder.wait()

    assert res.returncode == 0, res.stdout + res.stderr
    assert "[converge] another instance running" in res.stdout
    assert _intent_path(env).read_bytes() == before
    assert _run_count(logs) == 0
    assert _curl_argv(env).strip() == ""
    _assert_no_webhook_leak(res, env)


def test_deploy_script_receives_only_whitelist_without_intent_file_enabled(tmp_path):
    """(h) 部署脚本只收到白名单键，且不含 INTENT_FILE_ENABLED。"""
    env = _base_env(tmp_path)
    write_intent(env)
    logs = _install_fake_script(env, rc=0)

    res = _run_converge(env)
    assert res.returncode == 0, res.stdout + res.stderr
    recorded = _parse_env_file(logs["env_log"])
    for key in DEPLOY_ENV_KEYS:
        assert key in recorded, key
        assert recorded[key] == env.get(key, ""), key
    assert "INTENT_FILE_ENABLED" not in recorded
    assert "D3_INTENT_WEBHOOK" not in recorded
    assert "D3_RUN_NUMBER" not in recorded
    assert env["D3_INTENT_WEBHOOK"] not in recorded.values()
    _assert_no_webhook_leak(res, env)


@pytest.mark.parametrize(
    "webhook,expect_marker,expect_host,expect_send",
    [
        (WECOM_WEBHOOK, "msgtype", "qyapi.weixin.qq.com", True),
        (FEISHU_WEBHOOK, "msg_type", "open.feishu.cn", True),
        (LARK_WEBHOOK, "msg_type", "open.larksuite.com", True),
        (UNKNOWN_WEBHOOK, None, "example.invalid", False),
    ],
)
def test_webhook_dispatch_never_leaks_url(
    tmp_path, webhook, expect_marker, expect_host, expect_send
):
    """(i) webhook 按主机名分派企微/飞书/未知；stdout 与 stderr 都不含 URL。"""
    env = _base_env(tmp_path, webhook=webhook)
    write_intent(env)
    _install_fake_script(env, rc=1)

    res = _run_converge(env)
    assert res.returncode == 0, res.stdout + res.stderr
    blob = res.stdout + res.stderr
    assert webhook not in blob
    argv = _curl_argv(env)
    if expect_send:
        assert expect_marker in argv
        assert webhook in argv
        assert "P1" in argv
        assert "-sS" in argv
        assert "/dev/null" in argv
        assert "%{http_code}" in argv
    else:
        assert argv.strip() == ""
        assert "[converge] notify: unsupported-webhook-host" in res.stderr
        assert expect_host in res.stderr
        assert webhook not in res.stderr
    _assert_no_webhook_leak(res, env)
    assert webhook in _intent_path(env).read_text()


def test_missing_script_marks_failed_sends_p1(tmp_path):
    """(j) 脚本文件缺失 → failed 并发 P1。"""
    env = _base_env(tmp_path)
    write_intent(env)
    installed = _home(env) / ".d3-converge" / "scripts" / _script_ref(env)
    assert installed.is_file()
    installed.unlink()

    res = _run_converge(env)
    assert res.returncode == 0, res.stdout + res.stderr
    intent = _load_intent(env)
    assert intent["status"] == "failed"
    assert intent["attempts"] == 1
    kinds = _event_kinds(env)
    assert "attempt" in kinds
    assert "failed" in kinds
    argv = _curl_argv(env)
    assert "P1" in argv
    _assert_no_webhook_leak(res, env)


def test_notify_send_failed_logs_http_without_changing_status(tmp_path):
    """发送失败打 send-failed http=<code>，不改意图终态，不回显 URL。"""
    env = _base_env(tmp_path)
    curl_bin = Path(env["CURL_BIN"])
    _write_exec(
        curl_bin,
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$@\" >> '{env['_CURL_ARGV']}'\n"
        "printf '%s' '500'\n"
        "exit 0\n",
    )
    write_intent(env)
    _install_fake_script(env, rc=1)

    res = _run_converge(env)
    assert res.returncode == 0, res.stdout + res.stderr
    intent = _load_intent(env)
    assert intent["status"] == "failed"
    assert "[converge] notify: send-failed http=500" in res.stderr
    assert env["D3_INTENT_WEBHOOK"] not in res.stdout + res.stderr
    _assert_no_webhook_leak(res, env)


def test_systemd_unit_templates_oneshot_and_timer_interval():
    service = (SYSTEMD_DIR / "d3-converge.service").read_text()
    timer = (SYSTEMD_DIR / "d3-converge.timer").read_text()
    assert "Type=oneshot" in service
    assert "User=" in service
    assert "OnBootSec=2min" in timer
    assert "OnUnitActiveSec=2min" in timer
