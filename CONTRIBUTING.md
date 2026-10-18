# 贡献指南

> **本文件是开发侧的唯一权威来源**：开发环境、架构说明、代码规范、模板、离线自检用法与提交规范。
> 别处只写摘要和链接，不复制正文。
>
> 仓库级硬性规则见 [AGENTS.md](AGENTS.md) ｜ 用户文档见 [README.md](README.md) ｜
> 版本变更见 [CHANGELOG.md](CHANGELOG.md)。

## 目录

- [1. 开发环境](#1-开发环境)
- [2. 仓库结构](#2-仓库结构)
- [3. 架构总览](#3-架构总览)
- [4. 代码规范](#4-代码规范)
- [5. 模板](#5-模板)
- [6. 离线自检](#6-离线自检)
- [7. 提交规范](#7-提交规范)
- [8. 发版检查清单](#8-发版检查清单)

---

## 1. 开发环境

| 项 | 要求 |
|---|---|
| Python | **3.10+**（用到 PEP 604 的 `X \| None` 与 PEP 585 的 `list[str]`） |
| 运行时依赖 | `Pillow>=10.0.0`（见 `requirements.txt`，**只**用于 WebUI 管理页生成缩略图） |
| 跑离线自检 | **不需要**安装 AstrBot，**不需要** Pillow，**不需要**外网 |
| 跑真机验证 | 需要一台 AstrBot ≥ 4.13.0（WebUI 管理页需要 ≥ 4.24.1）与一个 OneBot v11（`aiocqhttp`）协议端 |

**Windows 控制台必须先设 `PYTHONUTF8=1`**：

```powershell
$env:PYTHONUTF8 = "1"
python tests/offline_check.py
```

不设的话，含 `✅` / `❌` / `⇒` 一类字符的输出会在 GBK 控制台上抛 `UnicodeEncodeError`。
本自检的入口**没有**兜 `BaseException`，因此这种情况会以**退出码 1** 结束 ——
和有断言失败长得一模一样。**看到退出码 1 时先看末行**（见 §6.2）。

**自检的临时目录**：默认建在插件目录内的 `.test_tmp/`（已在 `.gitignore` 里）。
在受限环境（例如 Windows 沙箱只允许改动自己新建的文件）下，插件目录内可能建不出目录，
此时用 `IMAGEPOOL_TEST_TMP` 指到一个**插件目录之外、可写**的位置：

```powershell
$env:IMAGEPOOL_TEST_TMP = "<插件目录之外的临时目录>"
```

## 2. 仓库结构

```
main.py                                  插件主体（单文件，约 4.1k 行）
metadata.yaml                            包身份：name/display_name/desc/version/author/repo/
                                         support_platforms/astrbot_version
_conf_schema.json                        配置项声明与默认值（**唯一声明处**，42 项）
requirements.txt                         运行时依赖（Pillow）
README.md                                用户文档
CHANGELOG.md                             版本变更记录（版本记录的权威）
CONTRIBUTING.md                          本文件
AGENTS.md                                仓库级规则
LICENSE                                  MIT
pages/manage/index.html                   WebUI 管理页
pages/manage/app.js                       页面逻辑（约 0.5k 行）
pages/manage/style.css                    页面样式
tests/offline_check.py                    离线自检（自带 astrbot.* 替身，约 1.6k 行）
.astrbot-plugin/i18n/{zh-CN,en-US}.json   插件页与插件名的本地化文案
.gitignore                               挡住运行时数据与构建产物
```

> 发版用 `git tag` + `git archive`，所以**没有提交的内容就是没公开** ——
> `.gitattributes export-ignore` 对 GitHub 自动生成的归档不起作用。
> 运行时数据（`data/`、`*.sqlite3`、`*.log`、`*.zip`）一律不入库。

## 3. 架构总览

### 3.1 `main.py` 的分层

单文件，按「纯函数在前、有状态在后」排列：

| 区段 | 内容 |
|---|---|
| 模块 docstring | 各版本的功能与行为变更说明 —— 改行为时同步改这里 |
| 导入 | `from __future__ import annotations`；`astrbot.api.web` 用 `try/except` 优雅降级（旧内核没有 Plugin Pages） |
| 常量 | `PLUGIN_NAME`、`IMAGE_EXTS`、`MAX_NAME_LEN` / `MAX_FILE_BYTES`、`DEFAULT_NAMESPACE`、`RESERVED_NAMES`、`MODE_CAPS`、`INGEST_BUSY_TIMEOUT_SEC` 等 |
| 纯工具函数 | `sniff_ext` / `normalize_name` / `name_key` / `to_bool` / `to_str_list` / `as_dict` / `parse_count` / `parse_fetch_arg` / `parse_tags` / `parse_delete_args` / `parse_move_args` / `check_alias_name` / `suggest_tags` |
| `ImagePoolPlugin(Star)` | 插件本体：配置读取 → 权限 → 数据层（`_db_*`）→ 文件写入（`_create_exclusive` / `_move_exclusive` / `_next_free_dest` / `_reference_index`）→ 指令 → 被动采集 → WebUI API |

`ImagePoolPlugin` 内部分段（自上而下）：`__init__` / `_init_db` → **配置读取** → 权限模型 →
SQLite 连接与 `_db_*` 一族 → 分类与别名解析 → **跨插件接口 `ingest_files`** →
文件写入与引用索引 → 清理（保留期 / 去重）→ 引用取图 → **指令**（加图 / 发图 / 来图 / 图库组 / 删图 / 移动）→
**被动采集** → **WebUI API** → `terminate`。

指令 handler 按域命名（`add_image` / `send_random` / `fetch_image` / `name_router` /
`pool_*` / `alias_*` / `delete_image` / `move_image`），WebUI handler 统一 `api_*`。

### 3.2 数据层

数据目录由 `StarTools.get_data_dir(PLUGIN_NAME)` 决定
（= `data/plugin_data/astrbot_plugin_image_pool/`），**不在插件目录里** ——
插件升级会整体替换插件目录，所以运行时数据绝不能写进插件目录。

4 张表（`_init_db` 里 `CREATE TABLE IF NOT EXISTS`）：

| 表 | 主键 / 唯一约束 | 作用 |
|---|---|---|
| `images` | `UNIQUE (char_name, seq)`、`UNIQUE (char_name, sha256)` | 图片行；`seq` 是用户可见 id，**不回收不重排** |
| `categories` | `name` | 分类清单 |
| `tag_index` | `key` | **正式名与别名共用一张表**（`kind` = `tag` / `alias`），解析永远是一次主键查询 |
| `blocked_hashes` | `sha256` | 全局指纹黑名单 |

**连接生命周期**：`_connect(busy_timeout_sec=30.0)` 设 `row_factory`、`journal_mode=WAL`、
`synchronous=NORMAL`；跨插件接口用更短的 `INGEST_BUSY_TIMEOUT_SEC`（5 s）。

**迁移**：启动时按 `PRAGMA table_info` 探测缺列并 `ALTER TABLE ADD COLUMN`，
**每列独立 `try/except`** —— 一个失败的 `ALTER` 绝不能让插件构造失败（那会把升级变成「插件直接加载不了」）。

### 3.3 文件写入与「绝不覆盖」

- 图片本体在文件系统、元数据在 SQLite；文件名形如 `pool/<分类名>/000001.png`，
  编号即 `seq`，由 `_next_free_dest()` 从 `MAX(seq)+1` 起**探测磁盘空位**分配 ——
  **任何写入路径都不得覆盖已存在的文件**。
- 写入与复制走 `_create_exclusive()`，搬移走 `_move_exclusive()`（`main.py:2410` / `:2447`，
  两者都靠 `open(dest, "xb")` 拿到独占语义，或占位 + `os.replace`）：
  「先检查再写」不是原子的，检查通过与真正写入之间被抢占就会覆盖。
- 删除或搬移文件前先查 `_reference_index()`（一次 O(n) 索引，身份键 `(st_dev, st_ino)`，
  文件不存在时回落到名字键）：**同一物理文件可能被多行引用**（多 tag、老库的折叠分类），
  直接 `unlink` 会把别人的图删掉。这条守卫必须铺到**全部会 unlink 的路径**
  （`/删图`、分类删除 purge、去重、保留期清理）—— 保留期清理是自动触发的，漏一条就会静默删图。
- 折叠冲突（仅大小写不同的两个分类共用同一物理目录）时**拒绝删除 / 迁移**，
  并提示先用 `/图库 别名 收编` 合并。

### 3.4 权限与群级模式

| 符号 | 作用 |
|---|---|
| `MODE_CAPS` | 三档模式各自允许的能力：`whitelist`（可加图可发图）/ `post`（仅发图）/ `none`（都不可） |
| `_is_global_admin()` | 全局管理员：只按**发送者 ID** 匹配 `plugin_admins` |
| `_group_mode()` | 取本群生效模式（`open_groups` / `post_groups` / `default_group_mode`，私聊走 `private_mode`） |
| `_in_id_list(event, key, scope)` | 名单匹配；`scope` **严格区分**「群号」与「用户 ID」两个命名空间 |
| `_can_manage_image()` | 删图 / 屏蔽的身份判定（全局管理员 / 群级管理员 / `whitelist_allow_manage`） |

**不要**用 AstrBot 的全局管理员（`@filter.permission_type`）当插件权限模型 —— 那是提权。
权限完全由插件自己的两份名单决定。

### 3.5 分类解析与别名

- 键由 `name_key()` 用 Python 侧 `casefold` 计算（SQLite 的 `lower()` 只认 ASCII，不能用）。
- `_db_resolve_tag` / `_db_resolve_many` 的判定顺序是**精确名优先、折叠键兜底** ——
  顺序反过来会让老库里「仅大小写不同」的两个分类互相顶掉。
- 别名与正式名**互斥**：别名不许撞正式名，新建 / 改名不许撞别名；
  同一别名被多个分类声明时**先到先得**（`tag_index.key` 主键硬唯一），要改指向用 `force`。
- 别名不参与目录路径，只做「输入 → 正式名」的映射；**目录名永远只能是正式名**。
- 分类名不能是 `RESERVED_NAMES` 里的内置指令名，也不能含零宽字符 / 空名 / `.` / `..` /
  首尾空白（`_ensure_category` 统一拦截）。

### 3.6 被动采集（偷图）

由 `@filter.event_message_type(EventMessageType.ALL)` 的 `passive_collect()` 驱动，
只入库、不回复。要点：

- 采集总开关是 `auto_collect`，**与权限配置正交**；落点永远是 `collect_namespace`。
- 会跳过「发给机器人的消息」：命中指令的（用 AstrBot 的 `handlers_parsed_params` 标记识别）
  与以唤醒前缀开头的（斜杠域）。
- 发送者过滤是黑白名单双模式；白名单模式留空 = **谁都不采**（安全默认）。
- **被动采集不回拉引用消息**：否则任何一条群消息都能诱导机器人去取任意 `message_id`。
- 静默（`silent_groups` / `auto_silent_in_collect_groups` / `silent_scope`）只在输出前拦一层，
  不影响图片落到哪个分类，也不影响权限判定。

### 3.7 跨插件公开接口

```python
async def ingest_files(
    self, paths, *, tag: str, group_id: str = "", uploader: str = "", source: str = "x"
) -> tuple[int, int]:   # (added, skipped)
```

- 与 `/加图` **共用同一条写入路径**（`_db_add`），因此继承全部既有语义，**没有第二份写入逻辑**。
- 不变量：正常返回时 `added + skipped == 取到的条目数`；
  **基础设施失败（库被锁 / 只读 / 损坏、目标目录不可写）抛异常，绝不伪装成 `skipped`** ——
  否则调用方会把「写不进去」当成「重复」。
- `tag` 归一化后为空即拒绝入库，**绝不回落「未分类」**。
- 调用方（如 `astrbot_plugin_x_monitor`）必须每次**重新解析本插件实例**并检查 `activated`，
  用 `hasattr` 做能力探测，**绝不直写 `index.sqlite3`**（漏掉分类索引维护会让该分类
  「看得见但够不到」：列表能从图片表兜底显示名字，按名字解析却永远找不到）。

### 3.8 WebUI 管理页

- 页面在 `pages/manage/`；路由由 `_register_web_api()` 里的 `routes` 列表声明，
  经 `context.register_web_api(f"/{PLUGIN_NAME}/<route>", handler, methods, desc)` 注册（**14 条**）。
- `WEB_API_AVAILABLE` 为假（内核 < v4.24.1）时跳过注册并打 warning，**指令功能不受影响**。
- 三条硬约束：
  1. **路由必须带插件名前缀**（`/astrbot_plugin_image_pool/overview`），而页面侧 endpoint **不能带**；
  2. **处理函数不能有位置参数** —— 一切都从 `web_api.request` 读；
  3. **请求体一律走 `as_dict()`** 归一成 dict：合法但非对象的 JSON（`[1,2]` / `"x"` / `5`）
     直接 `.get()` 会 `AttributeError` → HTTP 500。
- 缩略图缓存有界（240 项 / 24 MB）且**同一把锁**下取值、写入与淘汰 ——
  `api_thumbs` 每行都走 `asyncio.to_thread`，并发请求会真的并发读写同一份缓存。
- 页面权限 = Dashboard 登录账号，**不叠加** `plugin_admins`（两者之间没有 UID 映射）。
- **新增后端路由后必须重载插件**（注册只发生在 `__init__`）；只改静态资源刷新页面即可。

### 3.9 引用取图

`/加图`、`/删图`、`/移动` 支持引用一条含图消息：先取 AstrBot 已解析好的 `Reply.chain`，
为空时再通过协议端 `event.bot.call_action("get_msg", message_id=...)` 回拉。
`Reply` 对象没有 `message_id` 字段，只有 `id` —— 实现用的是 `id`。

回拉是**尽力而为**的：取决于协议端实现与消息是否过期，失败时静默降级为「未检测到可用图片」，
不影响主流程。**只有指令路径会回拉，被动采集不会**。

## 4. 代码规范

### 4.1 语言与风格

- **注释与 docstring 一律中文。** 模块 docstring 用「功能 / 权限 / 触发方式 / 存储」式分段。
- **类型注解用 PEP 585 + PEP 604**：`list[str]`、`dict[str, dict]`、`str | None`。
  **不要引入 `typing`**。
- 文件顶部保留 `from __future__ import annotations`。
- 命名：模块常量全大写；类 PascalCase；私有 helper 前缀 `_`；
  WebUI handler 统一 `api_*`；指令 handler 按域命名（见 §3.1）。
- 缩进与行宽与既有代码保持一致（4 空格、无 tab）。

### 4.2 日志

统一用 `from astrbot.api import logger`，**不自建 logger**。
每条日志带 `[image_pool]` 前缀，便于在宿主日志里过滤。

### 4.3 异常处理

- **构造期（`__init__` / `_init_db`）任何失败都不能让插件加载失败**：
  数据目录解析不出来、目录建不出来、库被锁 / 损坏 / 只读，都要兜住并大声报错，
  让插件仍能加载、指令与 WebUI 仍能各自报错。
- 宽 `except Exception` 只用于「解析失败退化为默认值」或「富化步骤失败不阻断主流程」，
  并且**必须**要么记一条日志、要么写清为什么静默。
- 事务里的异常一律 `rollback(); raise`，**不吞**。
- **不要**把基础设施失败伪装成业务结果（典型：把「写不进去」算成 `skipped`）。

### 4.4 配置读取

```python
self._cfg(key, default)      # 原始值；读不到时回落 default
to_bool(value, default)      # 真布尔：认得 "1"/"true"/"yes"/"on"/"是"/"开" 等字符串
to_str_list(value)           # 归一成 list[str]（名单类配置）
int(...) / float(...)        # 数值类
```

- **配置项只在 `_conf_schema.json` 里声明一次**，代码读到的键必须是它声明过的键。
- 布尔配置**必须**经 `to_bool()`：配置里写字符串 `"false"` 会被判为真，
  那样 `allow_purge`（安全开关）与 `auto_collect`（隐私开关）会**反向打开**。
- 名单类配置必须区分**群号**与**用户 ID** 两个命名空间（见 `_in_id_list` 的 `scope`）。
- 自检断言的是 schema 的**渲染形状**与**枚举键的 options 与代码常量一致**
  （`description` / `hint` 长度、下拉选项、`labels` 对齐、滑块区间、默认值落位）。
  ⚠️ **没有**「代码读到的键 ⊆ 声明键」这类键覆盖断言：漏声明或写错键名不会被自检发现
  （`_cfg` 读不到就静默回落默认值），新增配置项时必须自己双向核对。

### 4.5 数据安全纪律

- **任何写入路径都不得覆盖已存在的文件**（见 §3.3）。
- 删除 / 搬移前先查引用索引；**同一物理文件被多行引用时只改记录、不动文件**。
- 破坏性功能默认关闭（具体默认值见 [AGENTS.md](AGENTS.md) §2 第 5 条，本文件不重复）。
- 清理类功能**宁可不清，也不误删**：`retention_source` 取非法值时跳过本次清理。
- 文件操作与事务的顺序要能解释清楚（先 commit 再删文件 vs 先文件再 commit），
  中途失败必须留下可解释的状态，不能出现「行回滚了、文件已搬走」的悬空行。

### 4.6 注释纪律

不要在注释里写**开发过程用语**：审查轮次编号、阶段代号、「本轮」、「独立验证」、「对抗审核」
之类的措辞。这些会随包发布给公开读者看。描述**为什么这样写**，而不是「这是第几轮查出来的」。

> 现状提示：`main.py` 的模块 docstring 里仍残留一批这类编号，属**待清理项**；
> 新写的内容不要再带。

## 5. 模板

### 5.1 新增一条聊天指令

```python
    @filter.command("示例", alias={"demo"})
    async def demo_cmd(self, event: AstrMessageEvent, name: GreedyStr) -> None:
        """一句话说明：/示例 <分类名>"""
        if not str(name).strip():
            yield event.plain_result("用法：/示例 <分类名>")
            event.stop_event()
            return
        # ……业务逻辑……
        yield event.plain_result("✅ 完成。")
        event.stop_event()
```

硬性要求：

- handler 是 **async generator**（用 `yield`），第一个参数恒为 `event: AstrMessageEvent`。
- **每个出口都要 `event.stop_event()`**，否则同一条被唤醒的消息会继续落到 LLM，
  用户会同时收到插件回复和 AI 回复。
- **参数类型只有两种**：`xxx: str = ""`，或 `raw: GreedyStr`。
  **`GreedyStr` 一律不能有默认值** —— `CommandFilter` 只在参数没有默认值时才认得它。
  拿到 `raw` 后自己切分（本仓统一用 `TAG_SPLIT_RE` 一类正则或 `split(maxsplit=...)`）。
- **指令名不带前缀**（唤醒由 AstrBot 的 `wake_prefix` 负责，前缀在匹配前已被剥掉）。
- **新增的指令名要加进 `RESERVED_NAMES`**，否则 `/(分类名)` 的动态路由会把它当成分类名吞掉。
  该名单目前只登记了**主指令名**，9 个 `alias=` 别名（`addimg` / `add_image` / `随机图` / `random` /
  `randomimg` / `random_img` / `delimg` / `movetag` / `moveto`）**没有**登记 —— 新增别名时请一并考虑，
  这是个已知的清单级不一致（未发现由此产生的实际故障）。
- 权限在 handler 内部自校验（`_is_global_admin` / `_group_mode` / `_can_manage_image`），
  **不要**用 `@filter.permission_type`（那授予的是 AstrBot 的**全局**管理员，属于提权）。

> 裸分类名的 `@filter.regex` 路由（`name_router`）是**受控例外**：它必须由 `bare_name_enable`
> 与 `bare_name_groups` 双重把关，并先排除 `RESERVED_KEYS`。不要为其它功能新增 regex 触发。

### 5.2 新增一个配置项

先在 `_conf_schema.json` 里声明（**唯一声明处**），再在代码里读。条目形状：

```json
  "示例开关": {
    "description": "一句话说明（会显示在配置页标题）",
    "hint": "更长的解释：默认值是什么、改了会怎样。",
    "type": "bool",
    "obvious_hint": true,
    "default": false
  },
```

可用类型与附加字段：

| `type` | 附加字段 | 读取方式 |
|---|---|---|
| `bool` | — | `to_bool(self._cfg(key, default))` |
| `int` | `slider: {min,max,step}` | `int(self._cfg(key, default))` |
| `string` | `options` + `labels` | `self._cfg(key, default)`，**比较字符串** |
| `list` | 可带 `options` + `labels` | `to_str_list(self._cfg(key, default))` |

WebUI 渲染规则（源码核实，自检的 schema 段据此断言）：`bool` → 开关；`string` + `options` → 下拉
（`labels` 必须与 `options` 一一对应）；`int` + `slider` → 滑块（值会被钳制到 `[min,max]`）。
**说明文字放 `hint`，不要塞进 `description`** —— 本仓 `description` 一律 **≤ 12 字**，自检有断言。

自检守着这几条：枚举型条目的 `default` 必须在 `options` 内、带 `labels` 的条目 `labels` 与 `options` 等长。
⚠️ 这些断言只跑**当前硬编码的那 6 个枚举键**（`default_group_mode` / `private_mode` / `silent_scope` /
`batch_send_mode` / `retention_source` / `collect_sender_mode`）：**新增第 7 个枚举项不会被自动检查**，
要自己核对（或在自检的键清单里补上）。

### 5.3 新增一条 WebUI 路由

1. 在 `_register_web_api()` 的 `routes` 里加一行
   `("子路径", self.api_xxx, ["GET"|"POST"], "说明")`。
2. 写 `async def api_xxx(self) -> dict:` —— **不能有位置参数**。
   POST 的第一行固定是 `body = as_dict(await web_api.request.json(default={}))` 形态的归一化。
3. 页面侧调用时 endpoint **不带**插件名前缀。
4. **重载插件**后才会生效。

### 5.4 新增一条断言

断言写在 `tests/offline_check.py` 的 `main()` 里：

```python
    check("<一句中文描述>", <实得值>, <期望值>)   # 严格 ==
```

- 断言必须写在 `def main() -> int:` 内部、`return 0` 之前；失败项由末尾统一汇总。
- 标签写清「期望什么」，因为失败时只会打印 `FAIL  <标签>: <got>`。
- 用 `new_plugin(m, tag="...")` 建隔离实例（每个用例组一个 tag，避免互相污染）。
- 需要跑指令 handler 时用 `drive(agen)` 驱动 async generator，它会顺便校验「回复后必须 stop_event」。
- **断言质量比数量重要**：一条永远为真的断言等于没测。新增断言后做一次**证伪**（见 §6.5）。

### 5.5 新增一次数据库迁移

在 `_init_db()` 里按 `PRAGMA table_info(<表>)` 探测缺列，缺了就 `ALTER TABLE ADD COLUMN`，
**每列独立 `try/except`**，失败项汇总告警一次：

```python
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(images)")}
            if "new_column" not in cols:
                try:
                    conn.execute("ALTER TABLE images ADD COLUMN new_column INTEGER NOT NULL DEFAULT 0")
                except Exception as e:
                    logger.warning(f"[image_pool] 迁移 new_column 失败: {e}")
```

**一个失败的 `ALTER` 绝不能让插件构造失败。**

## 6. 离线自检

### 6.1 跑法

```powershell
$env:PYTHONUTF8 = "1"
python tests/offline_check.py
```

不装 AstrBot、不装 Pillow、不连外网。harness 会把 `astrbot.*` 全部 stub 掉，
再用 `importlib` 按文件路径加载 `main.py`；`new_plugin()` 用 `__new__` 造实例、跳过 `__init__`，
并给每个用例组分配独立的 DB 与图片目录。

### 6.2 退出码

| 码 | 含义 |
|---|---|
| `0` | 全部通过（末行 `✅ 全部通过`） |
| `1` | 有断言失败（末行 `❌ N 项失败` 并逐条列出），**或**在跑到汇总前就崩了（例如 `UnicodeEncodeError`） |

本自检**没有**区分「中断」与「失败」的专用退出码，所以**以末行输出为准**：
末行是 traceback 就是中断，后半段断言一条都没跑，**不是通过**。

### 6.3 断言数从哪来

`PASS` 行数是**运行期**计数，不是静态调用点数（循环会把同一个调用点放大）。
所以「断言数变了」既可能是新增 / 删除，也可能是某个循环的数据变了 ——
**判断回归要比「相同断言数下的失败集合」**。

### 6.4 覆盖范围

文件头嗅探、名称归一化与防路径穿越、参数消歧、群级三档权限、管理员分层与跨群越权、
静默判定、采集跳过指令（含原 bug 复现对照）、采集发送者黑白名单双模式、
标签别名（归一化键 / 校验约束 / 只读建议 / 增删改查 / 冲突与 force 接管 / 生命周期联动 /
收编 / 严格模式拒绝静默建分类 / 索引一致性）、多分类 tag 收录（含引用多图、上限截断）、
按引用图删图 / 移动（含跨分类、限定分类、权限拒绝、幂等跳过）、去重未分类的语义与范围、
保留期 / 容量清理 / 节流、引用消息收录（含回拉、失败降级、被动采集不展开）、
**配置 schema 与代码一致性**、SQLite 增删查去重与 seq 单调性、指纹屏蔽、全库随机取样、
move/copy 与多 tag、分类维护、老库迁移、缩略图、批量发送形态。

### 6.5 自检是变更探针，不是验收证据

绿灯只说明「没弄坏已知断言」，**不能**证明协议端行为、真实 AstrBot 的加载与升级、
真实 Dashboard 里的 WebUI 表现、与其它插件的真机联动。需要真机才能确定的结论必须标 `待核实`。

**证伪是硬要求。** 声称「修好了」之前：

1. 把改动**改回旧行为**（或禁用新分支）。
2. 重跑自检：**对应断言必须变红**。
3. 恢复改动，断言重新变绿。

第 2 步不变红 ⇒ 这条断言**没有守着你声称的行为**，那不是「修好了」，是「没测到」。

**作者不得自验。** 改动者不能用自己的自检结论结案；需要独立方按「能不能证伪」去验，
结论要落到具体断言或命令输出，不能只说「已修复」。

## 7. 提交规范

采用 **Conventional Commits**，**主题与正文用中文**。

```
<type>(<scope>): <中文主题，一句话，不超过 50 字>

- 变更要点（为什么改，不是重复主题）
- 影响面：命令 / 配置项 / 数据表 / 页面
```

### 7.1 `type`

| type | 用于 |
|---|---|
| `feat` | 新增用户可见能力 |
| `fix` | 修复缺陷 |
| `perf` | 性能与资源占用 |
| `refactor` | 不改变行为的重构 |
| `docs` | 只改文档 |
| `test` | 只改测试与 fixture |
| `chore` | 构建、依赖、杂项 |
| `chore(release)` | 发版准备 |

### 7.2 `scope`

用改动所在的层，常用：`store`（数据层）、`pool`（收录 / 出图）、`alias`（分类与别名）、
`collect`（采集）、`retention`（清理）、`perm`（权限）、`webui`、`ingest`（跨插件接口）、
`release`、`docs`。

### 7.3 示例

```
feat(alias): 新增 /图库 别名 收编，合并折叠成对分类

- 别名与正式名互斥，故只搬图 + 删源分类、不登记别名
- 影响：以前删除折叠冲突分类的提示指向的这条路现在真的可用
```

```
fix(perm): plugin_admins 改为只按发送者 ID 匹配

- 此前把群号填进去等于把整群提成全局管理员
- 影响：名单里误填群号的实例会失效，需改为填用户 ID（/sid 查）
```

### 7.4 破坏性变更

在 type 后加 `!`，并在正文写 `BREAKING CHANGE:` 段落说明影响与迁移方式：

```
feat(config)!: 采集发送者过滤改为黑白名单双模式

BREAKING CHANGE: 旧键 skip_senders 仅在 blacklist 模式下作为兼容来源生效，
请改用 collect_sender_mode + collect_senders。
```

### 7.5 版本与标签

- 版本号出现在 **3 处**：`metadata.yaml: version`、`main.py` 里 `@register(...)` 的版本字面量、
  `CHANGELOG.md` 的 `## vX.Y.Z` 小节。
- 发布时还要与 git tag `v<version>` 一致。
- **注意**：本项目自检**没有**「版本三处一致」的断言（`@register` 用的是字面量而非常量），
  所以这三处目前**只能靠人工核对** + 打包守卫兜底。
- 打标签：`git tag -a v1.7.0 -m "v1.7.0：<一句话要点>"`。

### 7.6 不要提交

运行时数据与产物已在 `.gitignore` 里：`data/`、`*.sqlite3*`、`*.log`、`*.zip`、`dist/`、
`__pycache__/`、`.test_tmp/`。**公开边界就是「提交了什么」** —— 打包时筛不掉东西，
所以不要提交本机绝对路径、私人邮箱、真实群号 / 实例名、真实第三方图片样本或任何凭证。

## 8. 发版检查清单

1. `CHANGELOG.md` 增加 `## vX.Y.Z` 小节，只列**用户可见**的变化。
2. 同步版本号：`metadata.yaml`、`main.py` 的 `@register(...)` 版本、`CHANGELOG.md`。
3. 跑离线自检，与「相同断言数下的基线」比失败集合。
4. 需要真机确认的项（合并转发形态、WebUI 在真实 Dashboard 的表现、群级三档权限的实际效果、
   与其它插件的联动）逐项目视确认，未确认的写 `待核实`。
5. 确认没有把开发期内容（审查记录、实测语料、本机信息）带进提交。
6. 打 tag、推送、按 tag 建 Release。

> 维护者另有一套打包与发布前守卫脚本，位于**开发工作区**、**不随包发布**；
> 它核对 metadata 合法性、版本一致性、归档白名单、体积上限与脱敏命中。
> 贡献者只需保证第 1–5 步成立。
