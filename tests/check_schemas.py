"""用主程序自己的 schema 模型校验 webui.json、_manifest.json 与组件声明。

需要主程序源码和它的依赖（pydantic）。源码位置用环境变量给出：
MAIBOT_ROOT=/path/to/MaiBot python tests/check_schemas.py
"""

import sys
from pathlib import Path

import os

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def maibot_root() -> Path:
    """主程序源码根：环境变量优先，其次插件目录的同级常见位置。"""
    candidates = [
        os.environ.get("MAIBOT_ROOT", ""),
        os.environ.get("MAIBOT_SRC", ""),
        str(PLUGIN_ROOT.parent / "MaiBot-1.3.15"),
        str(PLUGIN_ROOT.parent / "MaiBot"),
        str(PLUGIN_ROOT.parent.parent / "MaiBot"),
    ]
    for raw in candidates:
        if not raw:
            continue
        root = Path(raw).expanduser()
        if (root / "src" / "plugin_runtime").is_dir():
            return root
    raise SystemExit(
        "找不到 MaiBot 源码（需要 src/plugin_runtime 目录）。用 MAIBOT_ROOT=/path/to/MaiBot 指定；"
        "只有这一项校验依赖主程序源码，其余离线用例不需要。"
    )


sys.path.insert(0, str(maibot_root()))

from src.plugin_runtime.webui_schema import load_webui_extension  # noqa: E402

extension = load_webui_extension(str(PLUGIN_ROOT))
assert extension is not None, "webui.json 未被读取"
print("[ok] webui.json 通过 Host schema 校验")
print("     schema_version =", extension.schema_version, "| required_capabilities =", extension.required_capabilities)
for page in extension.pages:
    print(f"     page id={page.id} mode={page.mode} entry={page.entry} document_bytes={len(page.document.encode('utf-8'))}")
    print(f"     queries={list(page.queries)} actions={list(page.actions)}")

import json  # noqa: E402
from src.plugin_runtime.runner.manifest_validator import PluginManifest  # noqa: E402

raw = json.loads((PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))
manifest = PluginManifest.model_validate(raw)
print("[ok] _manifest.json 通过 Host manifest 模型校验")
print("     id =", manifest.id, "| version =", manifest.version, "| capabilities =", manifest.capabilities)

# 组件声明与 webui.json 的 API 绑定必须一一对应
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("mai_rss_plugin", PLUGIN_ROOT / "plugin.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
components = module.MaiRssPlugin().get_components()
print("[ok] 组件样例:", json.dumps(components[0], ensure_ascii=False)[:400])
declared = {(str(item.get("type") or ""), str(item.get("name"))) for item in components}
print("[ok] 组件声明:", sorted(declared))

page = extension.pages[0]
bound = {binding.api for binding in page.queries.values()} | {binding.api for binding in page.actions.values()}
api_names = {name for kind, name in declared if kind.lower() == "api"}
missing = bound - api_names
assert not missing, f"webui.json 绑定了未声明的 API: {missing}"
print("[ok] 页面绑定的 API 全部已声明:", sorted(bound))

# Command 参数键（Host 注入 stream_id/user_id/matched_groups/is_local_operator）
command_names = {name for kind, name in declared if kind.lower() == "command"}
assert "rss" in command_names, command_names
handler = module.MaiRssPlugin.handle_rss
print("[ok] Command 形参:", list(handler.__annotations__) if hasattr(handler, "__annotations__") else "-")
print("\nALL SCHEMA CHECKS PASSED")
