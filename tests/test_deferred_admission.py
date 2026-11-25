"""#104 admission-order regressions, driven by the real producers and flock."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from test_deferred_intent import _events_path, _intent_env, _intent_path, _load_intent
from test_pull_and_deploy import (
    REPO_ROOT,
    _mock_docker_matrix,
    _run,
    _seed_prev_good,
    _write_exec,
)

CONVERGE = REPO_ROOT / "scripts" / "converge" / "converge.sh"


def _hold_lock(path: Path, mode: str, ready: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            "bash", "-c",
            'exec 9>"$1"; flock "$2" 9; : > "$3"; sleep 30',
            "lock-holder", str(path), mode, str(ready),
        ],
    )
    for _ in range(500):
        if ready.exists():
            return proc
        if proc.poll() is not None:
            raise AssertionError("flock holder exited before acquiring lock")
        subprocess.run(["sleep", "0.01"], check=True)
    proc.terminate()
    proc.wait()
    raise AssertionError("flock holder did not acquire lock")


def _with_deferred_identity(env: dict, run: int, attempt: int = 1, webhook: str = "") -> dict:
    env.update(
        INTENT_FILE_ENABLED="true",
        D3_RUN_NUMBER=str(run),
        D3_RUN_ATTEMPT=str(attempt),
        D3_RUN_ID=str(44000000000 + run),
        D3_RUN_URL=f"https://github.com/zlxlabs/live-recorder/actions/runs/{44000000000 + run}",
        D3_REPO="zlxlabs/live-recorder",
        D3_INTENT_WEBHOOK=webhook,
        D3_NOTIFY_ON_SUCCESS="false",
        ROLLBACK_SAFETY="safe",
        ONESHOT_SERVICES="",
    )
    return env


def _seed_deferred(env: dict, run: int, webhook: str = "") -> None:
    ready = Path(env["BUSY_LOCK_FILE"] + ".ready")
    holder = _hold_lock(Path(env["BUSY_LOCK_FILE"]), "-s", ready)
    try:
        _with_deferred_identity(env, run, webhook=webhook)
        result = _run(env)
        assert result.returncode == 3, result.stdout + result.stderr
        assert "[deploy][evidence] intent: accepted" in result.stdout
    finally:
        holder.terminate()
        holder.wait()


def _stateful_docker(log: Path, latest: Path, running: Path) -> str:
    return f'''#!/bin/bash
printf '%s\\n' "$*" >> '{log}'
if [ "$1" = pull ]; then exit 0; fi
if [ "$1" = tag ]; then printf '%s\\n' "$2" > '{latest}'; exit 0; fi
if [ "$1" = compose ] && [ "$2" = config ] && [ "$3" = --services ]; then echo app; exit 0; fi
if [ "$1" = compose ] && [ "$2" = up ] && [ "$3" = -d ]; then cp '{latest}' '{running}'; exit 0; fi
if [ "$1" = compose ] && [ "$2" = ps ] && [[ "$*" == *"--status running"* ]]; then echo cid-app; exit 0; fi
if [ "$1" = image ] && [ "$2" = inspect ]; then
  if [ "$3" = --format ]; then format="$4"; ref="$5"; else ref="$3"; format="$5"; fi
  [ "$format" = '{{{{index .RepoDigests 0}}}}' ] && exit 0
  [ "$ref" = demo:latest ] && ref=$(< '{latest}')
  printf 'sha256:%s\\n' "${{ref##*:}}"
  exit 0
fi
if [ "$1" = inspect ] && [ "$2" = cid-app ] && [ "$3" = --format ]; then
  ref=$(< '{running}')
  printf 'sha256:%s\\n' "${{ref##*:}}"
  exit 0
fi
echo "unexpected-docker: $*" >&2
exit 97
'''


def _run_converge(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(CONVERGE)], env=env, capture_output=True, text=True, timeout=30
    )


def _companion_path(env: dict, run: int, attempt: int = 1) -> Path:
    stem = _load_intent(env)["script_ref"]
    name = stem.removeprefix("pull_and_deploy.").removesuffix(".sh")
    return Path(env["HOME"]) / ".d3-converge" / "scripts" / f"deferred_intent.{name}.sh"


def test_online_deploy_admits_identity_before_first_compose_and_is_idempotent(tmp_path):
    """在线部署写入 latest admission；同一身份重复 admission 不产生第二事件。"""
    env = _with_deferred_identity(_intent_env(tmp_path), 210)
    env["BUSY_LOCK_TIMEOUT"] = "2"

    first = _run(env)
    assert first.returncode == 0, first.stdout + first.stderr
    path = _intent_path(env)
    record = json.loads(path.read_text())
    assert (record["run_number"], record["run_attempt"]) == (210, 1)
    assert record["status"] == "admitted"
    assert "deploy_env" not in record
    assert "script_ref" not in record
    events_before = _events_path(env).read_text().splitlines()
    assert events_before[-1] == json.dumps(
        json.loads(events_before[-1]), separators=(",", ":")
    )
    assert json.loads(events_before[-1])["event"] == "admitted"

    second = _run(env)
    assert second.returncode == 0, second.stdout + second.stderr
    assert _events_path(env).read_text().splitlines() == events_before
    assert json.loads(path.read_text())["status"] == "admitted"


def test_same_identity_deferred_retry_replaces_online_admission_with_payload(tmp_path):
    """SSH 255 重试撞忙锁时，同身份 admitted 必须变成可重放 pending。"""
    env = _with_deferred_identity(_intent_env(tmp_path), 215)
    env["BUSY_LOCK_TIMEOUT"] = "2"
    latest = tmp_path / "same-identity-latest"
    running = tmp_path / "same-identity-running"
    docker_log = Path(env["DOCKER_LOG"])
    _write_exec(Path(env["DOCKER_BIN"]), _stateful_docker(docker_log, latest, running))
    online = _run(env)
    assert online.returncode == 0, online.stdout + online.stderr
    assert json.loads(_intent_path(env).read_text())["status"] == "admitted"

    ready = Path(env["BUSY_LOCK_FILE"] + ".same-identity")
    holder = _hold_lock(Path(env["BUSY_LOCK_FILE"]), "-s", ready)
    try:
        retried = _run(env)
    finally:
        holder.terminate()
        holder.wait()

    assert retried.returncode == 3, retried.stdout + retried.stderr
    assert "[deploy][evidence] intent: accepted" in retried.stdout
    pending = json.loads(_intent_path(env).read_text())
    assert pending["status"] == "pending"
    assert pending["run_number"] == 215
    assert pending["run_attempt"] == 1
    assert pending["deploy_env"]["GIT_SHA"] == env["GIT_SHA"]
    assert pending["script_ref"]
    script = Path(env["HOME"]) / ".d3-converge" / "scripts" / pending["script_ref"]
    companion = script.with_name("deferred_intent." + script.name.removeprefix("pull_and_deploy."))
    assert script.is_file()
    assert companion.is_file()
    bindir = Path(env["DOCKER_BIN"]).parent
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    replay = _run_converge(env)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert json.loads(_intent_path(env).read_text())["status"] == "deployed"


@pytest.mark.parametrize("mode", ["online", "deferred"])
@pytest.mark.parametrize("field", ["run_number", "run_attempt"])
def test_malformed_identity_watermark_fails_closed(tmp_path, mode, field):
    """身份字段损坏时在线 rc=7、延期 write-failed，均保留原记录字节。"""
    env = _with_deferred_identity(_intent_env(tmp_path), 275)
    env["BUSY_LOCK_TIMEOUT"] = "2"
    producer = _run(env)
    assert producer.returncode == 0, producer.stdout + producer.stderr
    path = _intent_path(env)
    before = path.read_bytes()
    events_before = _events_path(env).read_bytes()
    original = f'"{field}":{275 if field == "run_number" else 1}'.encode()
    replacement = f'"{field}":"not-positive"'.encode()
    damaged, count = before.replace(original, replacement, 1), before.count(original)
    assert count == 1
    path.write_bytes(damaged)
    compose_before = [line for line in Path(env["DOCKER_LOG"]).read_text().splitlines() if line.startswith("compose up -d")]

    target_run, target_attempt = (276, 1) if field == "run_number" else (275, 2)
    _with_deferred_identity(env, target_run, target_attempt)
    if mode == "deferred":
        ready = Path(env["BUSY_LOCK_FILE"] + ".malformed-watermark")
        holder = _hold_lock(Path(env["BUSY_LOCK_FILE"]), "-s", ready)
        try:
            result = _run(env)
        finally:
            holder.terminate()
            holder.wait()
        assert result.returncode == 3, result.stdout + result.stderr
    else:
        result = _run(env)
        assert result.returncode == 7, result.stdout + result.stderr

    assert "[deploy][evidence] intent: write-failed" in result.stdout
    assert path.read_bytes() == damaged
    assert _events_path(env).read_bytes() == events_before
    assert [line for line in Path(env["DOCKER_LOG"]).read_text().splitlines() if line.startswith("compose up -d")] == compose_before
    if mode == "deferred":
        assert not (Path(env["HOME"]) / ".d3-converge" / "scripts").exists()


def test_new_online_run_rejects_older_run_before_compose_or_state_change(tmp_path):
    """已上线 B 后手工重跑旧 A 在线取锁，必须专用拒绝且无线上副作用。"""
    env = _with_deferred_identity(_intent_env(tmp_path), 220)
    env["BUSY_LOCK_TIMEOUT"] = "2"
    newer = _run(env)
    assert newer.returncode == 0, newer.stdout + newer.stderr
    state = Path(env["STATE_DIR"])
    last_good_before = (state / "last_good_tag").read_bytes()
    docker_log = Path(env["DOCKER_LOG"])
    compose_before = [line for line in docker_log.read_text().splitlines() if line.startswith("compose up -d")]
    record_before = _intent_path(env).read_bytes()
    events_before = _events_path(env).read_bytes()

    _with_deferred_identity(env, 219)
    env["GIT_SHA"] = "older-sha"
    stale = _run(env)
    assert stale.returncode == 6, stale.stdout + stale.stderr
    assert "[deploy][evidence] intent: superseded" in stale.stdout
    assert "compose up" not in stale.stdout + stale.stderr
    assert (state / "last_good_tag").read_bytes() == last_good_before
    assert [line for line in docker_log.read_text().splitlines() if line.startswith("compose up -d")] == compose_before
    assert not any(line.startswith("tag ") and "older-sha" in line for line in docker_log.read_text().splitlines())
    assert json.loads(record_before)["run_number"] == 220
    assert json.loads(_intent_path(env).read_text())["run_number"] == 220
    assert len(_events_path(env).read_bytes().splitlines()) == len(events_before.splitlines()) + 1
    assert json.loads(_events_path(env).read_text().splitlines()[-1])["event"] == "superseded"


def test_deferred_same_identity_resend_is_idempotent(tmp_path):
    """相同 run/attempt 重写延期意图不改字节、不重复追加事件或重装脚本。"""
    env = _with_deferred_identity(_intent_env(tmp_path), 225)
    _seed_deferred(env, 225)
    intent_before = _intent_path(env).read_bytes()
    events_before = _events_path(env).read_bytes()
    script = Path(env["HOME"]) / ".d3-converge" / "scripts" / _load_intent(env)["script_ref"]
    script_before = script.read_bytes()

    ready = Path(env["BUSY_LOCK_FILE"] + ".ready-again")
    holder = _hold_lock(Path(env["BUSY_LOCK_FILE"]), "-s", ready)
    try:
        resend = _run(env)
    finally:
        holder.terminate()
        holder.wait()

    assert resend.returncode == 3, resend.stdout + resend.stderr
    assert "[deploy][evidence] intent: accepted" in resend.stdout
    assert _intent_path(env).read_bytes() == intent_before
    assert _events_path(env).read_bytes() == events_before
    assert script.read_bytes() == script_before


def test_online_admission_lock_failure_fails_closed_before_compose(tmp_path):
    """intents.lock 写侧不能取得锁时 rc=7，且部署未开始。"""
    env = _with_deferred_identity(_intent_env(tmp_path), 228)
    env["INTENT_LOCK_TIMEOUT"] = "1"
    lock = Path(env["HOME"]) / ".d3-converge" / "intents.lock"
    lock.parent.mkdir(parents=True)
    ready = tmp_path / "intents-lock-ready"
    holder = _hold_lock(lock, "-x", ready)
    try:
        result = _run(env)
    finally:
        holder.terminate()
        holder.wait()

    assert result.returncode == 7, result.stdout + result.stderr
    assert "intent: write-failed" in result.stdout
    assert not (Path(env["STATE_DIR"]) / "last_good_tag").exists()
    calls = Path(env["DOCKER_LOG"]).read_text().splitlines()
    assert not any(line.startswith("compose up -d") or line.startswith("tag ") for line in calls)
    assert not _intent_path(env).exists()


def test_deferred_old_run_after_new_online_admission_is_rejected(tmp_path):
    """B 在线接纳后 A 才写延期意图，A 被拒且不得覆盖 B。"""
    env = _with_deferred_identity(_intent_env(tmp_path), 230)
    env["BUSY_LOCK_TIMEOUT"] = "2"
    online = _run(env)
    assert online.returncode == 0, online.stdout + online.stderr
    admitted = _intent_path(env).read_bytes()

    busy_ready = Path(env["BUSY_LOCK_FILE"] + ".ready")
    holder = _hold_lock(Path(env["BUSY_LOCK_FILE"]), "-s", busy_ready)
    try:
        _with_deferred_identity(env, 229)
        old = _run(env)
    finally:
        holder.terminate()
        holder.wait()

    assert old.returncode == 3, old.stdout + old.stderr
    assert "[deploy][evidence] intent: superseded" in old.stdout
    assert _intent_path(env).read_bytes() == admitted
    assert json.loads(_events_path(env).read_text().splitlines()[-1])["event"] == "superseded"


def test_failed_new_admission_does_not_revive_older_deferred_request(tmp_path):
    """B 接纳后部署失败并回滚，旧 deferred A 仍被水位拒绝。"""
    env = _with_deferred_identity(_intent_env(tmp_path), 260)
    env["BUSY_LOCK_TIMEOUT"] = "2"
    env["GIT_SHA"] = "failed-version-b"
    previous_good = _seed_prev_good(env, "last-good-base")
    docker_log = Path(env["DOCKER_LOG"])
    _write_exec(Path(env["DOCKER_BIN"]), _mock_docker_matrix(docker_log, fail_compose_on=1))

    failed = _run(env)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert previous_good.read_text().strip() == "last-good-base"
    admitted = json.loads(_intent_path(env).read_text())
    assert admitted["run_number"] == 260
    assert admitted["status"] == "admitted"

    busy_ready = Path(env["BUSY_LOCK_FILE"] + ".ready-after-failure")
    holder = _hold_lock(Path(env["BUSY_LOCK_FILE"]), "-s", busy_ready)
    try:
        _with_deferred_identity(env, 259)
        env["GIT_SHA"] = "older-version-a"
        older = _run(env)
    finally:
        holder.terminate()
        holder.wait()

    assert older.returncode == 3, older.stdout + older.stderr
    assert "intent: superseded" in older.stdout
    final = json.loads(_intent_path(env).read_text())
    assert final["run_number"] == 260
    assert final["status"] == "admitted"
    assert previous_good.read_text().strip() == "last-good-base"


def test_current_pending_intent_replays_through_real_deploy_script(tmp_path):
    """收敛器将 pending 标为 running 后，配对 helper 验证身份并部署真实固化脚本。"""
    env = _with_deferred_identity(_intent_env(tmp_path), 235)
    env["GIT_SHA"] = "replay-version"
    latest = tmp_path / "replay-latest-image"
    running = tmp_path / "replay-running-image"
    docker_log = Path(env["DOCKER_LOG"])
    _write_exec(Path(env["DOCKER_BIN"]), _stateful_docker(docker_log, latest, running))
    _seed_deferred(env, 235)
    companion = _companion_path(env, 235)
    assert companion.read_bytes() == (REPO_ROOT / "scripts" / "deferred_intent.sh").read_bytes()
    bindir = Path(env["DOCKER_BIN"]).parent
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"

    result = _run_converge(env)
    assert result.returncode == 0, result.stdout + result.stderr
    intent = json.loads(_intent_path(env).read_text())
    assert intent["status"] == "deployed"
    assert intent["run_number"] == 235
    replay_state = Path(env["DEPLOY_DIR"]) / ".deploy-state"
    assert (replay_state / "last_good_tag").read_text().strip() == "replay-version"
    assert latest.read_text().strip() == f"{env['ACR_IMAGE']}:replay-version"
    assert running.read_text().strip() == f"{env['ACR_IMAGE']}:replay-version"
    assert sum(line.startswith("compose up -d") for line in docker_log.read_text().splitlines()) == 1
    assert "intent: superseded" not in result.stdout + result.stderr


def test_converge_rechecks_admission_after_waiting_at_real_host_lock(tmp_path):
    """A 已取快照并停在 host-flock 边界；B 真部署先完成，A 不调用 compose。"""
    env = _with_deferred_identity(_intent_env(tmp_path), 240)
    env["BUSY_LOCK_TIMEOUT"] = "2"
    env["GIT_SHA"] = "version-a"
    latest = tmp_path / "latest-image"
    running = tmp_path / "running-image"
    docker_log = Path(env["DOCKER_LOG"])
    _write_exec(Path(env["DOCKER_BIN"]), _stateful_docker(docker_log, latest, running))
    _seed_deferred(env, 240)

    bindir = Path(env["DOCKER_BIN"]).parent
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    gate = tmp_path / "release-a-retry"
    waiting = tmp_path / "a-retry-after-host-contention"
    host_ready = tmp_path / "host-lock-ready"
    host_holder = _hold_lock(Path(env["HOST_LOCK"]), "-x", host_ready)
    real_sleep = subprocess.run(["bash", "-c", "command -v sleep"], check=True, text=True, capture_output=True).stdout.strip()
    _write_exec(
        bindir / "sleep",
        "#!/bin/bash\n"
        'if [ "${D3_RUN_NUMBER:-}" = "240" ]; then\n'
        f'  : > "{waiting}"\n'
        f'  while [ ! -e "{gate}" ]; do "{real_sleep}" 0.01; done\n'
        "  exit 0\n"
        "fi\n"
        f'exec "{real_sleep}" "$@"\n',
    )

    converger = subprocess.Popen(
        ["bash", str(CONVERGE)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        for _ in range(1000):
            if waiting.exists():
                break
            if converger.poll() is not None:
                stdout, stderr = converger.communicate()
                raise AssertionError(f"converge exited before host-lock retry barrier: {stdout}{stderr}")
            subprocess.run(["sleep", "0.01"], check=True)
        assert waiting.exists(), "A did not contend on the real host flock"

        host_holder.terminate()
        host_holder.wait()
        _with_deferred_identity(env, 241)
        env["GIT_SHA"] = "version-b"
        b = _run(env)
        assert b.returncode == 0, b.stdout + b.stderr
        assert (Path(env["STATE_DIR"]) / "last_good_tag").read_text().strip() == "version-b"
        assert latest.read_text().strip() == f"{env['ACR_IMAGE']}:version-b"
        assert running.read_text().strip() == f"{env['ACR_IMAGE']}:version-b"
        log = Path(env["DOCKER_LOG"])
        calls_after_b = log.read_text().splitlines()
        compose_after_b = [line for line in calls_after_b if line.startswith("compose up -d")]
        assert len(compose_after_b) == 1

        gate.touch()
        stdout, stderr = converger.communicate(timeout=30)
        assert converger.returncode == 0, stdout + stderr
    finally:
        gate.touch()
        if host_holder.poll() is None:
            host_holder.terminate()
            host_holder.wait()
        if converger.poll() is None:
            converger.terminate()
            converger.wait()
    assert "intent: superseded" in stdout + stderr
    calls_after_a = log.read_text().splitlines()
    assert calls_after_a == calls_after_b
    assert [line for line in calls_after_a if line.startswith("compose up -d")] == compose_after_b
    assert (Path(env["STATE_DIR"]) / "last_good_tag").read_text().strip() == "version-b"
    assert latest.read_text().strip() == f"{env['ACR_IMAGE']}:version-b"
    assert running.read_text().strip() == f"{env['ACR_IMAGE']}:version-b"
    events = [json.loads(line) for line in _events_path(env).read_text().splitlines()]
    assert any(row["event"] == "superseded" and row["run_number"] == 240 for row in events)
    current = json.loads(_intent_path(env).read_text())
    assert current["run_number"] == 241
    assert current["status"] == "admitted"


def test_replay_rc6_for_same_running_intent_becomes_failed_p1(tmp_path):
    """真实部署命令 rc=6 且身份仍属 running 时，终态为 failed/P1 而非下轮 interrupted/P0。"""
    env = _with_deferred_identity(_intent_env(tmp_path), 245)
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/admission-rc6-test"
    curl_log = tmp_path / "rc6-curl-argv"
    _write_exec(Path(env["CURL_BIN"]), f"#!/bin/bash\nprintf '%s\\n' \\\"$@\\\" > '{curl_log}'\nprintf 200\n")
    docker_log = Path(env["DOCKER_LOG"])
    failing_docker = _mock_docker_matrix(docker_log, fail_compose_on=1).replace("exit 23", "exit 6")
    _write_exec(Path(env["DOCKER_BIN"]), failing_docker)
    _seed_deferred(env, 245, webhook=webhook)
    bindir = Path(env["DOCKER_BIN"]).parent
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"

    result = _run_converge(env)
    assert result.returncode == 0, result.stdout + result.stderr
    intent = json.loads(_intent_path(env).read_text())
    assert intent["status"] == "failed"
    rows = [json.loads(line) for line in _events_path(env).read_text().splitlines()]
    failed = [row for row in rows if row["event"] == "failed"]
    assert failed[-1]["rc"] == 6
    assert "P1" in curl_log.read_text()
    assert webhook not in result.stdout + result.stderr


def test_converge_rejects_legacy_script_without_admission_companion(tmp_path):
    """旧固化脚本缺少锁内校验伴侣时，置终态告警，不执行脚本。"""
    env = _with_deferred_identity(_intent_env(tmp_path), 250)
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/admission-legacy-test"
    curl_log = tmp_path / "legacy-curl-argv"
    _write_exec(Path(env["CURL_BIN"]), f"#!/bin/bash\nprintf '%s\\n' \\\"$@\\\" > '{curl_log}'\nprintf 200\n")
    _seed_deferred(env, 250, webhook=webhook)
    script_ref = _load_intent(env)["script_ref"]
    scripts = Path(env["HOME"]) / ".d3-converge" / "scripts"
    script = scripts / script_ref
    ran = tmp_path / "legacy-script-ran"
    script.write_text(f"#!/bin/bash\ntouch '{ran}'\nexit 0\n")
    script.chmod(0o700)
    companion = _companion_path(env, 250)
    companion.unlink()

    result = _run_converge(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not ran.exists()
    assert _load_intent(env)["status"] == "failed"
    rows = [json.loads(line) for line in _events_path(env).read_text().splitlines()]
    assert any(row["event"] == "legacy-script-rejected" for row in rows)
    assert "P1" in curl_log.read_text()
    assert webhook not in result.stdout + result.stderr


def test_workflow_rc6_explains_stale_run_must_be_triggered_again(tmp_path):
    """workflow 将 admission rc 专门判红并提示新触发 run，不落入成功或通用失败。"""
    from test_deferred_deploy_workflow import _run_deploy_step

    completed, output = _run_deploy_step(
        tmp_path,
        {
            "DEFERRED_DEPLOY": "true",
            "FEISHU_CI_WEBHOOK": "",
            "NOTIFY_ON_SUCCESS": "false",
            "D3_REPO": "zlxlabs/ci-templates",
            "D3_RUN_URL": "https://example.test/run/1",
            "MOCK_REMOTE_OUTPUT": "[deploy][evidence] intent: superseded",
            "MOCK_REMOTE_RC": "6",
        },
    )
    assert completed.returncode == 6, completed.stdout + completed.stderr
    assert "trigger a new workflow run" in completed.stdout.lower()
    assert "deploy ok" not in completed.stdout
    assert "admission superseded" in completed.stdout.lower()
