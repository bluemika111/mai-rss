import asyncio
import importlib.util
import tempfile
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("mai_rss_plugin", Path(__file__).resolve().parents[1] / "plugin.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

_counter = {"n": 0}


def fake_download(url, timeout):
    # 每次抓取多产出一条新条目，模拟持续更新的源
    _counter["n"] += 1
    index = _counter["n"]
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>F</title>'
        f'<item><title>标题 {url} #{index}</title><link>{url}/{index}</link><guid>{url}-{index}</guid>'
        f'<pubDate>{time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())}</pubDate>'
        "<description>正文</description></item></channel></rss>"
    ).encode("utf-8")


mod.download = fake_download


class Send:
    def __init__(self):
        self.sent = []

    async def text(self, text, stream_id, **kwargs):
        self.sent.append((stream_id, text))
        return True


class Config:
    async def get(self, key, default=None):
        return ["qq:10001"] if key == "plugin.permission" else default


class Chat:
    async def get_all_streams(self, platform="qq"):
        return {
            "success": True,
            "streams": [
                {"stream_id": "g:123", "chat_type": "group", "group_name": "摸鱼群", "group_id": "123"},
                {"stream_id": "p:456", "chat_type": "private", "user_nickname": "小明", "user_cardname": "", "user_id": "456"},
            ],
        }


class Log:
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): pass
    def error(self, *a, **kw): print("[plugin-error]", *a)


