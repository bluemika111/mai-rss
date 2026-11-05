"""Mai RSS —— 订阅 RSS / Atom 源，并把新文章的来源、时间与正文推送到指定聊天流。

设计要点：

- **订阅源与会话绑定分开存**：源列表存在插件数据目录的 JSON 里，会话绑定是
  ``stream_id -> [feed_id | "*"]`` 的映射；「无」就是不绑定任何源，「全部」用 ``*``
  通配，这样新加的源不用回头逐个会话补选。
- **抓取由插件自己的 asyncio 循环驱动**：插件运行时没有定时器能力，只能像
  state-monitor 那样在 ``on_load`` 起后台任务、在 ``on_unload`` 取消。
- **新增源时建立基线**：第一次抓取只把当时已有的条目记为「已读」，不推送，
  否则加一个历史很长的源会瞬间把聊天刷屏。
- **解析只用标准库**：RSS 2.0 / RSS 1.0(RDF) / Atom 都用 ``xml.etree`` 按局部标签名
  扫描，不引入 feedparser，Runner 侧也就不需要联网装包。
- **翻译失败绝不拦推送**：模型调用出错、输出解析不出来、超出本轮预算，都只让这一条
  退回原文并记日志；推送本身永远先发出去。
"""

from datetime import datetime
from email.utils import parsedate_to_datetime
from maibot_sdk import API, Command, Field, MaiBotPlugin, PluginConfigBase
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import asyncio
import hashlib
import html as html_lib
import json
import os
import re
import time
import uuid

try:  # 3.11+ 自带 tomllib；旧解释器退回 SDK 依赖里的 tomlkit
    import tomllib
except ImportError:  # pragma: no cover - 取决于运行解释器版本
    tomllib = None  # type: ignore[assignment]

try:
    import tomlkit
except ImportError:  # pragma: no cover - 有 tomllib 时不需要它
    tomlkit = None  # type: ignore[assignment]


# ============================================================================
# 配置模型
# ============================================================================


class PluginSectionConfig(PluginConfigBase):
    """宿主保留的插件开关与配置版本（[plugin] 节为 SDK 强制要求）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.1.0", description="配置版本")


class FetchConfig(PluginConfigBase):
    """抓取配置。"""

    __ui_label__ = "抓取"
    __ui_icon__ = "rss"
    __ui_order__ = 1

    enabled: bool = Field(default=True, description="是否开启定时抓取（关闭后只能用 /rss check 手动抓）")
    poll_interval_minutes: int = Field(default=10, description="抓取间隔（分钟），最小 1")
    request_timeout_seconds: int = Field(default=15, description="单个订阅源的网络超时（秒）")
    max_items_per_feed: int = Field(default=5, description="单轮每个源最多推送几条，超出的新条目只记已读不推送")


class PushConfig(PluginConfigBase):
    """推送内容配置。"""

    __ui_label__ = "推送"
    __ui_icon__ = "send"
    __ui_order__ = 2

    max_content_chars: int = Field(default=600, description="正文最多保留多少个字符，0 表示只发标题和时间")
    include_link: bool = Field(default=True, description="是否在消息末尾附上原文链接")
    lookback_hours: int = Field(default=72, description="只推送多少小时内发布的文章，0 表示不限制")
    send_delay_seconds: float = Field(default=0.6, description="连续发送时的间隔，避免触发平台风控")


class PermissionConfig(PluginConfigBase):
    """权限配置。"""

    __ui_label__ = "权限"
    __ui_icon__ = "shield"
    __ui_order__ = 3

    admin_only: bool = Field(default=True, description="是否只允许管理员增删订阅源与修改绑定")
    admins: List[str] = Field(default_factory=list, description="管理员列表，形如 qq:12345；留空则沿用主程序 [plugin].permission")


class TranslateConfig(PluginConfigBase):
    """译文翻译配置。

    ``translate_enabled`` / ``translate_model`` 这两项聊天命令能改，存在插件自己的
    数据文件里（主程序只允许内置插件管理改 config.toml）；它们非空时覆盖本段。
    """

    __ui_label__ = "翻译"
    __ui_icon__ = "languages"
    __ui_order__ = 4

    enabled: bool = Field(default=True, description="是否给非中文条目附上译文（聊天里 /rss translate off 可临时关掉）")
    provider: Literal["maibot", "custom"] = Field(
        default="maibot", description="翻译通道：maibot 用主程序已配的模型，custom 自己填 API 地址和密钥"
    )
    task_name: str = Field(default="", description="maibot 通道用哪个模型任务；留空则用主程序默认任务")
    model_name: str = Field(
        default="", description="maibot 通道直选的具体模型名（model_config.toml 里 [[models]] 的 name），留空按任务选"
    )
    target_lang: str = Field(default="中文", description="翻译成什么语言")
    fields: Literal["title_body", "title_only"] = Field(default="title_body", description="翻译标题+正文，还是只翻译标题")
    max_chars: int = Field(default=800, description="送去翻译的正文上限（字符），0 表示正文不译")
    skip_if_chinese: bool = Field(default=True, description="原文本来就是中文就跳过，不浪费一次调用")
    max_calls_per_round: int = Field(default=5, description="单轮抓取最多调用几次翻译，超出的条目只发原文")
    api_base: str = Field(default="", description="custom 通道的 OpenAI 兼容端点，如 https://api.deepseek.com/v1")
    api_key: str = Field(default="", description="custom 通道的密钥，只写进本地 config.toml，不会打包或回显")
    custom_model: str = Field(default="", description="custom 通道的模型标识符")
    timeout_seconds: int = Field(default=20, description="翻译请求超时（秒）")


class MaiRssConfig(PluginConfigBase):
    """Mai RSS 插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    push: PushConfig = Field(default_factory=PushConfig)
    permission: PermissionConfig = Field(default_factory=PermissionConfig)
    translate: TranslateConfig = Field(default_factory=TranslateConfig)


# ============================================================================
# 订阅源解析（RSS 2.0 / RSS 1.0 RDF / Atom）
# ============================================================================

