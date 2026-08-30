"""TDD tests for atomic multi-image release deployment on the SSH host."""

import os
import pytest
import signal
import stat
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "release_deploy.sh"


def write_exec(path: Path, body: str):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def mock_docker(path: Path, log: Path, *, compose_rc=0, fail_pull=False, fail_tag=False):
    write_exec(
        path,
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  exit 0
fi
if [ "$1" = image ] && [ "$2" = inspect ]; then exit {1 if fail_pull else 0}; fi
if [ "$1" = pull ] && {'true' if fail_pull else 'false'}; then exit 1; fi
if [ "$1" = tag ] && {'true' if fail_tag else 'false'}; then exit 1; fi
if [ "$1" = compose ]; then exit {compose_rc}; fi
exit 0
''',
    )


def mock_curl(path: Path, status: str):
    write_exec(path, f"#!/bin/bash\nprintf '%s' '{status}'\n")


def manifest(path: Path, sha="abc123456789"):
    path.write_text(
        "D3_RELEASE_MANIFEST=1\n"
        "image\tfrontend\tfrontend\n"
        "image\tbackend\tbackend\n"
        "probe\thttp://localhost/frontend\t200\n"
        "probe\thttp://localhost/api/health\t200\n"
    )


def base(tmp_path, *, status="200", compose_rc=0, fail_pull=False, fail_tag=False):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    mock_docker(bindir / "docker", log, compose_rc=compose_rc, fail_pull=fail_pull, fail_tag=fail_tag)
    mock_curl(bindir / "curl", status)
    deploy = tmp_path / "deploy"
    deploy.mkdir(exist_ok=True)
    (deploy / "compose.yml").write_text("services: {}\n")
    mf = tmp_path / "release.manifest"
    manifest(mf)
    env = dict(os.environ)
    env.update(
        RELEASE_MANIFEST=str(mf),
        D3_RELEASE_TAG="abc123456789",
        ACR_REGISTRY="registry",
        ACR_NAMESPACE="ns",
        DEPLOY_DIR=str(deploy),
        STATE_DIR=str(tmp_path / "state"),
        HOST_LOCK=str(tmp_path / "host.lock"),
        DOCKER_BIN=str(bindir / "docker"),
        CURL_BIN=str(bindir / "curl"),
        DOCKER_LOG=str(log),
        HEALTHCHECK_WARMUP="0",
        HEALTHCHECK_INTERVAL="0",
        HEALTHCHECK_RETRIES="1",
        HEALTHCHECK_TIMEOUT="1",
        PULL_RETRIES="1",
        PULL_RETRY_DELAY="0",
    )
    return env, log


def test_pull_retry_default_matches_acr_budget():
    text = SCRIPT.read_text()
    assert 'PULL_RETRIES="${PULL_RETRIES:-6}"' in text
    assert 'PULL_RETRY_DELAY="${PULL_RETRY_DELAY:-10}"' in text


def mock_curl_sequence(path: Path, statuses, *, pause_marker=None):
    count_file = path.parent / "curl.count"
    values = " ".join(statuses)
    marker_line = f'touch "{pause_marker}"; sleep 0.25' if pause_marker else ":"
    write_exec(
        path,
        f'''#!/bin/bash
count_file="{count_file}"
n=$(cat "$count_file" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$count_file"
statuses=({values})
printf '%s' "${{statuses[$((n - 1))]}}"
if [ "$n" -eq 1 ]; then {marker_line}; fi
''',
    )


def mock_curl_status_exit_sequence(path: Path, attempts):
    count_file = path.parent / "curl.count"
    statuses = " ".join(status for status, _ in attempts)
    exit_codes = " ".join(str(rc) for _, rc in attempts)
    write_exec(
        path,
        f'''#!/bin/bash
count_file="{count_file}"
n=$(cat "$count_file" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$count_file"
statuses=({statuses})
exit_codes=({exit_codes})
printf '%s' "${{statuses[$((n - 1))]}}"
exit "${{exit_codes[$((n - 1))]}}"
''',
    )


def mock_rollback_docker(path: Path, log: Path, *, image_names=("frontend", "backend"),
                         rollback_rc=0, rollback_marker=None, evidence_sleep=0.0,
                         remove_deploy_dir_on_rollback_config: Path | None = None,
                         identity_gate_failed_tag: str | None = None):
    count_file = log.parent / "compose-up.count"
    rendered = "".join(f"{name}:%s\\n" for name in image_names)
    rendered_args = " ".join(f'"$D3_RELEASE_TAG"' for _ in image_names)
    marker_line = f'touch "{rollback_marker}"' if rollback_marker else ":"
    sleep_line = "sleep 0.25" if rollback_marker else ":"
    remove_line = ":"
    if remove_deploy_dir_on_rollback_config is not None:
        deploy_dir = str(remove_deploy_dir_on_rollback_config)
        remove_line = f'mv -- "{deploy_dir}" "{deploy_dir}.gone"'
    if identity_gate_failed_tag is None:
        rendered_config = f"  printf '{rendered}' {rendered_args}"
    else:
        rendered_config = f'''  if [ "$D3_RELEASE_TAG" = "{identity_gate_failed_tag}" ]; then
    printf 'frontend:latest\\nbackend:latest\\n'
  else
    printf '{rendered}' {rendered_args}
  fi'''
    write_exec(
        path,
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
{rendered_config}
  if [ "$D3_RELEASE_TAG" = "abc123456789" ]; then
    {remove_line}
  fi
  exit 0
fi
if [ "$1" = compose ] && [[ " $* " == *" ps "* || " $* " == *" logs "* ]]; then
  sleep {evidence_sleep}
fi
if [ "$1" = compose ] && [[ " $* " == *" up -d "* ]]; then
  n=$(cat "{count_file}" 2>/dev/null || echo 0)
  n=$((n + 1))
  echo "$n" > "{count_file}"
  if [ "$D3_RELEASE_TAG" = "abc123456789" ]; then
    {marker_line}
    {sleep_line}
    exit {rollback_rc}
  fi
fi
exit 0
''',
    )


def mock_identity_gate_docker(path: Path, log: Path, *, failed_tag):
    write_exec(
        path,
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  if [ "$D3_RELEASE_TAG" = "{failed_tag}" ]; then
    printf 'frontend:latest\\nbackend:latest\\n'
  else
    printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  fi
  exit 0
fi
exit 0
''',
    )


@pytest.mark.parametrize(
    ("axis", "expected_rc", "expected_up_count"),
    [
        ("healthy", 0, 1),
        ("identity_gate_failed", 1, 0),
        ("identity_gate_failed_with_previous_good", 1, 2),
        ("no_previous_good", 4, 1),
        ("no_previous_good_pending_signal", 4, 1),
        ("image_set_changed", 4, 2),
        ("image_set_changed_pending_signal", 4, 2),
        ("rollback_compose_failed", 4, 3),
        ("rollback_healthy", 1, 3),
        ("rollback_unhealthy", 4, 3),
        ("signal_before_rollback", 130, 3),
        ("rollback_unhealthy_pending_signal", 4, 3),
        ("rollback_healthy_pending_signal", 130, 3),
    ],
)
def test_release_outcome_axis_table(tmp_path, axis, expected_rc, expected_up_count):
    env, log = base(tmp_path)
    if axis == "healthy":
        result = run(env)
    elif axis in {"identity_gate_failed", "identity_gate_failed_with_previous_good"}:
        failed_tag = env["D3_RELEASE_TAG"]
        if axis == "identity_gate_failed_with_previous_good":
            assert run(env).returncode == 0
            env["D3_RELEASE_TAG"] = "def567890123"
            failed_tag = env["D3_RELEASE_TAG"]
        mock_identity_gate_docker(Path(env["DOCKER_BIN"]), log, failed_tag=failed_tag)
        result = run(env)
    elif axis in {"no_previous_good", "no_previous_good_pending_signal"}:
        if axis.endswith("pending_signal"):
            marker = tmp_path / "probe.started"
            mock_curl_sequence(Path(env["CURL_BIN"]), ["500"], pause_marker=marker)
            result = run_with_signal_on_marker(env, marker)
        else:
            mock_curl(Path(env["CURL_BIN"]), "500")
            result = run(env)
    else:
        assert run(env).returncode == 0
        env["D3_RELEASE_TAG"] = "def567890123"

        if axis in {"image_set_changed", "image_set_changed_pending_signal"}:
            changed_manifest = tmp_path / "release2.manifest"
            changed_manifest.write_text(
                "D3_RELEASE_MANIFEST=1\n"
                "image\tfrontend\tfrontend\n"
                "image\tbackend2\tbackend2\n"
                "probe\thttp://localhost/frontend\t200\n"
                "probe\thttp://localhost/api/health\t200\n"
            )
            env["RELEASE_MANIFEST"] = str(changed_manifest)
            mock_rollback_docker(Path(env["DOCKER_BIN"]), log, image_names=("frontend", "backend2"))
            if axis.endswith("pending_signal"):
                marker = tmp_path / "probe.started"
                mock_curl_sequence(Path(env["CURL_BIN"]), ["500"], pause_marker=marker)
                result = run_with_signal_on_marker(env, marker)
            else:
                mock_curl(Path(env["CURL_BIN"]), "500")
                result = run(env)
        elif axis in {"rollback_compose_failed", "rollback_healthy", "rollback_unhealthy"}:
            rollback_rc = 1 if axis == "rollback_compose_failed" else 0
            mock_rollback_docker(Path(env["DOCKER_BIN"]), log, rollback_rc=rollback_rc)
            statuses = ["500", "500"]
            if axis == "rollback_healthy":
                statuses = ["500", "200", "200"]
            else:
                statuses = ["500", "500"]
            mock_curl_sequence(Path(env["CURL_BIN"]), statuses)
            result = run(env)
        elif axis == "signal_before_rollback":
            marker = tmp_path / "new-compose.started"
            count_file = tmp_path / "compose-up.count"
            write_exec(
                Path(env["DOCKER_BIN"]),
                f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  exit 0
fi
if [ "$1" = compose ] && [[ " $* " == *" up -d "* ]]; then
  n=$(cat "{count_file}" 2>/dev/null || echo 0)
  n=$((n + 1))
  echo "$n" > "{count_file}"
  if [ "$n" -eq 1 ]; then touch "{marker}"; sleep 0.25; fi
fi
exit 0
''',
            )
            proc = subprocess.Popen(
                ["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            deadline = time.time() + 3
            while time.time() < deadline and not marker.exists():
                time.sleep(0.01)
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=5)
            result = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
        else:
            marker = tmp_path / "probe.started"
            mock_rollback_docker(
                Path(env["DOCKER_BIN"]), log,
            )
            statuses = ["500", "200", "200"]
            if axis == "rollback_unhealthy_pending_signal":
                statuses = ["500", "500"]
            pause_marker = marker if axis.endswith("pending_signal") else None
            mock_curl_sequence(Path(env["CURL_BIN"]), statuses, pause_marker=pause_marker)
            if pause_marker is not None:
                result = run_with_signal_on_marker(env, marker)
            else:
                result = run(env)

    assert result.returncode == expected_rc, result.stdout + result.stderr
    up_count = sum(line.endswith(" up -d") for line in log.read_text().splitlines())
    assert up_count == expected_up_count, (
        f"axis {axis} executed {up_count} compose up calls, expected "
        f"{expected_up_count}; log:\n{log.read_text()}"
    )


def test_probe_evidence_records_http_and_curl_exit_sequence(tmp_path):
    env, _ = base(tmp_path)
    env["HEALTHCHECK_RETRIES"] = "2"
    mock_curl_status_exit_sequence(
        Path(env["CURL_BIN"]), [("000", 28), ("000", 7)],
    )

    result = run(env)
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "[deploy][evidence] probe-attempts url=http://localhost/frontend 000(curl=28),000(curl=7)" in out


def test_rollback_evidence_is_emitted_before_rollback_deploy(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    env["D3_RELEASE_TAG"] = "def567890123"
    mock_curl(Path(env["CURL_BIN"]), "500")

    result = run(env)
    out = result.stdout + result.stderr
    assert result.returncode != 0
    evidence_positions = [
        out.index("[deploy][evidence] compose-ps"),
        out.index("[deploy][evidence] container-logs"),
        out.index("[deploy][evidence] probe-attempts"),
    ]
    assert evidence_positions[2] < out.index("rolling back complete")
    lines = log.read_text().splitlines()
    evidence_commands = [
        index for index, line in enumerate(lines)
        if line == "compose ps" or line.startswith("compose logs ")
    ]
    rollback_up = [index for index, line in enumerate(lines) if line.endswith(" up -d")]
    assert evidence_commands
    assert rollback_up
    assert max(evidence_commands) < max(rollback_up)


def test_rollback_evidence_timeout_does_not_block_rollback(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    env.update(D3_RELEASE_TAG="def567890123", EVIDENCE_TIMEOUT="1")
    mock_curl_sequence(Path(env["CURL_BIN"]), ["500", "200", "200"])
    mock_rollback_docker(Path(env["DOCKER_BIN"]), log, evidence_sleep=30)

    started = time.monotonic()
    result = run(env, timeout=8)
    elapsed = time.monotonic() - started
    out = result.stdout + result.stderr

    assert result.returncode == 1, out
    assert elapsed < 8, f"evidence timeout did not bound the deploy: {elapsed:.2f}s"
    assert "compose-ps timed out after 1s" in out
    assert "container-logs timed out after 1s" in out
    assert sum(line.endswith(" up -d") for line in log.read_text().splitlines()) == 3


def test_rollback_cd_failure_does_not_mark_mutated(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    env["D3_RELEASE_TAG"] = "def567890123"
    log.write_text("")
    mock_curl(Path(env["CURL_BIN"]), "500")
    mock_rollback_docker(
        Path(env["DOCKER_BIN"]), log,
        remove_deploy_dir_on_rollback_config=Path(env["DEPLOY_DIR"]),
        identity_gate_failed_tag=env["D3_RELEASE_TAG"],
    )

    result = run(env)

    assert result.returncode == 1, result.stdout + result.stderr
    assert not any(line.endswith(" up -d") for line in log.read_text().splitlines())


def run(env, timeout=None):
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True,
        timeout=timeout,
    )


def run_with_signal_on_marker(env, marker):
    proc = subprocess.Popen(
        ["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    deadline = time.time() + 3
    while time.time() < deadline and not marker.exists():
        time.sleep(0.01)
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=5)
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def fail_mv_target(path: Path, suffix: str):
    write_exec(
        path,
        f'''#!/bin/bash
target=""
for arg in "$@"; do target="$arg"; done
case "$target" in
  */{suffix}) exit 1 ;;
esac
exec /usr/bin/mv "$@"
''',
    )


def test_group_deploy_retags_sha_and_compose_once(tmp_path):
    env, log = base(tmp_path)
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = log.read_text().splitlines()
    assert lines.count("compose --env-file " + str(Path(env["DEPLOY_DIR"]) / ".d3-release.env") + " up -d") == 1
    assert any("tag registry/ns/frontend:abc123456789 frontend:abc123456789" in line for line in lines)
    assert not any(":latest" in line for line in lines)
    assert (Path(env["STATE_DIR"]) / "last_good_manifest").exists()


def test_compose_preserves_existing_dotenv_and_overlays_release_tag(tmp_path):
    env, log = base(tmp_path)
    deploy = Path(env["DEPLOY_DIR"])
    (deploy / ".env").write_text("COMPOSE_PROJECT_NAME=existing\n")
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = (
        f"compose --env-file {deploy / '.env'} --env-file {deploy / '.d3-release.env'} up -d"
    )
    assert expected in log.read_text()
    assert (deploy / ".env").read_text() == "COMPOSE_PROJECT_NAME=existing\n"


def test_compose_identity_gate_rejects_latest_and_preserves_last_good(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    before = (Path(env["STATE_DIR"]) / "last_good_release").read_text()
    log.write_text("")
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:latest\\nbackend:latest\\n'
  exit 0
fi
exit 0
''',
    )
    env["D3_RELEASE_TAG"] = "def567890123"
    result = run(env)
    assert result.returncode != 0
    assert "compose up" not in log.read_text()
    assert (Path(env["STATE_DIR"]) / "last_good_release").read_text() == before


def test_compose_identity_gate_rejects_mixed_stale_tag_for_one_image(tmp_path):
    # P1-4: docker compose config --images can emit the same image name more than
    # once when multiple services reference it (e.g. one service's compose config
    # got the new SHA, another still resolves to `latest` or a stale SHA due to a
    # missed env-var interpolation). The old gate only required ONE occurrence of
    # a declared image name to match the expected <name>:<tag> — as long as any
    # single occurrence matched, the gate passed even though another occurrence of
    # the SAME image name was stale. That is exactly the "half new, half old"
    # image group this atomic-release gate exists to block: compose up must never
    # run with a mixed group.
    env, log = base(tmp_path)
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nfrontend:latest\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  exit 0
fi
exit 0
''',
    )
    result = run(env)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "up -d" not in log.read_text(), "compose up must never run when one occurrence of a declared image is stale"
    state = Path(env["STATE_DIR"])
    assert not (state / "last_good_release").exists()
    assert not (state / "last_good_sha").exists()
    assert not (state / "last_good_manifest").exists()


def test_compose_identity_gate_allows_repeated_occurrences_all_matching_expected_tag(tmp_path):
    # Positive control for P1-4: every occurrence of every declared image name
    # (including a repeated occurrence of the SAME image name from two services)
    # matches the expected <name>:<tag>, and an undeclared image (nginx) is also
    # present — the gate must still pass and compose up must run.
    env, log = base(tmp_path)
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nfrontend:%s\\nbackend:%s\\nnginx:1.27\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  exit 0
fi
exit 0
''',
    )
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "up -d" in log.read_text()


def test_compose_identity_gate_allows_extra_public_images(tmp_path):
    env, log = base(tmp_path)
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\nnginx:1.27\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
fi
exit 0
''',
    )
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "compose --env-file" in log.read_text()


def test_compose_config_and_up_run_from_deploy_dir(tmp_path):
    env, log = base(tmp_path)
    cwd_log = tmp_path / "cwd.log"
    env["CWD_LOG"] = str(cwd_log)
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ]; then printf '%s\\n' "$PWD" >> "{cwd_log}"; fi
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
fi
exit 0
''',
    )
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert cwd_log.read_text().splitlines() == [env["DEPLOY_DIR"], env["DEPLOY_DIR"]]


def test_remote_cleanup_deletes_only_exact_three_segment_paths(tmp_path):
    env, _ = base(tmp_path)
    nonce = f"{os.getpid()}-1-7"
    remote_script = Path("/tmp") / f"d3-release-{nonce}.sh"
    remote_manifest = Path("/tmp") / f"d3-release-{nonce}.manifest"
    nonmatching = Path("/tmp") / f"d3-release-{os.getpid()}-1.sh"
    remote_script.write_text("temporary")
    remote_manifest.write_text(Path(env["RELEASE_MANIFEST"]).read_text())
    nonmatching.write_text("keep")
    env.update(
        RELEASE_MANIFEST=str(remote_manifest),
        RELEASE_TEMP_SCRIPT=str(remote_script),
        BUSY_LOCK_FILE=str(tmp_path / "busy.lock"),
        BUSY_LOCK_TIMEOUT="invalid",
    )
    result = run(env)
    assert result.returncode == 1
    assert not remote_script.exists()
    assert not remote_manifest.exists()
    assert nonmatching.exists()
    nonmatching.unlink()


def test_cleanup_runs_even_when_earliest_deploy_dir_validation_fails(tmp_path):
    env, _ = base(tmp_path)
    nonce = f"{os.getpid()}-2-9"
    remote_script = Path("/tmp") / f"d3-release-{nonce}.sh"
    remote_manifest = Path("/tmp") / f"d3-release-{nonce}.manifest"
    remote_script.write_text("temporary")
    remote_manifest.write_text(Path(env["RELEASE_MANIFEST"]).read_text())
    env.update(
        RELEASE_MANIFEST=str(remote_manifest),
        RELEASE_TEMP_SCRIPT=str(remote_script),
        DEPLOY_DIR="",
    )
    result = run(env)
    assert result.returncode != 0
    assert not remote_script.exists()
    assert not remote_manifest.exists()


def test_probe_failure_rolls_back_entire_group_and_preserves_good(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    env["D3_RELEASE_TAG"] = "def567890123"
    env["RELEASE_MANIFEST"] = str(Path(env["RELEASE_MANIFEST"]))
    mock_curl(Path(env["CURL_BIN"]), "500")
    log.write_text("")
    result = run(env)
    assert result.returncode != 0
    lines = log.read_text()
    assert "compose" in lines
    assert "abc123456789" in lines
    assert "def567890123" in lines
    assert (Path(env["STATE_DIR"]) / "last_good_sha").read_text().strip() == "abc123456789"


def test_first_release_probe_failure_has_explicit_no_rollback(tmp_path):
    env, _ = base(tmp_path, status="500")
    result = run(env)
    assert result.returncode != 0
    assert "no previous" in (result.stdout + result.stderr).lower()
    assert not (Path(env["STATE_DIR"]) / "last_good_sha").exists()


def test_first_release_canonical_commit_failure_has_no_good_release(tmp_path):
    env, log = base(tmp_path)
    fail_mv_target(Path(env["DOCKER_BIN"]).parent / "mv", "last_good_release")
    env["PATH"] = f'{Path(env["DOCKER_BIN"]).parent}:{os.environ["PATH"]}'
    result = run(env)
    state = Path(env["STATE_DIR"])
    assert result.returncode != 0
    assert "canonical" in (result.stdout + result.stderr).lower()
    assert not (state / "last_good_release").exists()
    assert not (state / "last_good_sha").exists()
    assert not (state / "last_good_manifest").exists()


def test_canonical_commit_failure_rolls_back_runtime_and_preserves_old_state(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    state = Path(env["STATE_DIR"])
    before_release = (state / "last_good_release").read_bytes()
    before_sha = (state / "last_good_sha").read_bytes()
    before_manifest = (state / "last_good_manifest").read_bytes()
    log.write_text("")
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = pull ]; then exit 0; fi
if [ "$1" = image ] && [ "$2" = inspect ]; then exit 0; fi
env_file=""
read_next=0
for arg in "$@"; do
  if [ "$read_next" = 1 ]; then env_file="$arg"; read_next=0; fi
  if [ "$arg" = --env-file ]; then read_next=1; fi
done
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  # Reflect the tag actually written to --env-file for THIS invocation (new
  # release vs. rollback each write their own tag before calling config --images)
  # rather than a hardcoded mix of both tags — the stricter P1-4 gate now
  # requires every occurrence of a declared image name to match the currently
  # expected tag, so the mock must behave like the real docker compose would.
  tag=$(grep '^D3_RELEASE_TAG=' "$env_file" | cut -d= -f2)
  printf 'frontend:%s\\nbackend:%s\\n' "$tag" "$tag"
fi
if [ "$1" = compose ] && [[ " $* " == *" up -d "* ]]; then
  printf 'compose-env=%s\\n' "$(cat "$env_file")" >> "{log}"
fi
exit 0
''',
    )
    fail_mv_target(Path(env["DOCKER_BIN"]).parent / "mv", "last_good_release")
    env["PATH"] = f'{Path(env["DOCKER_BIN"]).parent}:{os.environ["PATH"]}'
    env["D3_RELEASE_TAG"] = "def567890123"
    result = run(env)
    lines = log.read_text().splitlines()
    assert result.returncode != 0
    assert sum(line.endswith("up -d") for line in lines) == 2
    assert "compose-env=D3_RELEASE_TAG=def567890123" in lines
    assert "compose-env=D3_RELEASE_TAG=abc123456789" in lines
    assert (Path(env["DEPLOY_DIR"]) / ".d3-release.env").read_text() == "D3_RELEASE_TAG=abc123456789\n"
    assert (state / "last_good_release").read_bytes() == before_release
    assert (state / "last_good_sha").read_bytes() == before_sha
    assert (state / "last_good_manifest").read_bytes() == before_manifest


def test_rollback_env_leak_does_not_poison_identity_gate(tmp_path):
    # P1-A: D3_RELEASE_TAG is exported into this script's process environment by
    # the SSH invocation (`D3_RELEASE_TAG=... bash release_deploy.sh`) and is
    # NEVER reassigned for the life of the process — it still holds the NEW
    # release's tag even while do_release() is rolling the compose group back
    # to the OLD tag it just wrote into ENV_FILE. Real `docker compose`
    # resolves ${D3_RELEASE_TAG} interpolation from the shell environment
    # BEFORE --env-file, so unless every compose invocation explicitly pins
    # D3_RELEASE_TAG to the tag it is actually deploying, the rollback's own
    # `config --images` gate check (and `up -d`) resolve the NEW tag instead
    # of the OLD one it was just told to deploy — the identity gate then
    # rejects its own rollback attempt as a "stray reference" and the host is
    # stuck on the failed release.
    #
    # The mock below deliberately reproduces real compose's env-over-file
    # precedence (rather than a "just cat the --env-file" shortcut, which
    # would silently hide this bug) so this test fails against the unfixed
    # script and passes once compose_release() pins D3_RELEASE_TAG=<tag> on
    # each docker invocation.
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    state = Path(env["STATE_DIR"])
    before_sha = (state / "last_good_sha").read_text().strip()
    assert before_sha == "abc123456789"

    log.write_text("")
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = pull ]; then exit 0; fi
if [ "$1" = image ] && [ "$2" = inspect ]; then exit 0; fi
if [ "$1" = tag ]; then exit 0; fi
env_file=""
read_next=0
for arg in "$@"; do
  if [ "$read_next" = 1 ]; then env_file="$arg"; read_next=0; fi
  if [ "$arg" = --env-file ]; then read_next=1; fi
done
# Reproduce real docker compose precedence: a shell-environment
# D3_RELEASE_TAG (if the invoking process still has one set) wins over
# --env-file. This is what a bare, unpinned `docker compose ...` call would
# actually resolve on the SSH host.
if [ -n "${{D3_RELEASE_TAG:-}}" ]; then
  tag="$D3_RELEASE_TAG"
else
  tag=$(grep '^D3_RELEASE_TAG=' "$env_file" | cut -d= -f2)
fi
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\n' "$tag" "$tag"
  exit 0
fi
if [ "$1" = compose ] && [[ " $* " == *" up -d "* ]]; then
  printf 'up-d-tag=%s\\n' "$tag" >> "{log}"
  exit 0
fi
exit 0
''',
    )
    env["D3_RELEASE_TAG"] = "def567890123"
    mock_curl(Path(env["CURL_BIN"]), "500")  # force the new release's probe to fail -> rollback
    result = run(env)
    assert result.returncode != 0
    out = result.stdout + result.stderr
    assert "compose identity gate" not in out, (
        "rollback must not be rejected by a D3_RELEASE_TAG leaked from the NEW "
        "release's process environment: " + out
    )
    lines = log.read_text().splitlines()
    assert f"up-d-tag={before_sha}" in lines, (
        "rollback's compose up must actually run, pinned to the OLD tag, "
        "despite the leaked process env"
    )
    assert (state / "last_good_sha").read_text().strip() == before_sha


def test_legacy_refresh_failure_after_canonical_commit_is_fail_open(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    state = Path(env["STATE_DIR"])
    old_sha = (state / "last_good_sha").read_bytes()
    log.write_text("")
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = pull ]; then exit 0; fi
if [ "$1" = image ] && [ "$2" = inspect ]; then exit 0; fi
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:def567890123\\nbackend:def567890123\\n'
fi
exit 0
''',
    )
    fail_mv_target(Path(env["DOCKER_BIN"]).parent / "mv", "last_good_sha")
    env["PATH"] = f'{Path(env["DOCKER_BIN"]).parent}:{os.environ["PATH"]}'
    env["D3_RELEASE_TAG"] = "def567890123"
    result = run(env)
    lines = log.read_text().splitlines()
    assert result.returncode == 0, result.stdout + result.stderr
    assert sum(line.endswith("up -d") for line in lines) == 1
    assert (state / "last_good_release").read_text().startswith("def567890123\n")
    assert (state / "last_good_sha").read_bytes() == old_sha
    assert "legacy last_good_sha update failed" in (result.stdout + result.stderr)


def test_pull_or_compose_failure_never_runs_compose_for_partial_group(tmp_path):
    env, log = base(tmp_path, fail_pull=True)
    result = run(env)
    assert result.returncode != 0
    assert not log.exists() or "compose" not in log.read_text()

    env, log = base(tmp_path, compose_rc=1)
    result = run(env)
    assert result.returncode != 0
    assert "compose" in log.read_text()
    assert not (Path(env["STATE_DIR"]) / "last_good_sha").exists()


def test_retag_failure_never_runs_compose(tmp_path):
    env, log = base(tmp_path, fail_tag=True)
    result = run(env)
    assert result.returncode != 0
    assert "compose" not in log.read_text()


def test_remote_manifest_rejects_zero_probes_defense_in_depth(tmp_path):
    env, log = base(tmp_path)
    Path(env["RELEASE_MANIFEST"]).write_text(
        "D3_RELEASE_MANIFEST=1\nimage\tfrontend\tfrontend\n"
    )
    result = run(env)
    assert result.returncode != 0
    assert "probe" in (result.stdout + result.stderr).lower()
    assert not log.exists() or "compose" not in log.read_text()


def test_remote_manifest_rejects_duplicate_or_full_registry_ref(tmp_path):
    env, log = base(tmp_path)
    Path(env["RELEASE_MANIFEST"]).write_text(
        "D3_RELEASE_MANIFEST=1\n"
        "image\tfrontend\tfrontend\n"
        "image\tfrontend\tfrontend\n"
        "probe\thttp://localhost/health\t200\n"
    )
    result = run(env)
    assert result.returncode != 0
    assert "compose" not in log.read_text() if log.exists() else True

    Path(env["RELEASE_MANIFEST"]).write_text(
        "D3_RELEASE_MANIFEST=1\n"
        "image\tfrontend\tregistry/ns/frontend\n"
        "probe\thttp://localhost/health\t200\n"
    )
    result = run(env)
    assert result.returncode != 0


def test_pull_exhausted_exact_sha_local_fallback(tmp_path):
    env, log = base(tmp_path)
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = pull ]; then exit 1; fi
if [ "$1" = image ] && [ "$2" = inspect ]; then exit 0; fi
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
fi
exit 0
''',
    )
    env["PULL_RETRIES"] = "1"
    env["PULL_RETRY_DELAY"] = "0"
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already local" in result.stdout


def test_rollback_reuses_exact_registry_or_local_immutable_refs(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    log.write_text("")
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = pull ]; then exit 0; fi
if [ "$1" = image ] && [ "$2" = inspect ]; then
  case "$3" in
    registry/ns/frontend:abc123456789) exit 0 ;;
    backend:abc123456789) exit 0 ;;
    *) exit 1 ;;
  esac
fi
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:abc123456789\\nbackend:abc123456789\\nfrontend:def567890123\\nbackend:def567890123\\n'
fi
exit 0
''',
    )
    env["D3_RELEASE_TAG"] = "def567890123"
    mock_curl(Path(env["CURL_BIN"]), "500")
    result = run(env)
    assert result.returncode != 0
    lines = log.read_text().splitlines()
    assert lines.count("pull registry/ns/frontend:def567890123") == 1
    assert lines.count("pull registry/ns/backend:def567890123") == 1
    assert not any("pull registry/ns/frontend:abc123456789" in line for line in lines)
    assert not any("pull registry/ns/backend:abc123456789" in line for line in lines)
    assert "using local immutable backend:abc123456789 for rollback" in result.stdout


