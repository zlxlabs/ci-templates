"""C1: pull_and_deploy.sh 在 deferred 时持久化部署意图。

file-size 闸把 test_pull_and_deploy.py 锁在 2237 行零余量,本文件承接新增断言。
驱动真实脚本子进程(假 DOCKER_BIN/CURL_BIN、tmp_path 真 flock、HOME 指向 tmp),
断言 stdout 标记字节与意图文件 JSON 字节/权限。
"""
from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess
import threading
import time
from pathlib import Path

from test_pull_and_deploy import REPO_ROOT, _base_env, _run

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/c1-test-secret-do-not-leak"

DEPLOY_ENV_KEYS = (
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
)

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def sanitize_deploy_dir(deploy_dir: str) -> str:
    """Python 镜像,与 pull_and_deploy.sh::sanitize_deploy_dir 逐字符同一规则。"""
    stripped = deploy_dir.lstrip("/")
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", stripped)
    hash8 = hashlib.sha256(deploy_dir.encode()).hexdigest()[:8]
    return f"{sanitized}-{hash8}"


def _intent_env(tmp_path: Path, *, timeout: str = "1") -> dict:
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    home = tmp_path / "home"
    home.mkdir()
    lock_file = tmp_path / "busy.lock"
    lock_file.touch()
    env.update(
        HOME=str(home),
        INTENT_FILE_ENABLED="true",
        BUSY_LOCK_FILE=str(lock_file),
        BUSY_LOCK_TIMEOUT=timeout,
        D3_RUN_NUMBER="131",
        D3_RUN_ATTEMPT="1",
        D3_RUN_ID="44111383925",
        D3_RUN_URL="https://github.com/zlxlabs/live-recorder/actions/runs/44111383925",
        D3_REPO="zlxlabs/live-recorder",
        D3_INTENT_WEBHOOK=WEBHOOK,
        D3_NOTIFY_ON_SUCCESS="false",
        ROLLBACK_SAFETY="safe",
        LOCAL_IMAGE="",
        ONESHOT_SERVICES="",
    )
    return env


def _home(env: dict) -> Path:
    return Path(env["HOME"])


def _intent_path(env: dict) -> Path:
    stem = sanitize_deploy_dir(env["DEPLOY_DIR"])
    return _home(env) / ".d3-converge" / "intents" / f"{stem}.json"


def _events_path(env: dict) -> Path:
    return _home(env) / ".d3-converge" / "events.jsonl"


def _assert_no_webhook_leak(res, env: dict) -> None:
    blob = res.stdout + res.stderr
    assert WEBHOOK not in blob
    events = _events_path(env)
    if events.is_file():
        assert WEBHOOK not in events.read_text()


def _assert_mode(path: Path, mode: int) -> None:
    assert stat.S_IMODE(path.stat().st_mode) == mode, oct(path.stat().st_mode)


def _assert_intent_payload(intent: dict, env: dict) -> None:
    assert intent["schema_version"] == 1
    assert intent["status"] == "pending"
    assert intent["repo"] == env["D3_REPO"]
    assert intent["run_number"] == int(env["D3_RUN_NUMBER"])
    assert intent["run_attempt"] == int(env["D3_RUN_ATTEMPT"])
    assert intent["run_id"] == int(env["D3_RUN_ID"])
    assert intent["run_url"] == env["D3_RUN_URL"]
    assert intent["git_sha"] == env["GIT_SHA"]
    assert intent["script_ref"] == f"pull_and_deploy.{sanitize_deploy_dir(env['DEPLOY_DIR'])}.sh"
    assert ISO_Z.match(intent["created_at"])
    assert ISO_Z.match(intent["updated_at"])
    assert intent["attempts"] == 0
    assert intent["last_attempt_at"] is None
    assert intent["last_alert_at"] is None
    assert intent["notify_on_success"] is False
    assert intent["webhook"] == WEBHOOK
    assert set(intent["deploy_env"]) == set(DEPLOY_ENV_KEYS)
    for key in DEPLOY_ENV_KEYS:
        assert intent["deploy_env"][key] == env.get(key, ""), key


def _load_intent(env: dict) -> dict:
    path = _intent_path(env)
    assert path.is_file(), path
    _assert_mode(path, 0o600)
    converge = _home(env) / ".d3-converge"
    _assert_mode(converge, 0o700)
    _assert_mode(converge / "intents", 0o700)
    return json.loads(path.read_text())


def _event_lines(env: dict) -> list[dict]:
    text = _events_path(env).read_text()
    assert text.endswith("\n")
    return [json.loads(line) for line in text.splitlines() if line]


