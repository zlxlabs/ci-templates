"""Real-Docker regression tests for migration-safe rollback in both deploy lanes."""

import os
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PULL_SCRIPT = REPO_ROOT / "scripts" / "pull_and_deploy.sh"
RELEASE_SCRIPT = REPO_ROOT / "scripts" / "release_deploy.sh"
OLD_SHA = "abc123456789"
NEW_SHA = "def567890123"


def _docker_or_skip():
    docker = shutil.which("docker")
    available = bool(docker)
    if available:
        available = subprocess.run(
            [docker, "compose", "version"], capture_output=True, text=True
        ).returncode == 0 and subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
        ).returncode == 0
    if not available:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("GitHub Actions must provide a working Docker daemon and Compose")
        pytest.skip("requires a working local Docker daemon and Docker Compose")
    return docker


def _run(command, *, env=None, timeout=120):
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout)


def _write_executable(path: Path, content: str):
    path.write_text(content)
    path.chmod(0o755)


def _prepare_project(tmp_path: Path, docker: str, *, release: bool):
    project = f"ci-rollback-{uuid.uuid4().hex[:16]}"
    image_name = project
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    (deploy_dir / "state").mkdir()
    compose_name = "compose.yml" if release else "docker-compose.yml"
    if release:
        compose_text = f'''services:
  app:
    image: {image_name}:${{D3_RELEASE_TAG:?D3_RELEASE_TAG is required}}
    command: ["sh", "-c", "sleep 3600"]
    labels:
      rollback-test.release: ${{D3_RELEASE_TAG}}
    depends_on:
      migrate:
        condition: service_completed_successfully
  migrate:
    image: {image_name}:${{D3_RELEASE_TAG:?D3_RELEASE_TAG is required}}
    command: ["sh", "-c", 'echo run >> /state/migrate-count; if [ -n "$FAIL_BUILD_REV" ] && [ "$BUILD_REV" = "$FAIL_BUILD_REV" ]; then exit 42; fi; exit 0']
    environment:
      FAIL_BUILD_REV: ${{FAIL_BUILD_REV:-}}
    volumes:
      - migration-data:/state
volumes:
  migration-data:
'''
    else:
        compose_text = f'''services:
  app:
    image: {image_name}:latest
    command: ["sh", "-c", "sleep 3600"]
    labels:
      rollback-test.release: ${{GIT_SHA}}
    depends_on:
      migrate:
        condition: service_completed_successfully
  migrate:
    image: {image_name}:latest
    command: ["sh", "-c", 'echo run >> /state/migrate-count; if [ -n "$FAIL_BUILD_REV" ] && [ "$BUILD_REV" = "$FAIL_BUILD_REV" ]; then exit 42; fi; exit 0']
    environment:
      FAIL_BUILD_REV: ${{FAIL_BUILD_REV:-}}
    volumes:
      - migration-data:/state
volumes:
  migration-data:
'''
    (deploy_dir / compose_name).write_text(compose_text)

    build_dir = tmp_path / "image"
    build_dir.mkdir()
    (build_dir / "Dockerfile").write_text(
        "FROM alpine:latest\nARG BUILD_REV\nENV BUILD_REV=${BUILD_REV}\n"
    )
    local_repo = f"{project}/image"
    remote_repo = f"registry.example.test/{project}/{image_name}"
    for sha, rev in ((OLD_SHA, "old"), (NEW_SHA, "new")):
        local_ref = f"{local_repo}:{sha}"
        result = _run(
            [docker, "build", "--build-arg", f"BUILD_REV={rev}", "-t", local_ref, str(build_dir)]
        )
        assert result.returncode == 0, result.stdout + result.stderr
        result = _run([docker, "tag", local_ref, f"{remote_repo}:{sha}"])
        assert result.returncode == 0, result.stdout + result.stderr
        if not release:
            result = _run([docker, "tag", local_ref, f"{image_name}:{sha}"])
            assert result.returncode == 0, result.stdout + result.stderr

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_wrapper = bin_dir / "docker-local"
    _write_executable(
        docker_wrapper,
        "#!/bin/bash\n"
        "if [ \"$1\" = pull ]; then\n"
        "  docker image inspect \"$2\" >/dev/null 2>&1\n"
        "  exit $?\n"
        "fi\n"
        f"exec {shlex.quote(docker)} \"$@\"\n",
    )
    env = dict(os.environ)
    env.update(
        DEPLOY_DIR=str(deploy_dir),
        STATE_DIR=str(tmp_path / "state"),
        HOST_LOCK=str(tmp_path / "host.lock"),
        DOCKER_BIN=str(docker_wrapper),
        CURL_BIN=str(bin_dir / "curl"),
        COMPOSE_PROJECT_NAME=project,
        ONESHOT_SERVICES="migrate",
        FAIL_BUILD_REV="",
        HEALTHCHECK_WARMUP="0",
        HEALTHCHECK_INTERVAL="0",
        HEALTHCHECK_RETRIES="1",
        HEALTHCHECK_TIMEOUT="1",
        PULL_RETRIES="1",
        PULL_RETRY_DELAY="0",
    )
    return docker, project, image_name, remote_repo, deploy_dir, env


