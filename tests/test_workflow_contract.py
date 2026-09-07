"""T3: static contract checks on the reusable build-deploy.yml.

eng-review A4 (codex#5): the workflow must declare secrets EXPLICITLY and must
NOT use `secrets: inherit`, so a compromised ci-templates can never reach
unrelated org secrets. Also asserts the per-host concurrency group (A3).
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-deploy.yml"
SCRIPT = REPO_ROOT / "scripts" / "pull_and_deploy.sh"

REQUIRED_SECRETS = {
    "ACR_USERNAME", "ACR_PASSWORD", "SSH_DEPLOY_KEY", "KNOWN_HOSTS",
    "TS_AUTHKEY",          # runner 入 Tailscale 连内网目标机
    "CI_TEMPLATES_PAT",    # 只读 PAT,checkout private ci-templates 的部署脚本
}
# 长期可选(#46,不是过渡期):webhook 2026-09-06 从 vars 迁到 secrets——vars 不被 Actions
# 打码,当时每次部署都把完整 URL 写进日志。之所以不设成必需:shoplazza-capabilities
# 合法地没有 webhook,它的 caller 传的表达式求值为空串,必需会让那个仓卡在 workflow 校验上。
# 取不到值时通知层打印 `skip notify` 跳过,不影响部署结论。
OPTIONAL_SECRETS = {"FEISHU_CI_WEBHOOK"}
EXPECTED_SECRETS = REQUIRED_SECRETS | OPTIONAL_SECRETS

PINNED_ACTIONS = {
    "actions/checkout": "34e114876b0b11c390a56381ad16ebd13914f8d5",
    "tailscale/github-action": "4e4c49acaa9818630ce0bd7a564372c17e33fb4d",
}


def _load():
    # PyYAML parses the `on:` key as boolean True — load and normalise.
    raw = yaml.safe_load(WORKFLOW.read_text())
    trigger = raw.get("on", raw.get(True))
    return raw, trigger


def test_workflow_is_workflow_call():
    _, trigger = _load()
    assert "workflow_call" in trigger, "must be a reusable workflow"


def test_workflow_uses_least_privilege_and_immutable_action_references():
    raw, _ = _load()
    assert raw.get("permissions") == {"contents": "read"}

    text = WORKFLOW.read_text()
    for action, sha in PINNED_ACTIONS.items():
        assert f"uses: {action}@{sha}" in text, f"{action} must be pinned to its full commit SHA"


def test_ci_templates_checkout_uses_org_repo_path_not_legacy_personal_path():
    # 2026-07-24:本仓真实地址是 zlxlabs/ci-templates(git remote 可证)。旧个人路径
    # zj1123581321/ci-templates 目前只靠 GitHub 仓库转移重定向才能工作——旧用户名
    # 一旦被他人重新注册,这步 checkout 就会拉到攻击者控制的仓库,而此时 job 已经
    # 握着 6 个部署 secret。统一到组织名下,不依赖重定向兜底。
    text = WORKFLOW.read_text()
    assert "zj1123581321" not in text, "legacy personal repo path must not reappear"
    assert "repository: zlxlabs/ci-templates" in text


def test_ci_templates_checkout_does_not_pass_dead_pat_token():
    # 2026-07-24:ci-templates 迁到 org 后是 public 仓(gh api 实证 visibility=
    # public;旧个人路径是同一 repo id 的重定向)。这一步过去带
    # CI_TEMPLATES_PAT 去 checkout,但该 PAT 在仓库转移到 org 后已失效——带
    # 无效 token 的请求即使对公开仓 GitHub 也回 401,git 转而要求交互输入用户名,
    # canary 实跑(run 30094192249)直接炸在
    # "could not read Username ... terminal prompts disabled"。公开仓 checkout
    # 不需要任何 token,默认 github.token 就够。CI_TEMPLATES_PAT 仍保留在
    # workflow_call 的 secrets 契约里(全舰队 caller 仍显式传它,见
    # test_secrets_declared_explicitly_not_inherited),只是这一步不再消费它——
    # 立即从契约摘除会打破所有 caller 的显式传递校验,留到 v2 大版本一起摘
    # (见 docs/BACKLOG.md)。
    text = WORKFLOW.read_text()
    assert "token: ${{ secrets.CI_TEMPLATES_PAT }}" not in text, (
        "the ci-templates checkout step must not pass the dead PAT token — "
        "the repo is public now, the default github.token is enough"
    )
    # regression guard: dropping the token line must not turn into silently
    # dropping the secret from the contract too — that stays a v2 decision.
    assert "CI_TEMPLATES_PAT" in text


def test_secrets_declared_explicitly_not_inherited():
    raw, trigger = _load()
    # strip comments — the contract is about real YAML, not prose
    code = "\n".join(
        ln for ln in WORKFLOW.read_text().splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "inherit" not in code, "must NOT use `secrets: inherit`"
    secrets = trigger["workflow_call"].get("secrets", {})
    assert isinstance(secrets, dict), "secrets must be an explicit mapping"
    assert set(secrets.keys()) == EXPECTED_SECRETS, (
        f"workflow must declare exactly {EXPECTED_SECRETS}, got {set(secrets.keys())}"
    )
    for name, spec in secrets.items():
        assert spec, f"{name} must have a spec"
        want = name not in OPTIONAL_SECRETS
        assert spec.get("required") is want, (
            f"{name} required must be {want} "
            f"(必需组不得被标成可选;可选组的理由见 OPTIONAL_SECRETS 上方注释)"
        )


def test_ssh_has_keepalive_and_retry():
    # ai-info canary 出现过偶发 `Connection reset ...:22`(重跑即过)。50 服务规模
    # 会常遇 → scp/ssh 必须带 keepalive,且部署 step 自带重试,别让一次抖动炸部署。
    text = WORKFLOW.read_text()
    assert "ServerAliveInterval" in text, "scp/ssh 需 -o ServerAliveInterval 防连接被静默掐断"
    assert "ServerAliveCountMax" in text
    assert "ConnectTimeout" in text, "连接阶段也要有超时,避免挂死"
    low = text.lower()
    assert "retry" in low or "attempt" in low, "部署 step 需对瞬时 SSH 失败重试"
    # 关键:只重试 SSH 传输层失败(退出码 255),不重试脚本真实失败(探针挂→已回滚→exit 1)。
    # 否则一个坏部署会被重拉/重部/重滚 3 遍。255 是 ssh 自身连接失败的专用码。
    assert "255" in text, "重试必须区分 SSH 传输失败(255)与真实部署失败(脚本 exit 1)"


def test_per_host_concurrency_group():
    raw, _ = _load()
    concurrency = raw.get("concurrency", {})
    assert "inputs.host" in str(concurrency.get("group", "")), (
        "concurrency group must key on the target host (A3)"
    )
    assert concurrency.get("cancel-in-progress") is False, (
        "must not cancel an in-flight deploy mid-rollout"
    )


def test_notify_on_success_input_is_optional_boolean_disabled_by_default():
    _, trigger = _load()
    spec = trigger["workflow_call"]["inputs"]["notify_on_success"]
    assert spec["type"] == "boolean"
    assert spec["required"] is False
    assert spec["default"] is False


def test_deploy_result_evidence_is_parsed_into_step_outputs():
    raw, _ = _load()
    deploy = next(
        step for step in raw["jobs"]["build-deploy"]["steps"]
        if step.get("id") == "deploy"
    )
    run = deploy["run"]
    assert "capture_deploy_result" in run
    assert "result_prefix='[deploy][evidence] result-json: '" in run
    for output in (
        "image_digest", "probe_status", "probe_attempts",
        "probe_elapsed_s", "outcome",
    ):
        assert f'(\"{output}\",' in run
    assert "result_json" in run
    assert "result_json=\"${line:${#result_prefix}}\"" in run
    assert "remote_output=\"$(\n" in run
    assert "printf '%s\\n' \"$remote_output\"" in run


@pytest.mark.parametrize(
    ("name", "evidence", "remote_rc"),
    [
        ("missing-result-json", "remote log without receipt", 0),
        ("missing-probe", '[deploy][evidence] result-json: {"outcome":"deployed"}', 1),
        (
            "missing-fields",
            '[deploy][evidence] result-json: {"probe":{"status":"ok"}}',
            255,
        ),
    ],
    ids=lambda value: value[0] if isinstance(value, tuple) else str(value),
)
def test_deploy_once_preserves_remote_rc_when_evidence_is_malformed(
    tmp_path, name, evidence, remote_rc
):
    deploy = next(
        step
        for step in _load()[0]["jobs"]["build-deploy"]["steps"]
        if step.get("id") == "deploy"
    )
    run = deploy["run"]
    fixture_end = run.index("\nattempt=1;")
    fixture = (
        run[:fixture_end]
        + '\nrc=0; deploy_once || rc=$?; printf \'deploy_once_rc=%s\\n\' "$rc"; exit "$rc"\n'
    )

    scp = tmp_path / "scp"
    scp.write_text("#!/bin/bash\nexit 0\n")
    scp.chmod(0o755)
    ssh = tmp_path / "ssh"
    ssh.write_text(
        f"#!/bin/bash\nprintf '%s\\n' {shlex.quote(evidence)}\nexit {remote_rc}\n"
    )
    ssh.chmod(0o755)
    output = tmp_path / "github-output"
    env = os.environ | {
        "SSH_USER": "deploy",
        "DEPLOY_HOST": "host.example",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_REPOSITORY": "zlxlabs/ci-templates",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_OUTPUT": str(output),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }

    completed = subprocess.run(
        ["bash", "-c", fixture],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert completed.returncode == remote_rc, (
        f"{name}: expected remote rc {remote_rc}, got {completed.returncode}; "
        f"stdout={completed.stdout!r}, stderr={completed.stderr!r}"
    )
    output_text = completed.stdout + completed.stderr
    assert "deploy result evidence missing" in output_text or (
        "deploy result evidence JSON parse failed" in output_text
    )
    assert f"deploy_once_rc={remote_rc}" in output_text


def test_real_deploy_stdout_is_the_receipt_parser_fixture(tmp_path):
    """跨 shell→workflow 边界使用真实脚本 stdout，不手写 result-json。"""
    docker_log = tmp_path / "docker.log"
    docker = tmp_path / "docker"
    docker.write_text(
        f"""#!/bin/bash
