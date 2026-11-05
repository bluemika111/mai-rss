from pathlib import Path

import importlib.util

spec = importlib.util.spec_from_file_location("mai_rss_plugin", Path(__file__).resolve().parents[1] / "plugin.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FEEDS = [
    ("DeepSeek 状态页(RSS)", "https://status.deepseek.com/feed.rss"),
    ("DeepSeek 状态页(Atom)", "https://status.deepseek.com/feed.atom"),
    ("少数派 RSS", "https://sspai.com/feed"),
    ("阮一峰周刊(Atom)", "https://www.ruanyifeng.com/blog/atom.xml"),
    ("GitHub Releases(Atom)", "https://github.com/MaiM-with-u/MaiBot/releases.atom"),
    ("Python 博客(Atom)", "https://blog.python.org/feeds/posts/default"),
    ("Mozilla 博客(RSS)", "https://hacks.mozilla.org/feed/"),
    ("WenYue/RSSHub 示例", "https://rsshub.app/telegram/channel/freebotsgroup"),
    ("36氪 /feed(实为网页)", "https://36kr.com/feed"),
    ("普通网页", "https://example.com/"),
]

for name, url in FEEDS:
    try:
        raw = mod.download(url, 25)
        items = mod.parse_feed(raw, url)
        first = items[0] if items else None
        print(f"[ok] {name}: {len(items)} 条 · 编码 {raw[:60].split(b'encoding=')[-1][:10]!r}")
        if first:
            print(f"      {first['title'][:44]} · {mod._format_time(first['published'])}")
            print(f"      {first['link'][:72]}")
            print(f"      正文 {first['text'][:60]!r}")
    except Exception as exc:
        print(f"[!!] {name}: {type(exc).__name__} → {str(exc)[:120]}")
