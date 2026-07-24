"""T4: examples/*.yml 必须与 build-deploy.yml 的真实接口对齐。

D3 激活后 caller 接口变了(host=Tailscale IP、加 ssh_user、6 个 secret)。
examples 是用户照抄的范例 —— 漏一个就让新服务"生成即部署不了"。这里把接口
契约钉死,例子漂移就 fail。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
NORMALIZE_RELEASE_SCRIPT = REPO_ROOT / "scripts" / "normalize_release.py"

# build-deploy.yml / build-deploy-release.yml 都显式声明这同一组 6 个 secret ——
# caller 必须逐个传齐,两条 lane 共用。
EXPECTED_SECRETS = {
    "ACR_USERNAME", "ACR_PASSWORD", "SSH_DEPLOY_KEY", "KNOWN_HOSTS",
    "TS_AUTHKEY", "CI_TEMPLATES_PAT",
}
TS_IP = re.compile(r"^100\.\d{1,3}\.\d{1,3}\.\d{1,3}$")  # Tailscale CGNAT 段


def _ship_with(path: Path):
    raw = yaml.safe_load(path.read_text())
    return raw["jobs"]["ship"]["with"], raw["jobs"]["ship"]["secrets"]


def test_caller_pins_v1_canary_pins_main():
    caller = yaml.safe_load((EXAMPLES / "caller-workflow.yml").read_text())
    canary = yaml.safe_load((EXAMPLES / "canary-workflow.yml").read_text())
    assert caller["jobs"]["ship"]["uses"].endswith("build-deploy.yml@v1")
    assert canary["jobs"]["ship"]["uses"].endswith("build-deploy.yml@main")


def test_examples_pass_all_six_secrets():
    for name in ("caller-workflow.yml", "canary-workflow.yml"):
        # secrets 解析成 6 键的 mapping 本身就排除了 `secrets: inherit`
        # (那样 yaml 会得到字符串 "inherit",.keys() 直接炸)。
        _, secrets = _ship_with(EXAMPLES / name)
        assert set(secrets.keys()) == EXPECTED_SECRETS, name


def test_examples_host_is_tailscale_ip_not_alias():
    # runner 没有 ~/.ssh/config,host 必须是 tailnet 可达地址,不能是别名(如 host-1)
    for name in ("caller-workflow.yml", "canary-workflow.yml"):
        with_, _ = _ship_with(EXAMPLES / name)
        assert TS_IP.match(str(with_["host"])), f"{name}: host 应是 Tailscale IP"
        assert with_["host"] != "host-1", name


def test_examples_pass_ssh_user():
    for name in ("caller-workflow.yml", "canary-workflow.yml"):
        with_, _ = _ship_with(EXAMPLES / name)
        assert with_.get("ssh_user"), f"{name}: 缺 ssh_user"


def test_examples_use_org_repo_path_not_legacy_personal_path():
    # 2026-07-24:本仓真实地址是 zlxlabs/ci-templates(git remote 可证)。旧个人
    # 路径 zj1123581321/ci-templates 目前只靠 GitHub 仓库转移重定向才能工作——
    # examples 是用户照抄的范例,写着旧路径就会把这个劫持面复制进每一个新服务仓。
    for name in ("caller-workflow.yml", "canary-workflow.yml", "release-caller-workflow.yml"):
        text = (EXAMPLES / name).read_text()
        assert "zj1123581321" not in text, f"{name}: legacy personal repo path must not reappear"
        assert "zlxlabs/ci-templates" in text, f"{name}: uses: must reference the org repo path"


# --- release-caller-workflow.yml: build-deploy-release.yml 的多镜像 caller 范例 ---


def _release_ship_with():
    raw = yaml.safe_load((EXAMPLES / "release-caller-workflow.yml").read_text())
    return raw["jobs"]["ship"]["with"], raw["jobs"]["ship"]["secrets"]


def test_release_example_uses_release_lane_pinned_to_v1():
    with_, _ = _release_ship_with()
    raw = yaml.safe_load((EXAMPLES / "release-caller-workflow.yml").read_text())
    assert raw["jobs"]["ship"]["uses"].endswith("build-deploy-release.yml@v1"), (
        "release example must call build-deploy-release.yml, pinned @v1 like the "
        "single-image caller (only the canary rides @main)"
    )


def test_release_example_passes_all_six_secrets():
    _, secrets = _release_ship_with()
    assert set(secrets.keys()) == EXPECTED_SECRETS


def test_release_example_host_is_tailscale_ip_not_alias():
    with_, _ = _release_ship_with()
    assert TS_IP.match(str(with_["host"])), "release example: host 应是 Tailscale IP"
    assert with_["host"] != "host-1"


def test_release_example_images_and_probes_json_are_valid_json_not_just_strings():
    # 不满足于"看起来像 JSON"的字符串断言:解析后再校验结构,并且直接喂给
    # build-deploy-release.yml 实际调用的同一个 normalize_release.py,证明这两个
    # JSON 值真的能通过 release lane 的校验(image_name 字符集、build_context/
    # dockerfile 相对路径规则、probe URL 语法等),而不是碰巧长得合法。
    with_, _ = _release_ship_with()
    images_json = with_["images_json"]
    probes_json = with_["probes_json"]
    assert isinstance(images_json, str) and isinstance(probes_json, str)

    images = json.loads(images_json)
    probes = json.loads(probes_json)

    # web_transcibe_translate 原型:backend/ + frontend/ 各一个 Dockerfile。
    assert isinstance(images, list) and len(images) == 2
    assert {item["build_context"] for item in images} == {"backend", "frontend"}
    for item in images:
        assert set(item) >= {"image_name", "build_context", "dockerfile"}

    # 前端入口 + 后端 API,至少各一条探针。
    assert isinstance(probes, list) and len(probes) == 2
    for probe in probes:
        assert probe["url"].startswith(("http://", "https://"))

    tmp_dir = REPO_ROOT / ".pytest_cache"
    tmp_dir.mkdir(exist_ok=True)
    manifest = tmp_dir / "release-example.manifest"
    builds = tmp_dir / "release-example.builds"
    try:
        result = subprocess.run(
            [
                sys.executable, str(NORMALIZE_RELEASE_SCRIPT),
                "--images-json", images_json,
                "--probes-json", probes_json,
                "--manifest-out", str(manifest),
                "--builds-out", str(builds),
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"release example images_json/probes_json must pass the real "
            f"normalize_release.py validator: {result.stderr}"
        )
    finally:
        manifest.unlink(missing_ok=True)
        builds.unlink(missing_ok=True)


def test_release_example_busy_lock_file_is_commented_optional():
    # 门禁是 opt-in:范例里应该有一行注释掉的 busy_lock_file,展示用法但不默认开启。
    text = (EXAMPLES / "release-caller-workflow.yml").read_text()
    assert "# busy_lock_file:" in text


def test_release_example_documents_immutable_compose_tag_requirement():
    # compose 必须用 ${D3_RELEASE_TAG:?...} 这类不可变发布变量,范例要把这个前提
    # 写清楚,否则用户照抄后 compose 不会真的按 SHA 切换镜像。
    text = (EXAMPLES / "release-caller-workflow.yml").read_text()
    assert "D3_RELEASE_TAG" in text