_USER_AGENT = "MaiBot-RSS/1.1 (+https://github.com/bluemika111/mai-rss)"
_MAX_BODY_BYTES = 4 * 1024 * 1024
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_WHITESPACE_RE = re.compile(r"[ \t\xa0\u3000]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def _local_name(tag: str) -> str:
    """取掉命名空间前缀，只留标签名。"""
    return tag.rsplit("}", 1)[-1]


def _strip_html(raw: str) -> str:
    """把正文里的标签、实体与控制字符清成可读纯文本。"""
    if not raw:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", raw)
    # <br>、</p> 这类换行语义用空格替换会把段落粘成一团，先转成换行
    text = re.sub(r"(?i)<\s*(?:br\s*/?|p\b[^>]*/?)\s*>", "\n", text)
    text = _TAG_STRIP_RE.sub(" ", text)
    text = html_lib.unescape(text)
    text = "".join(char if char == "\n" or ord(char) >= 32 else " " for char in text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def _parse_published(text: str) -> float:
    """解析 RFC 822 / ISO 8601 时间，失败返回 0.0。"""
    text = (text or "").strip()
    if not text:
        return 0.0
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    try:
        if parsed.tzinfo is None:
            # 无时区的发布时间按本地时间理解，避免凭空加减偏移把文章推出窗口
            return parsed.timestamp()
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


_DATE_FIELDS = ("pubdate", "published", "updated", "date", "modified")
_DATE_FIELDS_BY_PRIORITY = ("pubdate", "published", "date", "updated", "modified")


def _parse_item(element: Any) -> Optional[Dict[str, Any]]:
    """把一个 item / entry 节点解析成统一的条目字典。"""
    title = ""
    link = ""
    summary = ""
    content = ""
    key = ""
    date_texts: Dict[str, str] = {}

    for child in element:
        name = _local_name(child.tag).lower()
        text = (child.text or "").strip()
        if name == "title":
            if not title and text:
                title = text
        elif name == "link":
            href = (child.get("href") or "").strip()
            if href:
                # Atom 的链接是属性；rel 缺省即视为正文链接
                if not link and (child.get("rel") or "alternate").lower() == "alternate":
                    link = href
            elif text and not link:
                link = text
        elif name in ("description", "summary"):
            if not summary and text:
                summary = text
        elif name in ("encoded", "content", "body"):
            if not content:
                # xhtml 内容把正文嵌在子节点里
                content = text or "".join(child.itertext())
        elif name in ("guid", "id"):
            if not key and text:
                key = text
        elif name in _DATE_FIELDS:
            if text and name not in date_texts:
                date_texts[name] = text

    # 条目里可能同时有 published 和 updated；源内字段顺序没有约定，按语义优先级取值
    published_text = next((date_texts[name] for name in _DATE_FIELDS_BY_PRIORITY if name in date_texts), "")
    body = _strip_html(content or summary)
    title = _strip_html(title)
    link = _strip_html(link)
    if not (title or body or link):
        return None

    published = _parse_published(published_text)
    if not link and key and key.startswith("http"):
        link = key
    return {
        "title": title,
        "link": link,
        "text": body,
        "published": published,
        "key": key or link or f"{title}|{int(published)}",
    }


_XML_LEAD_RE = re.compile(rb"^\s*(?:<\?xml|<!--|<(?:\w+:)?(?:rss|rdf:RDF|feed))", re.I)


def parse_feed(raw: bytes, base_url: str = "") -> List[Dict[str, Any]]:
    """解析任意受支持的订阅源文本，返回按源内顺序排列的条目列表。"""
    if not _XML_LEAD_RE.match(raw[:512]) and b"<html" in raw[:1024].lower():
        # 国内不少站点把 /feed 指向网页，或在反爬时返回验证页，报错要说清楚
        raise ValueError("订阅源返回的是网页而不是 XML，该地址可能不是订阅端点（或被反爬拦截）")
    root = ElementTree.fromstring(raw)
    items: List[Dict[str, Any]] = []
    for element in root.iter():
        if _local_name(element.tag).lower() not in ("item", "entry"):
            continue
        item = _parse_item(element)
        if item is None:
            continue
        if base_url and item["link"]:
            item["link"] = urljoin(base_url, item["link"])
        items.append(item)
    return items


def download(url: str, timeout: int) -> bytes:
    """在同步上下文里抓取源地址；由调用方放进线程执行。"""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("订阅源地址必须以 http:// 或 https:// 开头")
    request = Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            "Accept-Encoding": "identity",
        },
    )
    with urlopen(request, timeout=max(int(timeout), 1)) as response:  # noqa: S310 —— 协议已在上面校验
        final_url = response.geturl()
        if not final_url.lower().startswith(("http://", "https://")):
            raise ValueError("订阅源重定向到了非 HTTP 地址")
        body = response.read(_MAX_BODY_BYTES + 1)
    if len(body) > _MAX_BODY_BYTES:
        raise ValueError("订阅源内容超过 4 MiB")
    if not body.strip():
        raise ValueError("订阅源返回空内容")
    return body


def feed_title_from_url(url: str) -> str:
    """从 URL 推一个可读的默认源名。"""
    try:
        parsed = urlparse(url)
        return (parsed.netloc or url).split(":")[0]
    except ValueError:
        return url


# ============================================================================
# 订阅数据落盘
# ============================================================================

_STORE_FILE = "subscriptions.json"
_SEEN_LIMIT_PER_FEED = 400
_BINDING_ALL = "*"


