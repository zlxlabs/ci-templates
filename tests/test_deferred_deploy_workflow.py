"""C3: build-deploy.yml deferred_deploy opt-in 契约。

file-size 闸把 test_workflow_contract.py 锁在 1294 行，本文件承接新增断言。
用 fake ssh/scp 执行 deploy step 的 run 脚本，断言文件列表、argv、stdin 与
GITHUB_OUTPUT。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from test_workflow_contract import (
    NOTIFY_ACTION,
    REPO_ROOT,
    SCRIPT,
    WORKFLOW,
    _load,
    _parse_github_output,
    _run_assemble_step,
    _step_by_id,
)

DEFERRED_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/c3-contract-secret"
DEFERRED_NOTIFY_IF = (
    "failure() && steps.deploy.outputs.deferred == 'true' && "
    "inputs.deferred_deploy == true"
)


def _deploy_once_only(run: str) -> str:
    fixture_end = run.index("\nattempt=1;")
    return (
        run[:fixture_end]
        + '\nrc=0; deploy_once || rc=$?; printf \'deploy_once_rc=%s\\n\' "$rc"; exit "$rc"\n'
    )


def _seed_ci_templates_scripts(tmp_path: Path) -> None:
    scripts = tmp_path / "ci-templates" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "pull_and_deploy.sh").write_text(SCRIPT.read_text())
    (scripts / "deferred_intent.sh").write_text(
        (REPO_ROOT / "scripts" / "deferred_intent.sh").read_text()
    )
    converge = scripts / "converge"
    systemd = converge / "systemd"
    systemd.mkdir(parents=True)
    src_converge = REPO_ROOT / "scripts" / "converge"
    (converge / "converge.sh").write_bytes((src_converge / "converge.sh").read_bytes())
    for name in ("d3-converge.service", "d3-converge.timer"):
        (systemd / name).write_bytes((src_converge / "systemd" / name).read_bytes())


def _write_transport_fakes(tmp_path: Path) -> None:
    log = tmp_path / "transport"
    log.mkdir()
    scp = tmp_path / "scp"
    scp.write_text(
        f"""#!/bin/bash
n=0
[ -f '{log}/scp.count' ] && n=$(cat '{log}/scp.count')
n=$((n+1))
printf '%s\\n' "$n" > '{log}/scp.count'
{{
  printf '%s\\n' "$@"
}} > '{log}/scp.'"$n"'.argv'
exit 0
"""
    )
    scp.chmod(0o755)
    ssh = tmp_path / "ssh"
    ssh.write_text(
        f"""#!/bin/bash
n=0
[ -f '{log}/ssh.count' ] && n=$(cat '{log}/ssh.count')
n=$((n+1))
printf '%s\\n' "$n" > '{log}/ssh.count'
{{
  printf '%s\\n' "$@"
}} > '{log}/ssh.'"$n"'.argv'
cat > '{log}/ssh.'"$n"'.stdin'
joined="$*"
if [[ "$joined" == *mktemp* ]]; then
  printf '%s\\n' '/tmp/d3-converge/bin/.converge.TESTTMP'
  exit 0
fi
if [[ "$joined" == *bash* ]]; then
  printf '%s\\n' "${{MOCK_REMOTE_OUTPUT-}}"
  exit "${{MOCK_REMOTE_RC:-0}}"
