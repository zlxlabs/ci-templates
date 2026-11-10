"""Issue #89: reconcile-only 重部署必须补回 `<image>:latest`。

prev_good == GIT_SHA 时脚本跳过 deploy_tag() 直接 reconcile；而
`${IMAGE_NAME}:latest` 这个别名正是 deploy_tag() 平时打出来的。宿主上该别名
一旦不存在（prune 或其他部署来源），reconcile 的 latest==expected 断言必报
`latest tag could not be inspected`（rc=5），同一个 SHA 的重部署假红。
修法：reconcile-only 分支在确认 expected_ref 存在后补打 latest（失败按
deploy_failed 处理，fail fast）；reconcile 断言本身一律不放宽——运行中的容器
漂移仍然要红。
远端回执复用 test_pull_and_deploy 的假 docker 注入，断言假 docker 实际收到
的 argv 与实际写出的 result-json。
"""
import json
from pathlib import Path

from test_pull_and_deploy import _base_env, _run, _write_exec

SHA_REF = "registry.example.com/ns/demo:abc1234"
LATEST_REF = "demo:latest"
EXPECTED_ID = "sha256:expected"
TAG_ARGV = f"tag {SHA_REF} {LATEST_REF}"
LATEST_INSPECT_ARGV = f"image inspect {LATEST_REF} --format {{{{.Id}}}}"
RECEIPT = "last_deploy_result.json"


def _reconcile_only_env(
    tmp_path: Path, *, tag_fails: bool = False,
    container_image_id: str = EXPECTED_ID,
) -> dict:
    """prev_good == GIT_SHA；宿主上 latest 别名缺失（tag 成功才出现）。"""
    mock_dir = tmp_path / "bin"
    mock_dir.mkdir()
    env = _base_env(tmp_path, mock_dir=mock_dir, status="200")
    docker_log = Path(env["DOCKER_LOG"])
    state = Path(env["STATE_DIR"])
    state.mkdir(parents=True)
    (state / "last_good_tag").write_text(env["GIT_SHA"] + "\n")
    latest_marker = tmp_path / "latest-tagged"
    fail_tag = "1" if tag_fails else "0"
    body = f'''#!/bin/bash
printf '%s\\n' "$*" >> "{docker_log}"
if [ "$1" = compose ] && [ "$2" = config ] && [ "$3" = --services ]; then
  printf '%s\\n' app; exit 0
fi
if [ "$1" = compose ] && [[ " $* " == *" ps -q --status running"* ]]; then
  printf '%s\\n' cid-app; exit 0
fi
if [ "$1" = image ] && [ "$2" = inspect ]; then
  if [[ "$*" == *"{{index .RepoDigests 0}}"* ]]; then
    printf '%s\\n' 'registry.example.com/ns/demo@sha256:good'
    exit 0
  fi
  inspect_ref=""
  for arg in "$@"; do
    case "$arg" in "{SHA_REF}") inspect_ref=sha;; "{LATEST_REF}") inspect_ref=latest;; esac
  done
  if [ "$inspect_ref" = latest ]; then
    [ -e "{latest_marker}" ] || exit 1
    printf '%s\\n' {EXPECTED_ID}
    exit 0
  fi
  if [ "$inspect_ref" = sha ]; then
    printf '%s\\n' {EXPECTED_ID}
    exit 0
  fi
fi
if [ "$1" = tag ] && [ "$2" = "{SHA_REF}" ] && [ "$3" = "{LATEST_REF}" ]; then
  [ "{fail_tag}" = 1 ] && exit 7
  touch "{latest_marker}"
  exit 0
fi
if [ "$1" = inspect ] && [ "$2" = cid-app ] && [ "$3" = --format ]; then
  printf '%s\\n' {container_image_id}
  exit 0
fi
echo "unexpected-docker: $*" >&2
exit 97
'''
    _write_exec(Path(env["DOCKER_BIN"]), body)
    return env


def _receipt(env: dict) -> dict:
    return json.loads((Path(env["STATE_DIR"]) / RECEIPT).read_text())


def _docker_calls(env: dict) -> list[str]:
    return Path(env["DOCKER_LOG"]).read_text().splitlines()


def test_reconcile_only_restores_latest_alias(tmp_path):
    env = _reconcile_only_env(tmp_path)

    result = _run(env)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "::notice::image reconcile passed" in output

    receipt = _receipt(env)
    assert receipt["outcome"] == "skipped_already_deployed"

    calls = _docker_calls(env)
    tag_indexes = [i for i, c in enumerate(calls) if c == TAG_ARGV]
    assert len(tag_indexes) == 1, calls
    latest_inspects = [i for i, c in enumerate(calls) if c == LATEST_INSPECT_ARGV]
    assert latest_inspects, calls
    assert tag_indexes[0] < latest_inspects[0], calls


def test_reconcile_only_tag_failure_fails_loudly(tmp_path):
    env = _reconcile_only_env(tmp_path, tag_fails=True)

    result = _run(env)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "deploy of abc1234 failed (rc=7)" in output

    receipt = _receipt(env)
    assert receipt["outcome"] == "deploy_failed"

    calls = _docker_calls(env)
    assert TAG_ARGV in calls
    assert not any(c.startswith(f"image inspect {LATEST_REF}") for c in calls)


def test_reconcile_only_running_drift_still_red(tmp_path):
    env = _reconcile_only_env(tmp_path, container_image_id="sha256:drifted")

    result = _run(env)
    output = result.stdout + result.stderr
    assert result.returncode == 5, output
    assert "image reconcile running container mismatch" in output
    assert "::notice::image reconcile passed" not in output

    receipt = _receipt(env)
    assert receipt["outcome"] == "reconcile_failed"

    # 补 latest 的修复已生效，但断言未被削弱：漂移的容器仍然抓到
    assert TAG_ARGV in _docker_calls(env)
