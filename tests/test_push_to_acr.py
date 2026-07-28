"""Contract tests for the ACR image publisher's bounded retries."""

import os
import shlex
import subprocess
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "push_to_acr.sh"


def _env(tmp_path: Path, docker_bin: Path, **overrides: str) -> dict[str, str]:
    env = os.environ | {
        "ACR_REGISTRY": "registry.example",
        "ACR_NAMESPACE": "namespace",
        "IMAGE_NAME": "service",
        "GIT_SHA": "abc123",
        "DOCKER_BIN": str(docker_bin),
        "BUILD_CONTEXT": str(tmp_path),
        "DOCKERFILE": "Dockerfile",
        "PUSH_RETRY_DELAY_SECONDS": "0",
    }
    env.update(overrides)
    return env


def _write_fake_docker(tmp_path: Path, body: str) -> Path:
    docker = tmp_path / "docker"
    argv_log = tmp_path / "docker-argv.log"
    docker.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "{ printf 'CALL'; printf ' %q' \"$@\"; printf '\\n'; } >> "
        f"\"{argv_log}\"\n"
        + body
    )
    docker.chmod(0o755)
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    return docker


def _recorded_argv(tmp_path: Path) -> list[list[str]]:
    return [shlex.split(line)[1:] for line in (tmp_path / "docker-argv.log").read_text().splitlines()]


def test_push_retries_a_transient_failure_without_rebuilding(tmp_path):
    calls = tmp_path / "calls"
    docker = _write_fake_docker(
        tmp_path,
        f'''if [ "$1" = build ]; then echo build >> "{calls}"; exit 0; fi
if [ "$1" = push ]; then
  echo "push:$2" >> "{calls}"
  [ "$(grep -c '^push:' "{calls}")" -eq 1 ] && exit 1
  exit 0
fi
''',
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)], env=_env(tmp_path, docker, PUSH_MAX_ATTEMPTS="3"),
        text=True, capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        "build",
        "push:registry.example/namespace/service:abc123",
        "push:registry.example/namespace/service:abc123",
    ]
    assert _recorded_argv(tmp_path) == [
        [
            "build",
            "--build-arg",
            "GIT_SHA=abc123",
            "-f",
            f"{tmp_path}/Dockerfile",
            "-t",
            "registry.example/namespace/service:abc123",
            str(tmp_path),
        ],
        ["push", "registry.example/namespace/service:abc123"],
        ["push", "registry.example/namespace/service:abc123"],
    ]
    assert all(call[0] != "buildx" for call in _recorded_argv(tmp_path))
    assert "attempt 2/3" in result.stdout


def test_push_times_out_and_stops_after_configured_attempts(tmp_path):
    docker = _write_fake_docker(
        tmp_path,
        '''if [ "$1" = build ]; then exit 0; fi
if [ "$1" = push ]; then sleep 2; fi
''',
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, PUSH_TIMEOUT_SECONDS="1", PUSH_MAX_ATTEMPTS="2"),
        text=True, capture_output=True,
    )

    assert result.returncode != 0
    assert "timed out after 1s" in result.stdout
    assert "failed after 2 attempts" in result.stdout


def test_push_timeout_kills_a_client_that_ignores_sigterm(tmp_path):
    docker = _write_fake_docker(
        tmp_path,
        '''if [ "$1" = build ]; then exit 0; fi
if [ "$1" = push ]; then trap '' TERM; sleep 10; fi
''',
    )

    started = time.monotonic()
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(
            tmp_path, docker, PUSH_TIMEOUT_SECONDS="1", PUSH_TIMEOUT_KILL_AFTER_SECONDS="1",
            PUSH_MAX_ATTEMPTS="1",
        ),
        text=True, capture_output=True,
    )

    assert result.returncode != 0
    assert time.monotonic() - started < 4
    assert "timed out after 1s" in result.stdout


def test_push_bounds_cannot_be_relaxed(tmp_path):
    docker = _write_fake_docker(tmp_path, 'exit 0\n')

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, PUSH_TIMEOUT_SECONDS="301", PUSH_MAX_ATTEMPTS="4"),
        text=True, capture_output=True,
    )

    assert result.returncode == 2
    assert "PUSH_TIMEOUT_SECONDS must not exceed 300" in result.stderr


# --- local registry dual-push (opt-in; LOCAL_REGISTRY empty = ACR-only, unchanged) ---
# 本地 registry 是部署关键路径,ACR 是异地存档 + 拉取回退。LOCAL_REGISTRY 未设置时
# (下面所有既有测试都不设置它)必须逐字节复刻改动前行为——见
# test_push_retries_a_transient_failure_without_rebuilding 等测试对 calls 的精确
# 断言,任何意外多出的 docker tag/push 调用都会让那些测试先炸。

