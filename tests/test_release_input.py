"""TDD contract for the multi-image release declaration normalizer."""
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "normalize_release.py"


def run_normalizer(tmp_path, images, probes=None):
    manifest = tmp_path / "release.manifest"
    builds = tmp_path / "builds.manifest"
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--images-json",
        json.dumps(images),
        "--manifest-out",
        str(manifest),
        "--builds-out",
        str(builds),
    ]
    if probes is None:
        probes = [{"url": "http://localhost/health", "expect_status": 200}]
    cmd += ["--probes-json", json.dumps(probes)]
    return subprocess.run(cmd, capture_output=True, text=True), manifest, builds


def valid_images():
    return [
        {"image_name": "frontend", "build_context": ".", "dockerfile": "Dockerfile"},
        {"image_name": "backend", "build_context": "services/api", "dockerfile": "Dockerfile"},
    ]


def test_normalizes_images_and_multiple_probes(tmp_path):
    probes = [
        {"url": "http://127.0.0.1:8080/", "expect_status": 200},
        {"url": "http://127.0.0.1:8000/healthz", "expect_status": 204},
    ]
    result, manifest, builds = run_normalizer(tmp_path, valid_images(), probes)
    assert result.returncode == 0, result.stderr
    assert "image\tfrontend\tfrontend" in manifest.read_text()
    assert "image\tbackend\tbackend" in manifest.read_text()
    assert "probe\thttp://127.0.0.1:8000/healthz\t204" in manifest.read_text()
    assert builds.read_text().count("build\t") == 2


def test_rejects_unknown_fields_and_duplicate_names(tmp_path):
    unknown = [{"image_name": "a", "build_context": ".", "dockerfile": "Dockerfile", "run": "x"}]
    result, _, _ = run_normalizer(tmp_path, unknown)
    assert result.returncode != 0
    assert "unknown" in result.stderr.lower()
    duplicate = [
        {"image_name": "a", "build_context": ".", "dockerfile": "Dockerfile"},
        {"image_name": "a", "build_context": "other", "dockerfile": "Dockerfile"},
    ]
    result, _, _ = run_normalizer(tmp_path, duplicate)
    assert result.returncode != 0
    assert "unique" in result.stderr.lower()


def test_rejects_control_chars_and_dangerous_paths(tmp_path):
    bad = [
        {"image_name": "bad\nname", "build_context": ".", "dockerfile": "Dockerfile"},
        {"image_name": "ok", "build_context": "../../etc", "dockerfile": "Dockerfile"},
        {"image_name": "ok2", "build_context": ".", "dockerfile": "/tmp/Dockerfile"},
    ]
    for item in bad:
        result, _, _ = run_normalizer(tmp_path, [item])
        assert result.returncode != 0, item


def test_build_alias_deduplicates_build_and_requires_same_source(tmp_path):
    images = [
        {"image_name": "worker", "build_alias": "worker", "build_context": ".", "dockerfile": "Dockerfile"},
        {"image_name": "worker-cron", "build_alias": "worker", "build_context": ".", "dockerfile": "Dockerfile"},
    ]
    result, _, builds = run_normalizer(tmp_path, images)
    assert result.returncode == 0, result.stderr
    assert builds.read_text().count("build\t") == 1
    assert "worker-cron" in builds.read_text()
    conflicting = [
        {"image_name": "a", "build_alias": "same", "build_context": ".", "dockerfile": "Dockerfile"},
        {"image_name": "b", "build_alias": "same", "build_context": "sub", "dockerfile": "Dockerfile"},
    ]
    result, _, _ = run_normalizer(tmp_path, conflicting)
    assert result.returncode != 0
    assert "build_alias" in result.stderr


def test_rejects_probe_injection_and_bad_status(tmp_path):
    result, _, _ = run_normalizer(
        tmp_path,
        valid_images(),
        [{"url": "http://localhost/health;touch /tmp/pwn", "expect_status": 200}],
    )
    assert result.returncode != 0


def test_release_requires_at_least_one_probe(tmp_path):
    result, _, _ = run_normalizer(tmp_path, valid_images(), [])
    assert result.returncode != 0
    assert "probe" in result.stderr.lower()
    result, _, _ = run_normalizer(
        tmp_path, valid_images(), [{"url": "http://localhost/health", "expect_status": 999}]
    )
    assert result.returncode != 0


