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


def test_release_rc3_rechecks_last_good_sha_after_prior_transport_failure():
    # P1-2: this mirrors the single-image lane's R5/R6-hardened recheck in
    # build-deploy.yml (had_transport_failure / pre_good / __unknown__), adapted to
    # the release lane's own last_good_sha path. release_deploy.sh's STATE_DIR is
    # ${DEPLOY_DIR}/.deploy-state/release (not the single-image lane's bare
    # .deploy-state), and the legacy simple-SHA view lives at last_good_sha (not
    # last_good_tag) — GOOD_SHA_FILE="$STATE_DIR/last_good_sha" in release_deploy.sh.
    #
    # A 255 (SSH transport failure) can happen AFTER the remote release already
    # promoted successfully but before the exit code made it back over the wire. A
    # later rc=3 (busy-lock deferred) must not be trusted at face value in that
    # case — recheck the host's last_good_sha before reporting "deferred".
    text = WORKFLOW.read_text()

    assert "pre_good" in text
    assert "had_transport_failure" in text
    assert "__unknown__" in text
    assert ".deploy-state/release/last_good_sha" in text, (
        "release lane's legacy last_good_sha view lives under .deploy-state/release, "
        "distinct from the single-image lane's .deploy-state/last_good_tag"
    )

    idx_while = text.index("while true")
    idx_pre_good_init = text.index("pre_good=")
    assert idx_pre_good_init < idx_while, (
        "pre_good's baseline capture must happen before the retry loop starts, not "
        "inside it — otherwise it can't represent the pre-run state"
    )

    idx_init = text.index("had_transport_failure=0")
    idx_rc3 = text.index('"$rc" -eq 3')
    idx_rc_ne_255 = text.index('"$rc" -ne 255')
    idx_set = text.index("had_transport_failure=1")
    assert idx_init < idx_rc3, "had_transport_failure must be initialised before the loop"
    assert idx_rc_ne_255 < idx_set, "had_transport_failure=1 must be set on the confirmed-255 path"

    # regression guard: deferred (busy_deferred) must still exist as the fallback.
    assert "busy_deferred=true" in text, (
        "rc=3 must still be able to fall through to deferred when the recheck "
        "doesn't confirm success"
    )

    # the success-promotion if-condition must require all three: the current
    # recheck value matches D3_RELEASE_TAG, pre_good was different beforehand (i.e.
    # it actually changed during this run), and pre_good was obtainable at all.
    idx_remote_good_if = text.index('"$remote_good"')
    line_start = text.rindex("\n", 0, idx_remote_good_if) + 1
    line_end = text.index("\n", idx_remote_good_if)
    promotion_line = text[line_start:line_end]
    assert "D3_RELEASE_TAG" in promotion_line
    assert "pre_good" in promotion_line, (
        "the success-promotion if-condition must also reference pre_good, not just "
        "the current recheck value — otherwise a historical same-value coincidence "
        "is indistinguishable from an in-run success"
    )
    assert "__unknown__" in promotion_line, (
        "the success-promotion if-condition must refuse to promote to success when "
        "the pre-loop baseline itself couldn't be fetched (ssh failure)"
    )


def test_release_checkouts_do_not_persist_credentials_and_ci_templates_leaves_build_context():
    # P1-B (codex review) parity with build-deploy.yml: actions/checkout
    # defaults to persist-credentials: true (writes the CI_TEMPLATES_PAT into
    # .git/config), and .ci-templates sits inside this job's build context —
    # a caller Dockerfile's `COPY . .` would bake the PAT and the private
    # ci-templates source into the image pushed to ACR. Both checkouts must
    # disable credential persistence, and ci-templates must move out of the
    # build context before any build step runs, with all later references
    # (normalize_release.py, push_to_acr.sh, push_alias_to_acr.sh,
    # release_deploy.sh) following it to $RUNNER_TEMP.
    text = WORKFLOW.read_text()
    assert text.count("persist-credentials: false") >= 2, (
        "both the caller-repo and ci-templates checkout steps must set "
        "persist-credentials: false"
    )
    assert 'mv .ci-templates "$RUNNER_TEMP/ci-templates"' in text, (
        "ci-templates must be moved out of the build_context (.) after checkout"
    )
    assert ".ci-templates/scripts/" not in text, (
        "no step may reference ci-templates scripts at their original "
        "in-build-context path once the relocation step exists — every "
        "reference must go through $RUNNER_TEMP/ci-templates/scripts/"
    )
    for script in (
        "normalize_release.py",
        "push_to_acr.sh",
        "push_alias_to_acr.sh",
        "release_deploy.sh",
    ):
        assert f"$RUNNER_TEMP/ci-templates/scripts/{script}" in text, (
            f"{script} must be invoked from $RUNNER_TEMP/ci-templates/scripts/"
        )

    idx_mv = text.index("mv .ci-templates")
    idx_normalize = text.index("normalize_release.py")
    assert idx_mv < idx_normalize, (
        "ci-templates must be relocated before the first step that uses its scripts"
    )


def test_rc3_recheck_cat_escapes_deploy_dir():
    # P1-C (codex review) parity with build-deploy.yml: the baseline
    # (pre_good) and recheck (remote_good) `ssh ... "cat '${DEPLOY_DIR}/...'"`
    # calls interpolate DEPLOY_DIR raw inside single quotes. Unlike this
    # lane's main deploy command (already `printf %q`-escaped end to end),
    # these two recheck calls were added in a later round and missed it — a
    # DEPLOY_DIR containing a single quote breaks the remote shell's quoting.
    text = WORKFLOW.read_text()
    assert "cat '${DEPLOY_DIR}/" not in text, (
        "recheck cat calls must not interpolate DEPLOY_DIR raw inside single "
        "quotes — %q-escape it first, matching the main deploy command's style"
    )
    assert text.count("cat ${_qdir}/.deploy-state/release/last_good_sha") == 2, (
        "both the pre_good baseline and remote_good recheck cat calls must use "
        "the %q-escaped DEPLOY_DIR"
    )


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
