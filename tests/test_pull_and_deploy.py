"""TDD for pull_and_deploy.sh — the SSH-side deploy internals.

Contract (eng-review A3 / T4):
  - per-host flock so concurrent deploys to the same host serialize
  - deploys an IMMUTABLE git-SHA image tag, records the last good tag
  - post-deploy health probe (warmup / retries / expected status)
  - probe failure -> automatic rollback to the previous good tag
  - rollback must NOT promote the failed tag to "last good"

docker / curl are mocked so no real daemon or network is touched. The script
honours DOCKER_BIN / CURL_BIN overrides for exactly this reason.
"""
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "pull_and_deploy.sh"

RECONCILE_IMAGE_ID = "sha256:deadbeef"


def _reconcile_ok_bash(*, image_id=RECONCILE_IMAGE_ID, service="app", container="cid-app"):
    """Identity probes used after a healthy deploy. --format distinguishes
    reconcile inspect from pull_image's existence check."""
    return f'''
if [ "$1" = compose ] && [[ " $* " == *" config --services "* ]]; then
  printf '{service}\\n'; exit 0
fi
if [ "$1" = compose ] && [[ " $* " == *" ps -q --status running"* ]]; then
  printf '{container}\\n'; exit 0
fi
if [ "$1" = image ] && [ "$2" = inspect ] && [[ " $* " == *" --format "* ]]; then
  printf '{image_id}\\n'; exit 0
fi
if [ "$1" = inspect ] && [[ " $* " == *" --format "* ]]; then
  printf '{image_id}\\n'; exit 0
fi
'''


def _write_exec(path: Path, body: str):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _mock_docker(log_path: Path, compose_sleep: float = 0.0) -> str:
    return f"""#!/bin/bash
echo "$@" >> "{log_path}"
{_reconcile_ok_bash()}
if [ "$1" = "compose" ]; then
  sleep {compose_sleep}
fi
exit 0
"""


def _mock_curl(status: str) -> str:
    # mimics: curl -s -o /dev/null -w '%{{http_code}}' ... -> prints an HTTP code
    return f"""#!/bin/bash
printf '%s' "{status}"
exit 0
"""


def _mock_curl_sequence(log_path: Path, sequence: list[tuple[str, int]]) -> str:
    """Return a curl mock with a deterministic (HTTP code, curl rc) sequence."""
    cases = []
    for attempt, (status, curl_rc) in enumerate(sequence, start=1):
        cases.append(
            f"{attempt}) echo '{attempt}' >> \"{log_path}\"; "
            f"printf '%s' '{status}'; exit {curl_rc};;"
        )
    cases_text = "\n".join(cases)
    return f"""#!/bin/bash
count_file="{log_path}.count"
attempt=$(cat "$count_file" 2>/dev/null || echo 0)
attempt=$((attempt + 1)); echo "$attempt" > "$count_file"
case "$attempt" in
{cases_text}
*) echo "$attempt" >> "{log_path}"; printf '%s' '500'; exit 0;;
esac
"""


def _mock_docker_matrix(log_path: Path, fail_compose_on: int | None = None) -> str:
    """Docker mock for the rollback outcome matrix."""
    fail_clause = ""
    if fail_compose_on is not None:
        fail_clause = f";\n  [ \"$count\" -eq {fail_compose_on} ] && exit 23"
    return f"""#!/bin/bash
echo "$@" >> "{log_path}"
{_reconcile_ok_bash()}
if [ "$1" = "compose" ]; then
  count_file="{log_path}.compose-count"
  count=$(cat "$count_file" 2>/dev/null || echo 0)
  count=$((count + 1)); echo "$count" > "$count_file"{fail_clause}
fi
exit 0
"""


def _mock_docker_evidence(log_path: Path, evidence_sleep: float = 0.0) -> str:
    return f"""#!/bin/bash
echo "$@" >> "{log_path}"
if [ "$1" = "compose" ] && [[ " $* " == *" ps "* || " $* " == *" logs "* ]]; then
  sleep {evidence_sleep}
fi
if [ "$1" = "compose" ] && [ "$2" = "ps" ]; then
  printf '%s\n' 'new-container running'
fi
if [ "$1" = "compose" ] && [ "$2" = "logs" ]; then
  printf '%s\n' 'new-container last-line'
fi
exit 0
"""


def _mock_docker_rollback_pull_failure(log_path: Path) -> str:
    return f"""#!/bin/bash
echo "$@" >> "{log_path}"
if [ "$1" = "pull" ]; then
  count_file="{log_path}.pull-count"
  count=$(cat "$count_file" 2>/dev/null || echo 0)
  count=$((count + 1)); echo "$count" > "$count_file"
  [ "$count" -ge 2 ] && exit 23
fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ] && [[ "$3" == *old1111 ]]; then
  exit 1
fi
exit 0
"""


def _base_env(tmp_path: Path, *, mock_dir: Path, status: str = "200",
              compose_sleep: float = 0.0) -> dict:
    docker_log = tmp_path / "docker.log"
    docker = mock_dir / "docker"
    curl = mock_dir / "curl"
    _write_exec(docker, _mock_docker(docker_log, compose_sleep))
    _write_exec(curl, _mock_curl(status))

    deploy_dir = tmp_path / "app"
    deploy_dir.mkdir(exist_ok=True)
    (deploy_dir / "docker-compose.yml").write_text("services: {}\n")

    env = dict(os.environ)
    env.update(
        IMAGE_NAME="demo",
        ACR_IMAGE="registry.example.com/ns/demo",
        GIT_SHA="abc1234",
        DEPLOY_DIR=str(deploy_dir),
        STATE_DIR=str(tmp_path / "state"),
        HOST_LOCK=str(tmp_path / "host.lock"),
        HEALTHCHECK_URL="http://localhost/health",
        HEALTHCHECK_EXPECT_STATUS="200",
        HEALTHCHECK_RETRIES="2",
        HEALTHCHECK_INTERVAL="0",
        HEALTHCHECK_WARMUP="0",
        HEALTHCHECK_TIMEOUT="1",
        DOCKER_BIN=str(docker),
        CURL_BIN=str(curl),
        DOCKER_LOG=str(docker_log),
    )
    return env


def _run(env, extra=None, timeout=None):
    e = dict(env)
    if extra:
        e.update(extra)
    return subprocess.run(
        ["bash", str(SCRIPT)], env=e, capture_output=True, text=True, timeout=timeout
    )


def test_script_exists_and_is_bash():
    assert SCRIPT.exists(), "pull_and_deploy.sh must exist"
    assert SCRIPT.read_text().startswith("#!"), "must have a shebang"