def _hold_shared(lock_file: str):
    return subprocess.Popen(["flock", "-s", lock_file, "sleep", "30"])


def _hold_exclusive(lock_file: str):
    return subprocess.Popen(["flock", "-x", lock_file, "sleep", "30"])


def test_busy_lock_timeout_writes_intent_with_whitelist_and_mode_0600(tmp_path):
    """(a)(c)(g) 忙锁 flock -w 超时分支(原 :731)写入意图,白名单逐键、0600、无 webhook 泄漏。"""
    env = _intent_env(tmp_path)
    holder = _hold_shared(env["BUSY_LOCK_FILE"])
    try:
        time.sleep(0.5)
        res = _run(env)
    finally:
        holder.terminate()
        holder.wait()

    assert res.returncode == 3, res.stdout + res.stderr
    assert "busy lock not acquired within budget" in res.stdout
    assert "[deploy][evidence] intent: accepted" in res.stdout
    intent = _load_intent(env)
    _assert_intent_payload(intent, env)
    events = _event_lines(env)
    assert events[-1]["event"] == "accepted"
    assert events[-1]["run_number"] == 131
    _assert_no_webhook_leak(res, env)


def test_host_lock_budget_writes_intent(tmp_path):
    """(a) 整机锁占满预算分支(原 :726)同样写入意图。"""
    env = _intent_env(tmp_path, timeout="2")
    holder = _hold_exclusive(env["HOST_LOCK"])
    try:
        time.sleep(0.5)
        res = _run(env)
    finally:
        holder.terminate()
        holder.wait()

    assert res.returncode == 3, res.stdout + res.stderr
    assert "host deploy lock busy through the whole" in res.stdout
    assert "[deploy][evidence] intent: accepted" in res.stdout
    intent = _load_intent(env)
    _assert_intent_payload(intent, env)
    assert _event_lines(env)[-1]["event"] == "accepted"
    _assert_no_webhook_leak(res, env)


def test_run_number_attempt_monotonic_overwrite_and_supersede(tmp_path):
    """(b)(c) 更大 run 覆盖;更小不覆盖且 superseded;同 run 更高 attempt 覆盖。"""
    env = _intent_env(tmp_path)
    holder = _hold_shared(env["BUSY_LOCK_FILE"])
    try:
        time.sleep(0.5)

        env["D3_RUN_NUMBER"] = "100"
        env["D3_RUN_ATTEMPT"] = "1"
        first = _run(env)
        assert first.returncode == 3, first.stdout + first.stderr
        assert "[deploy][evidence] intent: accepted" in first.stdout
        assert _load_intent(env)["run_number"] == 100
        _assert_no_webhook_leak(first, env)

        env["D3_RUN_NUMBER"] = "101"
        newer = _run(env)
        assert newer.returncode == 3, newer.stdout + newer.stderr
        assert "[deploy][evidence] intent: accepted" in newer.stdout
        assert _load_intent(env)["run_number"] == 101
        assert _load_intent(env)["run_attempt"] == 1
        _assert_no_webhook_leak(newer, env)

        env["D3_RUN_NUMBER"] = "50"
        older = _run(env)
        assert older.returncode == 3, older.stdout + older.stderr
        assert "[deploy][evidence] intent: superseded" in older.stdout
        assert "intent: accepted" not in older.stdout
        kept = _load_intent(env)
        assert kept["run_number"] == 101
        assert kept["run_attempt"] == 1
        _assert_no_webhook_leak(older, env)

        env["D3_RUN_NUMBER"] = "101"
        env["D3_RUN_ATTEMPT"] = "2"
        rerun = _run(env)
        assert rerun.returncode == 3, rerun.stdout + rerun.stderr
        assert "[deploy][evidence] intent: accepted" in rerun.stdout
        final = _load_intent(env)
        assert final["run_number"] == 101
        assert final["run_attempt"] == 2
        _assert_no_webhook_leak(rerun, env)
    finally:
        holder.terminate()
        holder.wait()

    events = _event_lines(env)
    kinds = [row["event"] for row in events]
    assert kinds == ["accepted", "accepted", "superseded", "accepted"]
    assert events[2]["run_number"] == 50
    assert events[2]["kept_run_number"] == 101
    assert events[3]["run_attempt"] == 2


def test_intent_dir_not_writable_emits_write_failed(tmp_path):
    """(d) 意图目录不可写 → intent: write-failed 且仍 exit 3。"""
    env = _intent_env(tmp_path)
    blocker = _home(env) / ".d3-converge"
    blocker.write_text("not-a-directory\n")
    holder = _hold_shared(env["BUSY_LOCK_FILE"])
    try:
        time.sleep(0.5)
        res = _run(env)
    finally:
        holder.terminate()
        holder.wait()

    assert res.returncode == 3, res.stdout + res.stderr
    assert "[deploy][evidence] intent: write-failed" in res.stdout
    assert "intent: accepted" not in res.stdout
    assert blocker.is_file()
    _assert_no_webhook_leak(res, env)