fi
exit 0
"""
    )
    ssh.chmod(0o755)


def _transport_records(tmp_path: Path, kind: str) -> list[tuple[str, str]]:
    log = tmp_path / "transport"
    count_path = log / f"{kind}.count"
    if not count_path.exists():
        return []
    count = int(count_path.read_text().strip() or "0")
    records = []
    for n in range(1, count + 1):
        argv = (log / f"{kind}.{n}.argv").read_text()
        stdin_path = log / f"{kind}.{n}.stdin"
        stdin = stdin_path.read_text() if stdin_path.exists() else ""
        records.append((argv, stdin))
    return records


def _deploy_env(tmp_path: Path, output: Path, extra: dict | None = None) -> dict:
    env = os.environ | {
        "ACR_REGISTRY": "registry.example.com",
        "ACR_NAMESPACE": "namespace",
        "IMAGE_NAME": "demo",
        "GIT_SHA": "abc1234",
        "SSH_USER": "deploy",
        "DEPLOY_HOST": "host.example",
        "DEPLOY_DIR": "/opt/demo",
        "RUNNER_TEMP": str(tmp_path),
        "LOCAL_REGISTRY": "",
        "HEALTHCHECK_URL": "",
        "HEALTHCHECK_EXPECT_STATUS": "200",
        "HEALTHCHECK_RETRIES": "1",
        "HEALTHCHECK_INTERVAL": "0",
        "HEALTHCHECK_WARMUP": "0",
        "BUSY_LOCK_FILE": "",
        "BUSY_LOCK_TIMEOUT": "600",
        "ONESHOT_SERVICES": "",
        "ROLLBACK_SAFETY": "safe",
        "GITHUB_REPOSITORY": "zlxlabs/ci-templates",
        "GITHUB_RUN_ID": "4411",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_NUMBER": "131",
        "GITHUB_OUTPUT": str(output),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "MOCK_REMOTE_OUTPUT": "remote log without receipt",
        "MOCK_REMOTE_RC": "0",
    }
    if extra:
        env.update(extra)
    return env


def _run_deploy_once(tmp_path: Path, extra: dict | None = None):
    deploy = next(
        step
        for step in _load()[0]["jobs"]["build-deploy"]["steps"]
        if step.get("id") == "deploy"
    )
    _seed_ci_templates_scripts(tmp_path)
    _write_transport_fakes(tmp_path)
    output = tmp_path / "github-output"
    env = _deploy_env(tmp_path, output, extra)
    completed = subprocess.run(
        ["bash", "-c", _deploy_once_only(deploy["run"])],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    github_output = output.read_text() if output.exists() else ""
    return completed, github_output, _transport_records(tmp_path, "scp"), _transport_records(
        tmp_path, "ssh"
    )


def test_deferred_deploy_input_is_optional_boolean_disabled_by_default():
    _, trigger = _load()
    spec = trigger["workflow_call"]["inputs"]["deferred_deploy"]
    assert spec["type"] == "boolean"
    assert spec["required"] is False
    assert spec["default"] is False


def test_default_deploy_once_scp_ssh_stdin_match_base_path(tmp_path):
    """(a) 未开启时 scp 只有部署脚本、ssh argv 无 D3_/webhook、stdin 空。

    红验不适用:锁的是关闭时与 base 同行为,在 base 上必然绿。
    """
    completed, github_output, scp_recs, ssh_recs = _run_deploy_once(tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert len(scp_recs) == 1, scp_recs
    scp_argv = scp_recs[0][0]
    assert "pull_and_deploy.sh" in scp_argv
    assert "deferred_intent.sh" not in scp_argv
    assert "converge.sh" not in scp_argv
    assert "d3-converge.service" not in scp_argv
    deploy_sshs = [argv for argv, _ in ssh_recs if "IMAGE_NAME=" in argv]
    assert len(deploy_sshs) == 1, ssh_recs
    ssh_argv = deploy_sshs[0]
    assert "INTENT_FILE_ENABLED" not in ssh_argv
    assert "D3_RUN_NUMBER" not in ssh_argv
    assert "D3_INTENT_WEBHOOK" not in ssh_argv
    assert "systemctl is-active" not in ssh_argv
    assert "d3-converge.timer" not in ssh_argv
    assert DEFERRED_WEBHOOK not in ssh_argv
    assert "intent_status=" not in github_output
    assert "converge_status=" not in github_output
    deploy_stdin = [stdin for argv, stdin in ssh_recs if "IMAGE_NAME=" in argv]
    assert deploy_stdin == [""]


def test_deferred_deploy_once_scps_intent_and_converge_without_webhook_in_argv(
    tmp_path,
):
    """(b) 开启时 scp 含 deferred_intent 与收敛器工件; ssh argv 有新 env 且无 webhook; stdin 首行是 webhook。"""
    completed, github_output, scp_recs, ssh_recs = _run_deploy_once(
        tmp_path,
        {
            "DEFERRED_DEPLOY": "true",
            "FEISHU_CI_WEBHOOK": DEFERRED_WEBHOOK,
            "NOTIFY_ON_SUCCESS": "false",
            "D3_REPO": "zlxlabs/ci-templates",
            "D3_RUN_URL": "https://github.com/zlxlabs/ci-templates/actions/runs/4411",
            "MOCK_REMOTE_OUTPUT": (
                "[deploy][evidence] intent: accepted\n"
                "[deploy][evidence] converge: active"
            ),
            "MOCK_REMOTE_RC": "3",
        },
    )
    assert completed.returncode == 3, completed.stdout + completed.stderr
    scp_blob = "\n".join(argv for argv, _ in scp_recs)
    assert "deferred_intent.sh" in scp_blob
    assert "converge.sh" in scp_blob
    assert "d3-converge.service" in scp_blob
    assert "d3-converge.timer" in scp_blob
    ssh_blob = "\n".join(argv for argv, _ in ssh_recs)
    assert DEFERRED_WEBHOOK not in ssh_blob
    deploy = [(argv, stdin) for argv, stdin in ssh_recs if "INTENT_FILE_ENABLED=true" in argv]
    assert len(deploy) == 1, ssh_recs
    argv, stdin = deploy[0]
    for key in (
        "INTENT_FILE_ENABLED=true",
        "D3_RUN_NUMBER=",
        "D3_RUN_ATTEMPT=",
        "D3_RUN_ID=",
        "D3_RUN_URL=",
        "D3_REPO=",
        "D3_NOTIFY_ON_SUCCESS=",
    ):
        assert key in argv, key
    assert "D3_INTENT_WEBHOOK=" not in argv
    assert "systemctl is-active --quiet d3-converge.timer" in argv
    assert "rm -rf --" in argv
    assert stdin.splitlines()[:1] == [DEFERRED_WEBHOOK]
    assert "intent_status=accepted" in github_output.splitlines()
    assert "converge_status=active" in github_output.splitlines()


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("[deploy][evidence] converge: active", "active"),
        ("[deploy][evidence] converge: inactive", "inactive"),
        ("remote log without converge marker", ""),
    ],
)
def test_converge_status_is_parsed_from_remote_stdout(tmp_path, marker, expected):
    completed, github_output, _, _ = _run_deploy_once(
        tmp_path,
        {
            "DEFERRED_DEPLOY": "true",
            "FEISHU_CI_WEBHOOK": "",
            "NOTIFY_ON_SUCCESS": "false",
            "D3_REPO": "zlxlabs/ci-templates",
            "D3_RUN_URL": "https://example.test/run/1",
            "MOCK_REMOTE_OUTPUT": marker,
            "MOCK_REMOTE_RC": "3",
        },
    )
    assert completed.returncode == 3, completed.stdout + completed.stderr
    parsed = _parse_github_output(github_output)
    assert parsed.get("converge_status", "MISSING") == expected


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("[deploy][evidence] intent: accepted", "accepted"),
        ("[deploy][evidence] intent: superseded", "superseded"),
        ("[deploy][evidence] intent: write-failed", "write-failed"),
        ("remote log without intent marker", ""),
    ],
)
def test_intent_status_is_parsed_from_remote_stdout(tmp_path, marker, expected):
    """(c) 三种 intent 标记解析进 intent_status; 没有标记时为空。"""
    completed, github_output, _, _ = _run_deploy_once(
        tmp_path,
        {
            "DEFERRED_DEPLOY": "true",
            "FEISHU_CI_WEBHOOK": "",
            "NOTIFY_ON_SUCCESS": "false",
            "D3_REPO": "zlxlabs/ci-templates",
            "D3_RUN_URL": "https://example.test/run/1",
            "MOCK_REMOTE_OUTPUT": marker,
            "MOCK_REMOTE_RC": "3",
        },
    )
    assert completed.returncode == 3, completed.stdout + completed.stderr
    parsed = _parse_github_output(github_output)
    assert parsed.get("intent_status", "MISSING") == expected


def _run_deploy_step(tmp_path: Path, extra: dict | None = None):
    deploy = next(
        step
        for step in _load()[0]["jobs"]["build-deploy"]["steps"]
        if step.get("id") == "deploy"
    )
    _seed_ci_templates_scripts(tmp_path)
    _write_transport_fakes(tmp_path)
    output = tmp_path / "github-output"
    summary = tmp_path / "github-step-summary"
    env = _deploy_env(tmp_path, output, extra) | {"GITHUB_STEP_SUMMARY": str(summary)}
    completed = subprocess.run(
        ["bash", "-c", deploy["run"]],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    github_output = output.read_text() if output.exists() else ""
    return completed, github_output


def test_rc3_warning_matches_intent_and_converge_status(tmp_path):
    """rc=3 warning 须与意图及收敛器状态一致；未开 opt-in 或其他状态保持原文本。"""
    base_warning = (
        "::warning::deploy DEFERRED — service busy, old container kept. "
        "Image abc1234 is already in ACR; re-run this workflow when the "
        "service is idle (the rerun rebuilds the same immutable SHA)."
    )

    # (a) accepted + active -> 新文案，不含 re-run this workflow
    completed, out = _run_deploy_step(
        tmp_path / "accepted-active",
        {
            "DEFERRED_DEPLOY": "true",
            "FEISHU_CI_WEBHOOK": DEFERRED_WEBHOOK,
            "NOTIFY_ON_SUCCESS": "false",
            "D3_REPO": "zlxlabs/ci-templates",
            "D3_RUN_URL": "https://example.test/run/1",
            "MOCK_REMOTE_OUTPUT": (
                "[deploy][evidence] intent: accepted\n"
                "[deploy][evidence] converge: active"
            ),
            "MOCK_REMOTE_RC": "3",
        },
    )
    assert completed.returncode == 3, completed.stdout + completed.stderr
    assert _parse_github_output(out).get("deferred") == "true"
    assert (
        "intent persisted on host; d3-converge will deploy this SHA "
        "automatically when the service is idle — no re-run needed"
    ) in completed.stdout
    assert "re-run this workflow" not in completed.stdout

    # (b) accepted + inactive (及 converge 缺失) -> enable 提示
    for case_name, remote_out in (
        (
            "accepted-inactive",
            "[deploy][evidence] intent: accepted\n[deploy][evidence] converge: inactive",
        ),
        ("accepted-missing-converge", "[deploy][evidence] intent: accepted"),
    ):
        completed, out = _run_deploy_step(
            tmp_path / case_name,
            {
                "DEFERRED_DEPLOY": "true",
                "FEISHU_CI_WEBHOOK": "",
                "NOTIFY_ON_SUCCESS": "false",
                "D3_REPO": "zlxlabs/ci-templates",
                "D3_RUN_URL": "https://example.test/run/1",
                "MOCK_REMOTE_OUTPUT": remote_out,
                "MOCK_REMOTE_RC": "3",
            },
        )
        assert completed.returncode == 3, (case_name, completed.stdout + completed.stderr)
        assert _parse_github_output(out).get("deferred") == "true", case_name
        assert "intent persisted on host" in completed.stdout, case_name
        assert "not active" in completed.stdout, case_name
        assert "scripts/converge/README.md" in completed.stdout, case_name
        assert "re-run this workflow" not in completed.stdout, case_name

    # (c) 未开 opt-in / superseded / write-failed / 空值 -> 与 Base 字节级一致
    for case_name, deferred_flag, remote_out in (
        ("opt-out", "false", ""),
        ("superseded", "true", "[deploy][evidence] intent: superseded\n[deploy][evidence] converge: active"),
        ("write-failed", "true", "[deploy][evidence] intent: write-failed\n[deploy][evidence] converge: active"),
        ("empty-intent", "true", "remote log without markers"),
    ):
        completed, out = _run_deploy_step(
            tmp_path / case_name,
            {
                "DEFERRED_DEPLOY": deferred_flag,
                "FEISHU_CI_WEBHOOK": "",
                "NOTIFY_ON_SUCCESS": "false",
                "D3_REPO": "zlxlabs/ci-templates",
                "D3_RUN_URL": "https://example.test/run/1",
                "MOCK_REMOTE_OUTPUT": remote_out,
                "MOCK_REMOTE_RC": "3",
            },
        )
        assert completed.returncode == 3, (case_name, completed.stdout + completed.stderr)
        assert _parse_github_output(out).get("deferred") == "true", case_name
        warning_lines = [
            line for line in completed.stdout.splitlines() if line.startswith("::warning::deploy DEFERRED")
        ]
        assert warning_lines == [base_warning], (case_name, warning_lines)


def test_deferred_notify_maps_intent_status_to_severity(tmp_path):
    """(d) 延期通知 if 条件与 severity 三态(空值→P1)。"""
    raw, _ = _load()
    assemble = _step_by_id(raw, "assemble_deferred")
    notify = next(
        step
        for step in raw["jobs"]["build-deploy"]["steps"]
        if step.get("name") == "部署延期通知 (deferred, fail-open)"
    )
    assert assemble["if"] == DEFERRED_NOTIFY_IF
    assert notify["if"] == DEFERRED_NOTIFY_IF
    assert notify["uses"] == NOTIFY_ACTION
    cases = (
        ("accepted", "active", "P2", "自动上线", "无需手动 re-run"),
        ("superseded", "", "P2", "已有更新的部署意图排队", "re-run 本 run 无效"),
        ("write-failed", "", "P1", "意图持久化失败", "自动收敛不会发生"),
        ("", "", "P1", "未能解析意图状态", "自动收敛不会发生"),
    )
    base_env = {
        "SVC": "demo",
        "HOST": "host.example",
        "REPO": "zlxlabs/ci-templates",
        "SHA": "abc1234",
        "RUN_URL": "https://example.test/run/1",
        "RUN_NUMBER": "131",
    }
    for status, converge, severity, *phrases in cases:
        completed, outputs = _run_assemble_step(
            assemble,
            base_env | {"INTENT_STATUS": status, "CONVERGE_STATUS": converge},
            tmp_path / f"out-{status or 'empty'}-{converge or 'none'}",
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert outputs["severity"] == severity, status
        assert outputs["title"] == "部署延期 · demo"
        for phrase in phrases:
            assert phrase in outputs["body"], (status, phrase)
        assert json.loads(outputs["links"])[0]["url"] == "https://example.test/run/1"


def test_deferred_notify_is_opt_in_and_enumerated():
    """未 opt-in 不触发延期通知；notify 步骤闭集含第四条。"""
    raw, _ = _load()
    steps = raw["jobs"]["build-deploy"]["steps"]
    notify_names = {
        step.get("name") for step in steps if step.get("uses") == NOTIFY_ACTION
    }
    assert "部署延期通知 (deferred, fail-open)" in notify_names
    deferred = next(step for step in steps if step.get("id") == "assemble_deferred")
    assert deferred["if"] == DEFERRED_NOTIFY_IF
    for step in steps:
        cond = step.get("if") or ""
        if "steps.deploy.outputs.deferred == 'true'" in cond:
            assert "inputs.deferred_deploy == true" in cond
    text = WORKFLOW.read_text()
    assert DEFERRED_NOTIFY_IF in text
    assert "rm -rf -- '${REMOTE_DIR}'" in text


def test_accepted_notify_severity_depends_on_converge_status(tmp_path):
    """accepted+active=P2; accepted+inactive 或标记缺失=P1。"""
    raw, _ = _load()
    assemble = _step_by_id(raw, "assemble_deferred")
    base_env = {
        "SVC": "demo",
        "HOST": "host.example",
        "REPO": "zlxlabs/ci-templates",
        "SHA": "abc1234",
        "RUN_URL": "https://example.test/run/1",
        "RUN_NUMBER": "131",
        "INTENT_STATUS": "accepted",
    }
    cases = (
        ("active", "P2", "自动上线"),
        ("inactive", "P1", "收敛器 timer 未启用"),
        ("", "P1", "scripts/converge/README.md"),
    )
    for converge, severity, phrase in cases:
        completed, outputs = _run_assemble_step(
            assemble,
            base_env | {"CONVERGE_STATUS": converge},
            tmp_path / f"converge-{converge or 'missing'}",
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert outputs["severity"] == severity, converge
        assert phrase in outputs["body"]
