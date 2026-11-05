import asyncio
import importlib.util
import json
import tempfile
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("mai_rss_plugin", Path(__file__).resolve().parents[1] / "plugin.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FEED = {
    "url": "https://fake/feed",
    "items": [
        ("老文章", "old-1", time.time() - 400 * 3600),
        ("新文章A", "new-a", time.time() - 2 * 3600),
    ],
}


FEED2 = {"items": [("第二源历史条目", "b-0", time.time() - 3600)]}


def make_item(title, key, published):
    when = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(published))
    return (
        f"<item><title>{title}</title><link>https://fake/{key}</link>"
        f"<guid>{key}</guid><pubDate>{when}</pubDate>"
        f"<description>{title} 的正文内容</description></item>"
    )


def fake_download(url, timeout):
    # 按 URL 分支：默认源用 FEED，第二源用 FEED2，这样多源轮询的断言才有区分度
    items = FEED2["items"] if url.endswith("/feed2") else FEED["items"]
    body = "".join(make_item(*item) for item in items)
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>F</title>{body}</channel></rss>'.encode("utf-8")


mod.download = fake_download


class FakeSend:
    def __init__(self):
        self.sent = []
        self.broken = set()

    async def text(self, text, stream_id, **kwargs):
        if stream_id in self.broken:
            return False
        self.sent.append((stream_id, text))
        return True


class FakeCtx:
    def __init__(self, data_dir):
        self.send = FakeSend()
        self.paths = Path(data_dir)
        self.chat = None
        self.logger = _Log()


class _Log:
    def info(self, *a, **kw):
        pass

    def warning(self, *a, **kw):
        pass

    def error(self, *a, **kw):
        print("[plugin-error]", *a)