def _curl(path: Path, status: str):
    _write_executable(path, f"#!/bin/bash\nprintf '%s' '{status}'\n")


def _snapshot_before_rollback(
    wrapper: Path, docker: str, project: str, snapshot: Path
):
    count_file = snapshot.with_suffix(".compose-up-count")
    _write_executable(
        wrapper,
        f'''#!/bin/bash
real_docker={shlex.quote(docker)}
if [ "$1" = compose ]; then
  is_up=0
  for arg in "$@"; do [ "$arg" = up ] && is_up=1; done
  if [ "$is_up" -eq 1 ]; then
    n=$(cat {shlex.quote(str(count_file))} 2>/dev/null || echo 0)
    n=$((n + 1)); echo "$n" > {shlex.quote(str(count_file))}
    if [ "$n" -eq 2 ]; then
      cid=$("$real_docker" compose --project-name {shlex.quote(project)} ps -a -q migrate)
      "$real_docker" inspect --format '{{{{.Id}}}} {{{{.State.StartedAt}}}} {{{{.State.ExitCode}}}}' "$cid" > {shlex.quote(str(snapshot) + '.container')}
      "$real_docker" run --rm --volumes-from "$cid" alpine:latest cat /state/migrate-count > {shlex.quote(str(snapshot) + '.count')}
    fi
  fi
fi
if [ "$1" = pull ]; then
  "$real_docker" image inspect "$2" >/dev/null 2>&1
  exit $?
fi
exec "$real_docker" "$@"
''',
    )


def _curl_fail_forward_and_snapshot_migration(
    path: Path, snapshot: Path, deploy_dir: Path, statuses: tuple[str, ...]
):
    count_file = path.with_suffix(".count")
    status_words = " ".join(statuses)
    _write_executable(
        path,
        f'''#!/bin/bash
count_file={shlex.quote(str(count_file))}
n=$(cat "$count_file" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$count_file"
if [ "$n" -eq 1 ]; then
  cid=$(docker compose ps -a -q migrate)
  docker inspect --format '{{{{.Id}}}} {{{{.State.StartedAt}}}} {{{{.State.ExitCode}}}}' "$cid" > {shlex.quote(str(snapshot) + '.container')}
  docker run --rm --volumes-from "$cid" alpine:latest cat /state/migrate-count > {shlex.quote(str(snapshot) + '.count')}
fi
statuses=({status_words})
printf '%s' "${{statuses[$((n - 1))]}}"
''',
    )


