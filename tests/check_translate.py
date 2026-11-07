"""翻译相关离线用例：提示词与解析、缓存、模型目录、两条通道、命令与 WebUI。"""

import asyncio
import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("mai_rss_plugin", Path(__file__).resolve().parents[1] / "plugin.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

_MODEL_TOML = """
[api_providers.DeepSeek]
name = "DeepSeek"
base_url = "https://api.deepseek.com"
api_key = "sk-should-never-appear-in-any-output"

[[models]]
model_type = "llm"
model_identifier = "deepseek-v4-flash"
name = "DeepSeek/deepseek-v4-flash"
api_provider = "DeepSeek"

[[models]]
model_type = "llm"
model_identifier = "deepseek-v4-pro"
name = "硅基流动/DeepSeek-V4.1-Flash"
api_provider = "SiliconFlow"

[[models]]
model_type = "embedding"
model_identifier = "Qwen/Qwen3-Embedding-0.6B"
name = "硅基流动/Qwen3-Embedding-0.6B"
api_provider = "SiliconFlow"

[[models]]
model_type = "image_embedding"
model_identifier = "some-vl"
name = "硅基流动/Qwen3-VL-Embedding-8B"
api_provider = "SiliconFlow"

[[models]]
model_type = "llm"
model_identifier = "no-provider-model"
name = "未分组模型"
"""


class FakeSend:
    def __init__(self):
        self.sent = []

    async def text(self, text, stream_id, **kwargs):
        self.sent.append((stream_id, text))
        return True


class FakeConfig:
    async def get(self, key, default=None):
        return ["qq:10001"] if key == "plugin.permission" else default


class FakeChat:
    async def get_all_streams(self, platform="qq"):
        return []


class FakeLLM:
    """主程序模型通道替身：可注入返回内容、原始响应与异常。"""

    def __init__(self, response="T|标题译文\nB|正文译文", error=None, raw=None):
        self.calls = []
        self.response = response
        self.error = error
        self.raw = raw
        self.tasks = ["utils", "replyer", "fast_model"]

    async def generate(self, prompt="", model="", temperature=None, max_tokens=None, **kwargs):
        self.calls.append({"prompt": prompt, "model": model, **kwargs})
        if self.error:
            raise self.error
        if self.raw is not None:
            return self.raw
        return {"success": True, "response": self.response, "model_name": "fake"}

    async def get_available_models(self):
        return list(self.tasks)


class _Log:
    def __init__(self):
        self.messages = []

    def _record(self, level, text, *args):
        self.messages.append(f"{level} {text % args if args else text}")

    def info(self, text, *args, **kw):
        self._record("info", text, *args)

    def warning(self, text, *args, **kw):
        self._record("warning", text, *args)

    def error(self, text, *args, **kw):
        self._record("error", text, *args)
        print("[plugin-error]", text, *args)


class FakePaths:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.runtime_dir = Path(data_dir) / "runtime"


class FakeCtx:
    def __init__(self, data_dir, llm=None):
        self.send = FakeSend()
        self.config = FakeConfig()
        self.chat = FakeChat()
        self.llm = llm if llm is not None else FakeLLM()
        self.logger = _Log()
        self.paths = FakePaths(data_dir)


def make_env(translate=None, llm=None, with_catalog=True):
    """搭一个临时数据目录：data/plugins/<id> 往上三级就是主程序根目录。"""
    root = tempfile.mkdtemp()
    data_dir = Path(root) / "data" / "plugins" / "bluemika.mai-rss"
    data_dir.mkdir(parents=True)
    if with_catalog:
        config_dir = Path(root) / "config"
        config_dir.mkdir()
        (config_dir / "model_config.toml").write_text(_MODEL_TOML, encoding="utf-8")
    plugin = mod.MaiRssPlugin()
    plugin._set_context(FakeCtx(str(data_dir), llm))
    payload = {
        "plugin": {"enabled": True, "config_version": "1.1.0"},
        "push": {"send_delay_seconds": 0},
        "permission": {"admin_only": True, "admins": ["qq:10001"]},
        "translate": translate or {},
    }
    plugin.set_plugin_config(payload)
    return plugin, root


def item(title="Incident with Actions and Pages", text="Investigating - degraded performance", **kwargs):
    entry = {"title": title, "text": text, "link": "https://example.com/i", "published": 1780000000.0, "key": "k1"}
    entry.update(kwargs)
    return entry


# ============================================================================
# 纯函数
# ============================================================================

assert mod.looks_chinese("Investigating - degraded performance") is False
assert mod.looks_chinese("我们在调查性能下降") is True
assert mod.looks_chinese("") is False
# 只有零星一两个汉字（比如源名里带的）不足以判定为中文原文
assert mod.looks_chinese("标题 https://a.example/feed #2") is False
assert mod.looks_chinese("Nov 4, 16:05 UTC 调查中 - Actions 与 Pages") is True
print("[ok] looks_chinese 占比阈值")

prompt = mod.build_translate_prompt("标题原文", "正文原文", "中文", True)
assert "T|" in prompt and "B|" in prompt and "标题：标题原文" in prompt and "正文：正文原文" in prompt, prompt
assert "正文：" not in mod.build_translate_prompt("标题原文", "正文原文", "中文", False)
assert mod.build_translate_prompt("", "正文原文", "", True).count("B|") >= 1
print("[ok] build_translate_prompt 按需要求 T|/B| 行")

assert mod.parse_translate_reply("T|甲\nB|乙", True, True) == ("甲", "乙")
assert mod.parse_translate_reply("标题：甲\n正文：乙", True, True) == ("甲", "乙")
assert mod.parse_translate_reply("T|甲\nB|乙", True, False) == ("甲", "")
assert mod.parse_translate_reply("T|甲", True, True) == ("甲", "")
assert mod.parse_translate_reply("只有一行没标记", True, True) == ("只有一行没标记", "")
assert mod.parse_translate_reply("", True, True) == ("", "")
assert mod.parse_translate_reply("随便一句无关的话\n又一句", False, True) == ("", "")
print("[ok] parse_translate_reply 标记、缺行与兜底")

cache = mod.TranslateCache()
assert cache.get("nope") is None
cache.put("k", "甲", "乙")
assert cache.get("k") == ("甲", "乙")
assert len(cache) == 1
cache_dir = tempfile.mkdtemp()
cache.bind(cache_dir)
cache.put("k2", "丙", "")
cache.save()
reloaded = mod.TranslateCache()
reloaded.bind(cache_dir)
reloaded.load()
assert reloaded.get("k2") == ("丙", "") and reloaded.get("missing") is None
for index in range(mod._TRANSLATE_CACHE_LIMIT + 50):
    cache.put(f"bulk{index}", "x", "")
assert len(cache) == mod._TRANSLATE_CACHE_LIMIT and cache.get("bulk0") is None
print("[ok] TranslateCache 落盘读回与上限淘汰")

assert mod.translate_cache_key("中文", "maibot:A", "t", "b") != mod.translate_cache_key("英文", "maibot:A", "t", "b")
assert mod.translate_cache_key("中文", "maibot:A", "t", "b") != mod.translate_cache_key("中文", "maibot:B", "t", "b")
assert mod.translate_cache_key("中文", "maibot:A", "t", "b") == mod.translate_cache_key("中文", "maibot:A", "t", "b")
print("[ok] 缓存键随语言/模型变化")

picked = mod.filter_llm_models(
    [
        {"model_type": "llm", "name": "A/a", "api_provider": "A", "model_identifier": "a"},
        {"model_type": "embedding", "name": "E/e", "api_provider": "A", "model_identifier": "e"},
        {"model_type": "llm", "name": "A/a", "api_provider": "A", "model_identifier": "a2"},
        {"model_type": "llm", "name": "", "api_provider": "A"},
        {"model_type": "llm", "name": "B/b"},
    ]
)
assert [model["name"] for model in picked] == ["A/a", "B/b"], picked
assert picked[0]["provider"] == "A" and picked[1]["provider"] == "未分组"
assert mod.filter_llm_models(None) == []
print("[ok] filter_llm_models 滤掉嵌入模型、去重、服务商兜底")

fake_root = os.path.join(tempfile.gettempdir(), "mai-rss-catalog-root")
data_dir = os.path.join(fake_root, "data", "plugins", "bluemika.mai-rss")
candidates = mod.model_config_candidates(data_dir)
assert os.path.join(fake_root, "config", "model_config.toml") in candidates, candidates
assert len(candidates) == len(set(candidates)), candidates
assert candidates[2] == os.path.join(fake_root, "config", "model_config.toml"), candidates
assert mod.model_config_candidates(""), candidates
print(f"[ok] model_config_candidates 从数据目录上推到主程序根目录：{len(candidates)} 个候选")


async def main():
    # ---- 目录读取（走真实 TOML 解析） ----
    plugin, root = make_env()
    await plugin.on_load()
    catalog, path, error = plugin._model_catalog()
    assert not error, error
    assert Path(path).name == "model_config.toml" and str(root) in path, path
    assert [model["name"] for model in catalog] == [
        "DeepSeek/deepseek-v4-flash",
        "硅基流动/DeepSeek-V4.1-Flash",
        "未分组模型",
    ], catalog
    assert "sk-should-never-appear-in-any-output" not in json.dumps(catalog, ensure_ascii=False)
    print("[ok] 模型目录读自 model_config.toml，只含 llm，且不带密钥")

    # mtime 未变时命中缓存，不再重复解析文件
    reads = {"n": 0}
    original_reader = mod.read_model_catalog

    def counting_reader(candidates):
        reads["n"] += 1
        return original_reader(candidates)

    mod.read_model_catalog = counting_reader
    try:
        again_catalog = plugin._model_catalog()[0]
        again_path = plugin._model_catalog()[1]
    finally:
        mod.read_model_catalog = original_reader
    assert again_catalog == catalog and again_path == path
    assert reads["n"] == 0, f"mtime 没变不该重新解析：{reads['n']} 次"
    print("[ok] 模型目录按 mtime 复用缓存")

    # 文件被改过就必须重新读（用户在 WebUI 里加模型的场景）
    catalog_file = Path(path)
    catalog_file.write_text(_MODEL_TOML + '\n[[models]]\nmodel_type = "llm"\nname = "新加的模型"\n', encoding="utf-8")
    os.utime(catalog_file, (time.time() + 5, time.time() + 5))
    refreshed = plugin._model_catalog()[0]
    assert [model["name"] for model in refreshed][-1] == "新加的模型", refreshed
    print("[ok] model_config.toml 变更后自动重读")

    # ---- 通道与优先级 ----
    assert plugin._translate_enabled() is True and plugin._translate_model_name() == ""
    assert "主程序默认任务" in plugin._translate_route_text()
    plugin.store.settings["translate_model"] = "硅基流动/DeepSeek-V4.1-Flash"
    assert plugin._translate_model_name() == "硅基流动/DeepSeek-V4.1-Flash"
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "1.1.0"},
            "translate": {"model_name": "DeepSeek/deepseek-v4-flash"},
        }
    )
    assert plugin._translate_model_name() == "硅基流动/DeepSeek-V4.1-Flash", "聊天里选的必须覆盖配置里写的"
    plugin.store.settings.pop("translate_model")
    assert plugin._translate_model_name() == "DeepSeek/deepseek-v4-flash"
    plugin.set_plugin_config(
        {"plugin": {"enabled": True, "config_version": "1.1.0"}, "translate": {"task_name": "fast_model"}}
    )
    assert "任务 fast_model" in plugin._translate_route_text()
    plugin.store.settings["translate_enabled"] = False
    assert plugin._translate_enabled() is False
    plugin.store.settings.pop("translate_enabled")
    assert plugin._translate_enabled() is True
    print("[ok] 开关与模型的覆盖优先级")

    # ---- 走主程序模型：一次调用、缓存命中、失败退回 ----
    plugin, _ = make_env({"model_name": "DeepSeek/deepseek-v4-flash"})
    await plugin.on_load()
    entry = item()
    assert await plugin._translate_for(entry) == ("标题译文", "正文译文")
    assert len(plugin.ctx.llm.calls) == 1
    assert plugin.ctx.llm.calls[0]["model_name"] == "DeepSeek/deepseek-v4-flash"
    assert "task_name" not in plugin.ctx.llm.calls[0], plugin.ctx.llm.calls[0]
    assert await plugin._translate_for(entry) == ("标题译文", "正文译文")
    assert len(plugin.ctx.llm.calls) == 1, "命中缓存不该再调模型"
    assert plugin._translate_errors == 0
    print("[ok] maibot 通道：一次调用 + 译文缓存命中")

    # 任务名路由（不指定具体模型）
    plugin2, _ = make_env({"task_name": "fast_model"}, llm=FakeLLM())
    await plugin2.on_load()
    await plugin2._translate_for(item(title="Another incident", text="Details here"))
    assert plugin2.ctx.llm.calls[0].get("task_name") == "fast_model", plugin2.ctx.llm.calls[0]
    assert "model_name" not in plugin2.ctx.llm.calls[0]
    print("[ok] 只填任务名时按任务路由，不直选模型")

    # 中文原文跳过
    plugin3, _ = make_env({}, llm=FakeLLM())
    await plugin3.on_load()
    assert await plugin3._translate_for(item(title="系统更新公告", text="本次更新包含若干修复")) == ("", "")
    assert plugin3.ctx.llm.calls == []
    print("[ok] 中文原文不浪费调用")

    # 模型报错 / 输出无法解析：只记日志，不发译文
    plugin4, _ = make_env({}, llm=FakeLLM(error=RuntimeError("未找到名为 'X' 的模型")))
    await plugin4.on_load()
    assert await plugin4._translate_for(item()) == ("", "")
    assert plugin4._translate_errors == 1
    assert any("翻译失败" in line for line in plugin4.ctx.logger.messages)
    plugin5, _ = make_env({}, llm=FakeLLM(response="抱歉，我不会翻译"))
    await plugin5.on_load()
    assert await plugin5._translate_for(item(title="X", text="Y")) == ("抱歉，我不会翻译", "")
    plugin6, _ = make_env({}, llm=FakeLLM(response=""))
    await plugin6.on_load()
    assert await plugin6._translate_for(item()) == ("", "") and plugin6._translate_errors == 1
    plugin7, _ = make_env({}, llm=FakeLLM(raw={"success": False, "error": "上游 500"}))
    await plugin7.on_load()
    assert await plugin7._translate_for(item()) == ("", "") and plugin7._translate_errors == 1
    assert any("上游 500" in line for line in plugin7.ctx.logger.messages)
    plugin7b, _ = make_env({}, llm=FakeLLM(raw="不是字典"))
    await plugin7b.on_load()
    assert await plugin7b._translate_for(item()) == ("", "") and plugin7b._translate_errors == 1
    print("[ok] 模型失败/空返回/非字典响应只记日志，不影响推送")

    # 单轮预算
    plugin8, _ = make_env({"max_calls_per_round": 2})
    await plugin8.on_load()
    for index in range(4):
        await plugin8._translate_for(item(key=f"k{index}", title=f"Title {index}"))
    assert len(plugin8.ctx.llm.calls) == 2, plugin8.ctx.llm.calls
    assert any("上限" in line for line in plugin8.ctx.logger.messages)
    print("[ok] 单轮翻译预算生效")

    # 关闭后完全不调
    plugin9, _ = make_env({"enabled": False})
    await plugin9.on_load()
    assert await plugin9._translate_for(item()) == ("", "") and plugin9.ctx.llm.calls == []
    print("[ok] enabled=false 时不调模型")

    # ---- custom 通道 ----
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured.update(url=url, payload=payload, headers=headers, timeout=timeout)
        return {"choices": [{"message": {"content": "T|甲\nB|乙"}}]}

    original_post = mod.post_json
    mod.post_json = fake_post
    try:
        pluginA, _ = make_env(
            {"provider": "custom", "api_base": "https://api.example.com/v1/", "api_key": "sk-test", "custom_model": "m1"}
        )
        await pluginA.on_load()
        assert await pluginA._translate_for(item()) == ("甲", "乙")
        assert captured["url"] == "https://api.example.com/v1/chat/completions", captured["url"]
        assert captured["headers"] == {"Authorization": "Bearer sk-test"}
        assert captured["payload"]["model"] == "m1" and captured["payload"]["stream"] is False
        route = pluginA._translate_route_text()
        assert "https://api.example.com/v1/" in route and "sk-test" not in route, route
        assert "sk-test" not in json.dumps(await pluginA.webui_translate_options(), ensure_ascii=False)
        print("[ok] custom 通道：端点、鉴权头与模型标识符，且不外泄密钥")

        pluginB, _ = make_env({"provider": "custom", "api_base": "javascript:alert(1)", "custom_model": "m"})
        await pluginB.on_load()
        assert await pluginB._translate_for(item()) == ("", "")
        assert any("http" in line for line in pluginB.ctx.logger.messages)
        pluginC, _ = make_env({"provider": "custom", "api_base": "https://api.example.com"})
        await pluginC.on_load()
        assert await pluginC._translate_for(item()) == ("", "")
        assert any("custom_model" in line for line in pluginC.ctx.logger.messages)
        print("[ok] custom 通道：非法协议与漏填模型都报错不推")

        mod.post_json = lambda url, payload, headers, timeout: {"error": {"message": "invalid api key"}}
        pluginD, _ = make_env(
            {"provider": "custom", "api_base": "https://api.example.com", "custom_model": "m", "api_key": "k"}
        )
        await pluginD.on_load()
        assert await pluginD._translate_for(item()) == ("", "") and pluginD._translate_errors == 1
        print("[ok] custom 通道：接口 error 字段被当作失败处理")
    finally:
        mod.post_json = original_post

    assert mod.extract_openai_text({"choices": [{"message": {"content": " 甲 "}}]}) == "甲"
    for bad in [{}, {"choices": []}, {"choices": [{"message": {}}]}]:
        try:
            mod.extract_openai_text(bad)
            raise AssertionError(f"应当抛错：{bad}")
        except ValueError:
            pass
    print("[ok] extract_openai_text 结构缺失时抛错")

    # ---- 渲染 ----
    pluginR, _ = make_env({"model_name": "DeepSeek/deepseek-v4-flash"})
    await pluginR.on_load()
    feed = pluginR.store.add_feed("github 状态", "https://a.example/feed")
    text = pluginR._render(feed, item(), await pluginR._translate_for(item()))
    assert text.splitlines()[0] == "📡 RSS/Atom 拉取 • github 状态", text
    assert "🗞 标题：Incident with Actions and Pages（标题译文）" in text, text
    assert "\n（正文译文）" in text, text
    print("[ok] 首行格式与译文括号:\n" + text)

    # ---- 命令 ----
    pluginC1, _ = make_env()
    await pluginC1.on_load()

    async def run(plugin, text, stream_id="g:123"):
        import re as _re

        match = _re.match(mod._COMMAND_PATTERN, text)
        assert match, text
        before = len(plugin.ctx.send.sent)
        await plugin.handle_rss(
            stream_id=stream_id, user_id="10001", is_local_operator=False, matched_groups=match.groupdict()
        )
        fresh = [msg for sid, msg in plugin.ctx.send.sent[before:] if sid == stream_id]
        return fresh[-1] if fresh else ""

    assert "主程序默认任务" in await run(pluginC1, "/rss translate")
    assert "已关闭翻译" in await run(pluginC1, "/rss translate off")
    assert pluginC1.store.settings["translate_enabled"] is False
    assert "已开启翻译" in await run(pluginC1, "/rss translate on")
    listing = await run(pluginC1, "/rss translate list")
    assert "DeepSeek/deepseek-v4-flash" in listing and "— SiliconFlow" in listing, listing
    assert "sk-should-never-appear-in-any-output" not in listing
    chosen = await run(pluginC1, "/rss translate model 2")
    assert "硅基流动/DeepSeek-V4.1-Flash" in chosen, chosen
    assert pluginC1.store.settings["translate_model"] == "硅基流动/DeepSeek-V4.1-Flash"
    assert "没有这个模型" in await run(pluginC1, "/rss translate model 不存在")
    assert "用法" in await run(pluginC1, "/rss translate model")
    assert "已恢复" in await run(pluginC1, "/rss translate model clear")
    assert "translate_model" not in pluginC1.store.settings
    status = await run(pluginC1, "/rss status")
    assert "🌐 翻译：" in status, status
    print("[ok] /rss translate 各分支与 status 汇总")

    # 设置确实落盘
    again = mod.RssStore()
    again.bind(str(pluginC1.ctx.paths.data_dir))
    again.load()
    assert again.settings.get("translate_enabled") is True, again.settings
    print("[ok] 翻译偏好写进插件数据文件，重启后仍生效")

    # ---- WebUI ----
    options = await pluginC1.webui_translate_options()
    assert options["enabled"] is True and options["catalog_error"] == ""
    assert [model["name"] for model in options["models"]][:2] == [
        "DeepSeek/deepseek-v4-flash",
        "硅基流动/DeepSeek-V4.1-Flash",
    ], options["models"]
    assert options["tasks"] == ["utils", "replyer", "fast_model"]
    assert "sk-should-never-appear-in-any-output" not in json.dumps(options, ensure_ascii=False)
    saved = await pluginC1.webui_set_translate(enabled=True, model="1")
    assert saved["model"] == "DeepSeek/deepseek-v4-flash", saved
    try:
        await pluginC1.webui_set_translate(model="没这个")
        raise AssertionError("不存在的模型应当抛错")
    except ValueError as exc:
        print("[ok] webui_set_translate 校验模型:", exc)
    cleared = await pluginC1.webui_set_translate(model="clear")
    assert cleared["model"] == "" and cleared["enabled"] is True
    original_download = mod.download
    mod.download = lambda url, timeout: (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>F</title>'
        "<item><title>Incident with Actions and Pages</title><link>https://a.example/1</link>"
        "<guid>tr-1</guid><description>Investigating - degraded performance</description></item>"
        "</channel></rss>"
    ).encode("utf-8")
    try:
        added = pluginC1.store.add_feed("github 状态", "https://a.example/feed")
        test = await pluginC1.webui_test_translate(added["id"])
    finally:
        mod.download = original_download
    assert test["translated"] is True and "（标题译文）" in test["text"], test
    assert "📡 RSS/Atom 拉取 • github 状态" in test["text"], test
    assert pluginC1.store.targets_for(added["id"]) == [], "试译不该真的推送"
    try:
        await pluginC1.webui_test_translate("zzzzzzzz")
        raise AssertionError("不存在的源应当抛错")
    except ValueError as exc:
        print("[ok] webui_test_translate 校验源:", exc)
    print("[ok] WebUI 翻译接口：选项、校验、试译不发送")

    # 模型目录缺失时明确说明并退回任务名
    # 官方镜像里 /MaiMBot/config/model_config.toml 是真实存在的，绝对回退路径会让
    # "读不到目录"这一分支在宿主环境永远走不到，所以这里把回退换成不可能存在的文件
    pluginX, root_x = make_env(with_catalog=False)
    original_fallbacks = mod._MODEL_CONFIG_FALLBACKS
    mod._MODEL_CONFIG_FALLBACKS = (os.path.join(root_x, "missing", "model_config.toml"),)
    try:
        await pluginX.on_load()
        listing = await run(pluginX, "/rss translate list")
        assert "没找到 model_config.toml" in listing and "utils" in listing, listing
        options = await pluginX.webui_translate_options()
        assert options["models"] == [] and options["catalog_error"], options
    finally:
        mod._MODEL_CONFIG_FALLBACKS = original_fallbacks
    print("[ok] 读不到 model_config.toml 时说明原因并退回任务名")

    # llm 能力缺失（老 Host / 未声明）时不该把推送带崩
    pluginY, _ = make_env()
    pluginY.ctx.llm = None
    await pluginY.on_load()
    assert await pluginY._translate_for(item()) == ("", "") and pluginY._translate_errors == 1
    print("[ok] llm 不可用时退回原文")

    await plugin.on_unload()
    print("\nALL TRANSLATE CHECKS PASSED")


asyncio.run(main())