def _lock_holder(path: Path, mode: str = "-x", seconds: str = "1"):
    return subprocess.Popen(
        ["bash", "-c", f'exec 8>"$1"; flock {mode} 8; sleep "$2"', "holder", str(path), seconds],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_busy_lock_timeout_defers_without_compose_or_last_good(tmp_path):
    env, log = base(tmp_path)
    busy = tmp_path / "busy.lock"
    holder = _lock_holder(busy, seconds="2")
    time.sleep(0.05)
    env.update(BUSY_LOCK_FILE=str(busy), BUSY_LOCK_TIMEOUT="1")
    result = run(env)
    holder.wait(timeout=3)
    assert result.returncode == 3
    assert "deferred" in (result.stdout + result.stderr).lower()
    assert not (Path(env["STATE_DIR"]) / "last_good_release").exists()
    assert not log.exists() or "compose" not in log.read_text()


def test_busy_lock_release_within_budget_allows_deploy(tmp_path):
    env, log = base(tmp_path)
    busy = tmp_path / "busy.lock"
    holder = _lock_holder(busy, seconds="0.2")
    env.update(BUSY_LOCK_FILE=str(busy), BUSY_LOCK_TIMEOUT="2")
    result = run(env)
    holder.wait(timeout=3)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "compose" in log.read_text()


def test_busy_lock_missing_file_warns_and_creates(tmp_path):
    env, _ = base(tmp_path)
    busy = tmp_path / "missing" / "busy.lock"
    env.update(BUSY_LOCK_FILE=str(busy), BUSY_LOCK_TIMEOUT="2")
    result = run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert busy.exists()
    assert "WARN" in result.stderr


def test_busy_lock_invalid_timeout_is_configuration_failure(tmp_path):
    env, log = base(tmp_path)
    env.update(BUSY_LOCK_FILE=str(tmp_path / "busy.lock"), BUSY_LOCK_TIMEOUT="nope")
    result = run(env)
    assert result.returncode == 1
    assert not log.exists() or "pull " not in log.read_text()


def test_busy_lock_timeout_rejects_leading_zero(tmp_path):
    # P2-2 (codex review round 3): the review flagged that a leading-zero
    # BUSY_LOCK_TIMEOUT (e.g. "0600") could slip past validation and then hit
    # bash arithmetic's octal interpretation ($((SECONDS + BUSY_LOCK_TIMEOUT))
    # reads "0600" as octal 384, and "08"/"09" is an outright arithmetic
    # error) instead of being rejected as a configuration error up front.
    #
    # Regression-tested here, but no source change was needed: is_positive_
    # integer() in release_deploy.sh has always been `^[1-9][0-9]*$` (added in
    # the very first commit that introduced busy-lock support, 3e24106),
    # which already requires the first digit to be 1-9 and so already rejects
    # every leading-zero form. Confirmed empirically before writing this test
    # (both via a direct manual run and via the pytest harness) that
    # BUSY_LOCK_TIMEOUT="0600" already exits 1 with a readable
    # "must be a positive integer" message, not exit 3 / an octal-arithmetic
    # surprise. This test exists purely as a regression guard against that
    # regex ever being loosened back to `^[0-9]+$`.
    env, log = base(tmp_path)
    env.update(BUSY_LOCK_FILE=str(tmp_path / "busy.lock"), BUSY_LOCK_TIMEOUT="0600")
    result = run(env)
    assert result.returncode == 1, (
        f"leading-zero BUSY_LOCK_TIMEOUT must be a configuration failure (rc=1), "
        f"not deferred (rc=3): got {result.returncode}; {result.stdout}{result.stderr}"
    )
    assert "positive integer" in (result.stdout + result.stderr), (
        "the rejection message must be readable, not a raw bash arithmetic error"
    )
    assert not log.exists() or "pull " not in log.read_text()


def test_busy_gate_host_contention_releases_admission_fd(tmp_path):
    env, _ = base(tmp_path)
    busy = tmp_path / "busy.lock"
    host_holder = _lock_holder(Path(env["HOST_LOCK"]), seconds="2")
    time.sleep(0.05)
    env.update(BUSY_LOCK_FILE=str(busy), BUSY_LOCK_TIMEOUT="1")
    result = run(env)
    host_holder.wait(timeout=3)
    assert result.returncode == 3
    probe = subprocess.run(["flock", "-n", str(busy), "-c", "true"])
    assert probe.returncode == 0, "busy admission lock leaked while host was contended"


def test_host_lock_flock_non_contention_error_surfaces_as_real_failure(tmp_path):
    # P1-3: `flock -n 9` returning rc=1 means genuine lock contention (the host
    # lock is held by another deploy) and is the ONLY case that should feed the
    # existing "release admission, sleep, retry" busy-host path. Any other
    # non-zero rc means flock itself failed (bad args, syscall error, etc.) and
    # must surface as a real error — not be silently swallowed and treated as
    # ordinary host-busy contention.
    #
    # A fake `flock` binary ahead of PATH intercepts only the exact `flock -n 9`
    # host-lock probe call (the sole call site in release_deploy.sh) and returns
    # a non-1, non-zero code (7); every other flock invocation (the busy-lock fd 8
    # wait/unlock calls) is delegated to the real flock binary so the rest of the
    # busy-lock gate behaves exactly as it would in production.
    env, log = base(tmp_path)
    busy = tmp_path / "busy.lock"

    bindir = tmp_path / "flockbin"
    bindir.mkdir()
    write_exec(
        bindir / "flock",
        '''#!/bin/bash
if [ "$1" = "-n" ] && [ "$2" = "9" ]; then
  exit 7
fi
exec /usr/bin/flock "$@"
''',
    )
    env["PATH"] = f'{bindir}:{env["PATH"]}'
    env.update(BUSY_LOCK_FILE=str(busy), BUSY_LOCK_TIMEOUT="5")
    result = run(env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "flock on host lock failed (rc=7" in result.stderr
    assert not log.exists() or "compose" not in log.read_text()
    state = Path(env["STATE_DIR"])
    assert not (state / "last_good_release").exists()
    # The busy admission lock must not leak on this error exit path either.
    probe = subprocess.run(["flock", "-n", str(busy), "-c", "true"])
    assert probe.returncode == 0, "busy admission lock leaked on the flock host-lock error path"


def test_busy_shared_service_lock_defers_before_compose(tmp_path):
    env, log = base(tmp_path)
    busy = tmp_path / "busy.lock"
    holder = _lock_holder(busy, mode="-s", seconds="2")
    time.sleep(0.05)
    env.update(BUSY_LOCK_FILE=str(busy), BUSY_LOCK_TIMEOUT="1")
    result = run(env)
    holder.wait(timeout=3)
    assert result.returncode == 3
    assert log.read_text().count("pull ") == 2
    assert "compose" not in log.read_text()
    assert not (Path(env["DEPLOY_DIR"]) / ".d3-release.env").exists()
    assert not (Path(env["STATE_DIR"]) / "last_good_release").exists()


def test_busy_lock_release_does_not_repeat_staged_pulls(tmp_path):
    env, log = base(tmp_path)
    busy = tmp_path / "busy.lock"
    holder = _lock_holder(busy, seconds="0.2")
    time.sleep(0.05)
    env.update(BUSY_LOCK_FILE=str(busy), BUSY_LOCK_TIMEOUT="2")
    result = run(env)
    holder.wait(timeout=3)
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text().count("pull ") == 2


def test_term_while_busy_lock_waiting_does_not_switch_release(tmp_path):
    env, log = base(tmp_path)
    busy = tmp_path / "busy.lock"
    holder = _lock_holder(busy, seconds="30")
    time.sleep(0.05)
    env.update(BUSY_LOCK_FILE=str(busy), BUSY_LOCK_TIMEOUT="30")
    started = time.time()
    proc = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=3)
    finally:
        holder.terminate()
        holder.wait(timeout=3)
    assert proc.returncode != 0, stdout + stderr
    assert time.time() - started < 3
    assert "compose" not in log.read_text()
    assert not (Path(env["STATE_DIR"]) / "last_good_release").exists()


def test_rollback_pull_failure_preserves_atomic_previous_release(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    state = Path(env["STATE_DIR"])
    before_release = (state / "last_good_release").read_text()

    # New release pulls two images, then rollback's first pull fails.  The
    # previous manifest/SHA must remain byte-for-byte untouched.
    count_file = Path(env["DOCKER_LOG"] + ".count")
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = pull ]; then
  n=$(cat "{count_file}" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "{count_file}"
  [ "$n" -ge 3 ] && exit 1
fi
if [ "$1" = compose ]; then exit 0; fi
exit 0
''',
    )
    env["D3_RELEASE_TAG"] = "def567890123"
    mock_curl(Path(env["CURL_BIN"]), "500")
    result = run(env)
    assert result.returncode != 0
    assert (state / "last_good_release").read_text() == before_release
    assert (state / "last_good_sha").read_text().strip() == "abc123456789"


def test_term_during_new_compose_rolls_back_and_releases_lock(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    state = Path(env["STATE_DIR"])
    before = (state / "last_good_release").read_text()
    log.write_text("")
    marker = tmp_path / "compose.done"
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  exit 0
fi
if [ "$1" = compose ]; then sleep 0.25; touch "{marker}"; fi
exit 0
''',
    )
    env["D3_RELEASE_TAG"] = "def567890123"
    proc = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 3
    while time.time() < deadline and "compose" not in log.read_text():
        time.sleep(0.01)
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=5)
    assert proc.returncode != 0, stdout + stderr
    lines = log.read_text().splitlines()
    assert sum(line.startswith("compose ") for line in lines) >= 2
    assert marker.exists(), "rollback compose must finish despite TERM"
    assert (state / "last_good_release").read_text() == before
    assert not list(state.glob(".release-*.release"))
    # The lock is not leaked by the signal handler.
    assert run(env).returncode == 0


