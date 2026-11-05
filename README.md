# Mai RSS 订阅推送

给 MaiBot 加 RSS / Atom 订阅：可以添加任意多个订阅源，可以给每个聊天流（群或私聊）单独选择
「不订阅 / 订阅一个 / 订阅多个」，抓到新文章时把**来源、时间、标题、正文、链接**发到对应聊天里。

要求 MaiBot `1.3.x`（插件 SDK `2.16+`），无第三方 Python 依赖：抓取用标准库 `urllib`，
解析用 `xml.etree`，Runner 不需要联网装包。

## 安装

三种方式，任选一种。装完都要在 Dashboard 的插件管理里**启用 `bluemika.mai-rss`**。

**A. ZIP 上传（最省事）**：下载 [Releases](https://github.com/bluemika111/mai-rss/releases) 里的
`mai-rss-<版本>.zip`，在 Dashboard → 插件页 → 「从 ZIP 安装」里选择它（上限 100 MB）。
npm 上也有同一份包：`npm i @bluemika/mai-rss` 后取 `node_modules/@bluemika/mai-rss/mai-rss-<版本>.zip`。
ZIP 安装后需要重启 MaiBot 才会加载。

**B. 直接放进插件目录**：把仓库内容 clone 到主程序的 `plugins/bluemika_mai-rss/`（目录名是插件 ID 里的
点换成下划线，不是 `mai-rss`）：

```bash
git clone https://github.com/bluemika111/mai-rss.git plugins/bluemika_mai-rss
```

Docker 部署则放进容器挂载的 `plugins/` 目录，容器内路径通常是 `/MaiMBot/plugins/bluemika_mai-rss`。

**C. 从仓库安装**：Dashboard 的「从仓库安装」走的是官方插件索引，本插件还没提交进索引；
愿意用接口的话可以直接调用 `POST /api/webui/plugins/install`，body 填
`{"plugin_id": "bluemika.mai-rss", "repository_url": "https://github.com/bluemika111/mai-rss", "branch": "main"}`。

首次加载会在 `data/plugins/bluemika.mai-rss/subscriptions.json` 建立订阅数据文件，Runner 也会在插件目录
生成 `config.toml`。升级版本时这两个文件都不会被覆盖。

从 1.1.0 或更早（那时插件 ID 是 `qoder.mai-rss`）升上来要注意：ID 变了等于换了个插件，订阅数据不会自动
跟到新目录，得手动把 `data/plugins/qoder.mai-rss/subscriptions.json` 复制到 `data/plugins/bluemika.mai-rss/`，
并把旧的 `plugins/qoder_mai-rss` 移出 `plugins/`，否则两份同时跑会把同一篇文章推两遍。


## 聊天命令

`/rss` 与 `/订阅` 等价，群聊里 @ 机器人也可以。

| 命令 | 作用 |
| --- | --- |
| `/rss` 或 `/rss help` | 查看帮助 |
| `/rss list` | 列出全部订阅源（编号、状态、条数、上次抓取时间） |
| `/rss add <URL> [名称]` | 添加订阅源；不写名称就用域名 |
| `/rss del <编号\|名称\|ID>` | 删除订阅源，同时清掉各会话对它的订阅 |
| `/rss bind <编号...>` | **覆盖**设置当前会话订阅哪几个源 |
| `/rss bind all` | 当前会话订阅全部源（以后新增的源自动生效） |
| `/rss bind none` | 当前会话不订阅任何源 |
| `/rss unbind <编号...\|all>` | 从当前会话去掉某些订阅 |
| `/rss status` | 当前会话订阅情况 + 全局抓取状态 |
| `/rss check [编号]` | 立刻抓一轮并推送 |
| `/rss preview <编号>` | 把最新一条按推送格式试发一次，不影响已读记录 |
| `/rss translate` | 查看当前翻译设置 |
| `/rss translate on\|off` | 开关译文（存进插件数据文件，重启仍有效） |
| `/rss translate list` | 列出主程序 `model_config.toml` 里配过的模型 |
| `/rss translate model <编号\|名称>` | 换翻译模型；`clear` 恢复跟随配置 |

编号可以是 `/rss list` 里的序号、源名称或源 ID。

命令正文由插件自己用 `send.text` 发回当前会话（Host 只会把 Command 的返回值写进日志，不会替插件回话），
拦截等级为 1，所以发命令不会额外触发一次 AI 回复。`/rss preview` 发出去的那条就是推送本身。
本地终端里没有可发送的会话，输出改为进日志。

## Dashboard 页面

启用后侧边栏会出现「RSS 订阅」页：加/删订阅源、选会话、勾选该会话要订阅的源、立即抓取，
以及一张翻译卡片（开关、按服务商分组的模型下拉、每条源的「试译」按钮——试译只在页面显示，不会发到聊天）。
页面通过 `@API` 读写同一份订阅数据，和聊天命令不会互相覆盖。

会话下拉框用的是主程序自己的聊天流列表（能力 `chat.get_all_streams`，`platform=all_platforms`，
数据源与 Dashboard 会话页相同：启动时从数据库全量载入的 `chat_manager` 会话）。因此：

- 列表为空通常是机器人还没和那个群/那个人产生过消息，数据库里没有这条聊天流；在群里 @ 它说句话
  再刷新页面即可。
- 取不到时页面会直接显示原因（`chat.get_all_streams` 未授权、RPC 失败等），`/rss status` 同样会打印，
  不会只留一个空下拉框。该能力的返回形状在不同 Host 版本里有裸列表和 `{"success", "streams"}` 两种，插件两者都认。
- 插件不能调用 Dashboard 的 HTTP 接口（运行时无 `web.*` 能力，页面 CSP 也禁网），所以走能力而不是走网页列表。

## 行为细节

- **加源不刷屏**：`add` 时会把当时源里已有的文章全部记为已读，只推送之后新增的。
- **推送格式**（开启翻译时标题和正文各带一段括号里的译文）：

  ```
  📡 RSS/Atom 拉取 • github 状态
  🗞 标题：Incident with Actions and Pages（Actions 与 Pages 出现异常）
  🕒 时间：2026-11-05 00:05
  Nov 4, 16:05 UTC Investigating - We are investigating reports of degraded performance
  （调查中 - 我们正在排查 Actions 与 Pages 性能下降的报告）
  🔗 https://www.githubstatus.com/incidents/xr3myzj7ms51
  ```

- **翻译**：一条新条目只调一次模型（标题和正文塞进同一个请求，要求模型输出 `T|` / `B|` 两行），
  多个会话共用同一份译文；正文单独一行加括号是因为长译文同行会挤成一团。
  译文按「原文 + 目标语言 + 模型」缓存到 `translate_cache.json`（上限 2000 条），
  重复推送与试译不再二次调用模型。原文本身已是中文（中日韩字符占比 ≥ 0.1）默认跳过，
  单轮抓取另有 `max_calls_per_round` 预算。**模型报错、返回空、输出解析不出 `T|`/`B|`
  都只记日志并退回原文，绝不阻塞推送。**
- **翻译模型怎么选**：`ctx.llm.get_available_models()` 只返回主程序的模型**任务名**，列不出具体模型，
  所以下拉框直接读 `model_config.toml` 的 `[[models]]`（只取 `model_type = "llm"`，按 `api_provider`
  分组）。路径从插件数据目录逐级上推到主程序根目录，不硬编码容器路径；按文件 mtime 重读，
  在 WebUI 里加完模型不用重启插件。选中具体模型后走 Host 的直选分支，名字不在配置里就报
  `未找到名为 'X' 的模型` 且不重试，因此这条失败同样只退回原文。
- **改动的翻译开关与模型存在插件数据文件里**：主程序只允许内置的插件管理写第三方插件的
  `config.toml`，所以 `/rss translate on|off` 和模型选择记在 `subscriptions.json` 的 `settings` 字段，
  优先级高于 `[translate]` 配置段；其余参数（语言、范围、预算、自定义 API）在配置页改。
- **轮询由插件自己的后台任务驱动**：插件运行时没有定时器能力，所以 `on_load` 起 asyncio 任务、
  `on_unload` 取消；间隔取 `[fetch] poll_interval_minutes`。
- **去重**：按 `guid`/`id`，没有就退回链接、标题+时间；每个源保留最近 400 条已读记录。
- **顺序**：不依赖源内顺序，统一按发布时间从旧到新推送；没有时间的条目排在最前。
- **防刷屏**：单轮每个源最多推 `max_items_per_feed` 条，超出的记为已读并在聊天里说明跳过多少条；
  只推 `lookback_hours` 之内的文章。
- **权限**：默认只有管理员能加删源、改绑定。管理员取 `[permission] admins`（形如 `qq:12345`），
  留空则沿用主程序 `[plugin].permission`；本地终端调试不受限。`admin_only = false` 后所有人可用。

## 配置

`config.toml`（首次运行由 Runner 按配置模型生成）：

| 节 | 键 | 默认 | 说明 |
| --- | --- | --- | --- |
| `[fetch]` | `enabled` | `true` | 定时抓取开关，关掉后只能手动 `/rss check` |
| `[fetch]` | `poll_interval_minutes` | `10` | 抓取间隔 |
| `[fetch]` | `request_timeout_seconds` | `15` | 单源网络超时 |
| `[fetch]` | `max_items_per_feed` | `5` | 单轮每源最多推送条数 |
| `[push]` | `max_content_chars` | `600` | 正文长度上限，`0` 只发标题和时间 |
| `[push]` | `include_link` | `true` | 是否附原文链接 |
| `[push]` | `lookback_hours` | `72` | 只推送多少小时内的文章，`0` 不限制 |
| `[push]` | `send_delay_seconds` | `0.6` | 连续发送间隔 |
| `[permission]` | `admin_only` | `true` | 是否仅管理员可管理 |
| `[permission]` | `admins` | `[]` | 管理员列表 |
| `[translate]` | `enabled` | `true` | 是否附译文（聊天里的开关优先级更高） |
| `[translate]` | `provider` | `maibot` | `maibot` 用主程序模型；`custom` 自己填 API |
| `[translate]` | `task_name` | `""` | maibot 通道用哪个模型任务，留空用主程序默认任务 |
| `[translate]` | `model_name` | `""` | maibot 通道直选的具体模型名，留空按任务选 |
| `[translate]` | `target_lang` | `中文` | 目标语言 |
| `[translate]` | `fields` | `title_body` | `title_body` 或 `title_only` |
| `[translate]` | `max_chars` | `800` | 送译正文上限，`0` 表示正文不译 |
| `[translate]` | `skip_if_chinese` | `true` | 中文原文跳过 |
| `[translate]` | `max_calls_per_round` | `5` | 单轮最多调用几次，`0` 不限制 |
| `[translate]` | `api_base` / `api_key` / `custom_model` | `""` | custom 通道的端点、密钥与模型标识符 |
| `[translate]` | `timeout_seconds` | `20` | 翻译请求超时 |

## 已知边界

- 只支持 `http(s)` 地址，非 HTTP 协议直接拒绝。
- 需要登录、带反爬验证页的源（部分国内站点的 `/feed` 实际返回 HTML）无法订阅，日志与
  `/rss list` 会给出「返回的是网页而不是 XML」这类明确原因。
- 已读记录是**按源**而不是按会话的：某个源在还没有会话订阅时更新，那段内容之后不会再补发给
  后来才订阅的会话。
- 同一个站点同时提供 `.rss` 和 `.atom`（如 `status.deepseek.com`）时只订一个：两者条目相同，
  但 RSS 的 `pubDate` 是事件创建时间、Atom 的 `updated` 是最近更新时间，两个都订会收到同一事件
  的两条消息，时间还不一样。
- 网页正文渲染、图片消息推送不在本插件范围内，只发文本。
- 翻译是**额外的模型调用**，有 token 成本：一条新条目一次，单轮受 `max_calls_per_round` 限制，
  命中缓存不花钱。不想花钱就把 `enabled` 关掉或把预算调小。
- 读不到 `model_config.toml`（比如主程序配置不在插件可上推到的目录里）时，模型下拉会空，
  `/rss translate list` 说明原因并退回列出任务名，此时在配置页手填 `task_name` 或 `model_name` 即可。
- `custom` 通道的 `api_key` 只留在实例上的 `config.toml`：不打包进 zip、不同步、不打日志、
  不在任何命令或页面输出里回显；容器需要能直连该端点。
- `custom` 通道用标准库 `urllib` 直连，不走主程序的联网能力（运行时无通用 HTTP POST 能力）。

## 已实测的源

`https://status.deepseek.com/feed.rss`（RSS 2.0，28 条）、`https://sspai.com/feed`（RSS 2.0）、
`https://www.ruanyifeng.com/blog/atom.xml`、`https://github.com/Mai-with-u/MaiBot/releases.atom`、
`https://blog.python.org/feeds/posts/default`（Atom）、`https://hacks.mozilla.org/feed/`（RSS 2.0）。
条目多、时间字段齐、`guid` 唯一的源都能正常去重；`36kr.com/feed` 之类返回反爬网页的地址会被
明确拒绝。

## 开发与测试

`tests/` 下的用例不需要主程序在跑，也不需要联网（`check_real_feeds.py` 除外，它会真的抓一轮源）。
Python 3.11+，先装插件运行时依赖的 SDK：

```bash
pip install maibot-plugin-sdk
python tests/check_plugin.py        # 解析、订阅仓库、命令匹配、推送格式
python tests/check_flow.py          # 抓取 → 去重 → 推送 → 已读 的完整流程
python tests/check_commands.py      # /rss 各子命令、权限、拦截等级
python tests/check_translate.py     # 译文：路由、缓存、模型目录、失败兜底、WebUI 接口
python tests/check_real_feeds.py    # 联网：真实源解析
```

`tests/check_schemas.py` 额外要主程序源码（`src/plugin_runtime`）和它的依赖，用环境变量指路：

```bash
MAIBOT_ROOT=/path/to/MaiBot python tests/check_schemas.py
```

改 `.py` 或 `_manifest.json` 会被主程序的文件监视自动重载；改 `webui.json` 或 `webui/index.html`
不在监视范围，要在聊天里发 `/pm plugin reload bluemika.mai-rss`。

打安装用的 ZIP（`_manifest.json` 必须在包根，或只包一层插件目录）：

```bash
zip -r ../mai-rss-1.1.1.zip . -x '*__pycache__*' -x 'logs/*' -x 'data/*' -x 'config.toml'
```

## 许可

GPL-3.0-or-later，与 MaiBot 主程序一致。