def test_local_registry_pushes_both_targets_when_set(tmp_path):
    """LOCAL_REGISTRY 设置后:build 一次,retag 到本地路径,先推本地再推 ACR,两处都要推。"""
    calls = tmp_path / "calls"
    docker = _write_fake_docker(
        tmp_path,
        f'''if [ "$1" = build ]; then echo build >> "{calls}"; exit 0; fi
if [ "$1" = tag ]; then echo "tag $2 $3" >> "{calls}"; exit 0; fi
if [ "$1" = push ]; then echo "push $2" >> "{calls}"; exit 0; fi
''',
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, LOCAL_REGISTRY="local.example:5001"),
        text=True, capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text().splitlines() == [
        "tag registry.example/namespace/service:abc123 local.example:5001/namespace/service:abc123",
        "push local.example:5001/namespace/service:abc123",
        "push registry.example/namespace/service:abc123",
    ]
    recorded = _recorded_argv(tmp_path)
    assert recorded[0] == ["buildx", "inspect", "ci-templates-registry-cache"]
    assert recorded[1] == [
        "buildx",
        "build",
        "--builder",
        "ci-templates-registry-cache",
        "--build-arg",
        "GIT_SHA=abc123",
        "-f",
        f"{tmp_path}/Dockerfile",
        "-t",
        "registry.example/namespace/service:abc123",
        "--cache-from",
        "type=registry,ref=local.example:5001/namespace/service:buildcache",
        "--cache-to",
        "type=registry,ref=local.example:5001/namespace/service:buildcache,mode=max,ignore-error=true",
        "--load",
        str(tmp_path),
    ]
    assert all(call[:2] != ["buildx", "create"] for call in recorded)
    # --load 有专属守卫,而不只是上面那张大 argv 表里的一个元素:docker-container
    # driver 的产物默认**不进**本地 daemon,少了它下面的 tag/push 会推一个陈旧或
    # 不存在的镜像——静默出错,是本增量最危险的失败模式。断言独立一条,回归时的
    # 报错才说得清是哪个不变式挂了,而不是一屏 argv 的 diff。
    assert "--load" in recorded[1], (
        "buildx build must --load into the local daemon, otherwise the tag/push below "
        "would operate on a stale or missing image"
    )
    assert recorded[2:] == [
        [
            "tag",
            "registry.example/namespace/service:abc123",
            "local.example:5001/namespace/service:abc123",
        ],
        ["push", "local.example:5001/namespace/service:abc123"],
        ["push", "registry.example/namespace/service:abc123"],
    ]


def test_builder_create_failure_falls_back_to_classic_build(tmp_path):
    calls = tmp_path / "calls"
    docker = _write_fake_docker(
        tmp_path,
        f'''if [ "$1" = buildx ] && [ "$2" = inspect ]; then exit 1; fi
if [ "$1" = buildx ] && [ "$2" = create ]; then exit 1; fi
if [ "$1" = build ]; then echo build >> "{calls}"; exit 0; fi
if [ "$1" = tag ] || [ "$1" = push ]; then exit 0; fi
''',
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, LOCAL_REGISTRY="local.example:5001", PUSH_MAX_ATTEMPTS="1"),
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning::buildx builder initialization failed; falling back to classic docker build" in result.stdout
    assert _recorded_argv(tmp_path) == [
        ["buildx", "inspect", "ci-templates-registry-cache"],
        [
            "buildx",
            "create",
            "--name",
            "ci-templates-registry-cache",
            "--driver",
            "docker-container",
            "--bootstrap",
        ],
        [
            "build",
            "--build-arg",
            "GIT_SHA=abc123",
            "-f",
            f"{tmp_path}/Dockerfile",
            "-t",
            "registry.example/namespace/service:abc123",
            str(tmp_path),
        ],
        [
            "tag",
            "registry.example/namespace/service:abc123",
            "local.example:5001/namespace/service:abc123",
        ],
        ["push", "local.example:5001/namespace/service:abc123"],
        ["push", "registry.example/namespace/service:abc123"],
    ]