async def main():
    plugin = mod.MaiRssPlugin()
    plugin._set_context(FakeCtx(tempfile.mkdtemp()))
    plugin.store.bind(str(plugin.ctx.paths))
    plugin.store.load()
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "1.0.0"},
            "fetch": {"enabled": True, "poll_interval_minutes": 1, "max_items_per_feed": 5},
            "push": {"lookback_hours": 72, "send_delay_seconds": 0},
        }
    )

    feed = plugin.store.add_feed("假源", "https://fake/feed")

    # 1) 建立基线：现有条目全部记为已读，不推送
    assert await plugin._baseline(feed) == 2
    assert plugin.ctx.send.sent == [], plugin.ctx.send.sent
    print("[ok] 基线不推送历史文章")

    # 2) 未绑定时来了新文章 → 不发
    FEED["items"].append(("没人订阅", "nobody", time.time() - 3600))
    stats = await plugin._poll_cycle()
    assert plugin.ctx.send.sent == [], plugin.ctx.send.sent
    print("[ok] 无绑定时不推送，统计:", json.dumps(stats))

    # 3) 会话级绑定：s1 只订阅该源，s2 未订阅
    plugin.store.set_binding("s1", [feed["id"]])
    plugin.store.set_binding("s2", [])
    FEED["items"].append(("绑定后新文章", "bound", time.time() - 3600))
    stats = await plugin._poll_cycle()
    assert [target for target, _ in plugin.ctx.send.sent] == ["s1"], plugin.ctx.send.sent
    print("[ok] 只发给绑定会话:", plugin.ctx.send.sent[0][1].replace("\n", " | "))

    # 4) 重复轮询不再重复推送
    before = len(plugin.ctx.send.sent)
    await plugin._poll_cycle()
    assert len(plugin.ctx.send.sent) == before
    print("[ok] 已读去重，不重复推送")

    # 5) 通配绑定：新加的源对所有 * 会话生效
    plugin.store.set_binding("s3", ["*"])
    feed2 = plugin.store.add_feed("第二个源", "https://fake/feed2")
    await plugin._baseline(feed2)  # 基线吸收 b-0
    plugin.store.set_binding("s1", [feed["id"], feed2["id"]])
    FEED2["items"].append(("第二源新文章", "b-1", time.time() - 60))
    before = len(plugin.ctx.send.sent)
    await plugin._poll_cycle()
    pushed = plugin.ctx.send.sent[before:]
    hits = [(target, text) for target, text in pushed if "第二源新文章" in text]
    targets = {target for target, _ in hits}
    assert targets == {"s1", "s3"}, (targets, pushed)
    print("[ok] 通配会话自动订阅新源:", sorted(targets))

    # 6) lookback 窗口：太旧的文章不推送
    plugin2 = mod.MaiRssPlugin()
    plugin2._set_context(FakeCtx(tempfile.mkdtemp()))
    plugin2.store.bind(str(plugin2.ctx.paths))
    plugin2.store.load()
    plugin2.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "1.0.0"},
            "fetch": {"enabled": True, "poll_interval_minutes": 1, "max_items_per_feed": 5},
            "push": {"lookback_hours": 1, "send_delay_seconds": 0},
        }
    )
    fresh = plugin2.store.add_feed("窗口源", "https://fake/feed")
    FEED["items"] = [("一小时前内的文章", "in-window", time.time() - 30 * 60)]
    plugin2.store.mark_seen(fresh["id"], "in-window")  # 只把这条记成基线
    await plugin2._poll_cycle()
    assert [text for _, text in plugin2.ctx.send.sent] == [], plugin2.ctx.send.sent
    FEED["items"].append(("刚发的文章", "fresh", time.time()))
    await plugin2._poll_cycle()
    assert len(plugin2.ctx.send.sent) == 0, plugin2.ctx.send.sent  # 30 分钟前的文章落在 1 小时窗口内，但还没有会话绑定
    plugin2.store.set_binding("solo", [fresh["id"]])
    FEED["items"].append(("又一轮", "fresh2", time.time()))
    await plugin2._poll_cycle()
    assert len(plugin2.ctx.send.sent) == 1, plugin2.ctx.send.sent
    print("[ok] 时间窗口过滤旧文章:", plugin2.ctx.send.sent[0][1].splitlines()[1])

    # 7) 每轮上限：一次涌进 9 条只推最近 5 条，并给出截断提示
    plugin3 = mod.MaiRssPlugin()
    plugin3._set_context(FakeCtx(tempfile.mkdtemp()))
    plugin3.store.bind(str(plugin3.ctx.paths))
    plugin3.store.load()
    plugin3.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "1.0.0"},
            "fetch": {"enabled": True, "poll_interval_minutes": 1, "max_items_per_feed": 5},
            "push": {"lookback_hours": 0, "send_delay_seconds": 0},
        }
    )
    burst = plugin3.store.add_feed("突发源", "https://fake/burst")
    plugin3.store.set_binding("room", [burst["id"]])
    await plugin3._baseline(burst)
    # 假源故意把最新一条放在中间，验证插件不依赖源内顺序
    FEED["items"] = [(f"第{i}条", f"burst-{i}", time.time() - i * 60) for i in (7, 8, 9, 4, 5, 6, 1, 2, 3)]
    await plugin3._poll_cycle()
    texts = [text for _, text in plugin3.ctx.send.sent]
    titles = [text.splitlines()[1] for text in texts if "未推送" not in text]
    assert len(titles) == 5, titles
    # 保留最近的 5 条（第1~5），并按时间从旧到新发出
    assert titles == ["🗞 标题：第5条", "🗞 标题：第4条", "🗞 标题：第3条", "🗞 标题：第2条", "🗞 标题：第1条"], titles
    assert any("未推送" in text for text in texts), texts
    print("[ok] 乱序源也按时间正序推送最近 5 条，并给出截断提示")

    # 8) 发送失败不炸循环
    plugin.ctx.send.broken.add("s1")
    FEED["items"].append(("发送会失败", "broken", time.time()))
    stats = await plugin._poll_cycle()
    assert isinstance(stats, dict)
    print("[ok] 发送失败被记录，轮询继续:", json.dumps(stats))

    # 9) 抓取异常记在源上，不影响其它源
    def boom(url, timeout):
        raise ValueError("502 Bad Gateway")

    original = mod.download
    mod.download = boom
    stats = await plugin._poll_cycle()
    mod.download = original
    assert stats["errors"] == len(plugin.store.feeds), stats
    assert all("502" in feed["last_error"] for feed in plugin.store.feeds.values())
    print("[ok] 抓取失败逐源记录:", plugin.store.feeds[feed["id"]]["last_error"])

    # 10) 落盘可读回
    await plugin._persist()
    reloaded = mod.RssStore()
    reloaded.bind(str(plugin.ctx.paths))
    reloaded.load()
    assert set(reloaded.feeds) == set(plugin.store.feeds)
    assert reloaded.bindings["s1"] == plugin.store.bindings["s1"]
    print("[ok] 订阅状态持久化读回一致")

    print("\nALL FLOW CHECKS PASSED")


asyncio.run(main())
