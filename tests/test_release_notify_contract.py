"""release lane(build-deploy-release.yml)通知层契约:gate 共用 notify action 接线。

2026-11 通知迁企微:3 处内联飞书发送改为「组装 step 写 GITHUB_OUTPUT
(severity/title/body/links)+ uses: zlxlabs/gate/.github/actions/notify@v2」。
本文件由 test_release_workflow_contract.py 拆出(commit 闸 file-size ratchet:
683 -> 835 超 800 行上限,按闸指引放 sibling 文件)。
"""
import json
import os
from pathlib import Path
import subprocess

import yaml

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "build-deploy-release.yml"


def load():
    raw = yaml.safe_load(WORKFLOW.read_text())
    return raw, raw.get("on", raw.get(True))


# ── 跨仓契约 fixture ────────────────────────────────────────────────────────
# zlxlabs/gate 的 .github/actions/notify(gate PR #401;@v2 指向
# 425222feeacd56662a8ede9b01d8beea83433cf1)声明的 inputs 键清单;
# gate 侧增删 input 时同步本 fixture。与 test_workflow_contract.py 的同名常量同源。
NOTIFY_ACTION = "zlxlabs/gate/.github/actions/notify@v2"
NOTIFY_ACTION_INPUTS = {"webhook", "title_prefix", "severity", "title", "body", "links"}

# 通知 step 名 -> (组装 step id, 触发条件[与 base 逐字一致], severity 映射)
# severity 按原卡颜色映射:yellow(busy defer)->P2;urgent/P0(回滚未证/失败)->P0。
NOTIFY_STEPS = {
    "release busy defer 通知 (deferred, fail-open)": (
        "assemble_busy_defer",
        "failure() && steps.deploy.outputs.busy_deferred == 'true'",
        "P2",
    ),
    "release rollback unhealthy 通知 (P0, fail-open)": (
        "assemble_rollback_unhealthy",
        "failure() && steps.deploy.outputs.busy_deferred != 'true' && "
        "steps.deploy.outputs.rollback_unhealthy == 'true'",
        "P0",
    ),
    "release failure 通知 (P0, fail-open)": (
        "assemble_release_failure",
        "failure() && steps.deploy.outputs.busy_deferred != 'true' && "
        "steps.deploy.outputs.rollback_unhealthy != 'true'",
        "P0",
    ),
}


def _steps_by_name(raw):
    return {step.get("name"): step for step in raw["jobs"]["release"]["steps"]}


def _step_by_id(raw, step_id):
    return next(
        step for step in raw["jobs"]["release"]["steps"] if step.get("id") == step_id
    )


def _parse_github_output(text):
    # GitHub 输出文件格式:name=value 单行,或 name<<DELIM ... DELIM 多行。
    result = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line:
            name, delim = line.split("<<", 1)
            buf = []
            i += 1
            while i < len(lines) and lines[i] != delim:
                buf.append(lines[i])
                i += 1
            result[name] = "\n".join(buf)
        else:
            name, _, value = line.partition("=")
            result[name] = value
        i += 1
    return result


def _run_assemble_step(step, env, output_path):
    completed = subprocess.run(
        ["bash", "-c", step["run"]],
        text=True,
        capture_output=True,
        env=os.environ | env | {"GITHUB_OUTPUT": str(output_path)},
        check=False,
    )
    parsed = (
        _parse_github_output(output_path.read_text()) if output_path.exists() else {}
    )
    return completed, parsed


def test_release_notify_steps_call_gate_notify_action_with_contract_inputs():
    # 跨发布边界:模板 -> zlxlabs/gate notify action 的输入面。3 处通知的 if 必须与
    # base 逐字一致,with: 键集合必须是 action.yml 声明 inputs 的子集且含
    # webhook/title/body,webhook 只能来自 secrets.FEISHU_CI_WEBHOOK
    # (#46:vars 不打码会进日志)。
    raw, _ = load()
    steps = _steps_by_name(raw)
    assert set(NOTIFY_STEPS) <= set(steps)
    for name, (assemble_id, condition, _severity) in NOTIFY_STEPS.items():
        step = steps[name]
        assert step["uses"] == NOTIFY_ACTION
        assert step["continue-on-error"] is True
        assert step["if"] == condition, f"{name} 触发条件必须与 base 逐字一致"
        with_inputs = step["with"]
        assert set(with_inputs) <= NOTIFY_ACTION_INPUTS, (
            f"{name} with: 超出 action 声明的 inputs: "
            f"{set(with_inputs) - NOTIFY_ACTION_INPUTS}"
        )
        assert {"webhook", "title_prefix", "severity", "title", "body", "links"} <= set(with_inputs)
        assert with_inputs["webhook"] == "${{ secrets.FEISHU_CI_WEBHOOK }}"
        assert with_inputs["title_prefix"] == "${{ vars.FEISHU_CI_TITLE_PREFIX }}"
        for field in ("severity", "title", "body", "links"):
            assert with_inputs[field] == (
                f"${{{{ steps.{assemble_id}.outputs.{field} }}}}"
            )
        # 组装 step 与 notify step 同条件、同 fail-open,且 run 内不内联 ${{ }} 用户可控字段。
        assemble = _step_by_id(raw, assemble_id)
        assert assemble["if"] == condition
        assert assemble["continue-on-error"] is True
        assert "${{ secrets." not in assemble["run"]
        assert "${{ inputs." not in assemble["run"]


