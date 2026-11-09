"""Release file transport, installation, read-back and rollback contracts."""

import json
import os
import stat
import subprocess
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


def mock_file_install_docker(path, log, mount_source, *, compose_file, inspect_fail=False):
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
    _compose_env_log = tmp_path / "compose-env.log"
    mock_file_install_docker(Path(env["DOCKER_BIN"]), log, str(live_conf.resolve()), compose_file=_compose_env_log)
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
    assert len([line for line in log.read_text().splitlines() if "up -d" in line]) == 4


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


def test_exit_cleanup_removes_only_exact_remote_files_tar(tmp_path):
    env, _ = base(tmp_path)
    nonce = f"{os.getpid()}-41-7"
    remote_script = Path("/tmp") / f"d3-release-{nonce}.sh"
    remote_manifest = Path("/tmp") / f"d3-release-{nonce}.manifest"
    remote_tar = Path("/tmp") / f"d3-release-{nonce}.files.tar"
    neighboring_tar = Path("/tmp") / f"d3-release-{os.getpid()}-41.files.tar"
    remote_script.write_text("temporary")
    remote_manifest.write_text(Path(env["RELEASE_MANIFEST"]).read_text())
    remote_tar.write_bytes(b"temporary archive")
    neighboring_tar.write_bytes(b"keep")
    env.update(
        RELEASE_MANIFEST=str(remote_manifest), RELEASE_TEMP_SCRIPT=str(remote_script),
        RELEASE_FILES_TAR=str(remote_tar), DEPLOY_DIR="",
    )

    result = run(env)

    assert result.returncode != 0
    assert not remote_script.exists()
    assert not remote_manifest.exists()
    assert not remote_tar.exists()
    assert neighboring_tar.read_bytes() == b"keep"
    neighboring_tar.unlink()