echo "$@" >> "{docker_log}"
if [ "$1" = pull ] && [ "$#" -eq 2 ]; then
  exit 0
fi
if [ "$1" = tag ] && [ "$#" -eq 3 ]; then
  exit 0
fi
if [ "$1" = compose ] && [ "$2" = up ] && [ "$3" = -d ] && [ "$#" -eq 3 ]; then
  exit 0
fi
if [ "$1" = compose ] && [ "$2" = config ] && [ "$3" = --services ] && [ "$#" -eq 3 ]; then
  printf '%s\\n' app
  exit 0
fi
if [ "$1" = compose ] && [ "$2" = ps ] && [ "$3" = -q ] && [ "$4" = --status ] && [ "$5" = running ] && [ "$#" -ge 5 ]; then
  printf '%s\\n' cid-app
  exit 0
fi
if [ "$1" = image ] && [ "$2" = inspect ] && [ "$3" = --format ] && [ "$4" = "{{{{index .RepoDigests 0}}}}" ] && [ "$#" -eq 5 ]; then
  printf '%s\\n' registry.example.com/ns/demo@sha256:image
  exit 0
fi
if [ "$1" = image ] && [ "$2" = inspect ] && [ "$3" = --format ] && [ "$4" = "{{{{.Id}}}}" ] && [ "$#" -eq 5 ]; then
  printf '%s\\n' sha256:image
  exit 0
fi
if [ "$1" = image ] && [ "$2" = inspect ] && [ "$4" = --format ] && [ "$#" -eq 5 ]; then
  printf '%s\\n' sha256:image
  exit 0
fi
if [ "$1" = inspect ] && [ "$3" = --format ] && [ "$#" -eq 4 ]; then
  printf '%s\\n' sha256:image
  exit 0
