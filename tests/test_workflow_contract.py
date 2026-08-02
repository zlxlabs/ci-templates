"""T3: static contract checks on the reusable build-deploy.yml.

eng-review A4 (codex#5): the workflow must declare secrets EXPLICITLY and must
NOT use `secrets: inherit`, so a compromised ci-templates can never reach
unrelated org secrets. Also asserts the per-host concurrency group (A3).
"""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-deploy.yml"

EXPECTED_SECRETS = {
    "ACR_USERNAME", "ACR_PASSWORD", "SSH_DEPLOY_KEY", "KNOWN_HOSTS",
    "TS_AUTHKEY",          # runner 入 Tailscale 连内网目标机
    "CI_TEMPLATES_PAT",    # 只读 PAT,checkout private ci-templates 的部署脚本
}

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
        assert spec and spec.get("required") is True, f"{name} must be required"


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


def test_deploy_notify_title_prefix_is_repo_variable_with_default():
    text = WORKFLOW.read_text()

    assert "FEISHU_TITLE_PREFIX" in text
    assert "vars.FEISHU_CI_TITLE_PREFIX" in text
    assert "[zlxlabs·CI]" in text
    assert "f\"🔴 {title_prefix} P0 部署失败" in text


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


def test_post_deploy_image_reconciliation_is_success_only_and_checks_all_layers():
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
    assert reconcile["if"] == "success() && steps.deploy.outputs.deferred != 'true'", (
        "reconciliation must only run after a real deploy success; deferred and "
        "deploy failures must skip it"
    )

    run = reconcile["run"]
    assert 'docker image inspect "${ACR_IMAGE}:${GIT_SHA}"' in run
    assert 'docker image inspect "${IMAGE_NAME}:latest"' in run
    assert 'docker compose ps -q --status running' in run
    assert 'docker inspect "$container_id" --format' in run
    assert "expected_id" in run and "latest_id" in run and "running_ids" in run
    assert "::error::" in run
    assert "SSH transport failed after ${max_attempts} attempts" in run
    assert "ServerAliveInterval" in run and "ServerAliveCountMax" in run


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


def test_yellow_card_step_exists_for_deferred_without_at_all():
    text = WORKFLOW.read_text()
    assert "deferred == 'true'" in text
    assert "部署延期" in text

    # 定位黄卡那一段(从"部署延期"关键词往后切),只在这一段里断言不含 <at id=all>;
    # 红卡那段仍然应该含 <at id=all>,不能用"全文不含"这种粗暴断言。
    idx = text.index("部署延期")
    yellow_section = text[idx:]
    assert "<at id=all>" not in yellow_section

    red_idx = text.index("Feishu 部署失败卡")
    # 红卡段落(该 step 起,到黄卡关键词出现前)仍然要 @全员
    red_section = text[red_idx:idx]
    assert "<at id=all>" in red_section


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
