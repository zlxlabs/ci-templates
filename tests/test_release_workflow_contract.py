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


def test_release_runner_input_and_hosted_fallback_contract():
    raw, trigger = load()
    runner = trigger["workflow_call"]["inputs"]["runner"]
    assert runner["type"] == "string"
    assert runner["required"] is False
    assert runner["default"] == "self"
    description = runner["description"]
    assert "VM201" in description
    assert "self-hosted/linux/x64/codex" in description
    assert "hosted" in description
    assert "ubuntu-latest" in description

    expected_runs_on = (
        "${{ inputs.runner == 'self' && "
        "fromJSON('[\"self-hosted\",\"linux\",\"x64\",\"codex\"]') || "
        "fromJSON('[\"ubuntu-latest\"]') }}"
    )
    assert raw["jobs"]["release"]["runs-on"] == expected_runs_on


def test_release_tailscale_connects_only_for_hosted_runner():
    raw, _ = load()
    tailscale = next(
        step for step in raw["jobs"]["release"]["steps"]
        if step.get("name") == "Connect to Tailscale"
    )
    assert tailscale["if"] == "inputs.runner == 'hosted'"


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


def test_ci_templates_checkout_uses_org_repo_path_not_legacy_personal_path():
    # 2026-07-24 parity with build-deploy.yml: the repo's real address is
    # zlxlabs/ci-templates (per git remote); the legacy personal path
    # zj1123581321/ci-templates only still works via GitHub's repo-transfer
    # redirect. If that old username is ever re-registered by someone else,
    # this checkout step would pull attacker-controlled code while the job
    # already holds 6 deploy secrets. Pin to the org path, not the redirect.
    text = WORKFLOW.read_text()
    assert "zj1123581321" not in text, "legacy personal repo path must not reappear"
    assert "repository: zlxlabs/ci-templates" in text


def test_ci_templates_checkout_does_not_pass_dead_pat_token():
    # 2026-07-24 parity with build-deploy.yml: ci-templates is a public repo
    # now (gh api confirms visibility=public; the legacy personal path is a
    # redirect onto the same repo id). CI_TEMPLATES_PAT went dead when the
    # repo moved to the org -- a request bearing an invalid token gets a 401
    # from GitHub even for a public repo, and git falls back to an
    # interactive username prompt, which fails non-interactively (canary run
    # 30094192249: "could not read Username ... terminal prompts disabled").
    # A public repo checkout needs no token at all; the default github.token
    # is enough. CI_TEMPLATES_PAT stays declared in the workflow_call secrets
    # contract (the whole fleet's callers still pass it explicitly, see
    # test_release_workflow_contract_and_six_secrets) -- only this step stops
    # consuming it. Dropping it from the contract is a v2 decision (see
    # docs/BACKLOG.md).
    text = WORKFLOW.read_text()
    assert "token: ${{ secrets.CI_TEMPLATES_PAT }}" not in text, (
        "the ci-templates checkout step must not pass the dead PAT token — "
        "the repo is public now, the default github.token is enough"
    )
    assert "CI_TEMPLATES_PAT" in text


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


