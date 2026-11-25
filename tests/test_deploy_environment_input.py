"""Contract for the optional GitHub Environment on the single-image deploy job."""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-deploy.yml"


def test_environment_input_is_optional_and_bound_to_the_deploy_job():
    raw = yaml.safe_load(WORKFLOW.read_text())
    trigger = raw.get("on", raw.get(True))
    inputs = trigger["workflow_call"]["inputs"]
    environment = inputs["environment"]

    assert environment["type"] == "string"
    assert environment["required"] is False
    assert environment["default"] == ""
    assert raw["jobs"]["build-deploy"]["environment"] == "${{ inputs.environment }}"
    assert [job for job, spec in raw["jobs"].items() if "environment" in spec] == [
        "build-deploy"
    ]