def test_enabled_without_busy_lock_exits_1(tmp_path):
    """(e) 启用但 BUSY_LOCK_FILE 为空 → exit 1 fail fast。"""
    env = _intent_env(tmp_path)
    env["BUSY_LOCK_FILE"] = ""
    res = _run(env)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "BUSY_LOCK_FILE" in res.stdout
    assert not (_home(env) / ".d3-converge").exists()
    _assert_no_webhook_leak(res, env)


def test_enabled_without_run_number_exits_1(tmp_path):
    """(e) 启用但 D3_RUN_NUMBER 缺失 → exit 1。"""
    env = _intent_env(tmp_path)
    env.pop("D3_RUN_NUMBER", None)
    res = _run(env)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "D3_RUN_NUMBER" in res.stdout
    assert not (_home(env) / ".d3-converge").exists()


def test_enabled_with_non_numeric_run_number_exits_1(tmp_path):
    """(e) 启用但 D3_RUN_NUMBER 非数字 → exit 1。"""
    env = _intent_env(tmp_path)
    env["D3_RUN_NUMBER"] = "abc"
    res = _run(env)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "D3_RUN_NUMBER" in res.stdout
    assert not (_home(env) / ".d3-converge").exists()


def test_enabled_without_run_attempt_exits_1(tmp_path):
    """(e) 启用但 D3_RUN_ATTEMPT 缺失 → exit 1。"""
    env = _intent_env(tmp_path)
    env.pop("D3_RUN_ATTEMPT", None)
    res = _run(env)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "D3_RUN_ATTEMPT" in res.stdout
    assert not (_home(env) / ".d3-converge").exists()


def test_disabled_does_not_create_d3_converge(tmp_path):
    """(f) 未启用 → 不创建 ~/.d3-converge,stdout 无 intent 标记(现状不变)。

    红验不适用:本断言锁的是「关闭时与 base 逐字节同行为」,在 base 上必然绿。
    """
    env = _intent_env(tmp_path)
    env.pop("INTENT_FILE_ENABLED", None)
    holder = _hold_shared(env["BUSY_LOCK_FILE"])
    try:
        time.sleep(0.5)
        res = _run(env)
    finally:
        holder.terminate()
        holder.wait()

    assert res.returncode == 3, res.stdout + res.stderr
    assert "DEFERRED" in res.stdout
    assert "intent:" not in res.stdout
    assert not (_home(env) / ".d3-converge").exists()
    _assert_no_webhook_leak(res, env)


def _copy_script_alone(tmp_path: Path) -> Path:
    """Simulate scp of pull_and_deploy.sh without deferred_intent.sh."""
    alone = tmp_path / "alone"
    alone.mkdir()
    script = alone / "pull_and_deploy.sh"
    script.write_text((REPO_ROOT / "scripts" / "pull_and_deploy.sh").read_text())
    return script


def test_enabled_without_sibling_script_exits_1(tmp_path):
    """启用但 deferred_intent.sh 缺失 → 配置校验 exit 1，不得静默跳过。"""
    env = _intent_env(tmp_path)
    script = _copy_script_alone(tmp_path)
    res = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True
    )
    assert res.returncode == 1, res.stdout + res.stderr
    assert "deferred_intent.sh" in res.stdout
    assert not (_home(env) / ".d3-converge").exists()
    _assert_no_webhook_leak(res, env)


def test_disabled_without_sibling_script_still_defers(tmp_path):
    """未启用不得要求 deferred_intent.sh 存在。红验不适用:base 同样不依赖该文件。"""
    env = _intent_env(tmp_path)
    env.pop("INTENT_FILE_ENABLED", None)
    script = _copy_script_alone(tmp_path)
    holder = _hold_shared(env["BUSY_LOCK_FILE"])
    try:
        time.sleep(0.5)
        res = subprocess.run(
            ["bash", str(script)], env=env, capture_output=True, text=True
        )
    finally:
        holder.terminate()
        holder.wait()
    assert res.returncode == 3, res.stdout + res.stderr
    assert "DEFERRED" in res.stdout
    assert "intent:" not in res.stdout
    assert not (_home(env) / ".d3-converge").exists()
    _assert_no_webhook_leak(res, env)