def test_buildx_failure_falls_back_to_classic_build(tmp_path):
    docker = _write_fake_docker(
        tmp_path,
        '''if [ "$1" = buildx ] && [ "$2" = inspect ]; then exit 0; fi
if [ "$1" = buildx ] && [ "$2" = build ]; then exit 1; fi
if [ "$1" = build ]; then exit 0; fi
if [ "$1" = tag ] || [ "$1" = push ]; then exit 0; fi
''',
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, LOCAL_REGISTRY="local.example:5001", PUSH_MAX_ATTEMPTS="1"),
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning::buildx build failed; falling back to classic docker build" in result.stdout
    assert _recorded_argv(tmp_path) == [
        ["buildx", "inspect", "ci-templates-registry-cache"],
        [
            "buildx",
            "build",
            "--builder",
            "ci-templates-registry-cache",
            "--build-arg",
            "GIT_SHA=abc123",
            "-f",
            f"{tmp_path}/Dockerfile",
            "-t",
            "registry.example/namespace/service:abc123",
            "--cache-from",
            "type=registry,ref=local.example:5001/namespace/service:buildcache",
            "--cache-to",
            "type=registry,ref=local.example:5001/namespace/service:buildcache,mode=max,ignore-error=true",
            "--load",
            str(tmp_path),
        ],
        [
            "build",
            "--build-arg",
            "GIT_SHA=abc123",
            "-f",
            f"{tmp_path}/Dockerfile",
            "-t",
            "registry.example/namespace/service:abc123",
            str(tmp_path),
        ],
        [
            "tag",
            "registry.example/namespace/service:abc123",
            "local.example:5001/namespace/service:abc123",
        ],
        ["push", "local.example:5001/namespace/service:abc123"],
        ["push", "registry.example/namespace/service:abc123"],
    ]


def test_fallback_classic_build_failure_is_propagated(tmp_path):
    docker = _write_fake_docker(
        tmp_path,
        '''if [ "$1" = buildx ] && [ "$2" = inspect ]; then exit 0; fi
if [ "$1" = buildx ] && [ "$2" = build ]; then exit 1; fi
if [ "$1" = build ]; then exit 17; fi
''',
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, LOCAL_REGISTRY="local.example:5001", PUSH_MAX_ATTEMPTS="1"),
        text=True,
        capture_output=True,
    )

    assert result.returncode == 17
    recorded = _recorded_argv(tmp_path)
    # 恰好三次调用:inspect → buildx build(失败) → classic build(失败)。锁死调用
    # 次数而不只是最后一条,否则「降级路径里多跑了一次 buildx build 才放弃」这类
    # 回归不会被发现——降级必须只发生一次。
    assert len(recorded) == 3, f"fallback must happen exactly once, got: {recorded}"
    assert recorded[0] == ["buildx", "inspect", "ci-templates-registry-cache"]
    assert recorded[1][:2] == ["buildx", "build"]
    assert recorded[-1] == [
        "build",
        "--build-arg",
        "GIT_SHA=abc123",
        "-f",
        f"{tmp_path}/Dockerfile",
        "-t",
        "registry.example/namespace/service:abc123",
        str(tmp_path),
    ]
    assert all(call[0] not in {"tag", "push"} for call in recorded)


def test_local_registry_push_failure_falls_back_to_acr_only(tmp_path):
    """本地 push 失败、ACR 成功 → 非致命:告警 + 退出 0,这次部署退化为纯 ACR。"""
    calls = tmp_path / "calls"
    docker = _write_fake_docker(
        tmp_path,
        f'''echo "$@" >> "{calls}"
if [ "$1" = build ] || [ "$1" = tag ]; then exit 0; fi
if [ "$1" = push ]; then
  case "$2" in
    local.example:5001/*) exit 1 ;;
    *) exit 0 ;;
  esac
fi
''',
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, LOCAL_REGISTRY="local.example:5001", PUSH_MAX_ATTEMPTS="1"),
        text=True, capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "local registry push failed" in result.stdout
    assert "push registry.example/namespace/service:abc123" in calls.read_text()


def test_acr_push_failure_with_local_registry_ok_is_non_fatal(tmp_path):
    """本地 push 成功、ACR 失败 → ACR 是 fallback,fallback 失效必须可见但不阻塞部署
    (部署关键路径本地已经拿到镜像了),打醒目 warning 而不是静默放行。"""
    docker = _write_fake_docker(
        tmp_path,
        '''if [ "$1" = build ] || [ "$1" = tag ]; then exit 0; fi
if [ "$1" = push ]; then
  case "$2" in
    registry.example/*) exit 1 ;;
    *) exit 0 ;;
  esac
fi
''',
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, LOCAL_REGISTRY="local.example:5001", PUSH_MAX_ATTEMPTS="1"),
        text=True, capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ACR push failed" in result.stdout
    assert "offsite archive/fallback unavailable" in result.stdout


def test_local_registry_and_acr_both_failing_is_fatal(tmp_path):
    """两处都失败 → 镜像不在任何地方,必须致命退出,不能悄悄放行一个哪都拉不到的 SHA。"""
    docker = _write_fake_docker(
        tmp_path,
        '''if [ "$1" = build ] || [ "$1" = tag ]; then exit 0; fi
if [ "$1" = push ]; then exit 1; fi
''',
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, LOCAL_REGISTRY="local.example:5001", PUSH_MAX_ATTEMPTS="1"),
        text=True, capture_output=True,
    )

    assert result.returncode != 0
    assert "image not available anywhere" in result.stdout