def test_release_no_direct_channel_posting_remains():
    # 发送逻辑全部收拢进 gate notify action:模板内不得残留直连 webhook 的代码;
    # 缺地址 notify-skipped、发送失败 ::error 均由 action 承担(被 continue-on-error 吸收)。
    text = WORKFLOW.read_text()
    assert text.count(f"uses: {NOTIFY_ACTION}") == 3
    assert "open.feishu.cn" not in text
    assert "open.larksuite.com" not in text
    assert "vars.FEISHU_CI_WEBHOOK" not in text
    assert '"$webhook"' not in text
    assert "msg_type" not in text
    assert "FEISHU_CI_WEBHOOK is not configured" not in text
    assert "request failed or timed out" not in text
    assert "response parse failed" not in text


def test_release_busy_defer_notification_is_p2_with_fixed_content(tmp_path):
    raw, _ = load()
    assemble = _step_by_id(raw, "assemble_busy_defer")
    completed, outputs = _run_assemble_step(assemble, {}, tmp_path / "out")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert outputs["severity"] == "P2"
    assert outputs["title"] == "D3 release deferred: service busy"
    assert outputs["body"] == ""
    assert json.loads(outputs["links"]) == []


def test_release_rollback_unhealthy_notification_fields(tmp_path):
    raw, _ = load()
    assemble = _step_by_id(raw, "assemble_rollback_unhealthy")
    run = assemble["run"]
    assert "production may be unavailable" in run
    assert "immediate host intervention required" in run
    assert "生产可能不可用，必须立即上机" in run
    for field in ("SVC", "HOST", "REPO", "SHA", "RUN_URL"):
        assert field in assemble["env"]
    for label in ("服务:", "目标机:", "仓库:", "SHA:"):
        assert label in run
    # 企微 markdown 不支持 @全员(gate notify action 无 mention 输入):
    # 原文本消息的 @ 标记随渠道迁移移除,唤醒强度由 heading 的 P0 级别词承担。
    text = WORKFLOW.read_text()
    assert "<at user_id=" not in text
    assert "<at id=all>" not in text

    completed, outputs = _run_assemble_step(
        assemble,
        {
            "SVC": "contract-service",
            "HOST": "contract-host",
            "REPO": "zlxlabs/ci-templates",
            "SHA": "0123456789ab",
            "RUN_URL": "https://example.test/actions/runs/123",
        },
        tmp_path / "out",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert outputs["severity"] == "P0"
    assert outputs["title"] == "D3 release rollback unhealthy"
    for value in (
        "contract-service", "contract-host", "zlxlabs/ci-templates", "0123456789ab",
        "生产可能不可用，必须立即上机",
    ):
        assert value in outputs["body"], f"body 缺 {value}"
    assert json.loads(outputs["links"]) == [
        {"text": "查看部署 Run", "url": "https://example.test/actions/runs/123"}
    ]


def test_release_failure_notification_has_remote_rc_split_and_identity_fields(tmp_path):
    raw, _ = load()
    assemble = _step_by_id(raw, "assemble_release_failure")
    assert assemble["env"]["REMOTE_RC"] == "${{ steps.deploy.outputs.deploy_rc }}"
    for field in ("SVC", "HOST", "REPO", "SHA", "RUN_URL"):
        assert field in assemble["env"]
    for label in ("服务:", "目标机:", "仓库:", "SHA:", "远端 rc:"):
        assert label in assemble["run"]

    base_env = {
        "SVC": "contract-service",
        "HOST": "contract-host",
        "REPO": "zlxlabs/ci-templates",
        "SHA": "0123456789ab",
        "RUN_URL": "https://example.test/actions/runs/123",
    }
    bodies = {}
    for remote_rc, expected_phrases in {
        "130": ("远端收到信号(rc=130)", "立即确认远端状态"),
        "1": ("生产停在已验证的 last_good，不需要紧急上机",),
        "5": ("部署已成功但对账失败", "需上机核验生产镜像"),
        "7": ("远端返回 rc=7",),
        "": ("未知/更早阶段失败", "SSH 传输耗尽", "远端状态未知"),
    }.items():
        completed, outputs = _run_assemble_step(
            assemble,
            base_env | {"REMOTE_RC": remote_rc},
            tmp_path / f"out-{remote_rc or 'empty'}",
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert outputs["severity"] == "P0"
        assert outputs["title"] == "D3 release failed"
        for phrase in expected_phrases:
            assert phrase in outputs["body"], f"rc={remote_rc!r} body 缺 {phrase}"
        assert json.loads(outputs["links"]) == [
            {"text": "查看部署 Run", "url": "https://example.test/actions/runs/123"}
        ]
        bodies[remote_rc] = outputs["body"]
    # 拿不到远端 rc 有两种成因、处置相反：更早阶段失败（生产没动）与 SSH 传输
    # 255 耗尽（远端可能已推进）。卡片必须两种都讲，不能断言成前者——build-deploy.yml
    # 早有留痕：本 run 出过 255 之后远端状态本质不确定。
    assert "本次未上线，生产仍是上一版本" not in bodies[""], (
        "拿不到远端 rc 时不得单方面断言「未上线」——SSH 传输耗尽也走这一支"
    )
