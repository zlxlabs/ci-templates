"""Release file transport, installation, read-back and rollback contracts."""

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from test_release_deploy import (
    _reconcile_mock_bash,
    base,
    mock_curl,
    run,
    write_exec,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def normalize_file_package(tmp_path, sha, compose_bytes, conf_bytes):
    root = tmp_path / f"caller-{sha}"
    (root / "nginx").mkdir(parents=True)
    (root / "docker-compose.yml").write_bytes(compose_bytes)
    (root / "nginx" / "nginx.conf").write_bytes(conf_bytes)
    manifest_path = tmp_path / f"{sha}.manifest"
    tar_path = tmp_path / f"{sha}.files.tar"
    builds_path = tmp_path / f"{sha}.builds"
    result = subprocess.run(
        [
            "python3", str(REPO_ROOT / "scripts" / "normalize_release.py"),
            "--images-json", json.dumps([
                {"image_name": "frontend", "build_context": ".", "dockerfile": "Dockerfile"},
                {"image_name": "backend", "build_context": ".", "dockerfile": "Dockerfile"},
            ]),
            "--probes-json", json.dumps([
                {"url": "http://localhost/frontend", "expect_status": 200},
            ]),
            "--files-json", json.dumps(["docker-compose.yml", "nginx/nginx.conf"]),
            "--repo-root", str(root),
            "--files-tar-out", str(tar_path),
            "--manifest-out", str(manifest_path),
            "--builds-out", str(builds_path),
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return manifest_path, tar_path


def mock_file_install_docker(path, log, mount_source, *, compose_file, inspect_fail=False,
                             recreate_fail=False):
    inspect_branch = (
        'if [ "$1" = inspect ] && [[ "$*" == *"Config.Labels"* ]]; then exit 9; fi'
        if inspect_fail else
        '''if [ "$1" = inspect ] && [[ "$*" == *"Config.Labels"* ]]; then
  if [ "$2" = cid-mounted ]; then
    printf 'frontend\\nMOUNT_SOURCE\\n'
  else
    printf 'backend\\n'
  fi
  exit 0
fi'''.replace("MOUNT_SOURCE", mount_source)
    )
    write_exec(
        path,
        f'''#!/bin/bash
printf '%s\\n' "$*" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" --force-recreate "* ]] && {'true' if recreate_fail else 'false'}; then exit 7; fi
if [ "$1" = compose ] && [[ " $* " == *" ps -q "* ]] && [[ " $* " != *" --status "* ]]; then
  printf 'cid-mounted\\ncid-unbound\\n'; exit 0
fi
if [ "$1" = compose ]; then printf '%s\\n' "${{COMPOSE_FILE-unset}}" >> "{compose_file}"; fi
if [ "$1" = ps ] && [ "$2" = "-q" ]; then
  printf 'cid-mounted\\ncid-unbound\\n'; exit 0
fi
{inspect_branch}
{_reconcile_mock_bash()}
if [ "$1" = compose ] && [[ " $* " == *" config --images "* ]]; then
  printf 'frontend:%s\\nbackend:%s\\n' "$D3_RELEASE_TAG" "$D3_RELEASE_TAG"
  exit 0
fi
exit 0
''',
    )


def test_real_normalizer_tar_installs_and_verifies_files_and_recreates_only_bound_service(tmp_path):
    env, log = base(tmp_path)
    deploy = Path(env["DEPLOY_DIR"])
    (deploy / "nginx").mkdir()
    live_compose = deploy / "docker-compose.yml"
    live_conf = deploy / "nginx" / "nginx.conf"
    live_compose.write_bytes(b"old compose\n")
    live_conf.write_bytes(b"old conf\n")
    override = deploy / "docker-compose.override.yml"
    override.write_bytes(b"services: {} # host-owned\n")
    override_before = override.read_bytes()
    manifest_path, tar_path = normalize_file_package(
        tmp_path, "abc123456789",
        b"services:\n  frontend: {}\n  backend: {}\n",
        b"new nginx config\n",
    )
    env.update(
        RELEASE_MANIFEST=str(manifest_path), RELEASE_FILES_TAR=str(tar_path),
        COMPOSE_FILE=str(tmp_path / "must-not-be-used.yml"),
    )
    compose_env_log = tmp_path / "compose-env.log"
    mock_file_install_docker(
        Path(env["DOCKER_BIN"]), log, str(live_conf.resolve()), compose_file=compose_env_log,
    )

    result = run(env)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert live_compose.read_bytes() == b"services:\n  frontend: {}\n  backend: {}\n"
    assert live_conf.read_bytes() == b"new nginx config\n"
    for relpath in ("docker-compose.yml", "nginx/nginx.conf"):
        ok_lines = [
            line for line in out.splitlines()
            if line.startswith(f"[release][config-verify] path={relpath} sha256=")
            and line.endswith("result=ok")
        ]
        assert len(ok_lines) == 3, (relpath, ok_lines, out)
    assert "[release][config-install] path=nginx/nginx.conf result=replaced" in out
    assert "[release][config-override] path=docker-compose.override.yml result=detected-untouched" in out
    assert override.read_bytes() == override_before
    assert set(compose_env_log.read_text().splitlines()) == {"unset"}
    ups = [line for line in log.read_text().splitlines() if " up -d" in line]
    assert len(ups) == 2
    forced = [line for line in ups if "--force-recreate" in line]
    assert len(forced) == 1
    assert "--force-recreate frontend" in forced[0]
    assert "backend" not in forced[0].split("--force-recreate", 1)[1]


def test_postprobe_live_mismatch_emits_error_and_restores_previous_file_tar(tmp_path):
    env, log = base(tmp_path)
    deploy = Path(env["DEPLOY_DIR"])
    (deploy / "nginx").mkdir()
    live_compose = deploy / "docker-compose.yml"
    live_conf = deploy / "nginx" / "nginx.conf"
    live_compose.write_bytes(b"old host compose\n")
    live_conf.write_bytes(b"old host conf\n")
    first_manifest, first_tar = normalize_file_package(
        tmp_path, "abc123456789",
        b"services:\n  frontend: {}\n  backend: {}\n",
        b"known good conf\n",
    )
    env.update(RELEASE_MANIFEST=str(first_manifest), RELEASE_FILES_TAR=str(first_tar))
    mock_file_install_docker(
        Path(env["DOCKER_BIN"]), log, str(live_conf.resolve()),
        compose_file=tmp_path / "compose-env.log",
    )
    first = run(env)
    assert first.returncode == 0, first.stdout + first.stderr

    next_manifest, next_tar = normalize_file_package(
        tmp_path, "def567890123",
        b"services:\n  frontend: {}\n  backend: {}\n",
        b"new release conf\n",
    )
    env.update(D3_RELEASE_TAG="def567890123", RELEASE_MANIFEST=str(next_manifest), RELEASE_FILES_TAR=str(next_tar))
    corrupt_once = tmp_path / "corrupt-once"
    write_exec(
        Path(env["CURL_BIN"]),
        f'''#!/bin/bash
if [ ! -e "{corrupt_once}" ]; then printf 'corrupted live\\n' > "{live_conf}"; touch "{corrupt_once}"; fi
printf '200'
''',
    )
    log.write_text("")
    result = run(env)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "[release][config-verify] path=nginx/nginx.conf expected=" in out
    assert " result=mismatch" in out
    assert "::error::release config verify mismatch" in out
    assert "[release][config-rollback] sha=abc123456789 result=restored" in out
    assert live_compose.read_bytes() == b"services:\n  frontend: {}\n  backend: {}\n"
    assert live_conf.read_bytes() == b"known good conf\n"
    deploy_commands = log.read_text().splitlines()
    assert len([line for line in deploy_commands if "up -d" in line]) == 4
    forced = [line for line in deploy_commands if "--force-recreate" in line]
    assert len(forced) == 2
    assert all("--force-recreate frontend" in line for line in forced)


def test_same_sha_live_mismatch_forbids_skip_forward(tmp_path):
    env, log = base(tmp_path)
    deploy = Path(env["DEPLOY_DIR"])
    (deploy / "nginx").mkdir()
    live_conf = deploy / "nginx" / "nginx.conf"
    live_conf.write_bytes(b"old conf\n")
    manifest_path, tar_path = normalize_file_package(
        tmp_path, "abc123456789",
        b"services:\n  frontend: {}\n  backend: {}\n",
        b"expected conf\n",
    )
    env.update(RELEASE_MANIFEST=str(manifest_path), RELEASE_FILES_TAR=str(tar_path))
    mock_file_install_docker(
        Path(env["DOCKER_BIN"]), log, str(live_conf.resolve()), compose_file=tmp_path / "compose-env.log",
    )
    assert run(env).returncode == 0
    live_conf.write_bytes(b"drifted conf\n")
    log.write_text("")

    result = run(env)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "skip forward deploy" not in out
    assert "result=mismatch" in out
    assert live_conf.read_bytes() == b"expected conf\n"
    assert any(
        "--force-recreate frontend" in line
        for line in log.read_text().splitlines() if " up -d" in line
    )


def test_legacy_manifest_without_file_records_leaves_compose_and_dotenv_bytes_unchanged(tmp_path):
    env, _ = base(tmp_path)
    deploy = Path(env["DEPLOY_DIR"])
    compose = deploy / "compose.yml"
    dotenv = deploy / ".env"
    compose.write_bytes(b"legacy compose bytes\n")
    dotenv.write_bytes(b"SECRET=keep\n")
    compose_before, dotenv_before = compose.read_bytes(), dotenv.read_bytes()

    result = run(env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert compose.read_bytes() == compose_before
    assert dotenv.read_bytes() == dotenv_before
    assert "[release][config-verify]" not in result.stdout + result.stderr


def test_legacy_last_good_without_file_records_skips_config_rollback_and_preserves_dotenv(tmp_path):
    env, _ = base(tmp_path)
    assert run(env).returncode == 0
    dotenv = Path(env["DEPLOY_DIR"]) / ".env"
    dotenv.write_bytes(b"SECRET=host-owned\n")
    env["D3_RELEASE_TAG"] = "def567890123"
    mock_curl(Path(env["CURL_BIN"]), "500")

    result = run(env)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "[release][config-rollback] sha=abc123456789 result=skipped-no-file-records" in out
    assert dotenv.read_bytes() == b"SECRET=host-owned\n"


def test_install_refuses_symlink_and_does_not_follow_target(tmp_path):
    env, _ = base(tmp_path)
    deploy = Path(env["DEPLOY_DIR"])
    (deploy / "nginx").mkdir()
    outside = tmp_path / "outside.conf"
    outside.write_bytes(b"must remain untouched\n")
    link = deploy / "nginx" / "nginx.conf"
    link.symlink_to(outside)
    manifest_path, tar_path = normalize_file_package(
        tmp_path, "abc123456789", b"services: {}\n", b"release config\n",
    )
    env.update(RELEASE_MANIFEST=str(manifest_path), RELEASE_FILES_TAR=str(tar_path))

    result = run(env)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "[release][config-install] path=nginx/nginx.conf result=symlink-refused" in out
    assert link.is_symlink()
    assert outside.read_bytes() == b"must remain untouched\n"


def test_install_fails_loud_on_read_only_target_directory_without_chmod(tmp_path):
    env, _ = base(tmp_path)
    manifest_path, tar_path = normalize_file_package(
        tmp_path, "abc123456789", b"services: {}\n", b"release config\n",
    )
    env.update(
        RELEASE_MANIFEST=str(manifest_path), RELEASE_FILES_TAR=str(tar_path),
        DEPLOY_DIR="/sys",
    )

    result = run(env)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "[release][config-install] path=docker-compose.yml result=not-writable" in out
    assert "compose up" not in out


@pytest.mark.parametrize(
    "denied_path",
    [
        ".env", "ops.env", ".d3-release.env", "secrets/runtime.env",
        "docker-compose.override.yml", "docker-compose.override.yaml",
        "compose.override.yml", "compose.override.yaml",
        ".deploy-state/cache/docker-compose.yml", ".git/docker-compose.yml",
        "../docker-compose.yml", "nginx//nginx.conf", "nginx/", "-compose.yml",
    ],
)
def test_host_manifest_rejects_unsafe_or_denylisted_paths(tmp_path, denied_path):
    env, log = base(tmp_path)
    manifest_path = tmp_path / "bad-file.manifest"
    manifest_path.write_text("\n".join([
        "D3_RELEASE_MANIFEST=1",
        "image\tfrontend\tfrontend",
        "image\tbackend\tbackend",
        "probe\thttp://localhost/health\t200",
        "file\t" + denied_path + "\t" + "a" * 64,
    ]) + "\n")
    env["RELEASE_MANIFEST"] = str(manifest_path)

    result = run(env)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "unsafe or forbidden file path in manifest" in out
    assert not log.exists() or "pull " not in log.read_text()


def test_rollback_requires_previous_cached_tar_and_returns_rc4(tmp_path):
    env, log = base(tmp_path)
    deploy = Path(env["DEPLOY_DIR"])
    (deploy / "nginx").mkdir()
    live_conf = deploy / "nginx" / "nginx.conf"
    live_conf.write_bytes(b"initial host conf\n")
    first_manifest, first_tar = normalize_file_package(
        tmp_path, "abc123456789",
        b"services:\n  frontend: {}\n  backend: {}\n",
        b"known good conf\n",
    )
    env.update(RELEASE_MANIFEST=str(first_manifest), RELEASE_FILES_TAR=str(first_tar))
    mock_file_install_docker(
        Path(env["DOCKER_BIN"]), log, str(live_conf.resolve()), compose_file=tmp_path / "compose-env.log",
    )
    first = run(env)
    assert first.returncode == 0, first.stdout + first.stderr
    (Path(env["STATE_DIR"]) / "files" / "abc123456789.tar").unlink()

    next_manifest, next_tar = normalize_file_package(
        tmp_path, "def567890123",
        b"services:\n  frontend: {}\n  backend: {}\n",
        b"new release conf\n",
    )
    env.update(D3_RELEASE_TAG="def567890123", RELEASE_MANIFEST=str(next_manifest), RELEASE_FILES_TAR=str(next_tar))
    mock_curl(Path(env["CURL_BIN"]), "500")
    log.write_text("")

    result = run(env)
    out = result.stdout + result.stderr
    assert result.returncode == 4, out
    assert "[release][config-rollback] sha=abc123456789 result=missing-tar" in out
    assert len([line for line in log.read_text().splitlines() if "up -d" in line]) == 2


def test_missing_previous_tar_returns_rc4_even_when_current_staging_fails_before_live_mutation(tmp_path):
    env, log = base(tmp_path)
    deploy = Path(env["DEPLOY_DIR"])
    (deploy / "nginx").mkdir()
    live_conf = deploy / "nginx" / "nginx.conf"
    first_manifest, first_tar = normalize_file_package(
        tmp_path, "abc123456789",
        b"services:\n  frontend: {}\n  backend: {}\n",
        b"known good conf\n",
    )
    env.update(RELEASE_MANIFEST=str(first_manifest), RELEASE_FILES_TAR=str(first_tar))
    mock_file_install_docker(
        Path(env["DOCKER_BIN"]), log, str(live_conf.resolve()),
        compose_file=tmp_path / "compose-env.log",
    )
    first = run(env)
    assert first.returncode == 0, first.stdout + first.stderr
    good_bytes = live_conf.read_bytes()
    (Path(env["STATE_DIR"]) / "files" / "abc123456789.tar").unlink()

    next_manifest, next_tar = normalize_file_package(
        tmp_path, "def567890123",
        b"services:\n  frontend: {}\n  backend: {}\n",
        b"unreachable staging payload\n",
    )
    next_tar.write_bytes(b"not a tar archive")
    env.update(D3_RELEASE_TAG="def567890123", RELEASE_MANIFEST=str(next_manifest), RELEASE_FILES_TAR=str(next_tar))
    log.write_text("")

    result = run(env)
    out = result.stdout + result.stderr

    assert result.returncode == 4, out
    assert "release file staging failed before live mutation" in out
    assert "[release][config-rollback] sha=abc123456789 result=missing-tar" in out
    assert live_conf.read_bytes() == good_bytes
    assert not any("up -d" in line for line in log.read_text().splitlines())


def test_bind_mount_inspect_failure_is_not_treated_as_no_match(tmp_path):
    env, log = base(tmp_path)
    deploy = Path(env["DEPLOY_DIR"])
    (deploy / "nginx").mkdir()
    live_conf = deploy / "nginx" / "nginx.conf"
    manifest_path, tar_path = normalize_file_package(
        tmp_path, "abc123456789", b"services: {}\n", b"release config\n",
    )
    env.update(RELEASE_MANIFEST=str(manifest_path), RELEASE_FILES_TAR=str(tar_path))
    mock_file_install_docker(
        Path(env["DOCKER_BIN"]), log, str(live_conf.resolve()),
        compose_file=tmp_path / "compose-env.log", inspect_fail=True,
    )

    result = run(env)
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "docker inspect failed during bind-mount detection" in out
    assert not (Path(env["STATE_DIR"]) / "last_good_release").exists()


def test_compose_project_list_failure_is_not_treated_as_no_bind(tmp_path):
    docker = tmp_path / "docker"
    log = tmp_path / "docker.log"
    write_exec(
        docker,
        f'''#!/bin/bash
printf '%s\\n' "$*" >> "{log}"
if [ "$1" = compose ] && [[ " $* " == *" ps -q "* ]]; then exit 7; fi
if [ "$1" = ps ] && [ "$2" = "-q" ]; then exit 0; fi
exit 0
''',
    )
    script = '''source "$1"
log() { printf '[release] %s\\n' "$*" >&2; }
release_files_init
DEPLOY_DIR="$2"
DOCKER_BIN="$3"
REPLACED_FILE_PATHS=(managed.conf)
find_bound_services
'''
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(REPO_ROOT / "scripts" / "release_files.sh"),
         str(tmp_path), str(docker)],
        capture_output=True, text=True,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "could not list running containers for bind-mount detection" in result.stderr
    assert "compose ps -q" in log.read_text()


def test_unresolvable_bind_source_fails_with_path_before_compose_up(tmp_path):
    env, log = base(tmp_path)
    deploy = Path(env["DEPLOY_DIR"])
    (deploy / "nginx").mkdir()
    missing_source = str(tmp_path / "missing-bind-source.conf")
    manifest_path, tar_path = normalize_file_package(
        tmp_path, "abc123456789", b"services: {}\n", b"release config\n",
    )
    env.update(RELEASE_MANIFEST=str(manifest_path), RELEASE_FILES_TAR=str(tar_path))
    mock_file_install_docker(
        Path(env["DOCKER_BIN"]), log, missing_source,
        compose_file=tmp_path / "compose-env.log",
    )

    result = run(env)
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert f"could not resolve inspected bind source: {missing_source}" in out
    assert "compose ps -q" in log.read_text()
    assert not any(" up -d" in line for line in log.read_text().splitlines())
    assert not (Path(env["STATE_DIR"]) / "last_good_release").exists()


def test_force_recreate_failure_has_release_config_recreate_marker(tmp_path):
    env, log = base(tmp_path)
    deploy = Path(env["DEPLOY_DIR"])
    (deploy / "nginx").mkdir()
    live_conf = deploy / "nginx" / "nginx.conf"
    manifest_path, tar_path = normalize_file_package(
        tmp_path, "abc123456789",
        b"services:\n  frontend: {}\n  backend: {}\n",
        b"release config\n",
    )
    env.update(RELEASE_MANIFEST=str(manifest_path), RELEASE_FILES_TAR=str(tar_path))
    mock_file_install_docker(
        Path(env["DOCKER_BIN"]), log, str(live_conf.resolve()),
        compose_file=tmp_path / "compose-env.log", recreate_fail=True,
    )

    result = run(env)
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert "release-config-recreate" in out
    assert "services frontend" in out
    assert not (Path(env["STATE_DIR"]) / "last_good_release").exists()


def test_helper_source_has_no_load_time_actions(tmp_path):
    helper = REPO_ROOT / "scripts" / "release_files.sh"
    before = set(tmp_path.iterdir())
    result = subprocess.run(
        ["bash", "-c", 'source "$1" && declare -F release_files_init >/dev/null && printf loaded', "bash", str(helper)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "loaded"
    assert set(tmp_path.iterdir()) == before


def test_release_files_library_is_required_even_for_empty_file_manifest(tmp_path):
    env, log = base(tmp_path)
    env.pop("RELEASE_FILES_LIB")

    result = run(env)
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert "RELEASE_FILES_LIB required" in out
    assert not log.exists() or "compose" not in log.read_text()


def test_exit_cleanup_removes_only_exact_remote_release_files(tmp_path):
    env, _ = base(tmp_path)
    nonce = f"{os.getpid()}-41-7"
    remote_script = Path("/tmp") / f"d3-release-{nonce}.sh"
    remote_lib = Path("/tmp") / f"d3-release-{nonce}.files.sh"
    remote_manifest = Path("/tmp") / f"d3-release-{nonce}.manifest"
    remote_tar = Path("/tmp") / f"d3-release-{nonce}.files.tar"
    neighboring_lib = Path("/tmp") / f"d3-release-{os.getpid()}-41.files.sh"
    remote_script.write_text("temporary")
    remote_lib.write_text("temporary library")
    remote_manifest.write_text(Path(env["RELEASE_MANIFEST"]).read_text())
    remote_tar.write_bytes(b"temporary archive")
    neighboring_lib.write_text("keep")
    env.update(
        RELEASE_MANIFEST=str(remote_manifest), RELEASE_TEMP_SCRIPT=str(remote_script),
        RELEASE_FILES_LIB=str(remote_lib), RELEASE_FILES_TAR=str(remote_tar), DEPLOY_DIR="",
    )

    result = run(env)

    assert result.returncode != 0
    assert not remote_script.exists()
    assert not remote_lib.exists()
    assert not remote_manifest.exists()
    assert not remote_tar.exists()
    assert neighboring_lib.read_text() == "keep"
    neighboring_lib.unlink()


def test_compose_project_bind_detection_uses_real_docker_format_output(tmp_path):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker CLI unavailable; real --format rendering is exercised on ubuntu-latest")

    deploy = tmp_path / "deploy"
    config = deploy / "nginx" / "nginx.conf"
    config.parent.mkdir(parents=True)
    config.write_text("managed config\n")
    other_bind = tmp_path / "other.conf"
    other_bind.write_text("unrelated bind\n")
    rendered_log = tmp_path / "rendered.bin"
    docker_log = tmp_path / "docker-argv.log"
    containers = {
        "project-frontend": {
            "service": "frontend",
            "mounts": [{"Type": "bind", "Source": str(config)}],
        },
        "project-backend": {
            "service": "backend",
            "mounts": [
                {"Type": "volume", "Source": "/unreachable/named-volume"},
                {"Type": "bind", "Source": str(other_bind)},
            ],
        },
        "project-migrate": {
            "service": "migrate",
            "mounts": [{"Type": "bind", "Source": str(config)}],
        },
        "outside-container": {
            "service": "outside",
            "mounts": [{"Type": "bind", "Source": str(config)}],
        },
    }

    class DockerAPI(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/_ping":
                body = b"OK"
            else:
                container_id = self.path.split("/containers/", 1)[1].removesuffix("/json")
                container = containers[container_id]
                body = json.dumps({
                    "Id": container_id,
                    "Config": {"Labels": {
                        "com.docker.compose.service": container["service"],
                    }},
                    "Mounts": container["mounts"],
                }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), DockerAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    wrapper = tmp_path / "docker-wrapper"
    write_exec(
        wrapper,
        f'''#!/bin/bash
printf '%s\\n' "$*" >> "{docker_log}"
if [[ "$1" == "compose" && " $* " == *" ps -q "* ]]; then
  printf 'project-frontend\\nproject-backend\\nproject-migrate\\n'
  exit 0
fi
if [[ "$1 $2" == "ps -q" ]]; then
  printf 'project-frontend\\nproject-backend\\nproject-migrate\\noutside-container\\n'
  exit 0
fi
output="$("{docker}" "$@")" || exit $?
printf '%s\\n\\0' "$output" >> "{rendered_log}"
printf '%s\\n' "$output"
''',
    )
    env = dict(os.environ)
    env.update(
        DOCKER_BIN=str(wrapper),
        DOCKER_HOST=f"tcp://127.0.0.1:{server.server_port}",
    )
    script = '''source "$1"
log() { printf '[release] %s\\n' "$*" >&2; }
release_files_init
DEPLOY_DIR="$2"
DOCKER_BIN="$3"
ONESHOT_SERVICES=migrate
REPLACED_FILE_PATHS=(nginx/nginx.conf)
find_bound_services || exit $?
printf 'SERVICES=%s\\n' "${FORCE_RECREATE_SERVICES[*]}"
'''
    try:
        result = subprocess.run(
            ["bash", "-c", script, "bash", str(REPO_ROOT / "scripts" / "release_files.sh"),
             str(deploy), str(wrapper), str(config)],
            env=env, capture_output=True, text=True,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "SERVICES=frontend\n"
    assert "compose ps -q" in docker_log.read_text()
    assert "SERVICES=outside" not in result.stdout
    expected_rendered = (
        f"frontend\n{config}\n\0"
        f"backend\n{other_bind}\n\0"
        f"migrate\n{config}\n\0"
    ).encode()
    assert rendered_log.read_bytes() == expected_rendered
    assert b"\\n" not in rendered_log.read_bytes()


def test_same_sha_with_managed_config_runs_full_apply_again(tmp_path):
    env, log = base(tmp_path)
    deploy = Path(env["DEPLOY_DIR"])
    (deploy / "nginx").mkdir()
    live_conf = deploy / "nginx" / "nginx.conf"
    live_conf.write_bytes(b"initial conf\n")
    manifest_path, tar_path = normalize_file_package(
        tmp_path, "abc123456789",
        b"services:\n  frontend: {}\n  backend: {}\n",
        b"expected conf\n",
    )
    env.update(RELEASE_MANIFEST=str(manifest_path), RELEASE_FILES_TAR=str(tar_path))
    mock_file_install_docker(
        Path(env["DOCKER_BIN"]), log, str(live_conf.resolve()),
        compose_file=tmp_path / "compose-env.log",
    )

    first = run(env)
    assert first.returncode == 0, first.stdout + first.stderr
    log.write_text("")
    second = run(env)
    out = second.stdout + second.stderr

    assert second.returncode == 0, out
    assert "skip forward deploy" not in out
    assert len([line for line in log.read_text().splitlines() if " up -d" in line]) == 2
    forced = [line for line in log.read_text().splitlines() if "--force-recreate" in line]
    assert len(forced) == 1
    assert "--force-recreate frontend" in forced[0]