class LLM:
    """主程序模型通道的替身：固定返回 T|/B| 两行，并记下每次调用参数。"""

    def __init__(self):
        self.calls = []

    async def generate(self, prompt="", model="", temperature=None, max_tokens=None, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return {"success": True, "response": "T|标题译文\nB|正文译文", "model_name": "fake"}

    async def get_available_models(self):
        return ["utils", "replyer", "fast_model"]


class Paths:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.runtime_dir = Path(data_dir) / "runtime"


class Ctx:
    def __init__(self, data_dir):
        self.send = Send()
        self.config = Config()
        self.chat = Chat()
        self.llm = LLM()
        self.logger = Log()
        self.paths = Paths(data_dir)


async def main():
    plugin = mod.MaiRssPlugin()
    plugin._set_context(Ctx(tempfile.mkdtemp()))
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "1.1.0"},
            "fetch": {"poll_interval_minutes": 1},
            "push": {"send_delay_seconds": 0},
            "permission": {"admin_only": True, "admins": ["qq:10001"]},
            # 假源里的标题中英混排，这里要验证的是译文进没进消息，不是语言判定
            "translate": {"skip_if_chinese": False},
        }
    )
    await plugin.on_load()
    plugin.store.set_binding("p:456", [])

    async def run(text, user_id="10001", local=False, stream_id="g:123"):
        match = __import__("re").match(mod._COMMAND_PATTERN, text)
        assert match, text
        before = len(plugin.ctx.send.sent)
        handled, response, intercept = await plugin.handle_rss(
            stream_id=stream_id,
            user_id=user_id,
            is_local_operator=local,
            matched_groups=match.groupdict(),
        )
        # 命令正文必须由插件自己 send.text（Host 只把 response 写日志），断言要看真正发出去的那条
        fresh = [msg for sid, msg in plugin.ctx.send.sent[before:] if sid == stream_id]
        assert not stream_id or fresh, (text, response)
        return handled, (fresh[-1] if fresh else response), intercept

    handled, reply, intercept = await run("/rss")
    assert handled and "Mai RSS 订阅" in reply and intercept == 1, reply
    print("[ok] /rss 出帮助")

    handled, reply, _ = await run("/rss list")
    assert "还没有订阅源" in reply, reply
    print("[ok] /rss list 空态")

    handled, reply, _ = await run("/rss del 9")
    assert "找不到" in reply, reply

    handled, reply, _ = await run("/rss add https://a.example/feed  announcements")
    assert "已添加订阅源" in reply and "1 条历史文章已记为已读" in reply, reply
    print("[ok] /rss add:", reply.replace("\n", " | "))

    handled, reply, _ = await run("/rss add https://a.example/feed")
    assert "已经是订阅源" in reply, reply
    handled, reply, _ = await run("/rss add not-a-url")
    assert "必须以 http:// 或 https:// 开头" in reply, reply
    print("[ok] /rss add 去重与协议校验")

    handled, reply, _ = await run("/rss list")
    assert "1." in reply and "announcements" in reply, reply
    print("[ok] /rss list:\n" + reply)

    handled, reply, _ = await run("/订阅 bind 1")
    assert "已订阅：announcements" in reply, reply
    handled, reply, _ = await run("/rss status")
    assert "本会话：announcements" in reply, reply
    print("[ok] /订阅 别名 + bind + status:\n" + reply)

    handled, reply, _ = await run("/rss bind all")
    assert "订阅全部" in reply, reply
    handled, reply, _ = await run("/rss bind none")
    assert "已取消全部" in reply, reply
    handled, reply, _ = await run("/rss unbind 1")
    assert "本来就没有订阅" in reply, reply
    print("[ok] bind all / bind none / unbind 空态")

    handled, reply, _ = await run("/rss bind 1")
    handled, reply, _ = await run("/rss check")
    # check 会真的推送：绑定的是 g:123（当前会话），应发出 1 条（推送正文在统计回复之前）
    assert "新推送 1 条" in reply, reply
    pushed = [msg for sid, msg in plugin.ctx.send.sent if sid == "g:123" and msg.startswith("📡 RSS/Atom 拉取")]
    assert len(pushed) == 1, plugin.ctx.send.sent
    assert "（标题译文）" in pushed[0], pushed[0]
    print("[ok] /rss check 推送落地:", pushed[-1].splitlines()[1])

    handled, reply, _ = await run("/rss preview 1")
    assert reply.startswith("📡 RSS/Atom 拉取"), reply
    assert "（正文译文）" in reply, reply
    print("[ok] /rss preview 试发（含译文）")

    # 源里条目乱序（旧的在前）时，preview 也必须取时间最新的那条
    base = time.time()
    original_download = mod.download
    mod.download = lambda url, timeout: (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>F</title>'
        + "".join(
            f'<item><title>第{i}条</title><link>https://example.com/{i}</link><guid>pv-{i}</guid>'
            f'<pubDate>{time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(base + i * 60))}</pubDate>'
            "<description>正文</description></item>"
            for i in (1, 3, 2)
        )
        + "</channel></rss>"
    ).encode("utf-8")
    try:
        await run("/rss preview 1")
    finally:
        mod.download = original_download
    preview_text = plugin.ctx.send.sent[-1][1]
    assert "第3条" in preview_text, preview_text
    print("[ok] /rss preview 在乱序源里仍取最新条目")

    handled, reply, _ = await run("/rss foobar")
    assert "不认识的操作" in reply, reply
    handled, reply, _ = await run("/rss bind", stream_id="")
    assert "取不到当前会话" in reply, reply
    print("[ok] 未知子命令与会话缺失的提示")

    handled, reply, _ = await run("/rss add https://b.example/feed x", user_id="20002")
    assert reply == "只有管理员能管理 RSS 订阅。", reply
    print("[ok] 非管理员被拒")

    handled, reply, _ = await run("/rss list", user_id="20002", local=True)
    assert handled and "1." in reply, reply
    print("[ok] 本地终端不受权限限制")

    overview = await plugin.webui_overview()
    assert overview["feeds"][0]["name"] == "announcements", overview
    labels = {item["stream_id"]: item["label"] for item in overview["streams"]}
    assert labels == {"g:123": "摸鱼群（123）", "p:456": "小明(456) 的私聊"}, labels
    by_id = {item["stream_id"]: item for item in overview["streams"]}
    assert by_id["g:123"]["feed_ids"] and by_id["p:456"]["feed_ids"] == [], overview["streams"]
    print("[ok] webui_overview:", overview["feeds"][0]["id"], labels)

    # 主程序取不到聊天流时必须把原因交出去，而不是给页面一个静悄悄的空列表
    original_chat = plugin.ctx.chat

    class BrokenChat:
        async def get_all_streams(self, platform="qq"):
            return {"success": False, "error": "能力未授权"}

    plugin.ctx.chat = BrokenChat()
    overview = await plugin.webui_overview()
    assert overview["streams"] == [] and "能力未授权" in overview["streams_error"], overview
    handled, reply, _ = await run("/rss status")
    assert "取不到主程序聊天流" in reply and "能力未授权" in reply, reply

    class RaisingChat:
        async def get_all_streams(self, platform="qq"):
            raise RuntimeError("RPC 通道断开")

    plugin.ctx.chat = RaisingChat()
    overview = await plugin.webui_overview()
    assert "RPC 通道断开" in overview["streams_error"], overview

    # 有的 Host 版本这个能力直接返回裸 list（字段还可能是 None），同样要能认
    class BareListChat:
        async def get_all_streams(self, platform="qq"):
            return [
                {
                    "session_id": "0757add9", "stream_id": "0757add9", "platform": "qq",
                    "user_id": None, "user_nickname": None, "user_cardname": None,
                    "group_id": "697162366", "group_name": "2021班哥布林巢穴", "account_id": "3441696556",
                    "scope": None, "is_group_session": True, "chat_type": "group",
                },
                {
                    "session_id": "06ffc7d0", "stream_id": "06ffc7d0", "platform": "webui",
                    "user_id": None, "user_nickname": "人类", "user_cardname": "人类",
                    "group_id": None, "group_name": None, "account_id": "self",
                    "scope": None, "is_group_session": False, "chat_type": "private",
                },
            ]

    plugin.ctx.chat = BareListChat()
    overview = await plugin.webui_overview()
    assert overview["streams_error"] == "", overview
    assert [(s["label"], s["platform"], s["chat_type"]) for s in overview["streams"]] == [
        ("2021班哥布林巢穴（697162366）", "qq", "group"),
        ("人类", "webui", "private"),
    ], overview["streams"]
    plugin.ctx.chat = original_chat
    print("[ok] 聊天流获取失败时原因可见（页面字段 + /rss status）；裸 list 返回同样可解析")

    added = await plugin.webui_add_feed("https://c.example/feed", "第三个源")
    assert added["name"] == "第三个源" and added["items"] == 1, added
    try:
        await plugin.webui_add_feed("https://a.example/feed")
        raise AssertionError("重复地址应当抛错")
    except ValueError as exc:
        print("[ok] webui_add_feed 去重抛错:", exc)
    try:
        await plugin.webui_add_feed("javascript:alert(1)")
        raise AssertionError("非法协议应当抛错")
    except ValueError as exc:
        print("[ok] webui_add_feed 协议抛错:", exc)

    saved = await plugin.webui_set_feeds("p:456", [added["id"], "不存在", "不存在"])
    assert saved["feed_ids"] == [added["id"]], saved
    wildcard = await plugin.webui_set_feeds("p:456", ["*"])
    assert wildcard["feed_ids"] == ["*"], wildcard
    cleared = await plugin.webui_set_feeds("p:456", [])
    assert cleared["feed_ids"] == [] and "p:456" not in plugin.store.bindings
    print("[ok] webui_set_feeds 过滤无效 ID / 通配 / 清空")

    removed = await plugin.webui_remove_feed(added["id"])
    assert removed["removed"], removed
    try:
        await plugin.webui_remove_feed("zzzzzzzz")
        raise AssertionError("不存在的源应当抛错")
    except ValueError as exc:
        print("[ok] webui_remove_feed 抛错:", exc)

    stats = await plugin.webui_check_now()
    assert set(stats) == {"feeds", "new", "sent", "errors"}, stats
    await plugin.on_unload()
    assert plugin._poll_task is None
    print("[ok] webui_check_now 统计与 on_unload 取消轮询任务")
    print("\nALL COMMAND CHECKS PASSED")


asyncio.run(main())
