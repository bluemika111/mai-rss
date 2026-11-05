import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("mai_rss_plugin", PLUGIN_DIR / "plugin.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["mai_rss_plugin"] = mod
spec.loader.exec_module(mod)
print("[ok] plugin.py 导入成功")

plugin_cls = mod.MaiRssPlugin
defaults = plugin_cls.build_default_config()
print("[ok] 默认配置:", json.dumps(defaults, ensure_ascii=False)[:240])
assert plugin_cls.has_config_model()

RSS2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Example</title><link>https://example.com</link>
<item><title>Hello &amp; World</title><link>https://example.com/1</link>
<guid isPermaLink="false">guid-1</guid>
<pubDate>Tue, 03 Jun 2025 09:30:00 GMT</pubDate>
<description>简短摘要</description>
<content:encoded><![CDATA[<p>全文第一段<br>第二段 &amp; 收尾</p>]]></content:encoded></item>
<item><title>旧文章</title><link>https://example.com/2</link><pubDate>Tue, 03 Jun 2025 08:00:00 GMT</pubDate></item>
</channel></rss>""".encode("utf-8")

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Atom 源</title><link rel="self" href="https://example.org/feed"/>
<entry><title type="html">Atom 标题</title>
<link rel="alternate" href="/posts/1"/>
<id>urn:uuid:abc</id><published>2025-06-03T10:00:00Z</published>
<summary>summary 文字</summary>
<content type="xhtml"><div><p>xhtml 正文</p></div></content></entry></feed>""".encode("utf-8")

RDF = """<?xml version="1.0" encoding="utf-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
<item rdf:about="https://example.net/a"><title>RDF 标题</title>
<link>https://example.net/a</link><description>RDF 描述</description>
<dc:date>2025-06-03T07:00:00+08:00</dc:date></item></rdf:RDF>""".encode("utf-8")

for name, raw, count in [("RSS2.0", RSS2, 2), ("Atom", ATOM, 1), ("RDF", RDF, 1)]:
    items = mod.parse_feed(raw, "")
    assert len(items) == count, (name, items)
    print(f"[ok] {name}:", {k: (v[:38] if isinstance(v, str) else v) for k, v in items[0].items()})
    assert items[0]["key"] and items[0]["published"] > 1700000000, (name, items[0])

rss_items = mod.parse_feed(RSS2, "")
assert rss_items[0]["title"] == "Hello & World", rss_items[0]["title"]
assert rss_items[0]["text"] == "全文第一段\n第二段 & 收尾", repr(rss_items[0]["text"])
assert rss_items[0]["link"] == "https://example.com/1"
assert len(mod.parse_feed(RSS2, "")) == 2
atom_items = mod.parse_feed(ATOM, "https://example.org/feed")
assert atom_items[0]["link"] == "https://example.org/posts/1", atom_items[0]["link"]
assert atom_items[0]["text"] == "xhtml 正文", repr(atom_items[0]["text"])
rdf_items = mod.parse_feed(RDF, "")
assert rdf_items[0]["link"] == "https://example.net/a"
assert abs(rdf_items[0]["published"] - 1748905200) < 5, rdf_items[0]["published"]
print("[ok] 相对链接补齐 / xhtml 正文 / CDATA / 实体清理 正确")

# 源内字段顺序没有约定：updated 写在 published 前面时也要取 published（发布时间优先）
ATOM_UPDATED_FIRST = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>混序</title>
<entry><title>条目</title><link href="https://example.org/e/1"/>
<updated>2025-06-04T20:00:00+08:00</updated><published>2025-06-03T09:00:00+08:00</published>
<summary>摘要</summary></entry></feed>""".encode("utf-8")
mixed = mod.parse_feed(ATOM_UPDATED_FIRST, "")[0]
assert abs(mixed["published"] - mod._parse_published("2025-06-03T09:00:00+08:00")) < 1, mixed
print("[ok] published 优先于 updated（不受字段顺序影响）")

# 状态页类 Atom 源（如 status.deepseek.com/feed.atom）只有 updated，且带 +08:00 时区
ATOM_UPDATED_ONLY = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>状态</title>
<entry><title>部分中断</title><link rel="alternate" href="https://status.example.com/incidents/1"/>
<id>urn:svc:change:1</id><updated>2026-11-03T18:21:58+08:00</updated>
<content type="html">&lt;p&gt;已实施修复，正在监控结果。&lt;/p&gt;</content></entry></feed>""".encode("utf-8")
status_item = mod.parse_feed(ATOM_UPDATED_ONLY, "")[0]
assert status_item["key"] == "urn:svc:change:1", status_item
assert abs(status_item["published"] - mod._parse_published("2026-11-03T18:21:58+08:00")) < 1, status_item
assert status_item["text"] == "已实施修复，正在监控结果。", repr(status_item["text"])
print("[ok] 仅 updated 的 Atom 源：时区解析 / 实体正文 / guid 作键 正确")

assert mod._parse_published("不在格式里") == 0.0
print("[ok] 非法时间降级为 0")

for bad in ["file:///etc/passwd", "ftp://x/y"]:
    try:
        mod.download(bad, 5)
        raise AssertionError("应当拒绝 " + bad)
    except ValueError as exc:
        print(f"[ok] 拒绝危险协议 {bad}: {exc}")

DIR = tempfile.mkdtemp()
store = mod.RssStore()
store.bind(DIR)
store.load()
f1 = store.add_feed("示例一", "https://a.example/feed")
f2 = store.add_feed("示例二", "https://b.example/feed")
assert store.resolve_feed("1")["id"] == f1["id"] and store.resolve_feed("示例二")["id"] == f2["id"]
assert store.resolve_feed("nope") is None

# 源 ID 是 8 位十六进制，偶尔全是数字（实测出现过 35237580），不能被当成越界序号吞掉
numeric_store = mod.RssStore()
numeric_store.bind(tempfile.mkdtemp())
numeric_store.load()
created = numeric_store.add_feed("数字ID源", "https://n.example/feed")
record = numeric_store.feeds.pop(created["id"])
record["id"] = "35237580"
numeric_store.feeds["35237580"] = record
assert numeric_store.resolve_feed("35237580")["url"] == "https://n.example/feed"
assert numeric_store.resolve_feed("1")["url"] == "https://n.example/feed"
assert numeric_store.resolve_feed("35237581") is None and numeric_store.resolve_feed("2") is None
print("[ok] 纯数字源 ID 优先于序号，序号查找仍然可用")
ids, err = store.resolve_tokens(["1", "2"])
assert ids == [f1["id"], f2["id"]] and not err, (ids, err)
assert store.resolve_tokens(["all"])[0] == ["*"]
assert store.resolve_tokens(["none"])[0] == []
ids, err = store.resolve_tokens([])
assert ids is None and err
store.set_binding("s1", [f1["id"]])
store.set_binding("s2", ["*"])
store.set_binding("s3", [])
assert "s3" not in store.bindings
assert store.targets_for(f1["id"]) == ["s1", "s2"]
assert store.matches("s2", f2["id"])
store.mark_seen(f1["id"], "k1")
assert store.is_seen(f1["id"], "k1") and not store.is_seen(f1["id"], "k2")
store.save()
again = mod.RssStore()
again.bind(DIR)
again.load()
assert again.feeds[f1["id"]]["name"] == "示例一"
assert again.bindings == {"s1": [f1["id"]], "s2": ["*"]}
assert again.is_seen(f1["id"], "k1")
again.remove_feed(f1["id"])
assert again.bindings == {"s2": ["*"]}
# 通配绑定与已删源无关，仍然匹配；被点名的 s1 绑定已在删源时清掉
assert again.targets_for(f1["id"]) == ["s2"]
print("[ok] 删除订阅源会清理点名绑定，通配绑定保持生效")
print("[ok] 仓库增删改查、绑定通配、落盘读回 正确")

inst = plugin_cls.__new__(plugin_cls)
inst.set_plugin_config({"plugin": {"enabled": True, "config_version": "1.0.0"}, "push": {"max_content_chars": 20}})
rendered = inst._render(f2, {"title": "标题", "link": "https://b", "text": "一" * 40, "published": 1780000000.0, "key": "k"})
print("[ok] 推送文本:\n" + rendered)
assert "📡 RSS/Atom 拉取 • 示例二" in rendered and "……" in rendered and "🔗 https://b" in rendered

pattern = re.compile(mod._COMMAND_PATTERN)
for text in ["/rss list", "@某某  /rss bind 1 2", "/订阅 status", "/rss"]:
    match = pattern.match(text)
    assert match, text
    print(f"[ok] 命令匹配 {text!r} -> arg={match.group('arg')!r}")
assert pattern.match("/rssx") is None
print("[ok] 不会误吞 /rssx")

routes = plugin_cls._routes.fget(inst)
for verb in ["help", "list", "add", "del", "bind", "unbind", "status", "check", "preview", "translate"]:
    assert callable(routes[verb]), verb
print("[ok] 命令路由齐备:", sorted(key for key in routes if key))
print("\nALL CHECKS PASSED")