def _migration_count(docker: str, project: str, deploy_dir: Path):
    cid = subprocess.run(
        [docker, "compose", "--project-name", project, "ps", "-a", "-q", "migrate"],
        cwd=deploy_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return subprocess.run(
        [docker, "run", "--rm", "--volumes-from", cid, "alpine:latest", "cat", "/state/migrate-count"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _assert_migration_unchanged(
    snapshot: Path, deploy_dir: Path, project: str, *, expected_exit_code: int
):
    expected_container = Path(str(snapshot) + ".container").read_text().strip()
    expected_count = Path(str(snapshot) + ".count").read_text().strip()
    assert len(expected_container.split()) == 3, expected_container
    assert expected_container.split()[2] == str(expected_exit_code)
    assert expected_count.splitlines() == ["run", "run"], "both forward releases must have run migrate"
    docker, = (shutil.which("docker"),)
    cid = subprocess.run(
        [docker, "compose", "--project-name", project, "ps", "-a", "-q", "migrate"],
        cwd=deploy_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    actual_container = subprocess.run(
        [docker, "inspect", "--format", "{{.Id}} {{.State.StartedAt}} {{.State.ExitCode}}", cid],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    actual_count = subprocess.run(
        [docker, "run", "--rm", "--volumes-from", cid, "alpine:latest", "cat", "/state/migrate-count"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert actual_count == expected_count, "rollback must not execute migrate again"
    assert actual_container == expected_container, "rollback must leave migrate container ID and StartedAt unchanged"


def _cleanup_project(docker: str, project: str, deploy_dir: Path):
    compose_file = deploy_dir / ("compose.yml" if (deploy_dir / "compose.yml").exists() else "docker-compose.yml")
    result = _run(
        [docker, "compose", "--project-name", project, "-f", str(compose_file), "down", "--volumes", "--remove-orphans"],
        env={**os.environ, "COMPOSE_PROJECT_NAME": project},
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("failure_mode", ["probe_failure", "migration_failure"])
def test_pull_and_deploy_rollback_does_not_rerun_depends_on_migration(tmp_path, failure_mode):
    docker = _docker_or_skip()
    docker, project, image_name, remote_repo, deploy_dir, env = _prepare_project(
        tmp_path, docker, release=False
    )
    env.update(
        IMAGE_NAME=image_name,
        ACR_IMAGE=remote_repo,
        GIT_SHA=OLD_SHA,
        HEALTHCHECK_URL="http://127.0.0.1/health",
    )
    try:
        _curl(Path(env["CURL_BIN"]), "200")
        first = _run(["bash", str(PULL_SCRIPT)], env=env)
        assert first.returncode == 0, first.stdout + first.stderr
        assert _migration_count(docker, project, deploy_dir) == "run"

        snapshot = tmp_path / "migration-before-rollback"
        env["GIT_SHA"] = NEW_SHA
        env["FAIL_BUILD_REV"] = "new" if failure_mode == "migration_failure" else ""
        if failure_mode == "probe_failure":
            _curl_fail_forward_and_snapshot_migration(
                Path(env["CURL_BIN"]), snapshot, deploy_dir, ("500", "200")
            )
            expected_exit_code = 0
        else:
            _snapshot_before_rollback(Path(env["DOCKER_BIN"]), docker, project, snapshot)
            _curl(Path(env["CURL_BIN"]), "200")
            expected_exit_code = 42
        rollback = _run(["bash", str(PULL_SCRIPT)], env=env)
        assert rollback.returncode == 1, rollback.stdout + rollback.stderr
        _assert_migration_unchanged(
            snapshot, deploy_dir, project, expected_exit_code=expected_exit_code
        )
    finally:
        _cleanup_project(docker, project, deploy_dir)


@pytest.mark.parametrize("failure_mode", ["probe_failure", "migration_failure"])
def test_release_deploy_rollback_does_not_rerun_depends_on_migration(tmp_path, failure_mode):
    docker = _docker_or_skip()
    docker, project, image_name, remote_repo, deploy_dir, env = _prepare_project(
        tmp_path, docker, release=True
    )
    env.update(
        RELEASE_MANIFEST=str(tmp_path / "release.manifest"),
        RELEASE_FILES_LIB=str(REPO_ROOT / "scripts" / "release_files.sh"),
        D3_RELEASE_TAG=OLD_SHA,
        ACR_REGISTRY=remote_repo.rsplit("/", 2)[0],
        ACR_NAMESPACE=remote_repo.split("/")[-2],
        HEALTHCHECK_URL="http://127.0.0.1/health",
    )
    manifest = Path(env["RELEASE_MANIFEST"])
    manifest.write_text(
        "D3_RELEASE_MANIFEST=1\n"
        f"image\t{image_name}\t{image_name}\n"
        "probe\thttp://127.0.0.1/health\t200\n"
    )
    try:
        _curl(Path(env["CURL_BIN"]), "200")
        first = _run(["bash", str(RELEASE_SCRIPT)], env=env)
        assert first.returncode == 0, first.stdout + first.stderr
        assert _migration_count(docker, project, deploy_dir) == "run"

        snapshot = tmp_path / "migration-before-rollback"
        env["D3_RELEASE_TAG"] = NEW_SHA
        env["FAIL_BUILD_REV"] = "new" if failure_mode == "migration_failure" else ""
        if failure_mode == "probe_failure":
            _curl_fail_forward_and_snapshot_migration(
                Path(env["CURL_BIN"]), snapshot, deploy_dir, ("500", "200")
            )
            expected_exit_code = 0
        else:
            _snapshot_before_rollback(Path(env["DOCKER_BIN"]), docker, project, snapshot)
            _curl(Path(env["CURL_BIN"]), "200")
            expected_exit_code = 42
        rollback = _run(["bash", str(RELEASE_SCRIPT)], env=env)
        assert rollback.returncode == 1, rollback.stdout + rollback.stderr
        _assert_migration_unchanged(
            snapshot, deploy_dir, project, expected_exit_code=expected_exit_code
        )
    finally:
        _cleanup_project(docker, project, deploy_dir)