def _prepare_intent_lock(env: dict) -> Path:
    converge = _home(env) / ".d3-converge"
    converge.mkdir(mode=0o700)
    lock = converge / "intents.lock"
    lock.touch()
    return lock


def test_intent_lock_waits_then_writes(tmp_path):
    """后台持有 intents.lock 时写入必须阻塞等待，锁释放后 accepted。"""
    env = _intent_env(tmp_path)
    lock = _prepare_intent_lock(env)
    busy = _hold_shared(env["BUSY_LOCK_FILE"])
    holder = subprocess.Popen(["flock", "-x", str(lock), "sleep", "3"])
    try:
        time.sleep(0.4)
        start = time.monotonic()
        res = _run(env)
        elapsed = time.monotonic() - start
    finally:
        holder.terminate()
        holder.wait()
        busy.terminate()
        busy.wait()

    assert res.returncode == 3, res.stdout + res.stderr
    assert "[deploy][evidence] intent: accepted" in res.stdout
    assert elapsed >= 2.2, elapsed
    _assert_intent_payload(_load_intent(env), env)
    _assert_no_webhook_leak(res, env)


def test_intent_lock_timeout_emits_write_failed(tmp_path):
    """intents.lock 持有超过 flock -w 上限 → intent: write-failed，不落盘。"""
    env = _intent_env(tmp_path)
    env["INTENT_LOCK_TIMEOUT"] = "2"
    lock = _prepare_intent_lock(env)
    busy = _hold_shared(env["BUSY_LOCK_FILE"])
    holder = subprocess.Popen(["flock", "-x", str(lock), "sleep", "20"])
    try:
        time.sleep(0.4)
        start = time.monotonic()
        res = _run(env)
        elapsed = time.monotonic() - start
    finally:
        holder.terminate()
        holder.wait()
        busy.terminate()
        busy.wait()

    assert res.returncode == 3, res.stdout + res.stderr
    assert "[deploy][evidence] intent: write-failed" in res.stdout
    assert "intent: accepted" not in res.stdout
    assert not _intent_path(env).exists()
    assert elapsed >= 2.2, elapsed
    assert elapsed < 10, elapsed
    _assert_no_webhook_leak(res, env)


def test_concurrent_writes_keep_larger_run_number(tmp_path):
    """两个子进程同时 deferred 写不同 run_number，最终文件保留较大的那个。"""
    env = _intent_env(tmp_path)
    busy = _hold_shared(env["BUSY_LOCK_FILE"])
    results: dict[int, subprocess.CompletedProcess] = {}

    def worker(run_number: int) -> None:
        child = dict(env)
        child["D3_RUN_NUMBER"] = str(run_number)
        results[run_number] = _run(child)

    try:
        time.sleep(0.4)
        t_small = threading.Thread(target=worker, args=(10,))
        t_large = threading.Thread(target=worker, args=(20,))
        t_small.start()
        t_large.start()
        t_small.join(timeout=20)
        t_large.join(timeout=20)
        assert not t_small.is_alive() and not t_large.is_alive()
    finally:
        busy.terminate()
        busy.wait()

    assert results[10].returncode == 3, results[10].stdout + results[10].stderr
    assert results[20].returncode == 3, results[20].stdout + results[20].stderr
    intent = _load_intent(env)
    assert intent["run_number"] == 20
    assert intent["run_attempt"] == 1
    _assert_no_webhook_leak(results[10], env)
    _assert_no_webhook_leak(results[20], env)


def test_events_unwritable_does_not_replace_intent(tmp_path):
    """events.jsonl 不可写 → write-failed，已发布的意图文件字节不变。

    把 events.jsonl 换成目录，对目录做 >> 追加 root 也会失败（chmod 0444
    在 euid=0 下无效，gate quality job 以 root 跑会假绿）。
    """
    env = _intent_env(tmp_path)
    busy = _hold_shared(env["BUSY_LOCK_FILE"])
    try:
        time.sleep(0.4)
        env["D3_RUN_NUMBER"] = "50"
        first = _run(env)
        assert first.returncode == 3, first.stdout + first.stderr
        assert "[deploy][evidence] intent: accepted" in first.stdout
        before = _intent_path(env).read_bytes()
        events = _events_path(env)
        events.unlink()
        events.mkdir()

        env["D3_RUN_NUMBER"] = "80"
        second = _run(env)
        assert second.returncode == 3, second.stdout + second.stderr
        assert "[deploy][evidence] intent: write-failed" in second.stdout
        assert "intent: accepted" not in second.stdout
        assert _intent_path(env).read_bytes() == before
        assert json.loads(before)["run_number"] == 50
        _assert_no_webhook_leak(second, env)
    finally:
        busy.terminate()
        busy.wait()