def test_hup_during_new_compose_rolls_back_and_releases_lock(tmp_path):
    # P1-D: OpenSSH delivers SIGHUP (not INT/TERM) to the remote command's
    # process group when the SSH transport drops mid-run. Before this fix,
    # only INT/TERM were trapped into the pending-signal -> rollback path, so
    # a dropped SSH connection during the critical section (after compose up,
    # before promote) would hit bash's default disposition for HUP
    # (terminate immediately, no EXIT trap, no rollback) and leave the host on
    # an unhealthy, unpromoted release. HUP must walk the exact same
    # check_pending()/rollback path as TERM.
    #
    # This is a copy of test_term_during_new_compose_rolls_back_and_releases_lock
    # with SIGHUP substituted for SIGTERM, plus an explicit exit-code check:
    # a *trapped* signal drives the script through its normal `exit 130` in
    # do_release() (a positive, WIFEXITED status), whereas an *untrapped*
    # fatal signal has the kernel kill the process directly (Python reports
    # that as a negative return code, -SIGHUP). That distinction holds
    # regardless of exactly how far the race got before the signal landed,
    # unlike the compose-line-count/marker checks below (kept for parity with
    # the TERM test, but an orphaned child can still finish those on its own
    # even when the parent was killed outright).
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    state = Path(env["STATE_DIR"])
    before = (state / "last_good_release").read_text()
    log.write_text("")
    marker = tmp_path / "compose.done"
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  exit 0
fi
if [ "$1" = compose ]; then sleep 0.25; touch "{marker}"; fi
exit 0
''',
    )
    env["D3_RELEASE_TAG"] = "def567890123"
    proc = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 3
    while time.time() < deadline and "compose" not in log.read_text():
        time.sleep(0.01)
    proc.send_signal(signal.SIGHUP)
    stdout, stderr = proc.communicate(timeout=5)
    assert proc.returncode == 130, (
        "HUP must drive the script through its normal pending-signal exit "
        f"(rc=130), not an untrapped kill (negative rc): got {proc.returncode}; "
        + stdout + stderr
    )
    lines = log.read_text().splitlines()
    assert sum(line.startswith("compose ") for line in lines) >= 2
    assert marker.exists(), "rollback compose must finish despite HUP"
    assert (state / "last_good_release").read_text() == before
    assert not list(state.glob(".release-*.release"))
    # The lock is not leaked by the signal handler.
    assert run(env).returncode == 0


def test_pending_signal_rechecked_before_compose_up(tmp_path):
    # P1 (codex review round 3): a signal (INT/TERM/HUP) arriving while
    # compose_release() is inside the identity-gate check -- right after
    # `docker compose config --images` returns, before `up -d` is issued --
    # was only recorded into PENDING_SIGNAL. The gate-check loop that follows
    # is pure in-process bash with no further check_pending() call, so
    # execution fell straight through into `docker compose up -d` regardless
    # of the pending cancellation. On a FIRST deploy (no previous good
    # release to roll back to) this is the worst case: the new, cancelled
    # release's containers get switched in, are never promoted, and there is
    # nothing to roll back to -- an unrecoverable half-cancelled deploy.
    #
    # Injection technique borrowed from the existing TERM/HUP compose tests:
    # the mock's `config --images` branch sleeps briefly (holding up the
    # command substitution bash is blocked on), the test polls the log for
    # that call to have started and sends the signal while it is still
    # "running", then lets it finish normally. bash defers dispatching the
    # trap until it regains control; the identity-gate loop that follows
    # makes no further docker calls, so PENDING_SIGNAL is guaranteed to
    # already be set by the time compose_release() reaches its pre-`up -d`
    # check -- this is deterministic, not a race, once the injected call has
    # actually started (which the log-polling loop confirms before signaling).
    env, log = base(tmp_path)
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  sleep 0.25
  printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  exit 0
fi
exit 0
''',
    )
    proc = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 3
    while time.time() < deadline and not (log.exists() and "config --images" in log.read_text()):
        time.sleep(0.01)
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=5)
    assert proc.returncode == 130, (
        "a signal caught during the identity-gate check must drive the script "
        f"through the normal pending-signal exit (rc=130): got {proc.returncode}; "
        + stdout + stderr
    )
    lines = log.read_text().splitlines()
    assert not any(line.endswith(" up -d") for line in lines), (
        "compose up must never run once a pending signal has been recorded, even "
        "if the identity gate already passed:\n" + "\n".join(lines)
    )
    assert "no previous good release available" in (stdout + stderr), (
        "first deploy (no previous good) must explicitly refuse pseudo-rollback, "
        "not silently leave a cancelled-but-switched runtime with no rollback target"
    )
    state = Path(env["STATE_DIR"])
    assert not (state / "last_good_release").exists()