class RssStore:
    """订阅源、会话绑定、已读记录与聊天侧偏好的 JSON 仓库。

    放在插件数据目录（``data/plugins/<plugin_id>/``）而不是 config.toml：
    绑定关系是运行期高频小改动，写进配置会每次都触发插件热重载。
    ``settings`` 同理——主程序只允许内置插件管理写插件的 config.toml，
    所以聊天里能改的那几项翻译偏好只能存这里。
    """

    def __init__(self) -> None:
        self._path = ""
        self.feeds: Dict[str, Dict[str, Any]] = {}
        self.bindings: Dict[str, List[str]] = {}
        self.seen: Dict[str, List[str]] = {}
        self.seen_sets: Dict[str, set] = {}
        self.settings: Dict[str, Any] = {}
        self.last_poll_at: float = 0.0

    def bind(self, data_dir: str) -> None:
        """绑定数据目录（Runner 注入的路径只有 on_load 之后才可用）。"""
        self._path = os.path.join(data_dir, _STORE_FILE) if data_dir else ""

    def load(self) -> None:
        """从磁盘恢复订阅状态；文件不存在按空仓库处理。"""
        self.feeds = {}
        self.bindings = {}
        self.seen = {}
        self.seen_sets = {}
        self.settings = {}
        self.last_poll_at = 0.0
        if not self._path or not os.path.isfile(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"订阅数据读取失败: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("订阅数据格式不是对象")

        for feed in payload.get("feeds") or []:
            if isinstance(feed, dict) and feed.get("id"):
                feed_id = str(feed["id"])
                self.feeds[feed_id] = {
                    "id": feed_id,
                    "name": str(feed.get("name") or feed_id),
                    "url": str(feed.get("url") or ""),
                    "added_at": float(feed.get("added_at") or 0.0),
                    "last_poll_at": float(feed.get("last_poll_at") or 0.0),
                    "last_error": str(feed.get("last_error") or ""),
                    "item_count": int(feed.get("item_count") or 0),
                }
        for stream_id, tokens in (payload.get("bindings") or {}).items():
            if isinstance(tokens, list):
                self.bindings[str(stream_id)] = [str(token) for token in tokens]
        for feed_id, keys in (payload.get("seen") or {}).items():
            if isinstance(keys, list):
                trimmed = [str(key) for key in keys][-_SEEN_LIMIT_PER_FEED:]
                self.seen[str(feed_id)] = trimmed
                self.seen_sets[str(feed_id)] = set(trimmed)
        self.last_poll_at = float(payload.get("last_poll_at") or 0.0)
        raw_settings = payload.get("settings")
        if isinstance(raw_settings, dict):
            self.settings = dict(raw_settings)

    def save(self) -> None:
        """原子写盘。"""
        if not self._path:
            return
        payload = {
            "feeds": list(self.feeds.values()),
            "bindings": self.bindings,
            "seen": self.seen,
            "settings": self.settings,
            "last_poll_at": self.last_poll_at,
        }
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        temp_path = f"{self._path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, self._path)

    # ---- 订阅源 ----

    def add_feed(self, name: str, url: str) -> Dict[str, Any]:
        """登记一个订阅源并返回它的记录。"""
        feed_id = uuid.uuid4().hex[:8]
        feed = {
            "id": feed_id,
            "name": name,
            "url": url,
            "added_at": time.time(),
            "last_poll_at": 0.0,
            "last_error": "",
            "item_count": 0,
        }
        self.feeds[feed_id] = feed
        return feed

    def remove_feed(self, feed_id: str) -> None:
        """删掉订阅源，同时清理它的已读记录与各会话绑定里的引用。"""
        self.feeds.pop(feed_id, None)
        self.seen.pop(feed_id, None)
        self.seen_sets.pop(feed_id, None)
        for stream_id, tokens in list(self.bindings.items()):
            kept = [token for token in tokens if token != feed_id]
            if kept:
                self.bindings[stream_id] = kept
            else:
                self.bindings.pop(stream_id, None)

    def ordered_feeds(self) -> List[Dict[str, Any]]:
        """按加入顺序返回订阅源。"""
        return sorted(self.feeds.values(), key=lambda item: item["added_at"])

    def resolve_feed(self, token: str) -> Optional[Dict[str, Any]]:
        """按 ID 或名称定位一个订阅源，都没命中才按 /rss list 的序号。

        先 ID/名称再序号：源 ID 是 8 位十六进制，偶尔会全是数字（如 ``35237580``），
        先判 ``isdigit()`` 会把它当成越界序号而查不到。
        """
        token = (token or "").strip()
        if not token:
            return None
        feeds = self.ordered_feeds()
        lowered = token.lower()
        for feed in feeds:
            if feed["id"].lower() == lowered or feed["name"].lower() == lowered:
                return feed
        if token.isdigit():
            index = int(token)
            if 1 <= index <= len(feeds):
                return feeds[index - 1]
        return None

    def resolve_tokens(self, tokens: List[str]) -> Tuple[Optional[List[str]], str]:
        """把一批绑定记号翻译成 feed_id 列表。

        返回 ``(feed_id 列表, 错误说明)``；``all``/``none`` 是保留字，
        ``all`` 会展开成通配符 ``*`` 以便以后新增的源自动生效。
        """
        if not tokens:
            return None, "请在订阅后面写明订阅源，例如 /rss bind 1 3；或 /rss bind none 取消订阅"
        first = tokens[0].strip().lower()
        if first in ("all", "every", "*", "全部", "所有"):
            return [_BINDING_ALL], ""
        if first in ("none", "off", "clear", "无", "不订阅", "取消"):
            return [], ""

        resolved: List[str] = []
        for token in tokens:
            if token.strip() in ("*", _BINDING_ALL):
                return [_BINDING_ALL], ""
            feed = self.resolve_feed(token)
            if feed is None:
                return None, f"找不到订阅源「{token}」，用 /rss list 查看编号"
            if feed["id"] not in resolved:
                resolved.append(feed["id"])
        return resolved, ""

    # ---- 会话绑定 ----

    def set_binding(self, stream_id: str, feed_ids: List[str]) -> None:
        """设置某个会话的订阅；空列表就是「无」。"""
        if feed_ids:
            self.bindings[stream_id] = feed_ids
        else:
            self.bindings.pop(stream_id, None)

    def targets_for(self, feed_id: str) -> List[str]:
        """列出订阅了某个源的全部会话。"""
        targets: List[str] = []
        for stream_id, tokens in self.bindings.items():
            if _BINDING_ALL in tokens or feed_id in tokens:
                targets.append(stream_id)
        return targets

    def matches(self, stream_id: str, feed_id: str) -> bool:
        """判断某会话是否订阅了某源。"""
        tokens = self.bindings.get(stream_id, [])
        return _BINDING_ALL in tokens or feed_id in tokens

    # ---- 已读记录 ----

    def is_seen(self, feed_id: str, key: str) -> bool:
        return key in self.seen_sets.get(feed_id, set())

    def mark_seen(self, feed_id: str, key: str) -> None:
        keys = self.seen.setdefault(feed_id, [])
        seen = self.seen_sets.setdefault(feed_id, set())
        if key in seen:
            return
        keys.append(key)
        seen.add(key)
        while len(keys) > _SEEN_LIMIT_PER_FEED:
            dropped = keys.pop(0)
            seen.discard(dropped)


# ============================================================================
# 常量与文案
# ============================================================================

# 前缀放行 "@昵称 "：群聊里消息常带 At 段，只锚定 ^/ 会永远匹配不上。
_COMMAND_PATTERN = r"(?i)^(?P<prefix>(?:@\S+\s+)*)/(?:rss|订阅)(?P<arg>(?:\s+.*)?)$"

_HELP_TEXT = """Mai RSS 订阅

订阅源管理：
  /rss list                 查看全部订阅源
  /rss add <URL> [名称]      新增订阅源（不写名称则用域名）
  /rss del <编号|名称>        删除订阅源
  /rss check [编号]          立即抓取一次
  /rss preview <编号|名称>    试发最新一条，看看推送效果

本会话订阅（无 / 单个 / 多个）：
  /rss bind <编号...>        订阅这些源（覆盖当前设置）
  /rss bind all              订阅全部源（以后新增的源也自动生效）
  /rss bind none             本会话不订阅任何源
  /rss unbind <编号...|all>  从本会话去掉某些订阅
  /rss status                查看本会话订阅与抓取状态

翻译（推送时在标题和正文后附译文）：
  /rss translate              查看当前翻译设置
  /rss translate on|off       临时开关（存进插件数据，重启仍有效）
  /rss translate list         列出主程序 model_config.toml 里配过的模型
  /rss translate model <编号|名称>  换翻译模型；clear 恢复用配置里的设置

编号可用序号、名称或 ID。"""


def _format_time(timestamp: float) -> str:
    if not timestamp:
        return datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return datetime.now().strftime("%Y-%m-%d %H:%M")


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "……"


# ============================================================================
# 翻译：提示词、输出解析、模型目录、译文缓存
# ============================================================================

_TRANSLATE_CACHE_FILE = "translate_cache.json"
_TRANSLATE_CACHE_LIMIT = 2000
# 中日韩汉字 + 假名 + 谚文；用来判断一段原文是否本来就值得翻
_CJK_RE = re.compile(r"[㐀-䶿一-鿿぀-ヿ가-힯]")
_TITLE_MARKERS = ("T|", "t|", "T:", "T：", "标题：", "标题|")
_BODY_MARKERS = ("B|", "b|", "B:", "B：", "正文：", "正文|")
_MODEL_CONFIG_RELATIVE = os.path.join("config", "model_config.toml")
# state-monitor 在这台机器上用的绝对路径，作为推不出根目录时的兜底
_MODEL_CONFIG_FALLBACKS = ("/MaiMBot/config/model_config.toml", _MODEL_CONFIG_RELATIVE)


def looks_chinese(text: str, threshold: float = 0.1) -> bool:
    """非空白字符里中日韩文字占比过阈值就当作中文原文，不必再翻。

    阈值压到 0.1 是因为状态类订阅的标题常是「Nov 4, 16:05 UTC 调查中 - Actions」
    这种中英混排：汉字不多但整句已经能读懂。
    """
    counted = [char for char in (text or "") if not char.isspace()]
    if not counted:
        return False
    hits = sum(1 for char in counted if _CJK_RE.match(char))
    return hits / len(counted) >= threshold


def build_translate_prompt(title: str, body: str, target_lang: str, want_body: bool) -> str:
    """一次调用翻标题和正文，所以要求模型固定输出 T| / B| 两行。"""
    lang = (target_lang or "中文").strip()
    lines = [f"把下面的 RSS 内容翻译成{lang}。", "只输出译文，不要解释、不要引号、不要 Markdown。"]
    if title:
        first = "第一行以 T| 开头，是标题的译文。" if want_body else "只输出一行，以 T| 开头，是标题的译文。"
        lines.append(first)
    if want_body:
        lines.append("第二行以 B| 开头，是正文的译文。")
    lines.append("")
    if title:
        lines.append(f"标题：{title}")
    if want_body and body:
        lines.append(f"正文：{body}")
    return "\n".join(lines)


def _pick_marked(lines: List[str], markers: Tuple[str, ...]) -> str:
    for line in lines:
        text = line.strip()
        for marker in markers:
            if text.startswith(marker):
                return text[len(marker) :].strip()
    return ""


def parse_translate_reply(raw: str, want_title: bool, want_body: bool) -> Tuple[str, str]:
    """取出标题/正文译文；一行都没解析出来时返回两个空串，由调用方退回原文。"""
    lines = (raw or "").strip().splitlines()
    title = _pick_marked(lines, _TITLE_MARKERS) if want_title else ""
    body = _pick_marked(lines, _BODY_MARKERS) if want_body else ""
    if not title and not body and lines and want_title:
        # 模型只给了一行且没带标记：当成标题译文，别把这次调用整个浪费掉
        title = lines[0].strip()
    return title, body


def translate_cache_key(target_lang: str, model_desc: str, title: str, body: str) -> str:
    """缓存键带上语言与模型：换语言或换模型都该重翻，而不是沿用旧译文。"""
    digest = hashlib.sha1(f"{target_lang}\n{model_desc}\n{title}\n{body}".encode("utf-8")).hexdigest()
    return digest[:20]


class TranslateCache:
    """``原文指纹 -> 译文`` 的落盘缓存，重复推送和预览不再二次调用模型。"""

    def __init__(self) -> None:
        self._path = ""
        self._entries: Dict[str, Dict[str, str]] = {}

    def bind(self, data_dir: str) -> None:
        self._path = os.path.join(data_dir, _TRANSLATE_CACHE_FILE) if data_dir else ""

    def load(self) -> None:
        self._entries = {}
        if not self._path or not os.path.isfile(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"译文缓存读取失败: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("译文缓存格式不是对象")
        for key, value in payload.items():
            if isinstance(value, dict):
                self._entries[str(key)] = {
                    "title": str(value.get("title") or ""),
                    "body": str(value.get("body") or ""),
                }

    def save(self) -> None:
        if not self._path:
            return
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        temp_path = f"{self._path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(self._entries, handle, ensure_ascii=False, indent=1)
        os.replace(temp_path, self._path)

    def get(self, key: str) -> Optional[Tuple[str, str]]:
        entry = self._entries.get(key)
        if entry is None:
            return None
        return entry["title"], entry["body"]

    def put(self, key: str, title: str, body: str) -> None:
        self._entries[key] = {"title": title, "body": body}
        while len(self._entries) > _TRANSLATE_CACHE_LIMIT:
            # dict 保序，超上限就丢掉最早写入的一条
            self._entries.pop(next(iter(self._entries)))

    def __len__(self) -> int:
        return len(self._entries)


def model_config_candidates(data_dir: str = "") -> List[str]:
    """model_config.toml 的候选路径。

    从插件数据目录逐级上推到主程序根目录，而不是像别的插件那样硬编码
    ``/MaiMBot``——容器里换了挂载点路径就找不到了。
    """
    paths: List[str] = []
    if data_dir:
        current = os.path.abspath(data_dir)
        for _ in range(4):
            current = os.path.dirname(current)
            candidate = os.path.join(current, _MODEL_CONFIG_RELATIVE)
            if candidate not in paths:
                paths.append(candidate)
    for fallback in _MODEL_CONFIG_FALLBACKS:
        absolute = os.path.abspath(fallback)
        if absolute not in paths:
            paths.append(absolute)
    return paths


def filter_llm_models(models: Any) -> List[Dict[str, str]]:
    """从 ``[[models]]`` 里挑出可调用的 LLM 并带上所属服务商。

    必须按 ``model_type`` 过滤：嵌入模型混进下拉后被选中，generate 会直接失败。
    """
    picked: List[Dict[str, str]] = []
    seen: set = set()
    for item in models or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("model_type") or "llm") != "llm":
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        picked.append(
            {
                "name": name,
                "provider": str(item.get("api_provider") or "").strip() or "未分组",
                "identifier": str(item.get("model_identifier") or "").strip(),
            }
        )
    return picked


def _load_toml_file(path: str) -> Dict[str, Any]:
    if tomllib is not None:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    if tomlkit is not None:
        with open(path, "r", encoding="utf-8") as handle:
            return dict(tomlkit.parse(handle.read()))
    raise RuntimeError("当前解释器既没有 tomllib 也没有 tomlkit，读不了 model_config.toml")


def read_model_catalog(candidates: List[str]) -> Tuple[List[Dict[str, str]], str, str]:
    """读第一个存在的模型配置文件，返回 (模型列表, 命中路径, 错误说明)。"""
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            data = _load_toml_file(path)
        except Exception as exc:
            return [], path, f"读取 {path} 失败：{exc}"
        return filter_llm_models(data.get("models")), path, ""
    return [], "", "没找到 model_config.toml"


def post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: int) -> Dict[str, Any]:
    """POST JSON 并解析 JSON 响应，供 custom 翻译通道使用。"""
    request = Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        request.add_header(key, value)
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise ValueError(f"翻译接口返回的不是 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("翻译接口返回的结构不是对象")
    return parsed


def extract_openai_text(payload: Dict[str, Any]) -> str:
    """从 OpenAI 兼容响应里取 ``choices[0].message.content``。"""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError(f"翻译接口没有 choices：{str(payload)[:200]}")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("翻译接口的 message.content 是空的")
    return content.strip()


# ============================================================================
# 插件主体
# ============================================================================


class MaiRssPlugin(MaiBotPlugin):
    """抓取 RSS / Atom 源并把新文章推送到订阅了它的聊天流。"""

    config_model = MaiRssConfig

    def __init__(self) -> None:
        super().__init__()
        self.store = RssStore()
        self.translate_cache = TranslateCache()
        self._poll_task: Optional[asyncio.Task] = None
        self._poll_lock = asyncio.Lock()
        self._store_lock = asyncio.Lock()
        self._plugin_permission_cache: List[str] = []
        # 模型目录按文件 mtime 重读，避免每条推送都解析一遍 model_config.toml
        self._catalog: List[Dict[str, str]] = []
        self._catalog_path = ""
        self._catalog_mtime = 0.0
        self._catalog_error = ""
        self._round_translate_calls = 0
        self._translate_errors = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        self.store.bind(str(self.ctx.paths.data_dir))
        self.translate_cache.bind(str(self.ctx.paths.data_dir))
        try:
            await asyncio.to_thread(self.store.load)
        except RuntimeError as exc:
            # 数据坏了就退回空仓库，但不能让它悄悄坏着：记一条明确的错误日志
            self.ctx.logger.error("订阅数据不可用，本轮从空状态开始: %s", exc)
            self.store = RssStore()
            self.store.bind(str(self.ctx.paths.data_dir))
        try:
            await asyncio.to_thread(self.translate_cache.load)
        except RuntimeError as exc:
            self.ctx.logger.error("译文缓存不可用，本轮从空缓存开始: %s", exc)
            self.translate_cache = TranslateCache()
            self.translate_cache.bind(str(self.ctx.paths.data_dir))
        await self._refresh_operator_cache()
        self._poll_task = asyncio.create_task(self._poll_loop())
        self.ctx.logger.info(
            "Mai RSS 已加载：%d 个订阅源，%d 个会话有订阅，间隔 %d 分钟，翻译%s（缓存 %d 条）",
            len(self.store.feeds),
            len(self.store.bindings),
            max(int(self.config.fetch.poll_interval_minutes), 1),
            "开启" if self._translate_enabled() else "关闭",
            len(self.translate_cache),
        )

    async def on_unload(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        self.ctx.logger.info("Mai RSS 配置已更新: scope=%s version=%s", scope, version)

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """后台抓取循环：睡一个间隔、抓一轮，如此往复。"""
        while True:
            delay = max(int(self.config.fetch.poll_interval_minutes), 1) * 60
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            if not self.config.fetch.enabled:
                continue
            try:
                await self._poll_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.error("定时抓取失败: %s", exc, exc_info=True)

    async def _poll_cycle(self, only_feed_id: Optional[str] = None) -> Dict[str, int]:
        """抓取一轮并推送新条目，返回统计。"""
        async with self._poll_lock:
            stats = {"feeds": 0, "new": 0, "sent": 0, "errors": 0}
            self._round_translate_calls = 0
            feeds = self.store.ordered_feeds()
            if only_feed_id:
                feeds = [feed for feed in feeds if feed["id"] == only_feed_id]
            for feed in feeds:
                stats["feeds"] += 1
                try:
                    pushed = await self._poll_feed(feed)
                    stats["new"] += pushed
                    stats["sent"] += pushed
                except Exception as exc:
                    stats["errors"] += 1
                    feed["last_error"] = str(exc)[:200]
                    self.ctx.logger.warning("订阅源 %s 抓取失败: %s", feed["name"], exc)
                feed["last_poll_at"] = time.time()
            if stats["feeds"]:
                self.store.last_poll_at = time.time()
                await self._persist()
            return stats

    async def _poll_feed(self, feed: Dict[str, Any]) -> int:
        """抓一个源，推送其中未读且在时间窗口内的条目。"""
        items = await self._fetch_items(feed)
        feed["item_count"] = len(items)
        feed["last_error"] = ""

        limit = max(int(self.config.fetch.max_items_per_feed), 1)
        lookback = max(int(self.config.push.lookback_hours), 0)
        window_start = time.time() - lookback * 3600 if lookback else 0.0

        # 源内顺序没有标准规定，统一按发布时间从旧到新推送；缺时间的条目稳定地排在最前
        pending: List[Dict[str, Any]] = []
        for item in items:
            if self.store.is_seen(feed["id"], item["key"]):
                continue
            self.store.mark_seen(feed["id"], item["key"])
            if window_start and item["published"] and item["published"] < window_start:
                continue
            pending.append(item)
        pending.sort(key=lambda entry: entry["published"])

        if not pending:
            return 0
        if len(pending) > limit:
            # 积压太厚时只推最近几条，其余静默标记已读，避免刷屏
            skipped = len(pending) - limit
            pending = pending[-limit:]
            self.ctx.logger.info("订阅源 %s 积压 %d 条未推送", feed["name"], skipped)
            await self._notify_overflow(feed, skipped)

        sent = 0
        for item in pending:
            sent += await self._deliver(feed, item)
        return sent

    async def _notify_overflow(self, feed: Dict[str, Any], skipped: int) -> None:
        """告知被截断的条数，让聊天里的人知道有内容没发出来。"""
        if skipped <= 0:
            return
        targets = self.store.targets_for(feed["id"])
        if not targets:
            return
        text = f"📰 {feed['name']}：本轮有 {skipped} 条较早的文章未推送（超出每轮上限），如需补看请调大 max_items_per_feed"
        for stream_id in targets:
            await self.ctx.send.text(text, stream_id)
            await asyncio.sleep(max(float(self.config.push.send_delay_seconds), 0.0))

    async def _deliver(self, feed: Dict[str, Any], item: Dict[str, Any]) -> int:
        """把一条新条目发给所有订阅了该源的会话。"""
        targets = self.store.targets_for(feed["id"])
        if not targets:
            return 0
        # 先翻译再渲染：一条条目只调一次模型，多个会话共用同一份译文
        translation = await self._translate_for(item)
        text = self._render(feed, item, translation)
        sent = 0
        for stream_id in targets:
            try:
                ok = await self.ctx.send.text(text, stream_id)
            except Exception as exc:
                self.ctx.logger.warning("推送到 %s 失败: %s", stream_id, exc)
                continue
            if ok:
                sent += 1
            else:
                self.ctx.logger.warning("推送到 %s 未成功（会话可能已失效）", stream_id)
            await asyncio.sleep(max(float(self.config.push.send_delay_seconds), 0.0))
        return sent

    def _render(self, feed: Dict[str, Any], item: Dict[str, Any], translation: Tuple[str, str] = ("", "")) -> str:
        """渲染推送文本：来源、标题、时间、正文、链接，译文用括号附在后面。"""
        translated_title, translated_body = translation
        lines = [f"📡 RSS/Atom 拉取 • {feed['name']}"]
        if item["title"]:
            title_line = f"🗞 标题：{item['title']}"
            if translated_title:
                title_line += f"（{translated_title}）"
            lines.append(title_line)
        lines.append(f"🕒 时间：{_format_time(item['published'])}")
        body = _truncate(item["text"], max(int(self.config.push.max_content_chars), 0))
        if body:
            lines.append(body)
            if translated_body:
                # 正文长，同行括号会挤成一团，单独一行更好读
                lines.append(f"（{translated_body}）")
        if self.config.push.include_link and item["link"]:
            lines.append(f"🔗 {item['link']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 翻译
    # ------------------------------------------------------------------

    def _translate_enabled(self) -> bool:
        """聊天里改过的开关优先于 config.toml（插件无权写自己的配置文件）。"""
        override = self.store.settings.get("translate_enabled")
        if isinstance(override, bool):
            return override
        return bool(self.config.translate.enabled)

    def _translate_model_name(self) -> str:
        """maibot 通道实际用的具体模型名：聊天/WebUI 选的覆盖配置里写的。"""
        picked = str(self.store.settings.get("translate_model") or "").strip()
        return picked or str(self.config.translate.model_name or "").strip()

    def _translate_route_text(self) -> str:
        """当前翻译通道的一句话说明，供命令与状态展示。"""
        cfg = self.config.translate
        if cfg.provider == "custom":
            model = str(cfg.custom_model or "").strip() or "未填模型"
            return f"自定义 API · {cfg.api_base or '未填地址'} · {model}"
        model = self._translate_model_name()
        if model:
            return f"主程序模型 · {model}"
        task = str(cfg.task_name or "").strip()
        return f"主程序模型 · 任务 {task}" if task else "主程序模型 · 主程序默认任务"

    def _translate_source_body(self, item: Dict[str, Any]) -> str:
        """送去翻译的正文：跟推送里显示的那段一致，再按翻译上限截一次。"""
        display = max(int(self.config.push.max_content_chars), 0)
        cap = max(int(self.config.translate.max_chars), 0)
        limits = [value for value in (display, cap) if value]
        return _truncate(item.get("text") or "", min(limits) if limits else 0)

    async def _translate_for(self, item: Dict[str, Any]) -> Tuple[str, str]:
        """取一条条目的译文；失败只记日志，永远不影响推送。"""
        cfg = self.config.translate
        if not self._translate_enabled():
            return "", ""
        title = (item.get("title") or "").strip()
        body = self._translate_source_body(item)
        want_title = bool(title)
        want_body = bool(body) and cfg.fields == "title_body"
        if not want_title and not want_body:
            return "", ""
        if cfg.skip_if_chinese and looks_chinese(f"{title}{body}"):
            return "", ""

        model_desc = self._translate_model_name() or f"task:{str(cfg.task_name or '').strip() or '默认'}"
        key = translate_cache_key(cfg.target_lang, f"{cfg.provider}:{model_desc}", title, body if want_body else "")
        cached = self.translate_cache.get(key)
        if cached is not None:
            return cached

        budget = max(int(cfg.max_calls_per_round), 0)
        if budget and self._round_translate_calls >= budget:
            self.ctx.logger.info("本轮翻译已达 %d 次上限，剩余条目只发原文", budget)
            return "", ""
        self._round_translate_calls += 1

        try:
            raw = await self._call_translator(title if want_title else "", body, want_title, want_body)
        except Exception as exc:
            self._translate_errors += 1
            self.ctx.logger.warning("翻译失败，本条只发原文: %s", exc)
            return "", ""
        translated_title, translated_body = parse_translate_reply(raw, want_title, want_body)
        if not translated_title and not translated_body:
            self._translate_errors += 1
            self.ctx.logger.warning("翻译输出解析不出 T|/B| 行，本条只发原文: %r", (raw or "")[:160])
            return "", ""
        self.translate_cache.put(key, translated_title, translated_body)
        await asyncio.to_thread(self.translate_cache.save)
        return translated_title, translated_body

    async def _call_translator(self, title: str, body: str, want_title: bool, want_body: bool) -> str:
        cfg = self.config.translate
        prompt = build_translate_prompt(title, body, str(cfg.target_lang or "中文"), want_body)
        if cfg.provider == "custom":
            return await self._call_custom_translator(prompt)
        kwargs: Dict[str, Any] = {"prompt": prompt}
        model = self._translate_model_name()
        if model:
            # 直选具体模型：Host 会跳过任务的选模策略，名字不在 model_config 里就报错
            kwargs["model_name"] = model
        elif str(cfg.task_name or "").strip():
            kwargs["task_name"] = str(cfg.task_name).strip()
        result = await self.ctx.llm.generate(**kwargs)
        if not isinstance(result, dict):
            raise RuntimeError(f"主程序 llm.generate 返回了非字典结果：{type(result).__name__}")
        if not result.get("success", False):
            raise RuntimeError(f"主程序模型翻译失败：{result.get('error') or '未知错误'}")
        text = str(result.get("response") or "").strip()
        if not text:
            raise RuntimeError("主程序模型返回空文本")
        return text

    async def _call_custom_translator(self, prompt: str) -> str:
        cfg = self.config.translate
        base = str(cfg.api_base or "").strip().rstrip("/")
        model = str(cfg.custom_model or "").strip()
        if not base.lower().startswith(("http://", "https://")):
            raise ValueError("自定义翻译端点必须以 http:// 或 https:// 开头")
        if not model:
            raise ValueError("自定义翻译通道还需要填 custom_model（模型标识符）")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        timeout = max(int(cfg.timeout_seconds), 5)
        parsed = await asyncio.to_thread(post_json, f"{base}/chat/completions", payload, headers, timeout)
        error = parsed.get("error")
        if error:
            detail = error.get("message") if isinstance(error, dict) else error
            raise ValueError(f"翻译接口报错：{str(detail)[:200]}")
        return extract_openai_text(parsed)

    def _model_catalog(self) -> Tuple[List[Dict[str, str]], str, str]:
        """主程序已配置的模型列表（同步读盘，调用方用 to_thread 包）。"""
        candidates = model_config_candidates(str(self.ctx.paths.data_dir))
        path = next((candidate for candidate in candidates if os.path.isfile(candidate)), "")
        if not path:
            self._catalog, self._catalog_path, self._catalog_error = [], "", "没找到 model_config.toml"
            return self._catalog, self._catalog_path, self._catalog_error
        try:
            mtime = os.path.getmtime(path)
        except OSError as exc:
            self._catalog_error = f"读取 {path} 时间戳失败：{exc}"
            return list(self._catalog), self._catalog_path, self._catalog_error
        if path == self._catalog_path and mtime == self._catalog_mtime:
            return list(self._catalog), self._catalog_path, self._catalog_error
        models, hit, error = read_model_catalog([path])
        self._catalog = models
        self._catalog_path = hit or path
        self._catalog_error = error
        self._catalog_mtime = 0.0 if error else mtime
        return list(models), self._catalog_path, error

    async def _llm_task_names(self) -> List[str]:
        """主程序的模型任务名列表；读不到就返回空，不抛给聊天。"""
        try:
            result = await self.ctx.llm.get_available_models()
        except Exception as exc:
            self.ctx.logger.warning("读取主程序模型任务列表失败: %s", exc)
            return []
        if isinstance(result, list):
            return [str(name) for name in result if str(name).strip()]
        self.ctx.logger.warning("llm.get_available_models 返回了非列表结果: %r", result)
        return []

    def _resolve_model_token(self, token: str) -> Optional[Dict[str, str]]:
        """把编号或名称翻译成一条模型记录。"""
        token = (token or "").strip()
        if not token:
            return None
        if token.isdigit() and 1 <= int(token) <= len(self._catalog):
            return self._catalog[int(token) - 1]
        lowered = token.lower()
        for model in self._catalog:
            if model["name"].lower() == lowered or model["identifier"].lower() == lowered:
                return model
        return None

    async def _fetch_items(self, feed: Dict[str, Any]) -> List[Dict[str, Any]]:
        """抓取并解析一个源。"""
        raw = await asyncio.to_thread(download, feed["url"], int(self.config.fetch.request_timeout_seconds))
        try:
            return parse_feed(raw, feed["url"])
        except ElementTree.ParseError as exc:
            raise ValueError(f"不是合法的 RSS/Atom XML：{exc}") from exc

    async def _persist(self) -> None:
        async with self._store_lock:
            await asyncio.to_thread(self.store.save)

    async def _baseline(self, feed: Dict[str, Any]) -> int:
        """新源首次抓取：把现存条目全部记为已读，建立不刷屏的起点。"""
        items = await self._fetch_items(feed)
        feed["item_count"] = len(items)
        feed["last_poll_at"] = time.time()
        feed["last_error"] = ""
        for item in items:
            self.store.mark_seen(feed["id"], item["key"])
        await self._persist()
        return len(items)

    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------

    async def _refresh_operator_cache(self) -> None:
        """把主程序 [plugin].permission 读进本地副本。"""
        try:
            value = await self.ctx.config.get("plugin.permission", [])
        except Exception as exc:
            self.ctx.logger.warning("读取 [plugin].permission 失败: %s", exc)
            return
        if isinstance(value, list):
            self._plugin_permission_cache = [str(item) for item in value]

    def _operator_candidates(self) -> List[str]:
        configured = [str(item).strip() for item in (self.config.permission.admins or []) if str(item).strip()]
        if configured:
            return configured
        return [str(item).strip() for item in self._plugin_permission_cache if str(item).strip()]

    def _is_operator(self, sender: str) -> bool:
        if not sender:
            return False
        return f"qq:{sender}" in self._operator_candidates()

    def _allowed(self, user_id: str, is_local_operator: bool) -> bool:
        """命令是否可用：本地终端、或管理员列表里的用户。"""
        if not self.config.permission.admin_only or is_local_operator:
            return True
        return self._is_operator(user_id)

    # ------------------------------------------------------------------
    # 聊天命令
    # ------------------------------------------------------------------

    @Command(
        "rss",
        description="订阅 RSS / Atom 源：/rss help 查看全部指令",
        pattern=_COMMAND_PATTERN,
    )
    async def handle_rss(self, stream_id: str = "", user_id: str = "", is_local_operator: bool = False, **kwargs: Any):
        """处理 /rss 命令族。"""
        matched = kwargs.get("matched_groups") or {}
        raw_arg = str(matched.get("arg") or "").strip() if isinstance(matched, dict) else ""
        tokens = raw_arg.split()
        sub = tokens[0].lower() if tokens else "help"
        rest = tokens[1:]

        if not self._allowed(user_id, is_local_operator):
            return await self._reply("只有管理员能管理 RSS 订阅。", stream_id)

        handler = self._routes.get(sub)
        if handler is None:
            return await self._reply(f"不认识的操作：{sub}\n\n{_HELP_TEXT}", stream_id)
        return await self._reply(await handler(stream_id, rest, kwargs), stream_id)

    async def _reply(self, text: str, stream_id: str) -> Tuple[bool, str, int]:
        """把命令输出发到当前会话。

        Host 只把 Command 的 response 写进日志（``命令处理完成，跳过后续消息处理: ...``），
        不会替插件回话，所以正文必须自己 send.text；response 留一句摘要供日志排查。
        """
        if not stream_id:
            # 本地终端没有可发送的会话，正文进日志
            return True, text, 1
        if not await self.ctx.send.text(text, stream_id):
            self.ctx.logger.warning("RSS 命令回复发送失败: stream_id=%s", stream_id)
        return True, "RSS 命令已回复", 1

    @property
    def _routes(self) -> Dict[str, Any]:
        return {
            "": self._cmd_help,
            "help": self._cmd_help,
            "?": self._cmd_help,
            "list": self._cmd_list,
            "feeds": self._cmd_list,
            "add": self._cmd_add,
            "del": self._cmd_del,
            "remove": self._cmd_del,
            "bind": self._cmd_bind,
            "subscribe": self._cmd_bind,
            "unbind": self._cmd_unbind,
            "unsubscribe": self._cmd_unbind,
            "status": self._cmd_status,
            "check": self._cmd_check,
            "preview": self._cmd_preview,
            "translate": self._cmd_translate,
            "翻译": self._cmd_translate,
        }

    async def _cmd_help(self, stream_id: str, rest: List[str], kwargs: Any) -> str:
        del stream_id, rest, kwargs
        return _HELP_TEXT

    async def _cmd_list(self, stream_id: str, rest: List[str], kwargs: Any) -> str:
        del stream_id, rest, kwargs
        feeds = self.store.ordered_feeds()
        if not feeds:
            return "还没有订阅源。用 /rss add <URL> [名称] 添加一个。"
        lines = [f"📚 订阅源 {len(feeds)} 个"]
        for index, feed in enumerate(feeds, start=1):
            mark = "⚠️" if feed["last_error"] else "✅"
            lines.append(
                f"{index}. {mark} {feed['name']} · {feed['item_count']} 条 · 上次 {_format_time(feed['last_poll_at']) if feed['last_poll_at'] else '从未'}"
            )
            lines.append(f"   {feed['url']}")
            if feed["last_error"]:
                lines.append(f"   错误：{feed['last_error']}")
        return "\n".join(lines)

    async def _cmd_add(self, stream_id: str, rest: List[str], kwargs: Any) -> str:
        del kwargs
        if not rest:
            return "用法：/rss add <URL> [名称]"
        url = rest[0].strip()
        name = " ".join(rest[1:]).strip() or feed_title_from_url(url)
        if not url.lower().startswith(("http://", "https://")):
            return "订阅源地址必须以 http:// 或 https:// 开头。"
        for feed in self.store.feeds.values():
            if feed["url"] == url:
                return f"这个地址已经是订阅源「{feed['name']}」了。"

        feed = self.store.add_feed(name, url)
        try:
            count = await self._baseline(feed)
        except Exception as exc:
            self.store.remove_feed(feed["id"])
            self.ctx.logger.warning("新增订阅源失败: %s", exc)
            return f"添加失败：{exc}"

        await self._persist()
        bound = len(self.store.targets_for(feed["id"]))
        return (
            f"已添加订阅源「{name}」（{feed['id']}），当前有 {count} 条历史文章已记为已读。\n"
            f"现在订阅它的会话：{bound} 个。用 /rss bind {len(self.store.ordered_feeds())} 让本会话订阅。"
        )

    async def _cmd_del(self, stream_id: str, rest: List[str], kwargs: Any) -> str:
        del stream_id, kwargs
        if not rest:
            return "用法：/rss del <编号|名称|ID>"
        feed = self.store.resolve_feed(rest[0])
        if feed is None:
            return f"找不到订阅源「{rest[0]}」，用 /rss list 查看编号。"
        self.store.remove_feed(feed["id"])
        await self._persist()
        return f"已删除订阅源「{feed['name']}」（{feed['id']}）。"

    async def _cmd_bind(self, stream_id: str, rest: List[str], kwargs: Any) -> str:
        del kwargs
        if not stream_id:
            return "取不到当前会话 ID，无法设置订阅。"
        feed_ids, error = self.store.resolve_tokens(rest)
        if error:
            return error
        self.store.set_binding(stream_id, feed_ids or [])
        await self._persist()
        if not feed_ids:
            return "本会话已取消全部 RSS 订阅。"
        if feed_ids == [_BINDING_ALL]:
            return f"本会话已订阅全部 {len(self.store.feeds)} 个源，以后新增的源也会自动生效。"
        names = "、".join(self.store.feeds[feed_id]["name"] for feed_id in feed_ids if feed_id in self.store.feeds)
        return f"本会话已订阅：{names}。"

    async def _cmd_unbind(self, stream_id: str, rest: List[str], kwargs: Any) -> str:
        del kwargs
        if not stream_id:
            return "取不到当前会话 ID，无法修改订阅。"
        current = list(self.store.bindings.get(stream_id, []))
        if not current:
            return "本会话本来就没有订阅任何源。"
        feed_ids, error = self.store.resolve_tokens(rest)
        if error:
            return error
        if feed_ids is None or feed_ids == [] or feed_ids == [_BINDING_ALL]:
            self.store.set_binding(stream_id, [])
            await self._persist()
            return "本会话已取消全部 RSS 订阅。"
        kept = [token for token in current if token not in feed_ids]
        self.store.set_binding(stream_id, kept)
        await self._persist()
        if not kept:
            return "本会话已取消这些订阅，现在不订阅任何源。"
        names = "、".join(self.store.feeds[feed_id]["name"] for feed_id in kept if feed_id in self.store.feeds)
        return f"本会话现在订阅：{names}。"

    async def _cmd_status(self, stream_id: str, rest: List[str], kwargs: Any) -> str:
        del rest, kwargs
        total_targets = len(self.store.bindings)
        streams, streams_error = await self._list_streams()
        lines = [
            f"⚙️ 抓取{'开启' if self.config.fetch.enabled else '关闭'}，间隔 {max(int(self.config.fetch.poll_interval_minutes), 1)} 分钟",
            f"📚 订阅源 {len(self.store.feeds)} 个，🧩 有订阅的会话 {total_targets} 个",
            f"🌐 翻译：{'开启' if self._translate_enabled() else '关闭'} · {self._translate_route_text()}",
        ]
        if streams_error:
            lines.append(f"⚠️ 取不到主程序聊天流：{streams_error}")
        else:
            lines.append(f"🧭 主程序聊天流 {len(streams)} 个")
        lines.append(
            f"🕒 上次抓取：{_format_time(self.store.last_poll_at)}" if self.store.last_poll_at else "🕒 还没抓过"
        )
        tokens = self.store.bindings.get(stream_id, [])
        if not tokens:
            lines.append("本会话：未订阅任何源（/rss bind 1 或 /rss bind all 开始订阅）")
        elif tokens == [_BINDING_ALL]:
            lines.append("本会话：订阅全部源")
        else:
            names = [self.store.feeds.get(feed_id, {}).get("name", feed_id) for feed_id in tokens]
            lines.append(f"本会话：{'、'.join(names)}")
        return "\n".join(lines)

    async def _cmd_check(self, stream_id: str, rest: List[str], kwargs: Any) -> str:
        del stream_id, kwargs
        only_feed_id = None
        if rest:
            feed = self.store.resolve_feed(rest[0])
            if feed is None:
                return f"找不到订阅源「{rest[0]}」。"
            only_feed_id = feed["id"]
        if not self.store.feeds:
            return "还没有订阅源。"
        stats = await self._poll_cycle(only_feed_id)
        return f"抓取完成：{stats['feeds']} 个源，新推送 {stats['new']} 条，失败 {stats['errors']} 个。"

    async def _cmd_preview(self, stream_id: str, rest: List[str], kwargs: Any) -> str:
        del stream_id, kwargs
        if not rest:
            return "用法：/rss preview <编号|名称|ID>"
        feed = self.store.resolve_feed(rest[0])
        if feed is None:
            return f"找不到订阅源「{rest[0]}」。"
        try:
            items = await self._fetch_items(feed)
        except Exception as exc:
            return f"抓取失败：{exc}"
        if not items:
            return "这个源里没有解析到任何条目。"
        newest = max(items, key=lambda entry: entry["published"])
        # 直接返回渲染结果，由 _reply 发到本会话：预览本身就是一条推送，不该再多发一句说明
        return self._render(feed, newest, await self._translate_for(newest))

    async def _cmd_translate(self, stream_id: str, rest: List[str], kwargs: Any) -> str:
        """查看与切换翻译：开关和模型存在插件自己的数据里，改完立即生效。"""
        del stream_id, kwargs
        action = (rest[0].lower() if rest else "show")
        args = rest[1:]

        if action in ("on", "开", "开启", "enable"):
            self.store.settings["translate_enabled"] = True
            await self._persist()
            return f"已开启翻译，通道：{self._translate_route_text()}。"
        if action in ("off", "no", "关", "关闭", "disable"):
            self.store.settings["translate_enabled"] = False
            await self._persist()
            return "已关闭翻译，之后只发原文。"
        if action in ("list", "models", "模型"):
            return await self._describe_translate_models()
        if action in ("model", "use"):
            if not args:
                return "用法：/rss translate model <编号|名称|clear>"
            token = " ".join(args).strip()
            if token.lower() in ("clear", "none", "reset", "默认"):
                if self.store.settings.pop("translate_model", None) is None:
                    return f"聊天里没有覆盖过模型，当前就用配置里的设置：{self._translate_route_text()}"
                await self._persist()
                return f"已恢复用配置里的设置：{self._translate_route_text()}"
            models, _, error = await asyncio.to_thread(self._model_catalog)
            if error or not models:
                return f"读不到主程序模型列表：{error or 'model_config.toml 里没有 llm 模型'}"
            picked = self._resolve_model_token(token)
            if picked is None:
                return f"主程序里没有这个模型：{token}\n用 /rss translate list 查看编号。"
            self.store.settings["translate_model"] = picked["name"]
            await self._persist()
            return f"翻译模型已设为「{picked['name']}」（{picked['provider']}）。"
        return self._translate_status_text()

    def _translate_status_text(self) -> str:
        cfg = self.config.translate
        lines = [
            f"🌐 翻译：{'开启' if self._translate_enabled() else '关闭'} · {self._translate_route_text()}",
            f"🈶 目标语言：{cfg.target_lang or '中文'} · 范围：{'标题+正文' if cfg.fields == 'title_body' else '仅标题'}",
            f"📏 送译正文上限 {max(int(cfg.max_chars), 0)} 字 · 单轮最多 {max(int(cfg.max_calls_per_round), 0)} 次"
            f" · 中文原文{'跳过' if cfg.skip_if_chinese else '照翻'}",
            f"🗂 译文缓存 {len(self.translate_cache)} 条 · 本会话累计翻译失败 {self._translate_errors} 次",
            "改这些参数请到 WebUI 的插件配置页 [翻译] 一节；聊天里能改开关和模型。",
        ]
        return "\n".join(lines)

    async def _describe_translate_models(self) -> str:
        models, path, error = await asyncio.to_thread(self._model_catalog)
        if not models:
            lines = [f"⚠️ 读不到主程序模型列表：{error or path or '空'}"]
            tasks = await self._llm_task_names()
            if tasks:
                lines.append(f"可用的模型任务名（配置页 translate.task_name）：{'、'.join(tasks)}")
            lines.append("也可以自己填 API：配置页 translate.provider = custom + api_base/api_key/custom_model。")
            return "\n".join(lines)
        current = self._translate_model_name()
        lines = [f"📇 主程序模型 {len(models)} 个（{path}）"]
        group = ""
        for index, model in enumerate(models, start=1):
            if model["provider"] != group:
                group = model["provider"]
                lines.append(f"— {group}")
            mark = "★" if model["name"] == current else "・"
            lines.append(f"{mark} {index}. {model['name']}")
        lines.append(f"当前：{self._translate_route_text()}")
        lines.append("换模型：/rss translate model <编号|名称>；恢复默认：/rss translate model clear")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # WebUI 接口
    # ------------------------------------------------------------------

    @API("webui_overview", description="订阅源、会话绑定与抓取状态", version="1")
    async def webui_overview(self) -> Dict[str, Any]:
        feeds = [
            {
                "id": feed["id"],
                "name": feed["name"],
                "url": feed["url"],
                "item_count": feed["item_count"],
                "last_poll": _format_time(feed["last_poll_at"]) if feed["last_poll_at"] else "从未",
                "last_error": feed["last_error"],
            }
            for feed in self.store.ordered_feeds()
        ]
        streams, streams_error = await self._list_streams()
        return {
            "feeds": feeds,
            "streams": streams,
            "streams_error": streams_error,
            "enabled": bool(self.config.fetch.enabled),
            "poll_interval_minutes": max(int(self.config.fetch.poll_interval_minutes), 1),
            "last_poll": _format_time(self.store.last_poll_at) if self.store.last_poll_at else "从未",
            "translate": {
                "enabled": self._translate_enabled(),
                "route": self._translate_route_text(),
                "provider": str(self.config.translate.provider),
                "model": self._translate_model_name(),
                "cache_size": len(self.translate_cache),
                "errors": self._translate_errors,
            },
        }

    @API("webui_add_feed", description="新增订阅源并建立已读基线", version="1")
    async def webui_add_feed(self, url: str, name: str = "") -> Dict[str, Any]:
        url = (url or "").strip()
        name = (name or "").strip() or feed_title_from_url(url)
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("订阅源地址必须以 http:// 或 https:// 开头")
        for feed in self.store.feeds.values():
            if feed["url"] == url:
                raise ValueError(f"这个地址已经是订阅源「{feed['name']}」")
        feed = self.store.add_feed(name, url)
        try:
            count = await self._baseline(feed)
        except Exception as exc:
            self.store.remove_feed(feed["id"])
            raise ValueError(str(exc)) from exc
        await self._persist()
        return {"added": True, "id": feed["id"], "name": feed["name"], "items": count}

    @API("webui_remove_feed", description="删除订阅源", version="1")
    async def webui_remove_feed(self, feed_id: str) -> Dict[str, Any]:
        feed = self.store.resolve_feed(feed_id)
        if feed is None:
            raise ValueError(f"找不到订阅源 {feed_id}")
        self.store.remove_feed(feed["id"])
        await self._persist()
        return {"removed": True, "name": feed["name"]}

    @API("webui_set_feeds", description="设置某会话订阅的源；空列表表示不订阅", version="1")
    async def webui_set_feeds(self, stream_id: str, feed_ids: List[str]) -> Dict[str, Any]:
        stream_id = (stream_id or "").strip()
        if not stream_id:
            raise ValueError("缺少会话 ID")
        cleaned: List[str] = []
        for token in feed_ids or []:
            if token == _BINDING_ALL:
                cleaned = [_BINDING_ALL]
                break
            if token in self.store.feeds and token not in cleaned:
                cleaned.append(token)
        self.store.set_binding(stream_id, cleaned)
        await self._persist()
        return {"saved": True, "stream_id": stream_id, "feed_ids": cleaned}

    @API("webui_check_now", description="立即抓取一次并推送", version="1")
    async def webui_check_now(self) -> Dict[str, int]:
        if not self.store.feeds:
            return {"feeds": 0, "new": 0, "sent": 0, "errors": 0}
        return await self._poll_cycle()

    @API("webui_translate_options", description="翻译通道现状与可选模型列表", version="1")
    async def webui_translate_options(self) -> Dict[str, Any]:
        cfg = self.config.translate
        models, path, error = await asyncio.to_thread(self._model_catalog)
        return {
            "enabled": self._translate_enabled(),
            "provider": str(cfg.provider),
            "route": self._translate_route_text(),
            "model": self._translate_model_name(),
            "task_name": str(cfg.task_name or ""),
            "target_lang": str(cfg.target_lang or "中文"),
            "models": models,
            "model_config_path": path,
            "catalog_error": error,
            "tasks": await self._llm_task_names(),
            "cache_size": len(self.translate_cache),
            "errors": self._translate_errors,
        }

    @API("webui_set_translate", description="开关翻译、指定翻译模型", version="1")
    async def webui_set_translate(self, enabled: Optional[bool] = None, model: str = "") -> Dict[str, Any]:
        if enabled is not None:
            self.store.settings["translate_enabled"] = bool(enabled)
        token = (model or "").strip()
        if token.lower() in ("clear", "none", "reset", "默认"):
            self.store.settings.pop("translate_model", None)
        elif token:
            models, _, error = await asyncio.to_thread(self._model_catalog)
            if error or not models:
                raise ValueError(f"读不到主程序模型列表：{error or 'model_config.toml 里没有 llm 模型'}")
            picked = self._resolve_model_token(token)
            if picked is None:
                raise ValueError(f"主程序里没有这个模型：{token}")
            self.store.settings["translate_model"] = picked["name"]
        await self._persist()
        return {
            "saved": True,
            "enabled": self._translate_enabled(),
            "model": self._translate_model_name(),
            "route": self._translate_route_text(),
        }

    @API("webui_test_translate", description="试译某个源最新的一条，只返回文本不发送", version="1")
    async def webui_test_translate(self, feed_id: str) -> Dict[str, Any]:
        feed = self.store.resolve_feed(feed_id)
        if feed is None:
            raise ValueError(f"找不到订阅源 {feed_id}")
        try:
            items = await self._fetch_items(feed)
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        if not items:
            return {"text": "这个源里没有解析到任何条目。", "translated": False}
        newest = max(items, key=lambda entry: entry["published"])
        translation = await self._translate_for(newest)
        return {
            "text": self._render(feed, newest, translation),
            "translated": bool(translation[0] or translation[1]),
        }

    async def _list_streams(self) -> Tuple[List[Dict[str, Any]], str]:
        """列出全部聊天流并附上当前绑定，返回 (会话列表, 失败原因)。

        用的是主程序 ``chat.get_all_streams``，即 Dashboard 会话页同一份来源：
        ``chat_manager`` 启动时从数据库载入的全部聊天流。
        """
        try:
            result = await self.ctx.chat.get_all_streams("all_platforms")
        except Exception as exc:
            self.ctx.logger.error("调用 chat.get_all_streams 失败: %s", exc, exc_info=True)
            return [], f"调用主程序 chat.get_all_streams 失败：{exc}"
        # 这个能力在不同 Host 版本里有两种形状：裸 list，或 {"success":..., "streams":[...]} 信封
        if isinstance(result, list):
            streams = result
        elif isinstance(result, dict):
            if not result.get("success", True):
                error = str(result.get("error") or "未知错误")
                self.ctx.logger.error("chat.get_all_streams 返回失败: %s", error)
                return [], f"主程序返回失败：{error}"
            streams = result.get("streams")
            if not isinstance(streams, list):
                self.ctx.logger.error("chat.get_all_streams 缺少 streams 字段: %r", result)
                return [], "主程序返回里没有 streams 列表"
        else:
            self.ctx.logger.error("chat.get_all_streams 返回结构异常: %r", result)
            return [], f"主程序返回了无法识别的结构：{type(result).__name__}"
        listed: List[Dict[str, Any]] = []
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            stream_id = str(stream.get("stream_id") or stream.get("session_id") or "")
            if not stream_id:
                continue
            label = self._stream_label(stream)
            listed.append(
                {
                    "stream_id": stream_id,
                    "label": label,
                    "chat_type": str(stream.get("chat_type") or "private"),
                    "platform": str(stream.get("platform") or ""),
                    "feed_ids": list(self.store.bindings.get(stream_id, [])),
                }
            )
        listed.sort(key=lambda item: item["label"])
        return listed, ""

    @staticmethod
    def _stream_label(stream: Dict[str, Any]) -> str:
        """会话显示名：群名，或「昵称(账号)的私聊」。"""
        if stream.get("chat_type") == "group" or stream.get("is_group_session"):
            group_name = str(stream.get("group_name") or "").strip()
            group_id = str(stream.get("group_id") or "").strip()
            if group_name and group_id:
                return f"{group_name}（{group_id}）"
            return group_name or f"群聊 {group_id}".strip()
        nickname = str(stream.get("user_cardname") or stream.get("user_nickname") or "").strip()
        user_id = str(stream.get("user_id") or "").strip()
        if nickname and user_id:
            return f"{nickname}({user_id}) 的私聊"
        return nickname or f"私聊 {user_id}".strip()


def create_plugin():
    """Runner 加载入口。"""
    return MaiRssPlugin()