def test_image_name_follows_docker_path_component_syntax(tmp_path):
    # P2-3 (codex review round 3): the old regex `^[a-z0-9][a-z0-9._-]{0,127}$`
    # allows any character from the class anywhere after the first, which lets
    # separators repeat or land at the very end -- names docker itself will
    # never accept as a repository path component (a trailing/leading
    # separator or a doubled `.` is not a valid path-component per Docker's
    # distribution reference grammar). Those invalid names slipped through
    # normalize_release.py's supposedly fail-fast validation and only blew up
    # much later, as an opaque "invalid reference format" from `docker build`/
    # `docker tag`. image_name (and build_alias, which reuses the same regex)
    # must be validated against the real path-component grammar:
    # alpha-numeric [separator alpha-numeric]*, separator = . | _ | __ | -+.
    for bad_name in ("frontend-", "-frontend", "frontend.", ".frontend", "foo..bar", "Frontend"):
        result, _, _ = run_normalizer(
            tmp_path, [{"image_name": bad_name, "build_context": ".", "dockerfile": "Dockerfile"}]
        )
        assert result.returncode != 0, f"{bad_name!r} must be rejected: {result.stderr}"
        assert "image_name" in result.stderr, result.stderr

    for good_name in ("front-end", "foo.bar", "foo_bar", "frontend", "a", "foo__bar"):
        result, _, _ = run_normalizer(
            tmp_path, [{"image_name": good_name, "build_context": ".", "dockerfile": "Dockerfile"}]
        )
        assert result.returncode == 0, f"{good_name!r} must be accepted: {result.stderr}"


def test_image_name_length_matches_remote_128_char_limit(tmp_path):
    # P2-A (codex review round 5): the remote load_manifest() in
    # release_deploy.sh validates image names against
    # `^[a-z0-9][a-z0-9._-]{0,127}$` -- a first char plus up to 127 more, 128
    # characters total. normalize_release.py's IMAGE_RE had no length limit at
    # all, so the build lane would accept, build, and push an image name over
    # 128 chars, only for the remote deploy script to reject it once the
    # deploy step ran -- burning a full CI cycle before the build/deploy
    # length mismatch surfaced. build_alias is not subject to this remote
    # grammar (it never appears in the manifest scp'd to the host -- it is a
    # purely local grouping key in d3-release.builds, which stays on the
    # runner), so it deliberately gets no length cap here.
    for good_len in (127, 128):
        name = "a" * good_len
        result, _, _ = run_normalizer(
            tmp_path, [{"image_name": name, "build_context": ".", "dockerfile": "Dockerfile"}]
        )
        assert result.returncode == 0, f"{good_len}-char name must be accepted: {result.stderr}"

    for bad_len in (129, 200):
        name = "a" * bad_len
        result, _, _ = run_normalizer(
            tmp_path, [{"image_name": name, "build_context": ".", "dockerfile": "Dockerfile"}]
        )
        assert result.returncode != 0, f"{bad_len}-char name must be rejected"
        assert "image_name" in result.stderr, result.stderr


def test_probe_url_nfkc_urlsplit_valueerror_becomes_validation_error(tmp_path):
    # A probe URL containing a fullwidth solidus (U+FF0F) survives the
    # FORBIDDEN_URL_RE character check (it is not one of the forbidden ASCII
    # shell metacharacters) but makes urlsplit() raise a bare ValueError
    # during its internal NFKC-normalization safety check on the netloc. That
    # ValueError is not a ValidationError, so main()'s
    # `except (json.JSONDecodeError, ValidationError, OSError)` does not catch
    # it: it must not propagate as an uncaught Python traceback (rc=1,
    # unstructured output) -- it must be surfaced through this file's uniform
    # "release input invalid: ..." ValidationError format (rc=2), exactly like
    # every other rejected probe URL.
    result, _, _ = run_normalizer(
        tmp_path,
        valid_images(),
        [{"url": "http://example.com／foo", "expect_status": 200}],
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "release input invalid" in result.stderr
    assert "Traceback" not in result.stderr
    assert "probe 0.url" in result.stderr