def test_pending_signal_rechecked_after_promotion_trap_before_promote():
    # P2-1 (codex review round 3): between `probe_release && check_pending`
    # returning true (probe passed, no cancellation seen yet) and the very
    # next line actually installing `trap ':' INT TERM HUP`, on_signal() is
    # still wired up as the live INT/TERM/HUP handler. A signal landing in
    # that gap sets PENDING_SIGNAL but is then silently swallowed forever:
    # every signal arriving AFTER the trap is installed is ignored (`:`), and
    # nothing rechecks PENDING_SIGNAL before promote() runs. Without a
    # recheck here, a release cancelled in that gap would still be committed
    # as last_good_release instead of taking the existing
    # rollback-without-promotion path.
    #
    # This window is a handful of in-process bash instructions with no
    # external command execution in between it -- unlike the up -d race
    # above (test_pending_signal_rechecked_before_compose_up), which is
    # anchored to an external `docker compose config --images` call the test
    # harness can make block long enough to land a signal deterministically,
    # there is no reliable way to inject a signal inside this specific gap
    # from outside the process. Per the review's own guidance for this case,
    # falling back to a static, contract-style assertion: the source must
    # recheck pending-signal state after the promotion trap is installed and
    # before promote() is called.
    text = SCRIPT.read_text()
    idx_trap = text.index("trap ':' INT TERM HUP")
    idx_promote = text.index('promote "$D3_RELEASE_TAG"')
    assert idx_trap < idx_promote, "the promotion trap must be installed before promote() is called"
    between = text[idx_trap:idx_promote]
    assert any(
        line.strip() == "if check_pending; then"
        for line in between.splitlines()
        if not line.lstrip().startswith("#")
    ), (
        "a pending-signal recheck must appear between installing the promotion "
        "trap and calling promote() -- otherwise a signal that lands in the gap "
        "before the trap is installed gets recorded, then is silently ignored "
        "by the trap that follows, and promote() commits a cancelled release"
    )