fi
echo "unexpected-docker: $*" >&2
exit 97
"""
    )
    docker.chmod(0o755)
    curl = tmp_path / "curl"
    curl.write_text("#!/bin/bash\nprintf '200'\n")
    curl.chmod(0o755)
    deploy_dir = tmp_path / "app"
    deploy_dir.mkdir()
    (deploy_dir / "docker-compose.yml").write_text("services: {}\n")
    env = os.environ.copy()
    env.update(
        IMAGE_NAME="demo",
        ACR_IMAGE="registry.example.com/ns/demo",
        GIT_SHA="abc1234",
        DEPLOY_DIR=str(deploy_dir),
        STATE_DIR=str(tmp_path / "state"),
        HOST_LOCK=str(tmp_path / "host.lock"),
        HEALTHCHECK_URL="http://localhost/health",
        HEALTHCHECK_EXPECT_STATUS="200",
        HEALTHCHECK_RETRIES="1",
        HEALTHCHECK_INTERVAL="0",
        HEALTHCHECK_WARMUP="0",
        HEALTHCHECK_TIMEOUT="1",
        DOCKER_BIN=str(docker),
        CURL_BIN=str(curl),
        DEPLOY_ID="workflow-fixture",
    )
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    prefix = "[deploy][evidence] result-json: "
    lines = [line for line in result.stdout.splitlines() if line.startswith(prefix)]
    assert len(lines) == 1
    receipt = json.loads(lines[0][len(prefix):])
    assert receipt["image_id"] == "sha256:image"
    outputs = {
        "image_digest": receipt["image_digest"],
        "probe_status": receipt["probe"]["status"],
        "probe_final_code": receipt["probe"]["final_code"],
        "probe_attempts": receipt["probe"]["attempts"],
        "probe_elapsed_s": receipt["probe"]["elapsed_s"],
        "outcome": receipt["outcome"],
    }
    assert outputs == {
        "image_digest": "sha256:image",
        "probe_status": "ok",
        "probe_final_code": "200",
        "probe_attempts": 1,
        "probe_elapsed_s": 0,
        "outcome": "deployed",
    }
    deploy_run = next(
        step["run"]
        for step in _load()[0]["jobs"]["build-deploy"]["steps"]
        if step.get("id") == "deploy"
    )
    assert "result_json" in deploy_run
    for key, value in outputs.items():
        assert f'("{key}",' in deploy_run

    success = next(
        step
        for step in _load()[0]["jobs"]["build-deploy"]["steps"]
        if step.get("name") == "Feishu 部署成功回执卡 (opt-in, fail-open)"
    )
    assert success["env"] == {
        "FEISHU_WEBHOOK": "${{ secrets.FEISHU_CI_WEBHOOK }}",
        "FEISHU_TITLE_PREFIX": "${{ vars.FEISHU_CI_TITLE_PREFIX }}",
        "SVC": "${{ inputs.image_name }}",
        "HOST": "${{ inputs.host }}",
        "REPO": "${{ github.repository }}",
        "SHA": "${{ steps.sha.outputs.git_sha }}",
        "IMAGE_DIGEST": "${{ steps.deploy.outputs.image_digest }}",
        "PROBE_STATUS": "${{ steps.deploy.outputs.probe_status }}",
        "PROBE_FINAL_CODE": "${{ steps.deploy.outputs.probe_final_code }}",
        "PROBE_ATTEMPTS": "${{ steps.deploy.outputs.probe_attempts }}",
        "PROBE_ELAPSED_S": "${{ steps.deploy.outputs.probe_elapsed_s }}",
        "OUTCOME": "${{ steps.deploy.outputs.outcome }}",
        "RUN_URL": "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}",
    }


def test_success_receipt_card_is_opt_in_fail_open_and_complete():
    raw, _ = _load()
    steps = raw["jobs"]["build-deploy"]["steps"]
    reconcile_index = next(
        i for i, step in enumerate(steps)
        if step.get("name", "").startswith("Reconcile deployed image")
    )
    success = steps[reconcile_index + 1]
    assert success["name"] == "Feishu 部署成功回执卡 (opt-in, fail-open)"
    assert success["if"] == "success() && inputs.notify_on_success == true"
    assert success["continue-on-error"] is True
    run = success["run"]
    assert "success receipt skipped:" in run
    assert "image_digest" in run
    assert "probe_status" in run
    for field in (
        "PROBE_FINAL_CODE", "PROBE_ATTEMPTS", "PROBE_ELAPSED_S",
        "IMAGE_DIGEST", "OUTCOME", "RUN_URL",
    ):
        assert field in run
    assert "FEISHU_WEBHOOK" in run
    assert "FEISHU_TITLE_PREFIX" in run
    assert "curl -fsS --max-time 10 -X POST" in run
    assert "部署成功" in run
    assert "镜像对账已通过" in run


def test_success_receipt_diagnostics_do_not_echo_response_body():
    success = next(
        step
        for step in _load()[0]["jobs"]["build-deploy"]["steps"]
        if step.get("name") == "Feishu 部署成功回执卡 (opt-in, fail-open)"
    )
    run = success["run"]

    assert "response_file" in run
    assert "curl_rc=" in run
    assert "http_status=" in run
    assert "response={raw" not in run
    assert "response:-no response" not in run
    assert "response parser failed" in run


def test_success_receipt_request_failure_reports_status_without_body(tmp_path):
    success = next(
        step
        for step in _load()[0]["jobs"]["build-deploy"]["steps"]
        if step.get("name") == "Feishu 部署成功回执卡 (opt-in, fail-open)"
    )
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        """#!/bin/bash
output=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then output="$2"; shift 2; else shift; fi
done
printf '%s' '{"code":13,"msg":"sensitive response body"}' > "$output"
printf '%s' '502'
exit 22
"""
    )
    fake_curl.chmod(0o755)
    env = os.environ | {
        "FEISHU_WEBHOOK": "https://example.test/webhook",
        "FEISHU_TITLE_PREFIX": "[contract]",
        "SVC": "contract-service",
        "HOST": "contract-host",
        "REPO": "zlxlabs/ci-templates",
        "SHA": "0123456789ab",
        "IMAGE_DIGEST": "sha256:contract",
        "PROBE_STATUS": "ok",
        "PROBE_FINAL_CODE": "200",
        "PROBE_ATTEMPTS": "1",
        "PROBE_ELAPSED_S": "3",
        "OUTCOME": "deployed",
        "RUN_URL": "https://example.test/actions/runs/123",
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }

    completed = subprocess.run(
        ["bash", "-c", success["run"]],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    output = completed.stdout + completed.stderr
    assert "curl_rc=22" in output
    assert "http_status=502" in output
    assert "sensitive response body" not in output
    assert '"code":13' not in output


def test_success_receipt_card_producer_emits_payload_with_receipt_fields():
    raw, _ = _load()
    success = next(
        step for step in raw["jobs"]["build-deploy"]["steps"]
        if step.get("name") == "Feishu 部署成功回执卡 (opt-in, fail-open)"
    )
    env = os.environ | {
        "FEISHU_TITLE_PREFIX": "[contract]",
        "SVC": "contract-service",
        "HOST": "contract-host",
        "REPO": "zlxlabs/ci-templates",
        "SHA": "0123456789ab",
        "IMAGE_DIGEST": "sha256:contract",
        "PROBE_STATUS": "ok",
        "PROBE_FINAL_CODE": "200",
        "PROBE_ATTEMPTS": "1",
        "PROBE_ELAPSED_S": "3",
        "OUTCOME": "deployed",
        "RUN_URL": "https://example.test/actions/runs/123",
    }
    run = success["run"]
    producer_start = run.index("\n", run.index("python3 - <<'PY'")) + 1
    producer_end = run.index("\nPY", producer_start)
    producer = run[producer_start:producer_end]
    completed = subprocess.run(
        [sys.executable, "-"],
        input=producer,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    content = payload["card"]["elements"][0]["text"]["content"]
    assert payload["msg_type"] == "interactive"
    assert payload["card"]["header"]["template"] == "green"
    for value in (
        "contract-service", "contract-host", "zlxlabs/ci-templates",
        "0123456789ab", "sha256:contract", "status=ok",
        "final_code=200", "attempts=1", "elapsed_s=3",
        "outcome=deployed",
    ):
        assert value in content
    assert payload["card"]["elements"][1]["actions"][0]["url"] == env["RUN_URL"]


def test_success_receipt_skips_without_digest_and_does_not_call_curl(tmp_path):
    raw, _ = _load()
    success = next(
        step for step in raw["jobs"]["build-deploy"]["steps"]
        if step.get("name") == "Feishu 部署成功回执卡 (opt-in, fail-open)"
    )
    curl_called = tmp_path / "curl-called"
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(f"#!/bin/bash\\ntouch '{curl_called}'\\n")
    fake_curl.chmod(0o755)
    output = tmp_path / "github-output"
    env = os.environ | {
        "FEISHU_WEBHOOK": "https://example.test/webhook",
        "FEISHU_TITLE_PREFIX": "[contract]",
        "SVC": "contract-service",
        "HOST": "contract-host",
        "REPO": "zlxlabs/ci-templates",
        "SHA": "0123456789ab",
        "IMAGE_DIGEST": "",
        "PROBE_STATUS": "ok",
        "PROBE_FINAL_CODE": "200",
        "PROBE_ATTEMPTS": "1",
        "PROBE_ELAPSED_S": "3",
        "OUTCOME": "deployed",
        "RUN_URL": "https://example.test/actions/runs/123",
        "GITHUB_OUTPUT": str(output),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }
    completed = subprocess.run(
        ["bash", "-c", success["run"]],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "::warning::success receipt skipped: image_digest" in completed.stdout
    assert not curl_called.exists()


def test_deploy_notify_title_prefix_is_repo_variable_with_default():
    text = WORKFLOW.read_text()

    assert "FEISHU_TITLE_PREFIX" in text
    assert "vars.FEISHU_CI_TITLE_PREFIX" in text
    assert "[zlxlabs·CI]" in text
    assert "f\"🔴 {title_prefix} P0 部署失败" in text


def test_feishu_notifications_warn_on_each_delivery_failure_mode():
    raw, _ = _load()
    steps = {
        step["name"]: step
        for step in raw["jobs"]["build-deploy"]["steps"]
        if step.get("name")
        in {
            "Feishu 部署失败卡 (P0, fail-open)",
            "Feishu 回滚健康未证紧急卡 (P0, fail-open)",
            "Feishu 部署延期卡 (deferred, fail-open)",
        }
    }
    expected = {
        "Feishu 部署失败卡 (P0, fail-open)": (
            "Feishu 部署失败卡",
            "failure() && steps.deploy.outputs.deferred != 'true' && "
            "steps.deploy.outputs.rollback_unhealthy != 'true'",
            "e27bf0a179d22114c8f5e150a130e6364f8dddd3fd21f4743ff57b6048dc2d40",
        ),
        "Feishu 回滚健康未证紧急卡 (P0, fail-open)": (
            "Feishu 回滚健康未证紧急卡",
            "failure() && steps.deploy.outputs.deferred != 'true' && "
            "steps.deploy.outputs.rollback_unhealthy == 'true'",
            "dc9c201a123fc6b7a4937ed2ef382ee27b67bfc9e0ff8464d9ba35375cfa30b0",
        ),
        "Feishu 部署延期卡 (deferred, fail-open)": (
            "Feishu 部署延期卡",
            "failure() && steps.deploy.outputs.deferred == 'true'",
            "7892e7a0a4bfc79cc66d81f5c976aefcfe97dc2e0acfd3da7c47d53fe9b525a4",
        ),
    }
    assert set(steps) == set(expected)

    for step_name, (label, condition, body_digest) in expected.items():
        step = steps[step_name]
        run = step["run"]
        assert step["if"] == condition
        assert step["continue-on-error"] is True

        # Each channel is independently visible in the Actions annotation UI:
        # missing webhook, transport failure, response without code, and
        # non-zero Feishu business code.
        assert "set -u -o pipefail" in run
        assert 'webhook="${FEISHU_WEBHOOK:-}"' in run
        assert (
            f'echo "::warning::{label} not sent: '
            'FEISHU_WEBHOOK is not configured"'
        ) in run
        assert (
            "if ! response=\"$(python3 - <<'PY' | curl -fsS "
            '--max-time 10 -X POST -H \'Content-Type: application/json\' '
            '--data-binary @- "$webhook" 2>&1'
        ) in run
        assert f'::warning::{label} request failed or timed out:' in run
        assert f'python3 - "{label}" "$response" <<\'PY\'' in run
        assert 'result["code"]' in run
        assert (
            f'::warning::{{label}} response parse failed:'
        ) in run
        assert 'if str(code) != "0":' in run
        assert f'::warning::{{label}} business error: code=' in run
        assert f'::warning::{label} response parser failed' in run

        body_start = run.index("body = (")
        body_end = run.index("card = {", body_start)
        body = run[body_start:body_end]
        assert hashlib.sha256(body.encode()).hexdigest() == body_digest

    text = WORKFLOW.read_text()
    assert "swallowed" not in text
    assert "skip notify" not in text


def test_feishu_notification_producers_emit_json_payload_for_curl():
    raw, _ = _load()
    names = {
        "Feishu 部署失败卡 (P0, fail-open)",
        "Feishu 回滚健康未证紧急卡 (P0, fail-open)",
        "Feishu 部署延期卡 (deferred, fail-open)",
    }
    env = os.environ | {
        "FEISHU_TITLE_PREFIX": "[contract]",
        "SVC": "contract-service",
        "HOST": "contract-host",
        "REPO": "zlxlabs/ci-templates",
        "SHA": "0123456789ab",
        "RUN_URL": "https://example.test/actions/runs/123",
    }

    for step in raw["jobs"]["build-deploy"]["steps"]:
        if step.get("name") not in names:
            continue
        run = step["run"]
        producer_start = run.index("\n", run.index("python3 - <<'PY'")) + 1
        producer_end = run.index("\nPY", producer_start)
        producer = run[producer_start:producer_end]
        completed = subprocess.run(
            [sys.executable, "-"],
            input=producer,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout)
        assert payload["msg_type"] == "interactive"
        content = payload["card"]["elements"][0]["text"]["content"]
        assert "contract-service" in content
        assert payload["card"]["elements"][1]["actions"][0]["url"] == env["RUN_URL"]


# --- busy-lock gate: inputs 透传 + rc=3 deferred 分流 + 双卡通知 --------------

def test_busy_lock_inputs_declared_with_safe_defaults():
    raw, trigger = _load()
    inputs = trigger["workflow_call"]["inputs"]

    assert "busy_lock_file" in inputs
    assert inputs["busy_lock_file"].get("default") == ""
    assert inputs["busy_lock_file"].get("required") is not True

    assert "busy_lock_timeout" in inputs
    assert inputs["busy_lock_timeout"].get("default") == "600"


def test_busy_lock_env_is_passed_through_to_deploy_step():
    text = WORKFLOW.read_text()
    assert "BUSY_LOCK_FILE" in text
    assert "BUSY_LOCK_TIMEOUT" in text
    assert "inputs.busy_lock_file" in text
    assert "inputs.busy_lock_timeout" in text


def test_deferred_exit_code_writes_output_before_nonzero_exit():
    text = WORKFLOW.read_text()
    assert "deferred=true" in text
    assert "GITHUB_OUTPUT" in text
    # rc=3 判断必须先于笼统的 "!= 255" 判断,否则 deferred 会被误判为
    # "已按探针门自动回滚" 报错退出,语义就错了。
    idx_rc3 = text.index('"$rc" -eq 3')
    idx_rc_ne_255 = text.index('"$rc" -ne 255')
    assert idx_rc3 < idx_rc_ne_255, "rc=3 分支必须在 != 255 判断之前"


def test_rollback_unhealthy_rc4_is_routed_before_transport_guard():
    text = WORKFLOW.read_text()
    idx_rc4 = text.index('"$rc" -eq 4')
    idx_rc_ne_255 = text.index('"$rc" -ne 255')
    assert idx_rc4 < idx_rc_ne_255, "rc=4 分支必须在 != 255 判断之前"
    assert "rollback_unhealthy=true" in text
    assert 'echo "::error::deploy failed (rc=4)' in text
    assert 'exit 4' in text


def test_deploy_output_write_failures_do_not_skip_rc4_exit():
    text = WORKFLOW.read_text()
    start = text.index('if [ "$rc" -eq 4 ]; then')
    end = text.index('if [ "$rc" -ne 255 ]; then', start)
    branch = text[start:end]
    output = 'echo "rollback_unhealthy=true" >> "$GITHUB_OUTPUT"'
    assert f'{output} || echo "::warning::' in branch
    assert branch.index("::error::") < branch.index("exit 4")


def test_deferred_output_write_failure_does_not_skip_rc3_exit():
    text = WORKFLOW.read_text()
    start = text.index('if [ "$rc" -eq 3 ]; then')
    end = text.index('if [ "$rc" -eq 4 ]; then', start)
    branch = text[start:end]
    output = 'echo "deferred=true" >> "$GITHUB_OUTPUT"'
    assert f'{output} || echo "::warning::' in branch
    assert branch.index("::warning::deploy DEFERRED") < branch.index("exit 3")


def test_deploy_failure_notifications_are_mutually_exclusive_and_exhaustive():
    text = WORKFLOW.read_text()
    assert "failure() && steps.deploy.outputs.deferred == 'true'" in text
    assert (
        "failure() && steps.deploy.outputs.deferred != 'true' && "
        "steps.deploy.outputs.rollback_unhealthy == 'true'"
    ) in text
    assert (
        "failure() && steps.deploy.outputs.deferred != 'true' && "
        "steps.deploy.outputs.rollback_unhealthy != 'true'"
    ) in text

    urgent_idx = text.index("回滚健康未证紧急卡")
    yellow_idx = text.index("Feishu 部署延期卡")
    urgent_section = text[urgent_idx:yellow_idx]
    assert "生产可能不可用，必须立即上机" in urgent_section
    assert "不需要紧急上机" not in urgent_section


def test_red_card_distinguishes_deploy_only_from_rc5_double_red():
    # rc=5 让 Deploy 与 Reconcile 两 step 同红。若首句仍写「Deploy 步骤红 = 已回滚」，
    # on-call 会把对账失败当成已回滚。单红与双红必须分行。
    text = WORKFLOW.read_text()
    assert "**Deploy 步骤**红且 **没有** Reconcile 红:" in text
    assert "Deploy 与 Reconcile **同时**红 = 对账失败（rc=5）" in text
    assert "按 Reconcile 那条处置" in text
    assert "**不要**按已回滚处置" in text
    assert "**Deploy 步骤**红:未部署成功" not in text


def test_post_deploy_image_reconciliation_is_success_only_and_checks_all_layers():
    # Aligned with #35: reconcile runs inside pull_and_deploy.sh while HOST_LOCK
    # is held; the workflow step is a thin shell that only re-emits rc=5.
    raw, _ = _load()
    steps = raw["jobs"]["build-deploy"]["steps"]
    deploy_index = next(i for i, step in enumerate(steps) if step.get("id") == "deploy")
    reconcile_index = next(
        i for i, step in enumerate(steps)
        if step.get("name", "").startswith("Reconcile deployed image")
    )
    reconcile = steps[reconcile_index]

    assert reconcile_index == deploy_index + 1, (
        "image reconciliation must run immediately after the deploy step"
    )
    assert reconcile["if"] == "failure() && steps.deploy.outputs.reconcile_failed == 'true'", (
        "thin shell must only run when deploy already mapped rc=5; deferred and "
        "ordinary deploy failures must skip it"
    )

    deploy_run = steps[deploy_index]["run"]
    assert 'if [ "$rc" -eq 5 ]; then' in deploy_run
    assert "reconcile_failed=true" in deploy_run
    assert deploy_run.index('if [ "$rc" -eq 5 ]; then') < deploy_run.index(
        "reconcile_failed=true"
    )
    assert deploy_run.index('if [ "$rc" -eq 5 ]; then') < deploy_run.index(
        '"$rc" -ne 255'
    )

    thin = reconcile["run"]
    assert "printf -v RECONCILE_COMMAND" not in thin
    assert "bash -s" not in thin
    assert "ssh " not in thin
    assert "image reconcile assertion failed" in thin

    script = SCRIPT.read_text()
    after_do_deploy = script[script.index("rc=0\ndo_deploy || rc=$?"):]
    assert "reconcile_deployed_image" in after_do_deploy
    assert after_do_deploy.index("reconcile_deployed_image") < after_do_deploy.index(
        "flock -u 9"
    )
    flock_acquire = script.index("\nflock 9\n")
    flock_release = script.index("flock -u 9", flock_acquire)
    call_at = script.index("reconcile_deployed_image", flock_acquire)
    assert flock_acquire < call_at < flock_release

    assert 'RECONCILE_CMD_TIMEOUT="${RECONCILE_CMD_TIMEOUT:-60}"' in script
    assert "reconcile_docker()" in script
    assert 'reconcile_docker image inspect "${ACR_IMAGE}:${GIT_SHA}"' in script
    assert 'reconcile_docker image inspect "${IMAGE_NAME}:latest"' in script
    assert 'ps -q --status running' in script
    assert '"${non_oneshot_services[@]}"' in script
    assert "image reconcile values:" in script
    assert "expected_id=" in script and "latest_id=" in script and "running_ids=" in script
    assert "rc=5" in after_do_deploy
    assert "::notice::image reconcile passed" in script


def test_remote_script_path_is_unique_per_run():
    # code review round 4 (P1): a fixed remote path (/tmp/pull_and_deploy.sh) is
    # NOT isolated across different service repos deploying to the same host —
    # GitHub concurrency groups are per-repo, not cross-repo. Two service repos
    # racing on the same box would clobber each other's script file, silently
    # executing whatever got written last (e.g. an old script without the
    # busy-lock gate logic, even though the busy-lock env vars were passed).
    # The remote path must be unique per workflow run so concurrent deploys
    # from different repos never collide on the same file.
    text = WORKFLOW.read_text()

    assert "GITHUB_RUN_ID" in text, "remote script path must be derived from the run id to be unique per run"

    # the same variable must be used both when scp'ing the script up and when
    # ssh invoking `bash <path>` — otherwise the two paths could drift apart.
    assert "REMOTE_SCRIPT" in text, "expected a REMOTE_SCRIPT variable naming the unique remote path"
    assert '${REMOTE_SCRIPT}' in text or "$REMOTE_SCRIPT" in text

    # used-then-deleted: the remote script must be cleaned up after execution.
    assert "rm -f" in text
    rm_idx = text.index("rm -f")
    nearby = text[max(0, rm_idx - 200): rm_idx + 200]
    assert "REMOTE_SCRIPT" in nearby, "rm -f must target the same REMOTE_SCRIPT path that was scp'd up"

    # regression guard: the old fixed path must be gone entirely.
    assert ":/tmp/pull_and_deploy.sh" not in text, "fixed remote path must not reappear — it's the bug this test guards against"

    # P1-1 parity: GITHUB_RUN_ID is only guaranteed unique within a single repo —
    # two different service repos deploying to the same host concurrently could
    # collide on run id (concurrency groups are per-repo too). The remote path
    # must also be anchored in repo identity, not just run id/attempt.
    assert "GITHUB_REPOSITORY" in text, (
        "remote script path must also be derived from repo identity, since "
        "GITHUB_RUN_ID alone is not unique across repositories"
    )
    assert "repo_slug" in text
    idx_remote_script_def = text.index('REMOTE_SCRIPT="')
    remote_script_line_end = text.index("\n", idx_remote_script_def)
    remote_script_line = text[idx_remote_script_def:remote_script_line_end]
    assert "repo_slug" in remote_script_line, (
        "repo_slug must appear directly in the REMOTE_SCRIPT path assembly"
    )


def test_rc3_after_prior_transport_failure_defers_with_uncertainty_warning():
    # 2026-07-24 减法重构:拆除"255 后自动判成功"证明机器(pre_good 基线捕获 +
    # remote_good 复核 + __unknown__ 哨兵 + 三条件提升,曾经历 R5/R6 两轮加固)。
    # 证明条件本身(历史同值假阳性、基线不可得语义、竞态窗口)在两轮 code review
    # 中先后被打洞,维护面大于价值,已整体拆除。回归诚实语义:本 run 出过 255 之后
    # 远端状态本质不确定,rc=3 一律按 deferred 处理,只是额外打一条"状态不确定"
    # 的告警,不再尝试反查远端状态来判定"其实已经成功"。
    text = WORKFLOW.read_text()

    # 证明机器必须已被整体拆除 —— 不应再出现基线/复核相关的变量名或哨兵值。
    assert "pre_good" not in text, "post-255 baseline capture must be gone"
    assert "remote_good" not in text, "post-255 remote recheck must be gone"
    assert "last_good_tag" not in text, (
        "the rc=3 recheck reading the host's last_good_tag must be gone"
    )
    assert "__unknown__" not in text, "the baseline-unknown sentinel must be gone"
    assert "_qdir" not in text, (
        "the %q-escaped DEPLOY_DIR token that only served the deleted recheck "
        "must be gone too"
    )

    # had_transport_failure 的追踪逻辑保留:循环前初始化、确认 255 后才置位 ——
    # 这是诚实告警仍然依赖的信号,不是本次拆除的对象。
    idx_init = text.index("had_transport_failure=0")
    idx_rc3 = text.index('"$rc" -eq 3')
    idx_rc_ne_255 = text.index('"$rc" -ne 255')
    idx_set = text.index("had_transport_failure=1")
    assert idx_init < idx_rc3, "had_transport_failure must be initialised before the loop"
    # the flag is set once rc is known to be 255, i.e. after the "!= 255" guard
    assert idx_rc_ne_255 < idx_set, "had_transport_failure=1 must be set on the confirmed-255 path"

    # 诚实告警保留:这是整套新语义的核心 —— 断过线就说"不确定",而不是悄悄判成功。
    assert (
        "::warning::transport was interrupted earlier in this run; remote state "
        "may have advanced beyond what 'deferred' implies" in text
    ), "the honest uncertainty warning must remain after dropping auto-promotion"

    # regression guard: deferred=true must still exist as the (only) outcome.
    assert "deferred=true" in text, "rc=3 must still fall through to deferred"


def test_red_card_step_skips_when_deferred():
    text = WORKFLOW.read_text()
    assert "deferred != 'true'" in text
    assert "rollback_unhealthy != 'true'" in text


def test_yellow_card_step_exists_for_deferred_without_at_all():
    raw, _ = _load()
    steps = raw["jobs"]["build-deploy"]["steps"]
    cards = {
        step["name"]: step
        for step in steps
        if step.get("name") in {
            "Feishu 部署延期卡 (deferred, fail-open)",
            "Feishu 部署失败卡 (P0, fail-open)",
            "Feishu 回滚健康未证紧急卡 (P0, fail-open)",
        }
    }

    def at_all_payload_lines(step):
        return [
            line.strip()
            for line in step["run"].splitlines()
            if line.strip().startswith('f"<at id=all></at>')
        ]

    yellow = cards["Feishu 部署延期卡 (deferred, fail-open)"]
    ordinary = cards["Feishu 部署失败卡 (P0, fail-open)"]
    urgent = cards["Feishu 回滚健康未证紧急卡 (P0, fail-open)"]
    assert yellow["if"] == "failure() && steps.deploy.outputs.deferred == 'true'"
    assert "部署延期" in yellow["name"]
    assert not at_all_payload_lines(yellow)
    assert at_all_payload_lines(ordinary)
    assert at_all_payload_lines(urgent)


def test_checkouts_do_not_persist_credentials_and_ci_templates_leaves_build_context():
    # P1-B (codex review): actions/checkout defaults to persist-credentials:
    # true, which writes the (in the ci-templates checkout's case, PAT-backed)
    # token into .git/config. Nothing after checkout needs git credentials —
    # deploy is pure SSH/scp/docker. Worse, .ci-templates lives inside this
    # job's `build_context: "."` — a caller Dockerfile with `COPY . .` would
    # bake the CI_TEMPLATES_PAT-backed .git/config *and* the private
    # ci-templates source straight into the image layer pushed to ACR. Both
    # checkouts must disable credential persistence, and ci-templates must be
    # relocated out of the build context (into $RUNNER_TEMP) before the build
    # step runs, with every later reference following it there.
    text = WORKFLOW.read_text()
    assert text.count("persist-credentials: false") >= 2, (
        "both the caller-repo and ci-templates checkout steps must set "
        "persist-credentials: false"
    )
    assert 'mv .ci-templates "$RUNNER_TEMP/ci-templates"' in text or \
        "mv .ci-templates \"$RUNNER_TEMP/ci-templates\"" in text, (
        "ci-templates must be moved out of the build_context (.) after checkout"
    )
    assert ".ci-templates/scripts/" not in text, (
        "no step may reference ci-templates scripts at their original "
        "in-build-context path once the relocation step exists — every "
        "reference must go through $RUNNER_TEMP/ci-templates/scripts/"
    )
    assert '$RUNNER_TEMP/ci-templates/scripts/push_to_acr.sh' in text
    assert '$RUNNER_TEMP/ci-templates/scripts/pull_and_deploy.sh' in text

    # the relocation must happen before the build step consumes push_to_acr.sh
    idx_mv = text.index('mv .ci-templates')
    idx_build_push = text.index('push_to_acr.sh')
    assert idx_mv < idx_build_push, (
        "ci-templates must be relocated before the build step references its scripts"
    )


def test_run_steps_do_not_directly_expand_inputs():
    # P1-A (codex review round 5): GitHub Actions substitutes ${{ }} expressions
    # into `run:` text via plain textual replacement BEFORE the shell ever sees
    # it -- it is not shell variable injection, it is a text splice. If the
    # substituted value contains a newline, whatever text follows the
    # substitution point on that line (even inside a shell comment) becomes a
    # new, literally-executed shell command. At this point in the job the
    # runner already holds ACR/SSH credentials, so this is a high-value
    # injection surface. `inputs.*` values must be routed through the step's
    # `env:` mapping and referenced as "$VAR" in the script body instead of
    # being spliced directly into `run:` text.
    #
    # P1-A follow-up (round 5 leftover, closed out on coordinator direction):
    # the same text-splice hazard applies to `${{ secrets.* }}` -- the round 5
    # fix only routed `inputs.*` out of run: text and explicitly left the 3
    # `secrets.*` splices (ACR login, SSH key setup) in place as an accepted-
    # risk item. This test now also guards against `${{ secrets.` regressing
    # back into run: text.
    raw, _ = _load()
    for job_name, job in raw["jobs"].items():
        for step in job.get("steps", []):
            run = step.get("run")
            if not run:
                continue
            assert "${{ inputs." not in run, (
                f"step {step.get('name')!r} in job {job_name!r} must not expand "
                "${{ inputs.* }} directly in its run: text -- route it through "
                "env: instead"
            )
            assert "${{ secrets." not in run, (
                f"step {step.get('name')!r} in job {job_name!r} must not expand "
                "${{ secrets.* }} directly in its run: text -- route it through "
                "env: instead"
            )


def test_ssh_user_and_host_are_syntax_validated_before_use():
    # P1-B (codex review round 5): if ssh_user starts with '-', the assembled
    # "${SSH_USER}@${DEPLOY_HOST}:path" argument itself starts with '-' and
    # scp/ssh parse it as a command-line option (e.g. a crafted
    # -oProxyCommand=... value), not as a user@host target -- double-quoting
    # does not stop this, because the problem is scp/ssh's own option parsing,
    # not shell word-splitting. Both values must be validated against a safe
    # character class before their first use in deploy_once()/SSH_OPTS.
    text = WORKFLOW.read_text()
    assert 'SSH_USER" =~' in text, (
        "ssh_user must be regex-validated against a safe charset before use"
    )
    assert 'DEPLOY_HOST" =~' in text, (
        "host must be regex-validated against a safe charset before use"
    )


# --- local registry pull/push (opt-in; empty default = today's ACR-only behavior) ---

def test_local_registry_input_declared_with_safe_default():
    raw, trigger = _load()
    inputs = trigger["workflow_call"]["inputs"]

    assert "local_registry" in inputs
    assert inputs["local_registry"].get("default") == ""
    assert inputs["local_registry"].get("required") is not True


def test_local_registry_env_wired_to_push_and_deploy_steps():
    text = WORKFLOW.read_text()
    # 必须显式路由到两个 step 的 env:(push 步骤 + deploy 步骤),不能只接一处；
    # 同一模式 "LOCAL_REGISTRY: ${{ inputs.local_registry }}" 出现两次即证明两处都接了。
    assert text.count("LOCAL_REGISTRY: ${{ inputs.local_registry }}") == 2, (
        "local_registry must be routed through env: to both the push step and "
        "the deploy step"
    )


def test_local_image_is_composed_from_parts_and_conditioned_on_local_registry():
    text = WORKFLOW.read_text()
    assert 'LOCAL_IMAGE="${LOCAL_REGISTRY}/${ACR_NAMESPACE}/${IMAGE_NAME}"' in text, (
        "LOCAL_IMAGE must mirror ACR_IMAGE's own composition (same namespace/image_name parts)"
    )
    # LOCAL_REGISTRY 为空(默认)时 LOCAL_IMAGE 必须保持空字符串——这是与 pull_and_deploy.sh
    # 的 opt-in 判断 `[ -n "$LOCAL_IMAGE" ]` 对齐的前置条件。
    assert '[ -n "$LOCAL_REGISTRY" ]' in text


def test_local_image_passed_through_to_remote_script():
    text = WORKFLOW.read_text()
    assert "LOCAL_IMAGE='${LOCAL_IMAGE}'" in text, (
        "LOCAL_IMAGE must be forwarded into the remote pull_and_deploy.sh invocation, "
        "the same way ACR_IMAGE already is"
    )


def test_orphaned_remote_script_cleaned_up_when_retries_exhausted():
    # P2-B (codex review round 5): REMOTE_SCRIPT's path is fixed for the whole
    # job (constructed once before the retry loop, not per-attempt), so if
    # scp/ssh keep failing at the transport layer (rc=255) across every
    # retry, the remote script never gets a chance to run its own cleanup
    # trap, and nothing else ever removes the orphaned /tmp file once retries
    # are exhausted. A best-effort ssh rm -f cleanup must run right before the
    # final give-up `exit 1` in the "attempt >= max_attempts" branch.
    text = WORKFLOW.read_text()
    assert "rm -f -- '${REMOTE_SCRIPT}'" in text, (
        "must best-effort clean up the orphaned REMOTE_SCRIPT path on the host "
        "when transport retries are exhausted"
    )
    idx_giveup = text.index("SSH transport failed after ${max_attempts} attempts (rc=255)")
    idx_cleanup = text.index("rm -f -- '${REMOTE_SCRIPT}'")
    idx_exit1 = text.index("exit 1", idx_giveup)
    assert idx_giveup < idx_cleanup < idx_exit1, (
        "cleanup must happen inside the exhausted-retries branch, before its "
        "final exit 1"
    )


def test_oneshot_services_input_declared_with_empty_default():
    _, trigger = _load()
    spec = trigger["workflow_call"]["inputs"]["oneshot_services"]
    assert spec["type"] == "string"
    assert spec["required"] is False
    assert spec["default"] == ""


def test_oneshot_services_env_is_passed_through_to_deploy_step():
    text = WORKFLOW.read_text()
    assert "ONESHOT_SERVICES: ${{ inputs.oneshot_services }}" in text
    assert "ONESHOT_SERVICES='${ONESHOT_SERVICES}'" in text


def test_oneshot_services_rc4_error_includes_schema_hint_when_declared():
    text = WORKFLOW.read_text()
    start = text.index('if [ "$rc" -eq 4 ]; then')
    end = text.index('if [ "$rc" -ne 255 ]; then', start)
    branch = text[start:end]
    assert 'if [ -n "$ONESHOT_SERVICES" ]; then' in branch
    assert "manual verification required" in branch