def test_healthy_deploy_succeeds_and_records_good_tag(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr

    good = Path(env["STATE_DIR"]) / "last_good_tag"
    assert good.exists(), "must record the last good tag on success"
    assert good.read_text().strip() == "abc1234"


def test_deploys_immutable_git_sha_tag(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr

    docker_log = Path(env["DOCKER_LOG"]).read_text()
    # the SHA-tagged image must be pulled, never ":latest" from ACR
    assert "pull registry.example.com/ns/demo:abc1234" in docker_log
    assert "pull registry.example.com/ns/demo:latest" not in docker_log


def test_probe_failure_triggers_rollback(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()

    # 1st deploy is healthy -> records good tag "abc1234"
    env_ok = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    assert _run(env_ok).returncode == 0

    # 2nd deploy of a new SHA is unhealthy -> must roll back to abc1234
    env_bad = _base_env(tmp_path, mock_dir=mock_dir, status="500")
    env_bad["GIT_SHA"] = "def5678"
    Path(env_bad["DOCKER_LOG"]).write_text("")  # reset log for assertions
    res = _run(env_bad)

    assert res.returncode != 0, "an unhealthy deploy must report failure"

    docker_log = Path(env_bad["DOCKER_LOG"]).read_text()
    # rollback retags the previous good image and brings it back up
    assert "abc1234" in docker_log, "rollback must redeploy the previous good tag"

    # the failed tag must NOT be promoted to last good
    good = (Path(env_bad["STATE_DIR"]) / "last_good_tag").read_text().strip()
    assert good == "abc1234", f"last good tag must stay abc1234, got {good}"


def test_probe_failure_without_previous_good_just_fails(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="500")
    res = _run(env)
    assert res.returncode != 0
    good = Path(env["STATE_DIR"]) / "last_good_tag"
    assert not good.exists(), "must not record a bad deploy as good"


# 单镜像 lane 退出状态轴表：首次探针 × prev_good × 回滚 compose × 回滚探针。
# 这张表刻意把无 HEALTHCHECK_URL 的不可达格子也锁住：它必须成功且不回滚。
@pytest.mark.parametrize(
    "case",
    [
        {
            "name": "probe-pass-with-prev-good",
            "status_sequence": [("200", 0)],
            "prev_good": "old1111",
            "git_sha": "new2222",
            "healthcheck_url": "http://localhost/health",
            "expected_rc": 0,
            "expected_last_good": "new2222",
            "rollback_tag": None,
        },
        {
            "name": "probe-skipped-with-prev-good",
            "status_sequence": [],
            "prev_good": "old1111",
            "git_sha": "new2222",
            "healthcheck_url": "",
            "expected_rc": 0,
            "expected_last_good": "new2222",
            "rollback_tag": None,
        },
        {
            "name": "probe-fails-without-prev-good",
            "status_sequence": [("500", 0), ("500", 0)],
            "prev_good": None,
            "git_sha": "new2222",
            "healthcheck_url": "http://localhost/health",
            "expected_rc": 4,
            "expected_last_good": None,
            "rollback_tag": None,
        },
        {
            "name": "probe-fails-with-same-prev-good",
            "status_sequence": [("500", 0), ("500", 0)],
            "prev_good": "new2222",
            "git_sha": "new2222",
            "healthcheck_url": "http://localhost/health",
            "expected_rc": 4,
            "expected_last_good": "new2222",
            "rollback_tag": None,
        },
        {
            "name": "rollback-probe-passes",
            "status_sequence": [("500", 0), ("500", 0), ("200", 0)],
            "prev_good": "old1111",
            "git_sha": "new2222",
            "healthcheck_url": "http://localhost/health",
            "expected_rc": 1,
            "expected_last_good": "old1111",
            "rollback_tag": "old1111",
        },
        {
            "name": "rollback-probe-fails",
            "status_sequence": [("500", 0), ("500", 0), ("500", 0), ("500", 0)],
            "prev_good": "old1111",
            "git_sha": "new2222",
            "healthcheck_url": "http://localhost/health",
            "expected_rc": 4,
            "expected_last_good": "old1111",
            "rollback_tag": "old1111",
        },
        {
            "name": "rollback-compose-fails",
            "status_sequence": [("500", 0), ("500", 0)],
            "prev_good": "old1111",
            "git_sha": "new2222",
            "healthcheck_url": "http://localhost/health",
            "expected_rc": 4,
            "expected_last_good": "old1111",
            "rollback_tag": "old1111",
            "fail_compose_on": 2,
        },
        {
            "name": "probe-skipped-never-rolls-back",
            "status_sequence": [],
            "prev_good": "old1111",
            "git_sha": "new2222",
            "healthcheck_url": "",
            "expected_rc": 0,
            "expected_last_good": "new2222",
            "rollback_tag": None,
        },
    ],
    ids=lambda case: case["name"],
)
def test_single_image_outcome_matrix(tmp_path, case):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="500")
    env["GIT_SHA"] = case["git_sha"]
    env["HEALTHCHECK_URL"] = case["healthcheck_url"]

    good = Path(env["STATE_DIR"]) / "last_good_tag"
    if case["prev_good"] is not None:
        good.parent.mkdir(parents=True)
        good.write_text(case["prev_good"] + "\n")

    curl_log = tmp_path / "curl-attempts.log"
    _write_exec(
        mock_dir / "curl",
        _mock_curl_sequence(curl_log, case["status_sequence"]),
    )
    _write_exec(
        mock_dir / "docker",
        _mock_docker_matrix(
            Path(env["DOCKER_LOG"]), case.get("fail_compose_on")
        ),
    )

    result = _run(env)

    assert result.returncode == case["expected_rc"], result.stdout + result.stderr
    actual_last_good = good.read_text().strip() if good.exists() else None
    assert actual_last_good == case["expected_last_good"]

    docker_log = Path(env["DOCKER_LOG"]).read_text()
    rollback_tag = case["rollback_tag"]
    if rollback_tag is None:
        assert "old1111" not in docker_log
    else:
        assert f":{rollback_tag}" in docker_log


def test_probe_evidence_keeps_http_code_and_curl_exit_code_sequence(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="500")
    env["GIT_SHA"] = "new2222"
    good = Path(env["STATE_DIR"]) / "last_good_tag"
    good.parent.mkdir(parents=True)
    good.write_text("old1111\n")

    curl_log = tmp_path / "curl-attempts.log"
    _write_exec(
        mock_dir / "curl",
        _mock_curl_sequence(
            curl_log,
            [("000", 28), ("503", 0), ("200", 0)],
        ),
    )
    _write_exec(
        mock_dir / "docker",
        _mock_docker_matrix(Path(env["DOCKER_LOG"])),
    )

    result = _run(env)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "[deploy][evidence] probe-attempts: 000(curl=28),503(curl=0)" in result.stdout
    assert "old version passed the same-budget health probe" in result.stdout


def test_rollback_evidence_is_captured_before_redeploy(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="500")
    env["GIT_SHA"] = "new2222"
    good = Path(env["STATE_DIR"]) / "last_good_tag"
    good.parent.mkdir(parents=True)
    good.write_text("old1111\n")

    curl_log = tmp_path / "curl-attempts.log"
    _write_exec(
        mock_dir / "curl",
        _mock_curl_sequence(
            curl_log,
            [("500", 0), ("500", 0), ("200", 0)],
        ),
    )
    _write_exec(
        mock_dir / "docker",
        _mock_docker_evidence(Path(env["DOCKER_LOG"])),
    )

    result = _run(env)

    assert result.returncode == 1, result.stdout + result.stderr
    ps_marker = "[deploy][evidence] compose-ps:"
    logs_marker = "[deploy][evidence] container-logs:"
    rollback_marker = "[deploy] rolling back to previous good tag old1111"
    assert ps_marker in result.stdout
    assert "[deploy][evidence] new-container running" in result.stdout
    assert logs_marker in result.stdout
    assert "[deploy][evidence] new-container last-line" in result.stdout
    assert result.stdout.index(ps_marker) < result.stdout.index(logs_marker)
    assert result.stdout.index(logs_marker) < result.stdout.index(rollback_marker)

    docker_log = Path(env["DOCKER_LOG"]).read_text()
    assert "compose logs --tail 100 --no-color" in docker_log


def test_rollback_evidence_timeout_does_not_block_rollback(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="500")
    env.update(GIT_SHA="new2222", EVIDENCE_TIMEOUT="1")
    good = Path(env["STATE_DIR"]) / "last_good_tag"
    good.parent.mkdir(parents=True)
    good.write_text("old1111\n")
    _write_exec(
        mock_dir / "curl",
        _mock_curl_sequence(
            tmp_path / "curl-attempts.log",
            [("500", 0), ("500", 0), ("200", 0)],
        ),
    )
    _write_exec(
        mock_dir / "docker",
        _mock_docker_evidence(Path(env["DOCKER_LOG"]), evidence_sleep=30),
    )

    started = time.monotonic()
    result = _run(env, timeout=6)
    elapsed = time.monotonic() - started

    assert result.returncode == 1, result.stdout + result.stderr
    assert elapsed < 6, f"evidence timeout did not bound the deploy: {elapsed:.2f}s"
    assert "compose-ps timed out after 1s" in result.stdout
    assert "container-logs timed out after 1s" in result.stdout
    assert "old1111" in Path(env["DOCKER_LOG"]).read_text(), "rollback must still run"


def test_rollback_pull_failure_returns_rc4_and_keeps_last_good(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="500")
    env["GIT_SHA"] = "new2222"
    env["PULL_RETRIES"] = "1"
    env["PULL_RETRY_DELAY"] = "0"
    good = Path(env["STATE_DIR"]) / "last_good_tag"
    good.parent.mkdir(parents=True)
    good.write_text("old1111\n")
    _write_exec(
        mock_dir / "curl",
        _mock_curl_sequence(
            tmp_path / "curl-attempts.log",
            [("500", 0), ("500", 0)],
        ),
    )
    _write_exec(
        mock_dir / "docker",
        _mock_docker_rollback_pull_failure(Path(env["DOCKER_LOG"])),
    )

    result = _run(env)

    assert result.returncode == 4, result.stdout + result.stderr
    assert good.read_text().strip() == "old1111"
    assert "rollback to old1111 failed" in result.stdout


def test_rollback_compose_failure_returns_rc4_and_keeps_last_good(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="500")
    env["GIT_SHA"] = "new2222"
    good = Path(env["STATE_DIR"]) / "last_good_tag"
    good.parent.mkdir(parents=True)
    good.write_text("old1111\n")
    _write_exec(
        mock_dir / "curl",
        _mock_curl_sequence(
            tmp_path / "curl-attempts.log",
            [("500", 0), ("500", 0)],
        ),
    )
    docker_log = Path(env["DOCKER_LOG"])
    compose_count = tmp_path / "compose-up.count"
    _write_exec(
        mock_dir / "docker",
        f'''#!/bin/bash
echo "$@" >> "{docker_log}"
if [ "$1" = "compose" ] && [ "$2" = "up" ] && [ "$3" = "-d" ]; then
  count=$(cat "{compose_count}" 2>/dev/null || echo 0)
  count=$((count + 1)); echo "$count" > "{compose_count}"
  [ "$count" -eq 2 ] && exit 23
fi
exit 0
''',
    )

    result = _run(env)

    assert result.returncode == 4, result.stdout + result.stderr
    assert good.read_text().strip() == "old1111"
    assert "rollback to old1111 failed" in result.stdout
    assert "rollback to old1111 complete" not in result.stdout


def test_concurrent_same_host_deploys_serialize(tmp_path):
    """Two deploys sharing HOST_LOCK must not run their critical sections at once."""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    event_log = tmp_path / "events.log"

    def launch(deploy_id, sub):
        sub.mkdir()
        env = _base_env(sub, mock_dir=sub, status="200", compose_sleep=0.4)
        env["HOST_LOCK"] = str(tmp_path / "shared-host.lock")  # same host lock
        env["DEPLOY_EVENT_LOG"] = str(event_log)
        env["DEPLOY_ID"] = deploy_id
        return _run(env)

    results = {}

    def worker(name):
        results[name] = launch(name, tmp_path / name)

    t1 = threading.Thread(target=worker, args=("A",))
    t2 = threading.Thread(target=worker, args=("B",))
    t1.start()
    time.sleep(0.05)  # ensure A grabs the lock first
    t2.start()
    t1.join()
    t2.join()

    assert results["A"].returncode == 0, results["A"].stderr
    assert results["B"].returncode == 0, results["B"].stderr

    events = [ln.strip() for ln in event_log.read_text().splitlines() if ln.strip()]
    # serialized critical sections => enter/exit are never interleaved
    assert events == ["enter:A", "exit:A", "enter:B", "exit:B"], events


def _mock_docker_flaky_pull(log_path: Path, fail_pulls: int, image_local: bool) -> str:
    """docker mock: first `fail_pulls` pull calls fail; `image inspect` mirrors local presence."""
    return f"""#!/bin/bash
echo "$@" >> "{log_path}"
{_reconcile_ok_bash()}
if [ "$1" = "pull" ]; then
  count_file="{log_path}.pullcount"
  n=$(cat "$count_file" 2>/dev/null || echo 0)
  n=$((n+1)); echo "$n" > "$count_file"
  [ "$n" -le {fail_pulls} ] && exit 1
  exit 0
fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  exit {0 if image_local else 1}
fi
exit 0
"""


def _flaky_env(tmp_path, *, fail_pulls: int, image_local: bool) -> dict:
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    _write_exec(mock_dir / "docker",
                _mock_docker_flaky_pull(Path(env["DOCKER_LOG"]), fail_pulls, image_local))
    env["PULL_RETRY_DELAY"] = "0"
    return env


def test_pull_flake_retries_then_succeeds(tmp_path):
    """registry 抖 2 次、第 3 次成功 → 部署照常绿。"""
    env = _flaky_env(tmp_path, fail_pulls=2, image_local=False)
    res = _run(env)
    assert res.returncode == 0, res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    assert log.count("pull ") == 3, log
    assert "compose up -d" in log


def test_pull_retries_default_is_six_succeeds_on_sixth_attempt(tmp_path):
    """2026-07-28 n305 实测 registry 探测 20% 失败率且成簇(见 pull_image 上方注释),
    默认预算从 3 次(10+20=30s)提到 6 次(10+20+30+40+50=150s)。前 5 次失败、
    第 6 次成功必须仍然放行部署 —— 锁定"退到第 6 次才判死"这条边界。"""
    env = _flaky_env(tmp_path, fail_pulls=5, image_local=False)
    assert env["PULL_RETRY_DELAY"] == "0"
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    assert log.count("pull ") == 6, log
    assert "compose up -d" in log


def test_pull_retries_default_exhausts_after_six_attempts(tmp_path):
    """前 6 次(等于 PULL_RETRIES 默认值)全部失败、且本地无镜像 → 第 7 次不再尝试,
    直接判死。锁定"至多 6 次 pull、不多不少"这条边界,防止后续改动悄悄漂移。"""
    env = _flaky_env(tmp_path, fail_pulls=6, image_local=False)
    res = _run(env)
    assert res.returncode != 0
    log = Path(env["DOCKER_LOG"]).read_text()
    assert log.count("pull ") == 6, log
    assert "compose up -d" not in log


def test_pull_exhausted_but_local_image_proceeds(tmp_path):
    """registry 全程不可达,但 SHA 镜像已在本地(预热/回滚残留)→ 放行部署。"""
    env = _flaky_env(tmp_path, fail_pulls=99, image_local=True)
    res = _run(env)
    assert res.returncode == 0, res.stderr
    assert "already local" in res.stdout
    assert "compose up -d" in Path(env["DOCKER_LOG"]).read_text()


def test_pull_exhausted_and_no_local_image_fails(tmp_path):
    """registry 不可达且本地也没镜像 → 该失败还是失败,不能拿旧 latest 蒙混。"""
    env = _flaky_env(tmp_path, fail_pulls=99, image_local=False)
    res = _run(env)
    assert res.returncode != 0
    log = Path(env["DOCKER_LOG"]).read_text()
    assert "compose up -d" not in log, "must not compose up without the image"
    good = Path(env["STATE_DIR"]) / "last_good_tag"
    assert not good.exists()


# --- busy-lock deploy gate (opt-in) -------------------------------------------
# 服务侧对一个文件持共享锁(LOCK_SH)表示"有不可打断任务在跑";部署脚本替换容器前
# 申请排他锁(LOCK_EX),拿不到就等,超时(BUSY_LOCK_TIMEOUT)就放弃(rc=3, deferred)。
# BUSY_LOCK_FILE 为空(默认)= 关闭该门禁,行为必须与现状逐字节不变。

def test_busy_lock_optout_leaves_behavior_unchanged(tmp_path):
    """不传 BUSY_LOCK_FILE(或为空)→ 现状不变:不多一次 pull,不多开 fd。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    # 只有 deploy_tag() 里那一次 pull;门禁的预拉不应该发生(因为门禁没开)
    assert log.count("pull ") == 1, log


def test_busy_lock_free_deploys_normally(tmp_path):
    """忙锁文件存在但空闲(无人持共享锁)→ 排他锁秒到,正常部署。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    lock_file = tmp_path / "busy.lock"
    lock_file.touch()
    env["BUSY_LOCK_FILE"] = str(lock_file)
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    assert "compose up -d" in log


def test_busy_lock_readonly_file_still_deploys(tmp_path):
    """锁文件只读(部署用户无写权限,真实 bootstrap 场景:容器内进程创建,0444/0644)
    → flock(2) 的互斥语义作用在 inode 上,不要求 fd 有写权限,只读打开一样能拿到
    排他锁,部署应正常进行,不能因为 Permission denied 被判死。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    lock_file = tmp_path / "busy.lock"
    lock_file.touch()
    os.chmod(lock_file, 0o444)
    env["BUSY_LOCK_FILE"] = str(lock_file)
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    assert "compose up -d" in log


def test_busy_lock_held_defers_untouched(tmp_path):
    """忙锁被服务侧共享锁占住,超预算仍未空闲 → rc=3,容器/last_good 完全不动。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    lock_file = tmp_path / "busy.lock"
    lock_file.touch()
    env["BUSY_LOCK_FILE"] = str(lock_file)
    env["BUSY_LOCK_TIMEOUT"] = "1"

    holder = subprocess.Popen(["flock", "-s", str(lock_file), "sleep", "5"])
    try:
        time.sleep(0.5)  # ensure the holder has actually grabbed the shared lock
        res = _run(env)
        assert res.returncode == 3, res.stdout + res.stderr
        log = Path(env["DOCKER_LOG"]).read_text()
        assert "compose up" not in log, log
        assert "tag " not in log, log
        good = Path(env["STATE_DIR"]) / "last_good_tag"
        assert not good.exists()
        assert "DEFERRED" in res.stdout
    finally:
        holder.terminate()
        holder.wait()


def test_busy_lock_released_within_budget_deploys(tmp_path):
    """忙锁在等待预算内被释放 → 正常拿到排他锁并完成部署。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    lock_file = tmp_path / "busy.lock"
    lock_file.touch()
    env["BUSY_LOCK_FILE"] = str(lock_file)
    env["BUSY_LOCK_TIMEOUT"] = "15"

    holder = subprocess.Popen(["flock", "-s", str(lock_file), "sleep", "2"])
    try:
        time.sleep(0.5)
        res = _run(env)
        assert res.returncode == 0, res.stdout + res.stderr
    finally:
        holder.terminate()
        holder.wait()


def test_busy_lock_missing_file_warns_and_proceeds(tmp_path):
    """锁文件(及其父目录)都不存在 → 创建 + 打显著 WARN,但不阻止部署(误配不 fail-closed)。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    env["BUSY_LOCK_FILE"] = str(tmp_path / "nope" / "busy.lock")
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "WARN" in res.stdout


def _mock_docker_admission_probe(log_path: Path) -> str:
    """docker mock:在 `compose` 调用当下,用宿主 flock 非阻塞探一次 BUSY_LOCK_FILE 的共享锁。

    这直接证明部署脚本在 compose up 期间确实握着 LOCK_EX —— 容器侧此刻申请
    LOCK_SH 必然失败(admission 已关闭),没有 TOCTOU 窗口。
    """
    return f"""#!/bin/bash
if [ "$1" = "compose" ]; then
  if flock -n -s "$BUSY_LOCK_FILE" true; then
    echo "sh_probe=open" >> "{log_path}"
  else
    echo "sh_probe=closed" >> "{log_path}"
  fi
fi
echo "$@" >> "{log_path}"
{_reconcile_ok_bash()}
exit 0
"""


def test_admission_closed_during_replace(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    _write_exec(mock_dir / "docker", _mock_docker_admission_probe(Path(env["DOCKER_LOG"])))

    lock_file = tmp_path / "busy.lock"
    lock_file.touch()
    env["BUSY_LOCK_FILE"] = str(lock_file)
    env["BUSY_LOCK_TIMEOUT"] = "15"

    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    assert "sh_probe=closed" in log, log


# --- P1 fix: don't hold one lock while waiting on the other ------------------
# GitHub concurrency is repo-scoped, so cross-service mutual exclusion on the
# same host relies entirely on HOST_LOCK. During the *waiting* phase, holding
# the busy lock while blocked on HOST_LOCK keeps admission closed even though
# no container replacement is happening yet — exactly what this gate is
# supposed to protect against. At any point while waiting, at most one of the
# two locks may be held: whichever one is currently being waited on.

def test_host_lock_contention_keeps_admission_open(tmp_path):
    """同主机另一服务占着整机锁时,等待阶段绝不能顺手攥着忙锁——admission 必须开着。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    lock_file = tmp_path / "busy.lock"
    lock_file.touch()
    env["BUSY_LOCK_FILE"] = str(lock_file)
    env["BUSY_LOCK_TIMEOUT"] = "15"
    host_lock = Path(env["HOST_LOCK"])

    holder = subprocess.Popen(["flock", "-x", str(host_lock), "sleep", "2"])
    results = {}

    def worker():
        results["res"] = _run(env)

    try:
        time.sleep(0.5)  # holder has grabbed the host lock by now
        t = threading.Thread(target=worker)
        t.start()
        time.sleep(1.0)  # script should be past its first attempt: busy lock
                          # already released, sleeping before the next retry
        probe = subprocess.run(["flock", "-n", "-s", str(lock_file), "true"])
        assert probe.returncode == 0, (
            "admission must stay open while the script is only waiting on the host lock"
        )
        t.join(timeout=15)
        assert not t.is_alive(), "deploy script did not finish within 15s"
    finally:
        holder.terminate()
        holder.wait()

    res = results["res"]
    assert res.returncode == 0, res.stdout + res.stderr
    assert "compose up -d" in Path(env["DOCKER_LOG"]).read_text()


def test_host_lock_held_past_budget_defers(tmp_path):
    """整机锁被占的时长超过忙锁预算 → 必须在预算内 deferred,不能被主机锁拖着死等。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    lock_file = tmp_path / "busy.lock"
    lock_file.touch()
    env["BUSY_LOCK_FILE"] = str(lock_file)
    env["BUSY_LOCK_TIMEOUT"] = "2"
    host_lock = Path(env["HOST_LOCK"])

    holder = subprocess.Popen(["flock", "-x", str(host_lock), "sleep", "12"])
    try:
        time.sleep(0.5)  # holder has grabbed the host lock by now
        start = time.monotonic()
        res = _run(env)
        elapsed = time.monotonic() - start
    finally:
        holder.terminate()
        holder.wait()

    assert res.returncode == 3, res.stdout + res.stderr
    # 2s 预算 + 重试循环里 clamp 后的等待,应该在 4.5s 内 deferred(留了余量,不是脆弱计时);
    # 若整机锁重试的 sleep 没有 clamp 到剩余预算,固定 sleep 5s 会把这里拖到 5-7s,便会超阈值。
    assert elapsed < 4.5, f"must defer within budget, not be dragged out by a fixed retry sleep, took elapsed={elapsed:.2f}s"
    docker_log_path = Path(env["DOCKER_LOG"])
    log = docker_log_path.read_text() if docker_log_path.exists() else ""
    assert "compose up -d" not in log
    assert "compose up" not in log


# --- local registry pull fast path (opt-in; LOCAL_IMAGE empty = ACR-only, unchanged) ---
# 本地 registry 是部署关键路径(同网段实测端到端 0.458s),ACR 是拉取回退。LOCAL_IMAGE
# 未设置时(上面所有既有测试都不设置它)pull_image() 必须逐字节复刻改动前的纯 ACR
# 单路径 —— 这正是那些既有测试(如 test_deploys_immutable_git_sha_tag 对 pull 次数
# 的精确断言)已经在锁的边界,这里再显式补一条便于按名字检索这条不变式。

def _mock_docker_local_then_acr(log_path: Path, local_ref: str, local_fail_pulls: int, acr_ref: str) -> str:
    """docker mock: pulls of `local_ref` fail `local_fail_pulls` times then succeed;
    pulls of `acr_ref` always succeed immediately; tag/compose always succeed."""
    return f"""#!/bin/bash
echo "$@" >> "{log_path}"
if [ "$1" = "pull" ]; then
  ref="$2"
  if [ "$ref" = "{local_ref}" ]; then
    count_file="{log_path}.localcount"
    n=$(cat "$count_file" 2>/dev/null || echo 0)
    n=$((n+1)); echo "$n" > "$count_file"
    [ "$n" -le {local_fail_pulls} ] && exit 1
    exit 0
  fi
  if [ "$ref" = "{acr_ref}" ]; then
    exit 0
  fi
  exit 1
fi
{_reconcile_ok_bash()}
exit 0
"""


def _local_registry_env(tmp_path: Path, *, local_fail_pulls: int) -> dict:
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    local_ref = "local.example:5001/ns/demo:abc1234"
    acr_ref = "registry.example.com/ns/demo:abc1234"  # matches ACR_IMAGE/GIT_SHA from _base_env
    _write_exec(
        mock_dir / "docker",
        _mock_docker_local_then_acr(Path(env["DOCKER_LOG"]), local_ref, local_fail_pulls, acr_ref),
    )
    env["LOCAL_IMAGE"] = "local.example:5001/ns/demo"
    env["LOCAL_PULL_RETRY_DELAY"] = "0"
    return env


def test_local_image_rejects_credentials_in_host(tmp_path):
    """user:pass@host 形式必须被拒绝——否则凭据会随镜像引用流进 pull/tag 命令行和
    log(OCR round-1 finding #5/#13,P1 不变式:凭据不得泄漏)。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    env["LOCAL_IMAGE"] = "user:secretpw@local.example:5001/ns/demo"
    res = _run(env)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "invalid registry host" in res.stdout
    # 校验本身绝不能回显原值,否则错误信息自己就是一条泄漏点。
    assert "secretpw" not in res.stdout
    assert "secretpw" not in res.stderr


def test_local_image_rejects_leading_dash_flag_injection(tmp_path):
    """"--evil-flag"(不含 @、不含协议前缀)必须被拒绝——拼进
    `docker pull "${LOCAL_IMAGE}:${tag}"` 后会被 docker CLI 当成命令行 flag 解析,
    而不是镜像名(OCR round-2 finding #7,high:CLI flag 注入)。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    env["LOCAL_IMAGE"] = "--evil-flag/ns/demo"
    res = _run(env)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "invalid registry host" in res.stdout


def test_local_image_rejects_scheme_prefix(tmp_path):
    """"https://host" 形式(协议前缀)必须被拒绝——第三条输入校验测试,与前两条
    (凭据、leading-dash flag 注入)构成同一组 host 形状校验的完整覆盖
    (OCR round-3 finding #15:补齐同组测试缺失的 docstring)。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    env["LOCAL_IMAGE"] = "https://local.example:5001/ns/demo"
    res = _run(env)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "invalid registry host" in res.stdout


def test_local_pull_retries_zero_is_rejected(tmp_path):
    """LOCAL_PULL_RETRIES=0 会让本地路径的循环一次都不执行,表现和"本地不可达"完全
    一样——每次都静默回退 ACR,而没有任何信号说明新链路根本没生效。必须是显式
    配置错误,不能被 bash 算术悄悄当 0 处理(OCR round-1 finding #12,防静默降级)。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    env["LOCAL_IMAGE"] = "local.example:5001/ns/demo"
    env["LOCAL_PULL_RETRIES"] = "0"
    res = _run(env)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "LOCAL_PULL_RETRIES must be a positive integer" in res.stdout


def test_local_pull_retries_above_five_is_rejected(tmp_path):
    """上限同样防静默降级(OCR round-3 finding #3):这条快速路径存在的理由是"本地
    同网段直连,预算必须远小于 ACR 的 150s"——不设上限,一次误配置就能让"本地优先"
    实际上变成"本地拖住部署很久才轮到 ACR",违背这条路径的设计意图。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    env["LOCAL_IMAGE"] = "local.example:5001/ns/demo"
    env["LOCAL_PULL_RETRIES"] = "6"
    res = _run(env)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "LOCAL_PULL_RETRIES must not exceed 5" in res.stdout


def test_local_pull_retry_delay_above_five_is_rejected(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    env["LOCAL_IMAGE"] = "local.example:5001/ns/demo"
    env["LOCAL_PULL_RETRY_DELAY"] = "6"
    res = _run(env)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "LOCAL_PULL_RETRY_DELAY must not exceed 5" in res.stdout


def test_local_pull_retry_delay_negative_is_rejected(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    env["LOCAL_IMAGE"] = "local.example:5001/ns/demo"
    env["LOCAL_PULL_RETRY_DELAY"] = "-1"
    res = _run(env)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "LOCAL_PULL_RETRY_DELAY must be a non-negative integer" in res.stdout


def test_local_pull_retry_delay_zero_is_allowed(tmp_path):
    """与 RETRIES 不同,DELAY=0(退避间隔为零、立即重试)是合法配置,不该被拒绝——
    与 push_to_acr.sh 的 PUSH_RETRY_DELAY_SECONDS 使用同一条 is_non_negative_integer
    规则保持一致。"""
    env = _local_registry_env(tmp_path, local_fail_pulls=0)
    env["LOCAL_PULL_RETRY_DELAY"] = "0"
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr


def test_local_registry_last_good_tag_is_bare_sha_without_registry_prefix(tmp_path):
    """P1 不变式:last_good_tag 只能记裸 SHA,不含 registry 前缀/冒号。本地 registry
    拉到的字节已经 retag 成 ACR_IMAGE:tag 规范名,写入 last_good_tag 的必须还是那个
    裸 tag,与字节来自哪个 registry 无关——一个把 LOCAL_IMAGE 前缀悄悄带进
    last_good_tag 的回归,不会被"部署成功"的既有断言拦住,只有精确内容比对能拦
    (OCR round-1 finding #10,P1:回滚拉错镜像 = 静默出错 + 故障放大)。"""
    env = _local_registry_env(tmp_path, local_fail_pulls=0)
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    good = Path(env["STATE_DIR"]) / "last_good_tag"
    content = good.read_text().strip()
    assert content == "abc1234", content
    assert "/" not in content and ":" not in content, (
        f"last_good_tag must be a bare SHA with no registry prefix, got {content!r}"
    )


def test_local_registry_rollback_keeps_last_good_tag_bare_sha(tmp_path):
    """回滚路径同理:探针失败触发回滚后,last_good_tag 必须还是原先那条裸 SHA——
    两次部署(健康 + 回滚)都走本地 registry 拉取,retag 环节不能让任何一次把
    registry 前缀带进 last_good_tag。复用 `_base_env` 而不是手搭一份环境(OCR
    round-2 finding #0):与 test_probe_failure_triggers_rollback 完全同款的两次
    _base_env(同一 tmp_path,故 STATE_DIR 共享)+ 改 status/GIT_SHA 套路,只多加
    LOCAL_IMAGE 这一个变量,不会因为 `_base_env` 未来增删必需变量而悄悄脱节。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()

    # 1st deploy: healthy via the local registry -> records bare "abc1234"
    env_ok = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    env_ok["LOCAL_IMAGE"] = "local.example:5001/ns/demo"
    assert _run(env_ok).returncode == 0

    good = Path(env_ok["STATE_DIR"]) / "last_good_tag"
    assert good.read_text().strip() == "abc1234"

    # 2nd deploy: a new SHA, also pulled via the local registry, but unhealthy
    # -> must roll back to abc1234 (re-pulled via the local registry too)
    env_bad = _base_env(tmp_path, mock_dir=mock_dir, status="500")
    env_bad["LOCAL_IMAGE"] = "local.example:5001/ns/demo"
    env_bad["GIT_SHA"] = "def5678"
    Path(env_bad["DOCKER_LOG"]).write_text("")  # reset log for assertions
    res_bad = _run(env_bad)
    assert res_bad.returncode != 0
    # 断言回滚分支真的跑过了,而不只是"凑巧"返回非零(比如 do_deploy 在到达回滚
    # 之前就以别的原因失败,last_good_tag 会因为压根没被碰过而巧合等于第一次部署
    # 写下的值,让下面的内容断言看似通过但其实没测到回滚逻辑,OCR round-3 finding #14)。
    assert "rolling back to previous good tag abc1234" in res_bad.stdout

    content = good.read_text().strip()
    assert content == "abc1234", content
    assert "/" not in content and ":" not in content, (
        f"last_good_tag must stay a bare SHA through a local-registry rollback, got {content!r}"
    )

    log = Path(env_bad["DOCKER_LOG"]).read_text()
    assert "pull local.example:5001/ns/demo:def5678" in log, log
    assert "pull local.example:5001/ns/demo:abc1234" in log, log


def test_local_registry_optout_leaves_behavior_unchanged(tmp_path):
    """不传 LOCAL_IMAGE(或为空)→ pull_image() 完全走原 ACR 单路径,不产生任何
    本地 registry 相关的 pull/tag 调用,不多一次 docker 调用。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    assert log.count("pull ") == 1, log
    assert "local" not in log.lower(), log
    # 不只断言 mock 调用序列,也断言脚本自身的 stdout/stderr——防止后来者加一行
    # 诊断 log 就悄悄破坏了默认路径与下游 grep 的假设(OCR round-1 finding #8/#9)。
    # 用 "local registry" 而不是裸 "local":HEALTHCHECK_URL 里的 "localhost" 本身
    # 就含 "local" 子串,裸子串断言会被这个既有、无关的巧合坑到误判。
    assert "local registry" not in res.stdout.lower()
    assert "local registry" not in res.stderr.lower()


def test_local_registry_explicit_empty_string_leaves_behavior_unchanged(tmp_path):
    """不只测"没传 LOCAL_IMAGE"(未设置),还要测"传了空字符串"——脚本用
    `[ -n "$LOCAL_IMAGE" ]` 判断,unset 和空串在 bash 里同样为假,但这是两条不同的
    输入路径,一次针对 unset 的重构(比如换成别的判断写法)可能悄悄放过空串这条
    分支(OCR round-3 finding #12)。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    env["LOCAL_IMAGE"] = ""
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    assert log.count("pull ") == 1, log
    assert "local registry" not in res.stdout.lower()
    assert "local registry" not in res.stderr.lower()


def test_local_registry_pull_succeeds_skips_acr_entirely(tmp_path):
    """本地 registry 一次拉到 → 直接 retag 成 ACR_IMAGE:tag 规范名,完全不碰 ACR。"""
    env = _local_registry_env(tmp_path, local_fail_pulls=0)
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    assert log.count("pull local.example:5001/ns/demo:abc1234") == 1, log
    assert "pull registry.example.com/ns/demo:abc1234" not in log, log
    assert "tag local.example:5001/ns/demo:abc1234 registry.example.com/ns/demo:abc1234" in log, log
    assert "compose up -d" in log


def test_local_registry_pull_retries_default_is_two_then_falls_back_to_acr(tmp_path):
    """本地 registry 全程不可达(2026-07-28 决策:同网段链路给它套 ACR 的 150s 预算
    是浪费,默认只给 2 次快速失败)→ 精确耗尽 2 次本地尝试后切到 ACR,ACR 照常成功。"""
    env = _local_registry_env(tmp_path, local_fail_pulls=99)
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    assert log.count("pull local.example:5001/ns/demo:abc1234") == 2, log
    assert log.count("pull registry.example.com/ns/demo:abc1234") == 1, log
    assert "falling back to ACR" in res.stdout
    assert "compose up -d" in log


def test_local_registry_retry_budget_is_configurable(tmp_path):
    """LOCAL_PULL_RETRIES 可覆盖(不是硬编码),锁定"精确耗尽配置值才切 ACR"这条边界。
    同时断言真的走了 fallback 分支(而不只是巧合地在别处也出现了一次 ACR pull)
    ——OCR round-2 finding #1。"""
    env = _local_registry_env(tmp_path, local_fail_pulls=99)
    env["LOCAL_PULL_RETRIES"] = "4"
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    log = Path(env["DOCKER_LOG"]).read_text()
    assert log.count("pull local.example:5001/ns/demo:abc1234") == 4, log
    assert log.count("pull registry.example.com/ns/demo:abc1234") == 1, log
    assert "falling back to ACR" in res.stdout


def test_local_registry_retag_failure_falls_back_to_acr(tmp_path):
    """本地拉取成功但 retag 到 ACR_IMAGE:tag 规范名失败(极端情况)→ 不重试本地,
    直接落到 ACR 路径,部署仍应成功(last_good_tag/回滚只认规范名,与来源 registry 无关)。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    local_ref = "local.example:5001/ns/demo:abc1234"
    acr_ref = "registry.example.com/ns/demo:abc1234"
    docker_log = Path(env["DOCKER_LOG"])
    _write_exec(
        mock_dir / "docker",
        f"""#!/bin/bash
echo "$@" >> "{docker_log}"
if [ "$1" = "pull" ]; then
  [ "$2" = "{local_ref}" ] && exit 0
  [ "$2" = "{acr_ref}" ] && exit 0
  exit 1
fi
if [ "$1" = "tag" ]; then
  [ "$2" = "{local_ref}" ] && exit 1
  exit 0
fi
{_reconcile_ok_bash()}
exit 0
""",
    )
    env["LOCAL_IMAGE"] = "local.example:5001/ns/demo"
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    log = docker_log.read_text()
    assert log.count(f"pull {local_ref}") == 1, log
    assert log.count(f"pull {acr_ref}") == 1, log
    assert "retag" in res.stdout and "failed" in res.stdout
    assert "compose up -d" in log
    # P1 不变式同样适用于这条 fallback 路径:即便字节来自"本地拉到但 retag 失败,
    # 转而从 ACR 拉"这条迂回路线,last_good_tag 也必须是裸 SHA(OCR round-3 finding #13)。
    good = Path(env["STATE_DIR"]) / "last_good_tag"
    content = good.read_text().strip()
    assert content == "abc1234", content
    assert "/" not in content and ":" not in content


def test_invalid_busy_lock_timeout_fails_hard(tmp_path):
    """BUSY_LOCK_TIMEOUT 不是正整数 → 必须是显式的配置错误(rc=1),不能被 bash 算术
    悄悄当 0 处理后伪装成"服务忙"的 deferred(rc=3)——两者运维含义完全不同。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    lock_file = tmp_path / "busy.lock"
    lock_file.touch()
    env["BUSY_LOCK_FILE"] = str(lock_file)
    env["BUSY_LOCK_TIMEOUT"] = "abc"

    res = _run(env)
    assert res.returncode == 1, res.stdout + res.stderr
    docker_log_path = Path(env["DOCKER_LOG"])
    log = docker_log_path.read_text() if docker_log_path.exists() else ""
    assert "compose up" not in log
    assert "BUSY_LOCK_TIMEOUT" in (res.stdout + res.stderr)


COMPOSE_ONESHOT_SERVICES = ("app", "migrate")


def _mock_docker_oneshot(log_path: Path) -> str:
    service_lines = "\\n".join(COMPOSE_ONESHOT_SERVICES)
    return f"""#!/bin/bash
echo "$@" >> "{log_path}"
if [ "$1" = "compose" ] && [[ " $* " == *" config --services "* ]]; then
  printf '{service_lines}\\n'
  exit 0
fi
if [ "$1" = "compose" ] && [[ " $* " == *" ps -q --status running"* ]]; then
  printf 'cid-app\\n'
  exit 0
fi
if [ "$1" = image ] && [ "$2" = inspect ] && [[ " $* " == *" --format "* ]]; then
  printf '{RECONCILE_IMAGE_ID}\\n'
  exit 0
fi
if [ "$1" = inspect ] && [[ " $* " == *" --format "* ]]; then
  printf '{RECONCILE_IMAGE_ID}\\n'
  exit 0
fi
exit 0
"""


def _compose_up_lines(log_text: str) -> list[str]:
    return [line for line in log_text.splitlines() if " up -d" in line]


def _assert_compose_up_has_no_service_args(line: str) -> None:
    assert line.rstrip().endswith(" up -d"), line


def _assert_compose_up_services(line: str, *, include: tuple[str, ...], exclude: tuple[str, ...]) -> None:
    parts = line.split()
    idx = parts.index("-d")
    services = parts[idx + 1 :]
    for name in include:
        assert name in services, (line, services)
    for name in exclude:
        assert name not in services, (line, services)


def _oneshot_env(tmp_path: Path, *, oneshot_services: str = "migrate") -> dict:
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    docker = mock_dir / "docker"
    curl = mock_dir / "curl"
    _write_exec(docker, _mock_docker_oneshot(docker_log))
    _write_exec(curl, _mock_curl("200"))

    deploy_dir = tmp_path / "app"
    deploy_dir.mkdir(exist_ok=True)
    (deploy_dir / "docker-compose.yml").write_text(
        "services:\n  app:\n    image: demo\n  migrate:\n    image: demo\n"
    )

    env = dict(os.environ)
    env.update(
        IMAGE_NAME="demo",
        ACR_IMAGE="registry.example.com/ns/demo",
        GIT_SHA="abc1234",
        DEPLOY_DIR=str(deploy_dir),
        STATE_DIR=str(tmp_path / "state"),
        HOST_LOCK=str(tmp_path / "host.lock"),
        HEALTHCHECK_URL="http://localhost/health",
        HEALTHCHECK_EXPECT_STATUS="200",
        HEALTHCHECK_RETRIES="2",
        HEALTHCHECK_INTERVAL="0",
        HEALTHCHECK_WARMUP="0",
        HEALTHCHECK_TIMEOUT="1",
        DOCKER_BIN=str(docker),
        CURL_BIN=str(curl),
        DOCKER_LOG=str(docker_log),
    )
    if oneshot_services:
        env["ONESHOT_SERVICES"] = oneshot_services
    return env


def _run_oneshot_rollback(
    env: dict, tmp_path: Path, *, unhealthy_sequence=None, oneshot_on_rollback=None
) -> subprocess.CompletedProcess:
    assert _run(env).returncode == 0
    env = dict(env)
    env["GIT_SHA"] = "def5678"
    if oneshot_on_rollback is not None:
        env["ONESHOT_SERVICES"] = oneshot_on_rollback
    Path(env["DOCKER_LOG"]).write_text("")
    mock_dir = Path(env["DOCKER_BIN"]).parent
    sequence = unhealthy_sequence or [("500", 0), ("500", 0), ("200", 0)]
    _write_exec(mock_dir / "curl", _mock_curl_sequence(tmp_path / "curl-seq.log", sequence))
    env["CURL_BIN"] = str(mock_dir / "curl")
    return _run(env)


@pytest.mark.parametrize(
    ("case", "oneshot_services", "phase"),
    [
        ("empty_forward", "", "forward"),
        ("empty_rollback", "", "rollback"),
        ("valid_forward", "migrate", "forward"),
        ("valid_rollback", "migrate", "rollback"),
        ("invalid_forward", "nosuch", "forward"),
        ("invalid_rollback_unreachable", "nosuch", "forward"),
        ("all_oneshot_rollback", "app migrate", "rollback"),
    ],
)
def test_oneshot_services_axis1(tmp_path, case, oneshot_services, phase):
    if case == "all_oneshot_rollback":
        # Seed last_good with a deploy that still has a long-running service.
        # All-oneshot would now fail reconcile (rc=5) before rollback can run.
        env = _oneshot_env(tmp_path, oneshot_services="migrate")
        result = _run_oneshot_rollback(
            env, tmp_path, oneshot_on_rollback="app migrate"
        )
    elif phase == "rollback":
        env = _oneshot_env(tmp_path, oneshot_services=oneshot_services)
        result = _run_oneshot_rollback(env, tmp_path)
    else:
        env = _oneshot_env(tmp_path, oneshot_services=oneshot_services)
        result = _run(env)

    out = result.stdout + result.stderr
    up_lines = _compose_up_lines(Path(env["DOCKER_LOG"]).read_text())

    if case == "empty_forward":
        assert result.returncode == 0, out
        assert len(up_lines) == 1
        _assert_compose_up_has_no_service_args(up_lines[0])
    elif case == "empty_rollback":
        assert result.returncode == 1, out
        assert len(up_lines) == 2
        for line in up_lines:
            _assert_compose_up_has_no_service_args(line)
    elif case == "valid_forward":
        assert result.returncode == 0, out
        assert len(up_lines) == 1
        _assert_compose_up_has_no_service_args(up_lines[0])
    elif case == "valid_rollback":
        assert result.returncode == 1, out
        assert len(up_lines) == 2
        _assert_compose_up_has_no_service_args(up_lines[0])
        _assert_compose_up_services(
            up_lines[1], include=("app",), exclude=("migrate",),
        )
    elif case in {"invalid_forward", "invalid_rollback_unreachable"}:
        assert result.returncode != 0, out
        assert "nosuch" in out.lower()
        assert not up_lines
    elif case == "all_oneshot_rollback":
        assert result.returncode != 0, out
        assert "nothing would remain" in out.lower() or "no services" in out.lower()
        assert len(up_lines) == 1


def test_oneshot_services_probe_failure_rollback_excludes_migrate(tmp_path):
    env = _oneshot_env(tmp_path, oneshot_services="migrate")
    result = _run_oneshot_rollback(env, tmp_path)
    up_lines = _compose_up_lines(Path(env["DOCKER_LOG"]).read_text())
    assert result.returncode == 1, result.stdout + result.stderr
    _assert_compose_up_services(up_lines[-1], include=("app",), exclude=("migrate",))


def test_oneshot_services_no_last_good_still_rejects_pseudo_rollback(tmp_path):
    env = _oneshot_env(tmp_path, oneshot_services="migrate")
    mock_dir = Path(env["DOCKER_BIN"]).parent
    _write_exec(mock_dir / "curl", _mock_curl("500"))
    env["CURL_BIN"] = str(mock_dir / "curl")
    result = _run(env)
    out = result.stdout + result.stderr
    assert result.returncode == 4, out
    assert "no previous good tag" in out.lower()
    up_lines = _compose_up_lines(Path(env["DOCKER_LOG"]).read_text())
    assert len(up_lines) == 1
    _assert_compose_up_has_no_service_args(up_lines[0])


def test_reconcile_passed_on_healthy_deploy(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    res = _run(env)
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "image reconcile values:" in out
    assert "expected_id=" in out and "latest_id=" in out and "running_ids=" in out
    assert "image reconcile passed" in out
    log = Path(env["DOCKER_LOG"]).read_text()
    assert "ps -q --status running app" in log
    assert "ps -q --status running\n" not in log


def test_reconcile_mismatch_returns_rc5(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    docker = Path(env["DOCKER_BIN"])
    body = docker.read_text().split("\n", 1)[1]
    _write_exec(
        docker,
        "#!/bin/bash\n"
        "if [ \"$1\" = inspect ]; then printf 'sha256:OTHER\\n'; exit 0; fi\n"
        + body,
    )
    res = _run(env)
    out = res.stdout + res.stderr
    assert res.returncode == 5, out
    assert "image reconcile values:" in out
    assert "expected_id=" in out and "latest_id=" in out and "running_ids=" in out
    assert "running container mismatch" in out
    good = Path(env["STATE_DIR"]) / "last_good_tag"
    assert good.exists() and good.read_text().strip() == "abc1234"


def test_reconcile_runs_while_host_lock_held(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    lock_state = tmp_path / "lock-state.log"
    host_lock = env["HOST_LOCK"]
    docker = Path(env["DOCKER_BIN"])
    body = docker.read_text().split("\n", 1)[1]
    _write_exec(
        docker,
        f'''#!/bin/bash
if [ "$1" = compose ] && [[ " $* " == *" ps -q --status running"* ]]; then
  exec 200>"{host_lock}"
  if flock -n 200; then echo RECONCILE_UNLOCKED >> "{lock_state}"; flock -u 200
  else echo RECONCILE_LOCKED >> "{lock_state}"; fi
fi
{body}
''',
    )
    res = _run(env)
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "image reconcile starting (host lock still held)" in out
    assert "RECONCILE_LOCKED" in lock_state.read_text()
    assert "RECONCILE_UNLOCKED" not in lock_state.read_text()


def test_reconcile_docker_timeout_returns_rc5(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    env["RECONCILE_CMD_TIMEOUT"] = "1"
    docker = Path(env["DOCKER_BIN"])
    _write_exec(
        docker,
        "#!/bin/bash\n"
        'if [ "$1" = inspect ] || { [ "$1" = image ] && [ "$2" = inspect ]; }; then sleep 8; fi\n'
        + docker.read_text().split("\n", 1)[1],
    )
    result = _run(env, timeout=20)
    out = result.stdout + result.stderr
    assert result.returncode == 5, out
    assert "timed out after 1s holding host lock" in out
    good = Path(env["STATE_DIR"]) / "last_good_tag"
    assert good.exists() and good.read_text().strip() == "abc1234"


def test_promoted_sha_reentry_skips_forward_and_reconciles_only(tmp_path):
    """255 重放时 last_good_tag 已是本次 GIT_SHA：跳过 deploy_tag / 探针，只对账。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    state = Path(env["STATE_DIR"])
    state.mkdir(parents=True)
    (state / "last_good_tag").write_text(env["GIT_SHA"] + "\n")

    res = _run(env)
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "skip forward deploy" in out
    assert "image reconcile values:" in out

    log = Path(env["DOCKER_LOG"]).read_text()
    assert " up -d" not in log
    assert "compose up" not in log
    assert not any(line.split()[:1] == ["pull"] for line in log.splitlines())