def test_term_during_pull_does_not_start_new_compose(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    state = Path(env["STATE_DIR"])
    before = (state / "last_good_release").read_text()
    log.write_text("")
    marker = tmp_path / "pull.started"
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  exit 0
fi
if [ "$1" = pull ]; then touch "{marker}"; sleep 0.3; fi
exit 0
''',
    )
    env["D3_RELEASE_TAG"] = "def567890123"
    proc = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 3
    while time.time() < deadline and not marker.exists():
        time.sleep(0.01)
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=5)
    assert proc.returncode != 0, stdout + stderr
    lines = log.read_text().splitlines()
    assert sum(line.startswith("compose ") for line in lines) <= 1
    assert (state / "last_good_release").read_text() == before
    assert run(env).returncode == 0


def test_term_during_rollback_is_ignored_until_group_finishes(tmp_path):
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    marker = tmp_path / "rollback.done"
    log.write_text("")
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ]; then
  n=$(grep -c '^compose ' "{log}")
  if [ "$n" -ge 2 ]; then sleep 0.25; touch "{marker}"; fi
fi
exit 0
''',
    )
    env["D3_RELEASE_TAG"] = "def567890123"
    mock_curl(Path(env["CURL_BIN"]), "500")
    proc = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 3
    while time.time() < deadline and log.read_text().count("compose ") < 2:
        time.sleep(0.01)
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=5)
    assert proc.returncode != 0, stdout + stderr
    assert marker.exists(), "TERM must not interrupt rollback compose"
    assert (Path(env["STATE_DIR"]) / "last_good_sha").read_text().strip() == "abc123456789"


