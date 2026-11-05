# 更新日志

## 1.1.1

- 插件 ID 由 `qoder.mai-rss` 改为 `bluemika.mai-rss`（开发期前缀换成作者命名空间）。
- **升级注意**：ID 变了就等于换了个插件。安装目录从 `plugins/qoder_mai-rss` 变成
  `plugins/bluemika_mai-rss`，订阅数据从 `data/plugins/qoder.mai-rss/` 变成 `data/plugins/bluemika.mai-rss/`，
  Dashboard 的启用状态与 `config.toml` 也各自独立。从 1.1.0 升上来要自己把旧数据目录里的
  `subscriptions.json`（以及 `translate_cache.json`，如果生成过）复制到新目录，并且**把旧目录从
  `plugins/` 里移走**——两份同时存在会各跑一轮抓取，同一篇文章推两遍。

## 1.1.0

主要功能

- 推送首行由「📰 来源：xxx」改为「📡 RSS/Atom 拉取 • xxx」。
- 新增译文：标题后同行括号附译文，正文另起一行括号附译文；一条新条目只调一次模型，多个会话共用同一份译文。
- 翻译通道二选一：`maibot` 复用主程序已配置的模型（可按任务名或直选具体模型），
  `custom` 自己填 OpenAI 兼容端点 + 密钥 + 模型标识符。
- `/rss translate`（查看）/ `on|off` / `list` / `model <编号|名称|clear>` 四条子命令；
  Dashboard 页面新增翻译卡片，含按服务商分组的模型下拉和每条源的「试译」按钮（只在页面显示，不发聊天）。

细节

- 模型下拉读 `model_config.toml` 的 `[[models]]` 并只保留 `model_type = "llm"`：
  `llm.get_available_models` 只返回任务名，列不出具体模型。路径由插件数据目录逐级上推得出，
  不硬编码容器路径；按 mtime 重读，WebUI 加完模型不必重启插件；只取名称与服务商，绝不带出 `api_key`。
- 译文按「原文 + 目标语言 + 模型」缓存到 `translate_cache.json`（上限 2000 条，超出淘汰最早的），
  重复推送和试译不再二次调用模型。
- 原文已是中文（中日韩字符占比 ≥ 0.1）默认跳过；单轮抓取受 `max_calls_per_round` 预算约束。
- 模型报错、返回空文本、输出解析不出 `T|`/`B|`、直选模型名不存在（Host 对直选不重试）
  一律只记日志并退回原文，不影响推送；失败次数在 `/rss translate` 与页面卡片里可见。
- 翻译开关与所选模型存进 `subscriptions.json` 的 `settings`：主程序只允许内置插件管理写第三方
  插件的 `config.toml`，聊天侧要能改就得自己存，优先级高于 `[translate]` 配置段。
- `_manifest.json` 声明 `llm.generate` 与 `llm.get_available_models`（Host 按能力名逐条授权，漏了会被拒）。
- 离线用例新增 `tests/check_translate.py`：提示词与输出解析、缓存命中与淘汰、模型目录过滤与重读、
  两条通道的成功/失败/协议校验、命令分支、WebUI 接口与「试译不推送」。
- 面向公开分发：`_manifest.json` 的署名与仓库地址改成本插件自己的仓库，抓取用的 User-Agent 同步；
  补 `LICENSE`（GPL-3.0-or-later）与 `.gitignore`（忽略 Runner 生成的 `config.toml`，里面有密钥）。
- `tests/check_schemas.py` 不再内置开发机的绝对路径，改为 `MAIBOT_ROOT` 环境变量或同级目录自动查找。

## 1.0.0

主要功能

- 支持添加多个 RSS 2.0 / RSS 1.0(RDF) / Atom 订阅源，只用标准库解析，不引入第三方依赖。
- 支持按会话选择订阅范围：不订阅、订阅单个源、订阅多个源，或用 `all` 通配让以后新增的源自动生效。
- 抓到新文章后把来源、标题、发布时间、正文与链接逐条推送到订阅了该源的聊天流。
- 提供 `/rss`（别名 `/订阅`）命令族：`list` / `add` / `del` / `bind` / `unbind` / `status` / `check` / `preview`。
- 提供 Dashboard 自定义页面：加删订阅源、选会话、勾选该会话订阅的源、立即抓取。

细节

- 新增源时建立已读基线，历史文章不补发，避免一次添加就刷屏。
- 推送顺序不依赖源内顺序，按发布时间从旧到新；无时间戳的条目稳定排在最前。
- 条目时间按语义优先级取 `pubDate` / `published` / `dc:date` / `updated` / `modified`，不受源内字段顺序影响；仅带 `updated` 的状态页类 Atom 源（如 `status.deepseek.com/feed.atom`）也能正确取到时间。
- `/rss preview` 同样按时间挑最新条目，不再假设源内新文在前。
- 单轮每源推送条数、正文长度、时间窗口、发送间隔均可配。
- 抓取失败与「源地址其实是网页」按源记录原因，在 `/rss list` 和面板上直接可见。
- 聊天流取不到时（能力未授权、RPC 异常、主程序返回 `success: false`）把具体原因交给页面与 `/rss status`，
  不再静默显示成空列表；多平台机器人的会话选项附带平台名以区分同名会话。
- 兼容 `chat.get_all_streams` 的两种返回形状：裸列表（实测 Host）与 `{"success", "streams"}` 信封，
  字段为 `None` 的会话也能正常显示名称。
- 命令正文改由插件自己 `send.text` 回当前会话：Host 只把 Command 的 `response` 写日志、不会替插件回话，
  此前 `/rss` 各子命令在群里静默无回复；`response` 降级为日志摘要，`/rss preview` 直接就是那条推送。
- 源查找改为「ID / 名称优先，其次序号」：源 ID 偶尔全是数字（实测 `35237580`），旧逻辑先判数字会把它当越界序号而查不到。
- 订阅数据落在 `data/plugins/bluemika.mai-rss/subscriptions.json`，热重载与重启都不丢。
