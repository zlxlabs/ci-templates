"""Static contract checks for the independent multi-image reusable workflow."""
from pathlib import Path
import yaml
WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "build-deploy-release.yml"


def load():
    raw = yaml.safe_load(WORKFLOW.read_text())
    return raw, raw.get("on", raw.get(True))


def test_release_workflow_contract_and_six_secrets():
    raw, trigger = load()
    assert "workflow_call" in trigger
    call = trigger["workflow_call"]
    assert call["inputs"]["images_json"]["required"] is True
    assert call["inputs"]["probes_json"]["required"] is True
    assert call["inputs"]["host"]["required"] is True
    assert set(call["secrets"]) == {
        "ACR_USERNAME", "ACR_PASSWORD", "SSH_DEPLOY_KEY", "KNOWN_HOSTS", "TS_AUTHKEY", "CI_TEMPLATES_PAT"
    }
    assert all(spec.get("required") is True for spec in call["secrets"].values())
    assert raw["permissions"] == {"contents": "read"}
    assert raw["concurrency"]["cancel-in-progress"] is False
    assert "inputs.host" in str(raw["concurrency"]["group"])


def test_release_workflow_pins_actions_and_has_atomic_build_gate():
    text = WORKFLOW.read_text()
    assert "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5" in text
    assert "tailscale/github-action@4e4c49acaa9818630ce0bd7a564372c17e33fb4d" in text
    assert "normalize_release.py" in text
    assert "push_to_acr.sh" in text
    assert "release_deploy.sh" in text
    assert "D3_RELEASE_TAG" in text
    assert "255" in text
    assert "ssh " in text


def test_release_transfer_paths_are_run_unique_and_failure_notify_is_fail_open():
    text = WORKFLOW.read_text()
    assert "GITHUB_RUN_ID" in text and "GITHUB_RUN_ATTEMPT" in text
    assert "/tmp/release_deploy.sh" not in text
    assert "/tmp/d3-release.manifest" not in text
    assert "rm -f" in text
    assert "if: failure()" in text
    assert "continue-on-error: true" in text
    assert "vars.FEISHU_CI_WEBHOOK" in text
    assert "vars.FEISHU_CI_TITLE_PREFIX" in text
    assert "transport_nonce" in text
    assert "${transport_attempt}" in text


def test_release_remote_paths_derive_from_repo_identity_not_just_run_id():
    # P1-1: GITHUB_RUN_ID is only guaranteed unique within a single repo, and this
    # workflow's concurrency group is per-repo too. If the same host is deployed to
    # by two different service repos concurrently, colliding run ids could make one
    # repo's remote manifest/script clobber the other's. The remote path must also
    # be anchored in repo identity (GITHUB_REPOSITORY), not just run id/attempt.
    text = WORKFLOW.read_text()
    assert "GITHUB_REPOSITORY" in text
    assert "repo_slug" in text

    idx_repo_slug_def = text.index("repo_slug=")
    idx_release_base_def = text.index("release_base=")
    assert idx_repo_slug_def < idx_release_base_def, (
        "repo_slug must be derived before release_base is assembled from it"
    )

    # release_base's own assignment line must fold in repo_slug so everything
    # downstream (transport_nonce, remote_manifest, remote_script) inherits it
    # without having to change more than the two validation regexes.
    line_end = text.index("\n", idx_release_base_def)
    release_base_line = text[idx_release_base_def:line_end]
    assert "repo_slug" in release_base_line

    # transport_nonce is built from release_base, and remote_manifest/remote_script
    # are built from transport_nonce — confirm that chain is intact.
    idx_nonce_def = text.index("transport_nonce=")
    nonce_line_end = text.index("\n", idx_nonce_def)
    nonce_line = text[idx_nonce_def:nonce_line_end]
    assert "release_base" in nonce_line

    assert 'remote_manifest="/tmp/d3-release-${transport_nonce}' in text
    assert 'remote_script="/tmp/d3-release-${transport_nonce}' in text


def test_release_busy_lock_and_compose_identity_contract():
    raw, trigger = load()
    assert trigger["workflow_call"]["inputs"]["busy_lock_file"]["default"] == ""
    assert trigger["workflow_call"]["inputs"]["busy_lock_timeout"]["default"] == "600"
    text = WORKFLOW.read_text()
    assert "BUSY_LOCK_FILE" in text and "BUSY_LOCK_TIMEOUT" in text
    assert "push_alias_to_acr.sh" in text
    assert "config" in (text + (WORKFLOW.parents[2] / "scripts" / "release_deploy.sh").read_text())
    assert "images" in (text + (WORKFLOW.parents[2] / "scripts" / "release_deploy.sh").read_text())
    assert "busy_deferred" in text
    assert "FEISHU_CI_TITLE_PREFIX" in text
    assert "--max-time 10" in text
    assert "json.dumps" in text
    assert 'python3 - "$webhook"' not in text
    assert '--data-binary @- "$webhook"' in text