def test_rollback_refuses_when_image_set_changed(tmp_path):
    # Codex review (release lane R4): rollback replays the OLD manifest's
    # declared images via deploy_group -> compose_release, but `compose up -d`
    # always acts on the FULL docker-compose.yml as it currently stands on the
    # host. If the declared image-name set changed between the previous good
    # release and this one (here: backend renamed to backend2 -- backend
    # removed, backend2 added), replaying only the old manifest would roll
    # frontend back while silently leaving backend2 untouched (still on the
    # new image) -- a partial rollback the script must never perform silently.
    # It must refuse outright: no docker call may reference the old SHA, the
    # existing failure exit path is reused, and the log must explain why.
    env, log = base(tmp_path)
    assert run(env).returncode == 0  # release 1: frontend+backend, promoted

    new_manifest = tmp_path / "release2.manifest"
    new_manifest.write_text(
        "D3_RELEASE_MANIFEST=1\n"
        "image\tfrontend\tfrontend\n"
        "image\tbackend2\tbackend2\n"
        "probe\thttp://localhost/frontend\t200\n"
        "probe\thttp://localhost/api/health\t200\n"
    )
    env["RELEASE_MANIFEST"] = str(new_manifest)
    env["D3_RELEASE_TAG"] = "def567890123"
    log.write_text("")
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend2:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  exit 0
fi
exit 0
''',
    )
    mock_curl(Path(env["CURL_BIN"]), "500")  # force release 2's probe to fail -> rollback attempt

    result = run(env)
    assert result.returncode != 0
    out = result.stdout + result.stderr
    assert "image set changed" in out, out

    lines = log.read_text().splitlines()
    # No docker call may reference the previous release's SHA -- that would
    # mean pull_and_retag/deploy_group ran for the old manifest despite the
    # mismatched image set.
    assert not any("abc123456789" in line for line in lines), lines
    # Only release 2's own (failed-probe) compose up may have run; a second
    # "up -d" would mean a rollback compose actually executed.
    assert sum(line.endswith(" up -d") for line in lines) == 1

    state = Path(env["STATE_DIR"])
    assert (state / "last_good_sha").read_text().strip() == "abc123456789"
    assert (state / "last_good_release").read_text().splitlines()[0] == "abc123456789"


def test_rollback_proceeds_when_image_set_unchanged(tmp_path):
    # Positive control for the image-set guard above: this scenario is
    # already covered by test_probe_failure_rolls_back_entire_group_and_
    # preserves_good (same RELEASE_MANIFEST file/images reused across both
    # releases, probe forced to fail, rollback must still complete and
    # restore last_good_sha to the previous release). Re-asserted here next
    # to the new guard test so the "sets identical -> rollback unaffected"
    # contract is visible in one place, without duplicating the scenario logic.
    env, log = base(tmp_path)
    assert run(env).returncode == 0
    env["D3_RELEASE_TAG"] = "def567890123"
    mock_curl(Path(env["CURL_BIN"]), "500")
    log.write_text("")
    result = run(env)
    assert result.returncode != 0
    out = result.stdout + result.stderr
    assert "image set changed" not in out
    lines = log.read_text().splitlines()
    assert sum(line.endswith(" up -d") for line in lines) == 2
    assert (Path(env["STATE_DIR"]) / "last_good_sha").read_text().strip() == "abc123456789"


COMPOSE_ONESHOT_SERVICES = ("app", "migrate")


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


def _mock_oneshot_release_docker(
    path: Path,
    log: Path,
    *,
    services: tuple[str, ...] = COMPOSE_ONESHOT_SERVICES,
    image_names: tuple[str, ...] = ("frontend", "backend"),
    rollback_rc: int = 0,
):
    count_file = log.parent / "compose-up.count"
    rendered = "".join(f"{name}:%s\\n" for name in image_names)
    rendered_args = " ".join(f'"$D3_RELEASE_TAG"' for _ in image_names)
    service_lines = "\\n".join(services)
    write_exec(
        path,
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --services "* ]]; then
  printf '{service_lines}\\n'
  exit 0
fi
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf '{rendered}' {rendered_args}
  exit 0
fi
if [ "$1" = compose ] && [[ " $* " == *" up -d "* ]]; then
  n=$(cat "{count_file}" 2>/dev/null || echo 0)
  n=$((n + 1))
  echo "$n" > "{count_file}"
  if [ "$D3_RELEASE_TAG" = "abc123456789" ]; then
    exit {rollback_rc}
  fi
fi
exit 0
''',
    )