def test_release_rc3_after_prior_transport_failure_defers_with_uncertainty_warning():
    # 2026-07-24 subtractive refactor (mirrors build-deploy.yml): dropped the
    # post-255 auto-promotion machine -- pre_good baseline capture, remote_good
    # recheck of the canonical last_good_release, the __unknown__ sentinel, and
    # the three-way promotion condition (hardened across R5/R6/P2 review
    # rounds). The proof conditions themselves (historical same-value false
    # positives, unobtainable-baseline semantics, a rollback-manifest race) kept
    # getting punched full of holes across review -- upkeep outweighed the
    # value. The honest semantics now: once this run has seen a 255, remote
    # state is inherently uncertain, so rc=3 always defers; the only thing that
    # changes is an extra "state uncertain" warning, never a promotion to
    # success.
    text = WORKFLOW.read_text()

    # the auto-promotion machine must be gone entirely -- no baseline/recheck
    # variables, no unknown-sentinel, no read of the canonical commit point.
    assert "pre_good" not in text, "post-255 baseline capture must be gone"
    assert "remote_good" not in text, "post-255 remote recheck must be gone"
    assert "__unknown__" not in text, "the baseline-unknown sentinel must be gone"
    assert ".deploy-state/release/last_good_release" not in text, (
        "the rc=3 recheck reading the canonical last_good_release must be gone"
    )
    assert "_qdir" not in text, (
        "the %q-escaped DEPLOY_DIR token that only served the deleted recheck "
        "must be gone too (deploy_once()'s own %q-escaped remote_cmd/"
        "cleanup_remote_cmd variables are unrelated and must remain untouched)"
    )

    # had_transport_failure tracking is preserved: initialised before the loop,
    # set only once rc==255 is confirmed. This is the signal the honest warning
    # still depends on -- it is not part of what got torn out.
    idx_init = text.index("had_transport_failure=0")
    idx_rc3 = text.index('"$rc" -eq 3')
    idx_rc_ne_255 = text.index('"$rc" -ne 255')
    idx_set = text.index("had_transport_failure=1")
    assert idx_init < idx_rc3, "had_transport_failure must be initialised before the loop"
    assert idx_rc_ne_255 < idx_set, "had_transport_failure=1 must be set on the confirmed-255 path"

    # the honest warning is the core of the new semantics: say "uncertain"
    # instead of quietly promoting to success.
    assert (
        "::warning::transport was interrupted earlier in this run; remote state "
        "may have advanced beyond what 'deferred' implies" in text
    ), "the honest uncertainty warning must remain after dropping auto-promotion"

    # regression guard: deferred (busy_deferred) must still exist as the (only)
    # outcome.
    assert "busy_deferred=true" in text, "rc=3 must still fall through to deferred"


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


def test_run_steps_do_not_directly_expand_inputs():
    # P1-A (codex review round 5) parity with build-deploy.yml: GitHub Actions
    # substitutes ${{ }} expressions into `run:` text via plain textual
    # replacement before the shell ever sees it -- not shell variable
    # injection, a text splice. If the substituted value contains a newline,
    # whatever follows the substitution point on that line (even inside a
    # shell comment) becomes a new, literally-executed shell command, and by
    # this point in the job the runner already holds ACR/SSH credentials.
    # `inputs.*` values must be routed through the step's `env:` mapping and
    # referenced as "$VAR" instead of being spliced directly into run: text.
    #
    # This lane was already clean of `${{ secrets. }}` splices in run: text
    # (secrets go through env: throughout) -- the assertion below is a
    # regression guard added for parity with build-deploy.yml's equivalent
    # test, not a fix.
    raw, _ = load()
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
    # P1-B (codex review round 5) parity with build-deploy.yml: an ssh_user
    # starting with '-' makes the assembled "${SSH_USER}@${DEPLOY_HOST}:path"
    # argument itself start with '-', which scp/ssh parse as a command-line
    # option rather than a user@host target -- double-quoting does not stop
    # this, since the problem is scp/ssh's own option parsing. Both values
    # must be regex-validated before their first use in deploy_once().
    text = WORKFLOW.read_text()
    assert 'SSH_USER" =~' in text, (
        "ssh_user must be regex-validated against a safe charset before use"
    )
    assert 'DEPLOY_HOST" =~' in text, (
        "host must be regex-validated against a safe charset before use"
    )


def test_orphaned_remote_files_cleaned_up_on_scp_failure():
    # P2-B (codex review round 5): remote_manifest/remote_script are local
    # variables inside deploy_once(), keyed by transport_attempt/
    # transport_nonce, so every retry uses a fresh nonce and never overwrites
    # or reclaims a prior failed attempt's files. If either scp fails, the
    # remote script never gets a chance to run at all -- its own cleanup trap
    # never installs -- so nothing else will ever remove those /tmp paths.
    # A best-effort ssh rm -f cleanup, covering both remote_manifest and
    # remote_script, must be reachable from both scp failure points inside
    # deploy_once().
    text = WORKFLOW.read_text()
    assert "cleanup_remote_cmd" in text, (
        "deploy_once() must build a reusable best-effort remote cleanup command"
    )
    assert "rm -f -- %q %q" in text, (
        "the cleanup command must %q-escape both remote paths, matching this "
        "function's existing printf %q discipline"
    )
    assert text.count('"$cleanup_remote_cmd"') == 2, (
        "the cleanup command must be invoked at exactly the two scp "
        "transport-failure points inside deploy_once(): the first scp and "
        "the second scp -- and nowhere else (see "
        "test_ssh_255_does_not_race_remote_rollback_cleanup for why the "
        "ssh_rc==255 branch must NOT also invoke it)"
    )


