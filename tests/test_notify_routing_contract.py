"""Production-risk notification routing contracts for build-deploy.yml."""
import os
from pathlib import Path
import subprocess

import yaml

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "build-deploy.yml"
NOTIFY_ACTION = "zlxlabs/gate/.github/actions/notify@v2"


def _steps():
    raw = yaml.safe_load(WORKFLOW.read_text())
    return raw["jobs"]["build-deploy"]["steps"]


def _step_by_id(steps, step_id):
    return next(step for step in steps if step.get("id") == step_id)


def _run_assemble(step, env, output_path):
    completed = subprocess.run(
        ["bash", "-c", step["run"]],
        text=True,
        capture_output=True,
        env=os.environ | env | {"GITHUB_OUTPUT": str(output_path)},
        check=False,
    )
    outputs = {}
    if output_path.exists():
        lines = output_path.read_text().splitlines()
        index = 0
        while index < len(lines):
            line = lines[index]
            if "<<" in line:
                name, delimiter = line.split("<<", 1)
                index += 1
                body = []
                while index < len(lines) and lines[index] != delimiter:
                    body.append(lines[index])
                    index += 1
                outputs[name] = "\n".join(body)
            else:
                name, _, value = line.partition("=")
                outputs[name] = value
            index += 1
    return completed, outputs


def test_build_deploy_notify_steps_are_exhaustive_and_routed_by_production_risk():
    steps = _steps()
    notify_steps = {
        step["name"]: step for step in steps if step.get("uses") == NOTIFY_ACTION
    }
    expected = {
        "部署成功回执通知 (opt-in, fail-open)": (
            "assemble_success_receipt",
            "success() && inputs.notify_on_success == true",
        ),
        "生产镜像身份未证通知 (P0, fail-open)": (
            "assemble_reconcile_failed",
            "failure() && steps.deploy.outputs.reconcile_failed == 'true'",
        ),
        "回滚健康未证紧急通知 (P0, fail-open)": (
            "assemble_rollback_unhealthy",
            "failure() && steps.deploy.outputs.deferred != 'true' && "
            "steps.deploy.outputs.rollback_unhealthy == 'true'",
        ),
    }
    assert set(notify_steps) == set(expected)
    for name, (assemble_id, condition) in expected.items():
        notify = notify_steps[name]
        assemble = _step_by_id(steps, assemble_id)
        assert notify["if"] == condition
        assert assemble["if"] == condition
        assert notify["continue-on-error"] is True
        assert assemble["continue-on-error"] is True


def test_rollback_notification_skips_deferred_and_removed_failure_routes():
    text = WORKFLOW.read_text()
    assert "steps.deploy.outputs.deferred != 'true'" in text
    assert "steps.deploy.outputs.rollback_unhealthy == 'true'" in text
    assert "steps.deploy.outputs.reconcile_failed == 'true'" in text
    assert "steps.deploy.outputs.rollback_unhealthy != 'true'" not in text


def test_reconcile_failure_notification_requests_running_sha_check(tmp_path):
    steps = _steps()
    assemble = _step_by_id(steps, "assemble_reconcile_failed")
    completed, outputs = _run_assemble(
        assemble,
        {
            "SVC": "contract-service",
            "HOST": "contract-host",
            "REPO": "zlxlabs/ci-templates",
            "SHA": "0123456789ab",
            "RUN_URL": "https://example.test/actions/runs/123",
        },
        tmp_path / "reconcile-output",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert outputs["severity"] == "P0"
    assert outputs["title"] == "生产镜像身份未证 · contract-service"
    for phrase in (
        "Reconcile 步骤", "生产可能正在跑", "镜像身份未证",
        "立即核对运行中容器镜像 SHA",
    ):
        assert phrase in outputs["body"]
    assert "contract-host" in outputs["body"]