def _oneshot_base(tmp_path, *, oneshot_services: str = "migrate"):
    env, log = base(tmp_path)
    deploy = Path(env["DEPLOY_DIR"])
    (deploy / "compose.yml").write_text(
        "services:\n  app:\n    image: frontend\n  migrate:\n    image: frontend\n"
    )
    _mock_oneshot_release_docker(Path(env["DOCKER_BIN"]), log)
    if oneshot_services:
        env["ONESHOT_SERVICES"] = oneshot_services
    return env, log


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
    env, log = _oneshot_base(tmp_path, oneshot_services=oneshot_services)
    if phase == "rollback":
        assert run(env).returncode == 0
        env["D3_RELEASE_TAG"] = "def567890123"
        log.write_text("")
        mock_curl(Path(env["CURL_BIN"]), "500")
        result = run(env)
    else:
        result = run(env)

    out = result.stdout + result.stderr
    up_lines = _compose_up_lines(log.read_text())

    if case == "empty_forward":
        assert result.returncode == 0, out
        assert len(up_lines) == 1
        _assert_compose_up_has_no_service_args(up_lines[0])
    elif case == "empty_rollback":
        assert result.returncode != 0, out
        assert len(up_lines) == 2
        for line in up_lines:
            _assert_compose_up_has_no_service_args(line)
    elif case == "valid_forward":
        assert result.returncode == 0, out
        assert len(up_lines) == 1
        _assert_compose_up_has_no_service_args(up_lines[0])
    elif case == "valid_rollback":
        assert result.returncode != 0, out
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