def test_ssh_255_does_not_race_remote_rollback_cleanup():
    # Codex review round 7 (P2-B-2): round 5's fix (e1688ba) also invoked
    # cleanup_remote_cmd from the ssh_rc==255 branch, reasoning that ssh
    # transport failure meant the remote script "never got a chance to run".
    # That premise is wrong for this branch specifically: both scp calls
    # already succeeded by the time ssh_rc is checked, so release_deploy.sh
    # was shipped and launched. rc=255 only means this SSH connection lost
    # the transport before the remote exit status made it back -- if that
    # happens while the remote script is mid-flight, OpenSSH sends it SIGHUP,
    # which release_deploy.sh traps (on_signal()/PENDING_SIGNAL) instead of
    # dying on, taking a rollback-without-promotion path that re-reads
    # $RELEASE_MANIFEST to compare image sets (the R4 guard) before touching
    # anything. Deleting remote_manifest from the workflow side right after
    # ssh_rc==255 races that read: if the rm -f wins, load_manifest sees a
    # missing file, the R4 guard reads that as an image-set change, and the
    # remote script refuses to roll back at all -- worse than the orphaned
    # /tmp file this cleanup was meant to prevent. So this branch must NOT
    # invoke the cleanup; only release_deploy.sh's own EXIT trap may remove
    # these two files once the script has actually been shipped.
    text = WORKFLOW.read_text()
    start = text.index('local ssh_rc=$?')
    end = text.index('return "$ssh_rc"\n          }', start)
    branch_text = text[start:end]
    assert "cleanup_remote_cmd" not in branch_text, (
        "the code path after `local ssh_rc=$?` (covering the ssh_rc==255 "
        "case) must not invoke cleanup_remote_cmd -- doing so races the "
        "remote script's own HUP-triggered rollback, which needs "
        "$RELEASE_MANIFEST to still exist to compare image sets before it "
        "will roll back"
    )
    assert 'if [[ "$ssh_rc" -eq 255 ]]; then' not in text, (
        "the ssh_rc==255 special case must be removed entirely, not just "
        "its cleanup call -- deploy_once() should fall through to a bare "
        "`return \"$ssh_rc\"` for every ssh outcome"
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


def test_release_rc4_output_and_notification_routing_contract():
    raw, _ = load()
    steps = raw["jobs"]["release"]["steps"]
    text = WORKFLOW.read_text()

    output = 'echo "rollback_unhealthy=true" >> "$GITHUB_OUTPUT"'
    idx_non255 = text.index('if [[ "$rc" -ne 255 ]]; then')
    idx_output = text.index(output, idx_non255)
    idx_exit = text.index('exit "$rc"', idx_non255)
    assert idx_non255 < idx_output < idx_exit
    assert text.count(output) == 1

    busy = next(step for step in steps if step.get("name") == "Feishu release busy defer card (yellow, fail-open)")
    urgent = next(step for step in steps if step.get("name") == "Feishu release rollback unhealthy card (urgent, fail-open)")
    ordinary = next(step for step in steps if step.get("name") == "Feishu release failure card (P0, fail-open)")
    assert busy["if"] == "failure() && steps.deploy.outputs.busy_deferred == 'true'"
    assert urgent["if"] == (
        "failure() && steps.deploy.outputs.busy_deferred != 'true' && "
        "steps.deploy.outputs.rollback_unhealthy == 'true'"
    )
    assert ordinary["if"] == (
        "failure() && steps.deploy.outputs.busy_deferred != 'true' && "
        "steps.deploy.outputs.rollback_unhealthy != 'true'"
    )
    assert "production may be unavailable" in urgent["run"]
    assert "immediate host intervention required" in urgent["run"]
    assert "生产可能不可用，必须立即上机" in urgent["run"]
    assert '"msg_type":"text"' in urgent["run"]
    assert "<at id=all></at>" in urgent["run"]