def test_local_registry_rejects_credentials_in_host(tmp_path):
    """user:pass@host 形式必须被拒绝——否则凭据会随镜像引用流进 tag/push 命令行和日志
    (OCR round-1 finding #5/#13,P1 不变式:凭据不得泄漏)。"""
    docker = _write_fake_docker(tmp_path, "exit 0\n")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, LOCAL_REGISTRY="user:secretpw@local.example:5001"),
        text=True, capture_output=True,
    )
    assert result.returncode == 2
    assert "invalid shape" in result.stderr
    # 校验本身绝不能回显原值,否则错误信息自己就是一条泄漏点。
    assert "secretpw" not in result.stdout
    assert "secretpw" not in result.stderr


def test_local_registry_rejects_scheme_prefix(tmp_path):
    docker = _write_fake_docker(tmp_path, "exit 0\n")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, LOCAL_REGISTRY="https://local.example:5001"),
        text=True, capture_output=True,
    )
    assert result.returncode == 2
    assert "invalid shape" in result.stderr


def test_local_registry_rejects_embedded_path(tmp_path):
    docker = _write_fake_docker(tmp_path, "exit 0\n")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, LOCAL_REGISTRY="local.example:5001/extra"),
        text=True, capture_output=True,
    )
    assert result.returncode == 2
    assert "invalid shape" in result.stderr


def test_local_registry_rejects_leading_dash_flag_injection(tmp_path):
    """"--evil-flag" 形式(不含 @、不含 /、不含协议前缀)必须被拒绝——它能通过一个
    不锚定首字符的正则,拼进 docker tag/push 的镜像引用后会被 docker CLI 当成
    命令行 flag 解析,而不是镜像名(OCR round-2 finding #4/#7,high)。"""
    docker = _write_fake_docker(tmp_path, "exit 0\n")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, LOCAL_REGISTRY="--evil-flag"),
        text=True, capture_output=True,
    )
    assert result.returncode == 2
    assert "invalid shape" in result.stderr


def test_local_registry_accepts_plain_host_port(tmp_path):
    """回归护栏:合法的 host:port(含端口)不应该被新校验误伤。"""
    docker = _write_fake_docker(
        tmp_path,
        '''if [ "$1" = build ] || [ "$1" = tag ]; then exit 0; fi
if [ "$1" = push ]; then exit 0; fi
''',
    )
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, LOCAL_REGISTRY="local.example:5001"),
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_default_path_success_stdout_is_exact_no_extra_lines(tmp_path):
    """默认路径(LOCAL_REGISTRY 未设置)成功时,stdout 的收尾行必须还是改动前那句
    `[push] done: ...`,不能多出任何汇总行——把"stdout 差异只发生在失败路径,成功
    路径逐字节不变"这个结论从注释口头保证升级成测试断言(OCR round-3 finding #4/#9)。"""
    docker = _write_fake_docker(
        tmp_path,
        '''if [ "$1" = build ]; then exit 0; fi
if [ "$1" = push ]; then exit 0; fi
''',
    )
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=_env(tmp_path, docker), text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith(
        "[push] done: registry.example/namespace/service:abc123"
    ), result.stdout
    assert "image not available anywhere" not in result.stdout


def test_default_path_stdout_never_mentions_local_registry(tmp_path):
    """LOCAL_REGISTRY 未设置(默认)→ 输出里不出现 "local" 字样,防止后来者加一行
    诊断 log 就悄悄破坏了默认路径与下游 grep 的假设(OCR round-1 finding #8/#9)。"""
    docker = _write_fake_docker(
        tmp_path,
        '''if [ "$1" = build ]; then exit 0; fi
if [ "$1" = push ]; then exit 0; fi
''',
    )
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=_env(tmp_path, docker), text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "local" not in result.stdout.lower()
    assert "local" not in result.stderr.lower()


def test_local_retag_failure_skips_local_push_and_falls_back_to_acr(tmp_path):
    """build 后的本地 retag 本身失败(极端情况,如磁盘满)→ 不尝试本地 push,直接走 ACR。"""
    calls = tmp_path / "calls"
    docker = _write_fake_docker(
        tmp_path,
        f'''if [ "$1" = build ]; then echo build >> "{calls}"; exit 0; fi
if [ "$1" = tag ]; then echo "tag $2 $3" >> "{calls}"; exit 1; fi
if [ "$1" = push ]; then echo "push $2" >> "{calls}"; exit 0; fi
''',
    )

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_env(tmp_path, docker, LOCAL_REGISTRY="local.example:5001", PUSH_MAX_ATTEMPTS="1"),
        text=True, capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "local retag" in result.stdout
    assert calls.read_text().splitlines() == [
        "tag registry.example/namespace/service:abc123 local.example:5001/namespace/service:abc123",
        "push registry.example/namespace/service:abc123",
    ]
