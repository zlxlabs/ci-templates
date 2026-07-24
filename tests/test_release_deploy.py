"""TDD tests for atomic multi-image release deployment on the SSH host."""

import os
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


def run(env):
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)


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
    assert "check_pending" in between or "PENDING_SIGNAL" in between, (
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
