"""Issue #87: 本地 registry 回退 ACR 要可见。

脚本侧:锁死 result-json 的 image_source 三种取值(local / acr / acr-fallback)
与回退时的 ::warning:: annotation 行(pull-failed / retag-failed 两种失败类别)。
workflow 侧:沿用 test_deploy_once_preserves_remote_rc_when_evidence_is_malformed
的抽取-执行方式,跑真实 deploy step 里的 capture_deploy_result,含/不含
image_source 的真实格式回执各一份,断言写入 GITHUB_STEP_SUMMARY 的字节。
远端回执复用 test_pull_and_deploy 的假 docker 注入,不手搭环境。
"""
import json
import os
import subprocess
from pathlib import Path

from test_pull_and_deploy import (
    REPO_ROOT,
    _base_env,
    _local_registry_env,
    _reconcile_ok_bash,
    _run,
    _write_exec,
)
from test_workflow_contract import _load, _step_by_id

PULL_FAILED_WARNING = "::warning::local registry pull failed, fell back to ACR (pull-failed)"
RETAG_FAILED_WARNING = "::warning::local registry pull failed, fell back to ACR (retag-failed)"


def _receipt(env: dict) -> dict:
    return json.loads((Path(env["STATE_DIR"]) / "last_deploy_result.json").read_text())


def test_local_registry_success_records_image_source_local(tmp_path):
    env = _local_registry_env(tmp_path, local_fail_pulls=0)
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert _receipt(env)["image_source"] == "local"
    assert "::warning::local registry" not in res.stdout + res.stderr


def test_local_pull_exhausted_falls_back_acr_fallback_with_warning(tmp_path):
    env = _local_registry_env(tmp_path, local_fail_pulls=99)
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert _receipt(env)["image_source"] == "acr-fallback"
    assert PULL_FAILED_WARNING in res.stderr


def test_retag_failure_falls_back_acr_fallback_with_warning(tmp_path):
    """本地拉到但 retag 失败同样是一次真实回退(pull_from_local_registry 的第二条
    fallback 出口),warning 类别必须区分出来,不能只覆盖 pull 耗尽一条路。"""
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
if [ "$1" = pull ]; then
  [ "$2" = "{local_ref}" ] && exit 0
  [ "$2" = "{acr_ref}" ] && exit 0
  exit 1
fi
if [ "$1" = tag ]; then
  [ "$#" -eq 3 ] && [ "$2" = "{local_ref}" ] && exit 1
  [ "$#" -eq 3 ] && [ "$2" = "{acr_ref}" ] && exit 0
  echo "unexpected-docker: $*" >&2; exit 97
fi
{_reconcile_ok_bash()}
if [ "$1" = compose ] && [ "$2" = up ] && [ "$3" = -d ] && [ "$#" -eq 3 ]; then exit 0; fi
echo "unexpected-docker: $*" >&2
exit 97
""",
    )
    env["LOCAL_IMAGE"] = "local.example:5001/ns/demo"
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert _receipt(env)["image_source"] == "acr-fallback"
    assert RETAG_FAILED_WARNING in res.stderr


def test_without_local_registry_records_image_source_acr(tmp_path):
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    res = _run(env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert _receipt(env)["image_source"] == "acr"
    assert "::warning::local registry" not in res.stdout + res.stderr


# --- workflow 侧:capture_deploy_result → GITHUB_STEP_SUMMARY ------------------

def _receipt_line(image_source: str | None) -> str:
    """按 write_deploy_result 的 printf 字段顺序拼一条真实格式的回执行;
    image_source=None 模拟旧版远端脚本写的没有该字段的回执。"""
    receipt = (
        '{"schema_version":1,"deploy_id":"ct87","git_sha":"abc1234def5678",'
        '"tag":"abc1234def5678","image_id":"sha256:image","image_digest":"sha256:digest",'
    )
    if image_source is not None:
        receipt += f'"image_source":"{image_source}",'
    receipt += (
        '"outcome":"deployed",'
        '"probe":{"status":"ok","final_code":"200","attempts":1,"elapsed_s":0},'
        '"finished_at":"2026-11-10T00:00:00Z"}'
    )
    return "[deploy][evidence] result-json: " + receipt


def _run_capture(tmp_path: Path, remote_output: str):
    deploy_run = _step_by_id(_load()[0], "deploy")["run"]
    fixture = deploy_run[: deploy_run.index("\nattempt=1;")] + '\ncapture_deploy_result "$1"\n'
    output = tmp_path / "github-output"
    summary = tmp_path / "step-summary"
    env = os.environ | {
        "SSH_USER": "deploy",
        "DEPLOY_HOST": "host.example",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_REPOSITORY": "zlxlabs/ci-templates",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    completed = subprocess.run(
        ["bash", "-c", fixture, "capture-fixture", remote_output],
        text=True, capture_output=True, env=env, check=False,
    )
    return completed, output, summary


def test_capture_writes_image_source_line_to_summary(tmp_path):
    completed, output, summary = _run_capture(tmp_path, _receipt_line("acr-fallback"))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert summary.read_text() == "image source: acr-fallback\n"
    assert "image_source=acr-fallback" in output.read_text()


def test_capture_old_receipt_without_image_source_shows_unknown(tmp_path):
    completed, output, summary = _run_capture(tmp_path, _receipt_line(None))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert summary.read_text() == "image source: unknown\n"
    assert "image_source=unknown" in output.read_text()
    assert "::warning::" not in completed.stdout + completed.stderr
