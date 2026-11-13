"""Caller contract for this repo's gate.yml (ci-templates#93)."""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "gate.yml"


def _jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def test_gate_uses_gate_v2_at_main():
    assert _jobs()["gate"]["uses"] == (
        "zlxlabs/gate/.github/workflows/gate-v2.yml@main"
    )


def test_gate_tier_is_internal():
    assert _jobs()["gate"]["with"]["tier"] == "internal"


def test_gate_has_no_tested_evidence_job():
    assert "tested-evidence" not in _jobs()
