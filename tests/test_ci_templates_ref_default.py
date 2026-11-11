"""#82: build-deploy.yml 的 ci_templates_ref 默认值必须跟随 workflow 自己的 major。

默认值曾是 "v1"，而全部稳定 caller 用 @v2 引用本 workflow——caller 漏传参数时
静默 checkout 出 v1 的部署脚本，两边版本错配且不报错。本文件独立于
test_workflow_contract.py（该文件已过 800 行 ratchet，新代码放同目录新文件）。
"""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-deploy.yml"


def test_ci_templates_ref_default_tracks_workflow_major():
    """在 base(5a18e34, 默认 "v1") 上本测试按设计为红。"""
    # PyYAML parses the `on:` key as boolean True — load and normalise.
    raw = yaml.safe_load(WORKFLOW.read_text())
    trigger = raw.get("on", raw.get(True))
    spec = trigger["workflow_call"]["inputs"]["ci_templates_ref"]
    assert spec["type"] == "string"
    assert spec["required"] is not True
    assert spec["default"] == "v2"