@pytest.mark.parametrize(
    "trigger",
    [
        "probe_failure",
        "promote_failure",
        "pending_signal",
    ],
)
def test_oneshot_services_release_rollback_trigger_excludes_migrate(tmp_path, trigger):
    env, log = _oneshot_base(tmp_path, oneshot_services="migrate")
    assert run(env).returncode == 0

    env["D3_RELEASE_TAG"] = "def567890123"
    log.write_text("")

    if trigger == "probe_failure":
        mock_curl(Path(env["CURL_BIN"]), "500")
        result = run(env)
    elif trigger == "promote_failure":
        mock_curl(Path(env["CURL_BIN"]), "200")
        fail_mv_target(Path(env["DOCKER_BIN"]).parent / "mv", "last_good_release")
        env["PATH"] = f'{Path(env["DOCKER_BIN"]).parent}:{os.environ["PATH"]}'
        result = run(env)
    else:
        marker = tmp_path / "probe.started"
        mock_curl_sequence(Path(env["CURL_BIN"]), ["500"], pause_marker=marker)
        result = run_with_signal_on_marker(env, marker)

    assert result.returncode != 0, result.stdout + result.stderr
    up_lines = _compose_up_lines(log.read_text())
    assert len(up_lines) >= 2, up_lines
    _assert_compose_up_services(up_lines[-1], include=("app",), exclude=("migrate",))


def test_oneshot_services_image_set_guard_still_blocks_before_rollback(tmp_path):
    env, log = _oneshot_base(tmp_path, oneshot_services="migrate")
    assert run(env).returncode == 0
    env["D3_RELEASE_TAG"] = "def567890123"
    changed_manifest = tmp_path / "release2.manifest"
    changed_manifest.write_text(
        "D3_RELEASE_MANIFEST=1\n"
        "image\tfrontend\tfrontend\n"
        "image\tbackend2\tbackend2\n"
        "probe\thttp://localhost/frontend\t200\n"
        "probe\thttp://localhost/api/health\t200\n"
    )
    env["RELEASE_MANIFEST"] = str(changed_manifest)
    log.write_text("")
    write_exec(
        Path(env["DOCKER_BIN"]),
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --services "* ]]; then
  printf 'app\\nmigrate\\n'
  exit 0
fi
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend2:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  exit 0
fi
exit 0
''',
    )
    mock_curl(Path(env["CURL_BIN"]), "500")

    result = run(env)
    out = result.stdout + result.stderr

    assert "image set changed" in out
    up_lines = _compose_up_lines(log.read_text())
    assert len(up_lines) == 1
    _assert_compose_up_has_no_service_args(up_lines[0])


def test_readarray_does_not_swallow_compose_list_services_exit_status():
    """Contract: both callers capture compose_list_services via command
    substitution + `if !` + here-string readarray, matching the workflow
    pattern in build-deploy-release.yml. Process substitution would make
    `|| return 1` after readarray a no-op (issue #29)."""
    text = SCRIPT.read_text()
    assert 'readarray -t all_services <<< "$all_services_output"' in text
    assert text.count('readarray -t all_services <<< "$all_services_output"') == 2
    assert "readarray -t all_services < <(" not in text, (
        "readarray must not swallow compose_list_services failures via process substitution"
    )
    assert 'if ! all_services_output="$(compose_list_services "$tag")"; then' in text
    assert text.count('if ! all_services_output="$(compose_list_services "$tag")"; then') == 2


def _mock_identity_ok_compose_services_fail(path: Path, log: Path, *, services_fail_from=1):
    """Identity gate (config --images) succeeds; config --services fails from
    the Nth call onward (1-based)."""
    count_file = log.parent / "compose-services.count"
    write_exec(
        path,
        f'''#!/bin/bash
echo "$@" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" config --services "* ]]; then
  n=$(cat "{count_file}" 2>/dev/null || echo 0)
  n=$((n + 1))
  echo "$n" > "{count_file}"
  if [ "$n" -ge {services_fail_from} ]; then
    echo "compose: invalid compose file" >&2
    exit 1
  fi
  printf 'app\\nmigrate\\n'
  exit 0
fi
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  exit 0
fi
exit 0
''',
    )


def test_validate_oneshot_services_compose_config_failure_is_attributed(tmp_path):
    env, log = _oneshot_base(tmp_path, oneshot_services="migrate")
    _mock_identity_ok_compose_services_fail(
        Path(env["DOCKER_BIN"]), log, services_fail_from=1,
    )
    result = run(env)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "compose config --services failed" in out
    assert "oneshot_services references unknown" not in out
    assert not _compose_up_lines(log.read_text())


def test_rollback_compose_services_compose_config_failure_is_attributed(tmp_path):
    env, log = _oneshot_base(tmp_path, oneshot_services="migrate")
    assert run(env).returncode == 0, log.read_text() if log.exists() else "no docker log"

    env["D3_RELEASE_TAG"] = "def567890123"
    log.write_text("")
    (log.parent / "compose-services.count").unlink(missing_ok=True)
    _mock_identity_ok_compose_services_fail(
        Path(env["DOCKER_BIN"]), log, services_fail_from=2,
    )
    mock_curl(Path(env["CURL_BIN"]), "500")
    result = run(env)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "compose config --services failed" in out
    assert "nothing would remain" not in out.lower()
    assert "oneshot_services covers every compose service" not in out
