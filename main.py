"""角色图库插件 (AstrBot Plugin)

v1.7.0 变更（**只增不改**：为 astrbot_plugin_x_monitor 提供公开入库接口）
    - 新增公开方法 ``ingest_files(paths, *, tag, group_id="", uploader="", source="x")``：
      其他插件把本地图片文件交给图库归档，返回 ``(added, skipped)``。
      它复用 ``_db_add`` 的全部既有语义（``_ensure_category`` 同步 ``tag_index``、
      seq 分配、``(tag, sha256)`` 去重、``blocked_hashes`` 全局指纹黑名单、20 MB 上限），
      因此**没有第二份写入逻辑**，也不会有"图存进去了但 /图库 列表 看不见"的静默失败。
    - 不变量：正常返回时 ``added + skipped == len(paths)``；``skipped`` 覆盖读不出/空文件/超大/
      非图片/黑名单/重复/分类名被拒。**基础设施失败（库被锁/只读/损坏、目录不可写）仍然抛异常**，
      不伪装成 ``skipped``。
    - 入参容错：``paths`` 支持列表 / 单个 ``str`` / 单个 ``Path`` / 生成器；
      单值不会被逐字符迭代，``None`` 元素计入 ``skipped``。
    - 魔数不认时只接受明确的图片后缀（``IMAGE_EXTS``）：此前草稿的"后缀兜底 ``.jpg``"
      会把视频存成图片。
    - ``tag`` 归一化后为空即拒绝入库，绝不回落 ``未分类``（避免外部图与自采图混在一起）。
    - **对抗审核修复（同一未发布版本内）**：
      · ``_ensure_category`` 补上 ``_db_category_create`` 已有的**别名占用**闸：
        少了它，``ingest_files`` 能建出"与已登记别名同名"的分类（``/别名`` 静默改道，
        且该分类没有 ``kind='tag'`` 行）；折叠键被别的分类占用时改用合成键，
        保持"一分类一行 tag_index"这条不变量。
      · ``_db_add`` **绝不覆盖已存在的文件**：写入前把 ``seq`` 顺延到磁盘上的空位。
        这才是折叠成对分类（``Hatsune`` / ``HATSUNE`` 共用同一个物理目录）互相覆盖的根治手段 ——
        ``seq`` 原本只在各自 ``char_name`` 内递增，两边都算出 1，后写的直接盖掉前一个 tag 的图，
        而 DB 里两行 ``sha256`` 各记一份 ⇒ ``/发图`` 发错图（独立验证的反例 F-1 实测）。
        顺延分配让两个分类的文件编号天然错开，谁都不用被冻结。
        **三条写入路径共用同一个 ``_next_free_dest()``，且"选路失败后的处理"也统一**
        （``_db_add`` / ``_db_transfer`` / ``_db_category_delete``）：
        G-1 证明只在 ``_db_add`` 里顺延、且循环有上限后仍会覆盖；
        G-2 / F-I-1 / F-I-2 证明漏掉 ``_db_transfer``（WebUI 重新分类）同样会覆盖 ——
        它的 ``shutil.copy2`` 对"目标是目录"的语义是**把文件复制进目录**（静默成功），
        所以"目标已存在"必须在调用文件操作**之前**就按失败处理。
        连空位都找不到（极端占位）时**跳过这一张**，绝不覆盖。
        失败回滚时按"内容指纹仍一致"清理本次新建的文件，既不留孤儿图、也不误删别人的文件
        （独立验证的反例 F-2 覆盖不可回滚、F-3 归属 TOCTOU）。
      · 分类迁移改为**只跳过"文件占位"**、被目录占住仍按失败处理（保持 P1-4 的语义），
        并在源与目标其实是同一个文件时直接改 DB 行（折叠成对分类合并要用）。
      · ``/图库 别名 收编`` 现在**真的能合并折叠成对分类**：别名与正式名互斥，
        所以这类合并只搬图 + 删源分类、不登记别名。此前 ``_db_category_delete`` 拒绝折叠冲突时
        给用户指的就是这条路，但它一直是死的（既删不掉也合不了 —— 独立验证 F-3/D4）。
      · **遗留的"两行 → 同一物理文件"状态**（旧版覆盖 bug 之后的真实库）不再被二次伤害：
        `/删图` 删文件前先看还有没有别的行指向它（有就只删记录）；`/移动` 与折叠对合并
        在文件被别的行共用时**改成复制**而不是 rename（rename 会把对方那行变成悬空行）——
        独立验证 F-I-4 实测过"发出字节与行 sha 不符"。
      · ``_ensure_category`` 再挡掉空名 / 纯空白 / ``.`` / ``..`` / 带首尾空白的名字
        （Windows 上 ``pool/E4 /`` 这种目录名会直接 ``FileNotFoundError``，G-4 / F-I-5）。
      · **"还有谁在引用这个文件"统一走 `_reference_index()`**（一次 O(n)，身份键
        `(st_dev, st_ino)`，文件不存在时回落到名字键）：删/搬之前先看这一眼，并且**铺到全部四条
        会 unlink 的路径** —— `/删图`、`_db_category_delete(purge)`、`_db_dedupe_namespace`、
        `_db_sweep_retention`。K 的反例证明只护住 `/删图` 是不够的：**保留期清理是自动触发的**，
        它会把"另一个分类唯一一份图"删掉，用户连拦的机会都没有（K-N1/N-3/N-4）。
        用索引而不是两两 `os.path.samefile`，是因为后者是 O(n²) 且逐条发生在
        `BEGIN IMMEDIATE` 里：10k 行时每条 ≈2s，move 60 条持锁 117s、200 条 465s，
        并发入库直接 `database is locked`（K-N5 —— 这是本版自己引入的性能回归，已修）。
        身份键必须用 `st_ino` 而不是 `normcase`：`mklink /J`（普通用户可建）会让两条
        rel_path 落到同一个物理文件而名字键不同，purge/删图 照删不误（M-N1）。
      · `_db_category_delete` 的"目标已有同图 → 删源"分支**先确认目标行真的可用**
        （`is_file()`：文件不存在**或被同名目录占住**都算不可用）：那时源文件是磁盘上
        唯一一份，删掉等于全库丢图（K-N2 / M）。修法是**把源文件搬进目标分类自己的目录**
        再"过继"给目标行 —— 只改 `rel_path` 会让文件留在源分类目录里，而 `/图库 改名`
        是按 `char_name` 重写路径 + 只改目录名，过继行随后就变悬空行
        （独立验证 O-N1 实测 `/发图` 报"图片文件都丢失了"，文件其实还躺在旧目录）。
      · `_db_category_delete` 的 move 分支把文件操作**在失败时撤销**、把 dup 分支的 unlink
        **推迟到 commit 之后**：文件操作早于 commit 是刻意的（否则行会指向不存在的文件），
        但事务中途失败时行会回滚、文件却已经搬走 ⇒ 永久悬空行（M-N2 用触发器在真实 SQL
        路径上复现）。purge / 去重 / 清理本来就是"先 commit 再删文件"，move 是漏网的那条。
      · **写入/复制/搬移都改成原子独占**（`open(dest,"xb")` / 占位 + `os.replace`）：
        "先检查再写"不是原子的，检查通过与真正写入之间被别的写入者抢占就会覆盖
        （K-N6 用打桩确定性复现）。写入中途失败（如磁盘满）时，`_create_exclusive`
        **自己收掉刚建出来的半截文件**再抛 —— 调用方是写成功后才登记路径的，
        异常在写入中抛出时它根本不知道有这个文件（独立验证 O-A4）。
      · `_ensure_category` 再挡掉**含零宽字符**的名字（`\u200b` 这类 `str.strip()` 不去，
        建出来的分类名字不可见、用户敲 `初音` 也解析不到 `初音\u200b` —— K-N7 / M-N6）；
        `_db_category_rename` 也补了"源分类必须存在"的检查（M-N4）。
      · **已知代价**：`_next_free_dest` 是从 `MAX(seq)+1` 起线性探测空位。
        正常情况 1 次命中；只有"同目录里堆着大量**不属于本分类**的文件"（折叠对里另一方
        有 10 万张图）才会退化成最坏 ~10 万次探测（实测 11–20 s/次）。
        这是为了"绝不覆盖"付的代价，有界（不会无限循环）。
      · **已知边界**：全部条目被黑名单/去重跳过时仍会先建出一个空分类（K-N8）；
        symlink 形态未能在本机构造（无权限），只有 junction 的直接证据。
      · ``/加图`` 一张都没进时不再一律报"跳过重复"：分类名被拒也会说明，
        避免用户以为图已经在库里（独立验证 F-6）。
      · ``ingest_files`` 的锁等待上限收紧到 ``INGEST_BUSY_TIMEOUT_SEC``（5 s，普通指令路径仍 30 s），
        避免"调用方超时放弃、写入稍后落地"的对账不一致。
      · ``ingest_files`` 的 docstring 改为如实描述异常语义、不变量口径与 ``tag_index`` 缺行的真实后果
        （``/图库 列表`` 会从 ``images`` 兜底显示名字，"看得见但够不到"）。
      · 迁移/删除目标被拒时的提示不再一律说"是内置指令名"，而是列出三类真实原因
        （保留字 / 物理目录冲突 / 已被登记为别名），免得把排查方向带偏。
      · **已知未修**：``normalize_name`` 不拒绝 Windows 保留设备名，所以 ``/加图 NUL`` 仍会在写盘时
        抛 ``FileNotFoundError``（跨插件调用方已被 x_monitor 侧守卫挡住，见那边 ``pool_tag``）。
    - 未改动表结构与配置项；既有指令在**正常输入**下行为不变 —— 新增拒绝的只有此前会
      **静默写坏数据**的分类名（保留字之外再加"仅大小写不同"与"已被登记为别名"两类）。

v1.6.2 变更（对抗式审查修复：2 严重 + 3 严重级 + 9 中等）
    - 权限：分类新建/改名/删除改回 `_is_global_admin`。此前白名单群开
      `whitelist_allow_manage` 会让**普通成员**跨群改/删分类（purge 连图删除），
      与 README / schema / 开发指南三处承诺矛盾
    - 权限：`plugin_admins` 只按发送者 ID 匹配。此前群号填进去等于把整群提成全局管理员
    - 稳定性：`_init_db` 全链路兜异常。此前库被锁/损坏/只读时异常逃出 `__init__`，
      AstrBot 会把整个插件记为加载失败（指令 + WebUI + 采集全挂）
    - WebUI：9 条 POST 路由统一 `as_dict()` 归一请求体。此前合法但非对象的 JSON
      （`[1,2]` / `"x"` / `5`）会 AttributeError → HTTP 500
    - WebUI：`thumbs` 回传生效 `limit`，前端页大小 96 → 60。此前请求 96、服务端只给 60，
      中间的图被静默跳过且分页文案谎报
    - 解析：`_db_resolve_tag` / `_db_resolve_many` 改为**精确名优先、折叠键兜底**。
      此前顺序相反，老库（≤v1.5.0）里"仅大小写不同"的两个分类会互相顶掉，
      `/图库 删除 purge HATSUNE` 实际删的是 `Hatsune` 的目录连图
    - 配置：新增 `to_bool()` 并在全部布尔配置/请求字段上使用。此前配置写字符串 `"false"`
      会被判为真 —— `allow_purge`（安全）与 `auto_collect`（隐私）会反向打开
    - 静默：`served=True` 且无结果时收口事件。此前 `silent_scope=all` 下 `/分类名`
      图片被吞但事件不收口，LLM 会补一条文本回复
    - 数据：分类迁移搬移失败不再改写 DB 行、不再回执"成功"；`_resolve_paths` 遇到
      文件缺失只告警不再删索引行（瞬时不可用会永久丢元数据）
    - 清理：`retention_source` 非法值 fail-close（此前回落 `WHERE 1=1`，连人工图一起删）
    - 分类名：`_db_category_create` / `_db_category_rename` 拒绝保留字（此前 WebUI
      与指令都能建出「加图」「图库」这类永远解析不到的僵尸分类）
    - 性能：缩略图加有界缓存（此前每次请求串行重算，1024px 单页实测 2.4 s / 12.5 MB）
    - 移除：`_db_pick` 里生产不可达的 `COUNT + OFFSET` 随机分支（随机一律走
      `_db_pick_many` 的 `ORDER BY RANDOM()`）
    - 第二轮（独立对抗验证发现）：
      · 保留字校验下沉到 `_ensure_category` —— 此前 `/加图 <新名>`（`tag_auto_create`
        默认开）与 WebUI 重新分类仍能建出「图库」「help」僵尸分类
      · 文件被外部删除后重新收录同一张图，会**就地修复**那一行，不再被孤儿行挡住
      · 折叠冲突（`Hatsune`/`HATSUNE` 共用目录）时**拒绝删除/迁移**并提示先合并；
        清索引不再按折叠键误删对方的 tag 行
      · `plugin_admins` 只认非群会话 UMO（群 UMO + 成员 UID 撞群号不再提权）
      · `to_bool("")`/`to_bool("  ")` 走默认值；缩略图缓存并发首访不再重复记账
      · 分类迁移的"已移入"不再把"因目标已有同图而去重"的项算进去
      · 构造期 `StarTools.get_data_dir` 失败不再让插件加载失败；移除死代码 safe_rmtree
    - 测试：377 → 464 项断言

v1.6.0 变更
    - 标签别名：可为每个分类登记一组别名（大小写变体、昵称、常见错拼），
      解析指令时归一到原 tag，而不是新建 tag
    - 新增索引表 `tag_index(key PRIMARY KEY, canonical, kind, alias_text)`：
      正式名（kind='tag'）与别名（kind='alias'）共用一张表 ⇒ 解析 = 一次主键查询，
      且「同一别名被多个 tag 声明」由主键硬唯一直接消灭（先到先得）
    - 新增 `/图库 别名 列表|加|删|改|收编`
    - 新增 `tag_auto_create`（关掉即根治「打错字多出一个分类」）、`alias_suggest_on_miss`
    - 匹配规则：整词精确匹配、大小写不敏感（Python 侧 casefold，SQLite 的 lower() 只认 ASCII）；
      不做部分/模糊匹配，未命中只给只读建议

v1.5.0 变更
    - `/加图 A B C`：一个指令可同时给多个分类（tag）打标，同一条消息里（含引用）的
      每一张图都会分别落到每一个分类下；上限 `max_tags_per_add`
    - `/删图` 支持引用：`/删图 [分类名]` + 引用含图消息，按图片内容（sha256）删除；
      不填分类名 = 跨所有分类找同内容的图
    - 新增 `/移动 目标分类 [源分类] [id]`：把图片换 tag（指定 id 或引用取图）
    - 去重未分类语义收紧：**只**清「采集落点」分类里已在其它 tag 出现的自动采集图，
      永不触碰其它 tag（多人图在 A/B/C 的合法多 tag 记录天然安全）；
      不带参数 = 与所有现有 tag 依次比对

v1.4.1 变更
    - WebUI 配置项：6 个枚举型 string 改为下拉（options + labels），
      4 个小数数值项加滑块，38 项说明统一拆成短 description + hint（+ 关键项 obvious_hint）
    - 新增 schema 与代码一致性的断言（下拉选项集、labels 对齐、滑块区间、description 长度）

v1.4.0 变更
    - 采集发送者过滤改为黑白名单双模式：`collect_sender_mode` + `collect_senders`
      （旧键 `skip_senders` 仍作为黑名单模式的兼容来源）
    - `/加图` 支持引用（reply）消息：收录被引用消息里的图片；链为空时尝试协议端 get_msg 回拉
    - 明确边界：引用取图只在 `/加图` 指令路径生效，被动采集**不**回拉引用
      （否则任何消息都能诱导机器人去取任意 message_id）

v1.3.0 变更
    - 修复：带 `/加图 分类名` 的消息会被采集器再收一份到默认分类（同一图同时进目标分类与默认分类）
      新增 `collect_skip_commands`（默认开）——指令/斜杠域消息属"发给机器人的"，不进被动采集
    - 新增 `/图库 去重未分类`：清理历史误归类（仅动 source='auto'，人工收录不动）
    - 新增保留期清理：`retention_days` 超时删除 + `max_pool_mb` 容量淘汰，默认关闭，
      默认只清理采集入库的图（`retention_source='auto'`），`retention_exempt` 可豁免分类
    - 新增 `/图库 清理`：手动触发一次清理

v1.2.0 变更
    - 权限重构为「群级三模式」：白名单群 / 发图群 / 未配置（默认仅管理员）
    - 新增群级管理员 `group_admins`：可管理本群采集的图片（全局管理员不受限）
    - 新增 `/发图 [n]`：从全库随机发送 n 张
    - 新增多图发送：/角色名 x3、/来图 角色名 3张、/发图 3，默认用合并转发（聊天记录）包裹
    - WebUI 新增「重新分类」：批量移动到 / 复制到其他分类，单张也可改分类
    - 明确 tag 语义：分类即 tag；移动=换 tag，复制=加 tag（一张图可多 tag）

功能
    1. ``/加图 分类名``            —— 收录同一条消息内的图片（权限见「群级模式」）
    2. ``/角色名`` / ``/角色名 x3`` / ``/角色名 3`` —— 随机 1 张 / 随机 3 张 / 指定 id
    3. ``/发图 [n]``              —— 从全部图片中随机发 n 张
    4. ``/图库 <子指令>``          —— 整理（列表/新建/改名/删除/屏蔽/解屏/已屏蔽）
    5. 被动采集（默认关闭）         —— 群内潜水偷图，不回复任何消息

群级权限模式
    | 模式      | 加图 | 发图 | 管理（删图/分类/屏蔽） |
    |-----------|------|------|------------------------|
    | whitelist | 全员 | 全员 | 仅管理员（可用 whitelist_allow_manage 放开，且限本群图） |
    | post      | 仅管理员 | 全员 | 仅管理员            |
    | none      | 仅管理员 | 仅管理员 | 仅管理员           |

设计约束（与 MaiBot 共存）
    所有主动触发都收在斜杠域内。MaiBot 用 ``^\\/.*`` 屏蔽斜杠消息，
    斜杠域于是成为 AstrBot 独占通道，两者互不抢答。

存储
    图片二进制 -> 文件系统 ``data/plugin_data/<plugin>/pool/<分类名>/<seq>.<ext>``
    元数据索引 -> SQLite ``data/plugin_data/<plugin>/index.sqlite3``
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import io
import json
import os
import random
import re
import shutil
import sqlite3
import threading
import time
import traceback
import unicodedata
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Face, Image, Reply
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Node, Nodes
from astrbot.core.star.filter.command import GreedyStr

try:  # Plugin Pages 需要 AstrBot >= v4.24.1
    from astrbot.api.web import error_response, file_response, json_response, request

    WEB_API_AVAILABLE = True
except Exception:  # pragma: no cover - 旧版本优雅降级
    WEB_API_AVAILABLE = False
    request = None

    def json_response(*args, **kwargs):
        raise RuntimeError("astrbot.api.web 不可用，请升级 AstrBot")

    def error_response(*args, **kwargs):
        raise RuntimeError("astrbot.api.web 不可用，请升级 AstrBot")

    def file_response(*args, **kwargs):
        raise RuntimeError("astrbot.api.web 不可用，请升级 AstrBot")


PLUGIN_NAME = "astrbot_plugin_image_pool"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MAX_NAME_LEN = 32
MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_NAMESPACE = "未分类"

#: 跨插件 ``ingest_files`` 的锁等待上限（秒）。调用方（如 x_monitor）有自己的时间预算，
#: 让它在这里干等 30 s 只会造成"调用方已超时放弃、写入却稍后落地"的对账不一致；
#: 宁可早点抛 ``database is locked``，由调用方按失败记账。普通指令路径仍用 30 s。
INGEST_BUSY_TIMEOUT_SEC = 5.0

# 已被其他插件/内置占用的指令名，避免被 "/(分类名)" 动态路由吞掉
RESERVED_NAMES = {
    "加图", "来图", "发图", "图库", "删图", "移动", "别名", "随机图",
    "help", "reset", "new", "stop", "sid", "set", "unset",
    "provider", "stats", "name", "dashboard_update", "random",
}
# 保留字比较也要大小写不敏感，否则 "Help" 能绕过
RESERVED_KEYS = {n.casefold() for n in RESERVED_NAMES}

# tag_index.kind
KIND_TAG = "tag"
KIND_ALIAS = "alias"
# 建议层的最低相似度（低于它就闭嘴，避免噪声提示）
SUGGEST_MIN_RATIO = 0.6

# 群级权限模式 -> 非管理员拥有的能力
MODE_CAPS: dict[str, dict[str, bool]] = {
    "whitelist": {"add": True, "fetch": True},
    "post": {"add": False, "fetch": True},
    "none": {"add": False, "fetch": False},
}
VALID_MODES = set(MODE_CAPS)

# "分类名 可选参数"
NAME_ARG_RE = re.compile(r"^(?P<name>[^\s/]+)(?:\s+(?P<arg>\S+))?$")
# 数量标记：x3 / *3 / ×3 / 3张
COUNT_MARK_RE = re.compile(r"^[x*×]\s*(\d{1,3})$|^(\d{1,3})\s*张$")
# 多 tag 分隔符：空白、半/全角逗号、顿号、加号
TAG_SPLIT_RE = re.compile(r"[\s,，、+]+")

# 缩略图缓存的惰性初始化锁（模块级：实例状态在首次使用时才建）
_THUMB_INIT_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# 纯函数工具
# --------------------------------------------------------------------------- #
def sniff_ext(head: bytes) -> str | None:
    """按文件头嗅探图片扩展名（不依赖 Python 3.13 已移除的 imghdr）。"""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"BM"):
        return ".bmp"
    return None


def normalize_name(raw: str) -> str | None:
    """归一化分类名，使其可安全用作目录名（防路径穿越）。"""
    if not raw:
        return None
    name = unicodedata.normalize("NFKC", str(raw)).strip()
    name = ILLEGAL_CHARS.sub("", name)
    name = name.strip(" .")
    if not name or name in {".", ".."}:
        return None
    return name[:MAX_NAME_LEN]


def to_str_list(value) -> list[str]:
    """把配置项统一成非空字符串列表。"""
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    out = []
    for v in value:
        s = str(v).strip()
        if s:
            out.append(s)
    return out


def to_bool(value, default: bool = False) -> bool:
    """把配置 / JSON 里的值统一成布尔，必须显式处理字符串。

    ``bool("false")`` 是 ``True`` —— 配置里写成字符串 ``"false"`` 会让
    "默认关闭"的开关反向打开（``allow_purge`` 连图删除、``auto_collect`` 采集）。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if not s:
        # 空 / 纯空白：当作"没配置"，走默认值，而不是当成 False ——
        # 否则默认打开的开关遇到空白配置会被静默关掉。
        return default
    if s in {"1", "true", "yes", "on", "y", "t"}:
        return True
    if s in {"0", "false", "no", "off", "n", "f"}:
        return False
    return default


def as_dict(payload) -> dict:
    """Web API 请求体归一化。

    ``request.json(default=...)`` 只在**解析失败**时返回 default；合法但非对象的
    JSON（``[1,2]`` / ``"x"`` / ``5`` / ``null`` / ``true``）会原样穿透，
    紧接着的 ``payload.get(...)`` 会抛 AttributeError → HTTP 500。
    """
    return payload if isinstance(payload, dict) else {}


def remove_dir_if_empty(path: Path) -> None:
    """迁移后收尾：目录里还有文件就保留，避免误删未成功迁移的数据。"""
    try:
        if path.exists() and path.is_dir():
            for p in path.iterdir():
                if p.is_file():
                    return
            path.rmdir()
    except Exception:
        pass


def parse_count(raw, default: int = 1) -> int:
    """解析"数量"参数：裸数字、x3、*3、×3、3张 均可。"""
    s = unicodedata.normalize("NFKC", str(raw or "")).strip()
    if not s:
        return default
    if s.isdigit():
        return int(s)
    m = COUNT_MARK_RE.match(s)
    if m:
        return int(m.group(1) or m.group(2))
    return default


def parse_fetch_arg(raw) -> tuple[int | None, int, bool]:
    """解析出图参数，返回 (seq, count, recognized)。

    消歧规则：
        "3"  -> seq=3, count=1   （裸数字 = 指定 id，保持既有语义）
        "x3" -> seq=None, count=3（带标记 = 数量）
        "3张" -> seq=None, count=3
    """
    s = unicodedata.normalize("NFKC", str(raw or "")).strip()
    if not s:
        return None, 1, True
    if s.isdigit():
        return int(s), 1, True
    m = COUNT_MARK_RE.match(s)
    if m:
        return None, int(m.group(1) or m.group(2)), True
    return None, 1, False


def parse_tags(raw, limit: int = 5) -> tuple[list[str], int]:
    """把 ``/加图 A B C`` 的贪婪参数拆成多个分类名：返回 (tags, dropped)。

    去重保序、逐个归一化（防路径穿越），超出 ``limit`` 的部分丢弃并计数。
    一个分类名内部不能含空格 —— 历史上 ``/加图`` 会把空格直接删掉，
    所以不存在"带空格的分类名"这一包袱，改成按空白/逗号切分不会破坏旧数据。
    """
    if not raw:
        return [], 0
    out: list[str] = []
    for part in TAG_SPLIT_RE.split(str(raw)):
        name = normalize_name(part)
        if name and name not in out:
            out.append(name)
    cap = max(1, int(limit or 1))
    if len(out) <= cap:
        return out, 0
    return out[:cap], len(out) - cap


def parse_delete_args(arg1, arg2) -> tuple[str | None, int | None, str]:
    """解析 ``/删图`` 参数，返回 (分类名, id, err)。

    两种形态：
        ``/删图 分类名 id``  -> (分类名, id, "")
        ``/删图 [分类名]``   -> (分类名或 None, None, "") 随后走引用取图
    只给一个裸数字视为歧义（id 必须配分类名），返回错误提示而不是瞎猜。
    """
    a = unicodedata.normalize("NFKC", str(arg1 or "")).strip()
    b = unicodedata.normalize("NFKC", str(arg2 or "")).strip()
    if not a and not b:
        return None, None, ""
    if b:
        if not b.isdigit():
            return None, None, "第二个参数应是图片 id（数字）。"
        char = normalize_name(a)
        if not char:
            return None, None, "分类名非法（不可含空格与 / \\ : * ? \" < > |）。"
        return char, int(b), ""
    if a.isdigit():
        return None, None, "指定 id 时必须同时给出分类名：/删图 分类名 id"
    return normalize_name(a), None, ""


def parse_move_args(
    arg1, arg2, arg3
) -> tuple[str | None, str | None, int | None, str]:
    """解析 ``/移动`` 参数，返回 (目标分类, 源分类, id, err)。

    形态：
        ``/移动 目标 源 id``  -> 把「源」分类下 id 这张图换 tag 到「目标」
        ``/移动 目标 [源]``   -> 引用一条含图消息，把引用的图换 tag 到「目标」
    目标分类放在第一个参数位（读作"移动到 X"），避免源/目标顺序歧义。
    """
    target = normalize_name(str(arg1 or ""))
    if not target:
        return None, None, None, "用法：/移动 目标分类 [源分类] [id]"
    src = normalize_name(str(arg2 or "")) if str(arg2 or "").strip() else None
    raw3 = str(arg3 or "").strip()
    if raw3:
        if not raw3.isdigit():
            return None, None, None, "第三个参数应是图片 id（数字）。"
        if not src:
            return None, None, None, "指定 id 时必须同时给出源分类：/移动 目标分类 源分类 id"
        return target, src, int(raw3), ""
    if str(arg2 or "").strip().isdigit():
        return None, None, None, "缺少源分类：/移动 目标分类 源分类 id"
    return target, src, None, ""


# --------------------------------------------------------------------------- #
# 标签别名：归一化键 / 校验 / 只读建议
# --------------------------------------------------------------------------- #
def name_key(raw: str) -> str | None:
    """归一化匹配键 = ``normalize_name`` + ``str.casefold()``。

    折叠必须在 **Python 侧**做：SQLite 内置的 ``lower()`` 只处理 ASCII，
    对 ``ß`` / ``İ`` 这类字符会漏；NFKC 已经在 ``normalize_name`` 里处理了全角。
    """
    name = normalize_name(raw)
    return name.casefold() if name else None


def check_alias_name(raw: str) -> tuple[str | None, str]:
    """校验别名（比分类名更严），返回 (别名, 错误信息)。

    额外三条约束的理由：
        * 不含空白/``/`` —— 动态路由 ``NAME_ARG_RE`` 只吃单个 token，多词别名永远匹配不到；
        * 非纯数字      —— 裸数字已被 ``parse_fetch_arg`` 定义成图片 id；
        * 不在保留字里  —— 否则别名可以劫持内置指令（``/加图`` 之类）。
    """
    text = str(raw or "").strip()
    if not text:
        return None, "别名不能为空。"
    if re.search(r"[\s/]", text):
        return None, "别名不能含空白或「/」——否则在指令里永远匹配不到（只支持单个词）。"
    name = normalize_name(text)
    if not name:
        return None, "别名非法（不能只由空白或非法字符组成）。"
    if len(name) > MAX_NAME_LEN:
        return None, f"别名过长（最多 {MAX_NAME_LEN} 字）。"
    if name.isdigit():
        return None, "别名不能是纯数字（会和图片 id 撞车）。"
    if name.casefold() in RESERVED_KEYS:
        return None, f"「{name}」是内置指令名，不能作为别名。"
    return name, ""


def suggest_tags(raw: str, candidates: list[str], limit: int = 3) -> list[str]:
    """只读建议：把输入折叠后与候选正式分类名比对，返回最相似的几个。

    只用于**提示**，绝不据此改写解析结果 —— 否则同一个输入会随"当前有哪些 tag"
    漂移（今天落到 A、明天落到 B），属于不可复现的隐式路由。
    """
    key = name_key(raw)
    if not key:
        return []
    folded = [(c, c.casefold()) for c in candidates]
    exact = [c for c, f in folded if f == key]
    if exact:
        return exact[:limit]
    scored: list[tuple[float, str]] = []
    for name, f in folded:
        if not f:
            continue
        if f.startswith(key) or key.startswith(f):
            ratio = 0.95  # 一方是另一方的前缀，是很强的信号
        else:
            ratio = difflib.SequenceMatcher(None, key, f).ratio()
        if ratio >= SUGGEST_MIN_RATIO:
            scored.append((ratio, name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [n for _, n in scored[:limit]]


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
@register(
    PLUGIN_NAME,
    "califorliu",
    "角色图库：/加图 收录图片（支持多分类/别名/引用），/角色名 或 /发图 随机出图，/图库 整理",
    "1.7.0",
)
class ImagePoolPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        try:
            self.base_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception as e:
            # 数据目录解析失败（AstrBot 数据路径未配置 / ensure_dir 无权限）时，
            # 构造期异常会让整个插件加载失败。这里退到 AstrBot 的常规相对位置
            # （CWD 通常就是 AstrBot 根目录），并大声报错，让插件仍能加载与提示。
            logger.error(
                f"[image_pool] 无法解析插件数据目录（{e}），回退到 ./data/plugin_data/{PLUGIN_NAME}"
            )
            self.base_dir = Path("data") / "plugin_data" / PLUGIN_NAME
        self.pool_dir: Path = self.base_dir / "pool"
        self.db_path: Path = self.base_dir / "index.sqlite3"
        self.face_log: Path = self.base_dir / "faces.jsonl"
        try:
            self.pool_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            # 与 _init_db 同理：构造期异常会让整个插件加载失败。
            # 目录建不出来时插件仍应加载（后续写盘会各自报错）。
            logger.error(f"[image_pool] 创建图片目录失败: {e}")
        self._sweep_at = 0.0
        self._init_db()
        self._register_web_api()
        logger.info(f"[image_pool] 数据目录: {self.base_dir}")
        if self._collect_sender_filter()[0] == "whitelist" and not self._cfg(
            "collect_senders", []
        ):
            logger.warning(
                "[image_pool] 采集发送者过滤是「白名单」模式但名单为空 → 不会采集任何人的图片。"
                "请在 collect_senders 里填入允许采集的用户 ID。"
            )

    # ----------------------------- 配置读取 ----------------------------- #
    def _cfg(self, key: str, default):
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _in_id_list(self, event: AstrMessageEvent, key: str, scope: str = "group") -> bool:
        """名单匹配。scope 严格区分"群号"与"用户 ID"两个命名空间。

        - ``scope="group"``  → ``open_groups`` / ``post_groups`` / ``silent_groups``：
          只比群号与会话 UMO（schema 写的就是"群号或 UMO"）。
        - ``scope="sender"`` → ``plugin_admins``：只比发送者 ID，以及**末段等于该 ID**
          的会话 UMO（私聊 UMO 因此仍可用）。

        两者混用会造成越权：群号填进 ``plugin_admins`` = 该群所有人变成全局管理员；
        用户 ID 填进 ``open_groups`` = 这个人在**所有群**都被当成白名单群。
        """
        ids = set(to_str_list(self._cfg(key, [])))
        if not ids:
            return False
        umo = str(event.unified_msg_origin or "")
        if scope == "sender":
            uid = str(event.get_sender_id())
            if uid in ids:
                return True
            # 只认"非群会话"的 UMO（私聊 UMO 末段就是本人 ID）。
            # 群 UMO 一律不算：群号与 QQ 号共用号码空间，若只比末段，
            # "群 UMO 入名单 + 成员 UID 恰好等于群号"就会把该成员提成全局管理员。
            return (
                bool(umo)
                and umo in ids
                and "GroupMessage" not in umo
                and umo.rsplit(":", 1)[-1] == uid
            )
        gid = str(event.get_group_id() or "")
        return (bool(gid) and gid in ids) or (bool(umo) and umo in ids)

    def _group_allowed(self, event: AstrMessageEvent, key: str) -> bool:
        """通用群白名单：列表为空 = 不限制。"""
        allow = to_str_list(self._cfg(key, []))
        if not allow:
            return True
        return (
            str(event.get_group_id() or "") in allow
            or event.unified_msg_origin in allow
        )

    # ----------------------------- 标签解析 ----------------------------- #
    def _cfg_tag(self, key: str, default: str) -> str:
        """读配置里的分类名并解析别名。

        解析不到就用字面量（保持既有行为，不因为引入别名而让老配置失效），
        **绝不**因为配置里写错名字就去建分类。
        """
        raw = str(self._cfg(key, default) or default)
        info = self._db_resolve_tag(raw)
        return info["canonical"] or normalize_name(raw) or default

    def _plan_tags(self, names: list[str]) -> dict:
        """把用户输入的分类名映射成正式名；未命中的按 `tag_auto_create` 决定新建还是拒绝。

        返回 ``{"canonical": [...], "new": [...], "rejected": [...], "misses": [...]}``。
        别名与正式名可能指向同一个 tag（如 ``/加图 初音 hx``），所以 canonical 要去重 ——
        否则会对同一个分类写两次，第二次全是"跳过重复"，白跑一遍写盘。
        """
        mapping = self._db_resolve_many(names)
        canonical: list[str] = []
        for n in names:
            c = mapping.get(n)
            if c and c not in canonical:
                canonical.append(c)
        misses = [n for n in names if n not in mapping]
        # 保留字既不能自动新建、也不该混进"拼写建议"里：它是内置指令名，不是分类。
        reserved = [n for n in misses if (name_key(n) or n) in RESERVED_KEYS]
        misses = [n for n in misses if n not in reserved]
        if to_bool(self._cfg("tag_auto_create", True), True):
            return {
                "canonical": canonical,
                "new": misses,
                "rejected": reserved,
                "reserved": reserved,
                "misses": misses,
            }
        return {
            "canonical": canonical,
            "new": [],
            "rejected": reserved + misses,
            "reserved": reserved,
            "misses": misses,
        }

    def _suggestion_text(self, misses: list[str]) -> str:
        """未命中的名字给只读建议（不自动改写解析结果）。"""
        if not misses or not to_bool(self._cfg("alias_suggest_on_miss", True), True):
            return ""
        names = self._db_tag_names()
        lines = []
        for miss in misses[:3]:
            tips = suggest_tags(miss, names)
            if tips:
                lines.append(
                    f"· 「{miss}」你是不是想找「{tips[0]}」？"
                    f"要固定下来请发：/图库 别名 加 {miss} {tips[0]}"
                )
        return "\n".join(lines)

    def _new_tag_hint(self, new_tags: list[str]) -> str:
        """新建了分类时的提示：只有在"这个新名字看着像已有分类"时才多说一句。

        没有相似候选就什么都不加 —— 老老实实建一个新分类是正常操作，
        不该给回执塞噪音（也保持老文案稳定）。
        """
        if not new_tags or not to_bool(self._cfg("alias_suggest_on_miss", True), True):
            return ""
        names = self._db_tag_names()
        # 排除刚建出来的这些名字本身，否则会提示"你是不是想找「甲」找「甲」"
        exclude = set(new_tags)
        lines = []
        for tag in new_tags[:3]:
            tips = [t for t in suggest_tags(tag, names) if t not in exclude]
            if tips:
                lines.append(
                    f"· 「{tag}」你是不是想找「{tips[0]}」？"
                    f"要固定下来请发：/图库 别名 加 {tag} {tips[0]}"
                )
        if not lines:
            return ""
        return "（新建分类：" + "、".join(new_tags) + "）\n" + "\n".join(lines)

    # ----------------------------- 权限模型 ----------------------------- #
    def _is_global_admin(self, event: AstrMessageEvent) -> bool:
        """全局管理员：插件配置的 plugin_admins（不借道 AstrBot 全局 admins_id）。

        名单语义是"用户 ID"（schema 如此声明），所以只按发送者匹配 ——
        群号误填进这里曾经等于把整个群提成全局管理员。
        """
        return self._in_id_list(event, "plugin_admins", scope="sender")

    def _is_group_admin(self, event: AstrMessageEvent) -> bool:
        """群级管理员：group_admins 里的 "群号:UID"；"*:UID" 表示所有群。"""
        uid = str(event.get_sender_id())
        gid = str(event.get_group_id() or "")
        pairs = set(to_str_list(self._cfg("group_admins", [])))
        return f"{gid}:{uid}" in pairs or f"*:{uid}" in pairs

    def _group_mode(self, event: AstrMessageEvent) -> str:
        """解析当前会话的权限模式：whitelist | post | none。"""
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            mode = str(self._cfg("private_mode", "post")).strip().lower()
            return mode if mode in VALID_MODES else "post"
        if self._in_id_list(event, "open_groups"):
            return "whitelist"
        if self._in_id_list(event, "post_groups"):
            return "post"
        mode = str(self._cfg("default_group_mode", "none")).strip().lower()
        return mode if mode in VALID_MODES else "none"

    def _can(self, event: AstrMessageEvent, action: str) -> bool:
        """action: add | fetch | manage。"""
        if self._is_global_admin(event):
            return True
        mode = self._group_mode(event)
        if action == "manage":
            return to_bool(self._cfg("whitelist_allow_manage", False), False) and mode == "whitelist"
        return bool(MODE_CAPS.get(mode, {}).get(action, False))

    def _manage_scope(self, event: AstrMessageEvent) -> str:
        """管理权限范围：all（全库）/ local（仅本群图）/ none。"""
        if self._is_global_admin(event):
            return "all"
        if self._is_group_admin(event) or self._can(event, "manage"):
            return "local"
        return "none"

    def _can_manage_image(
        self, event: AstrMessageEvent, char: str, seq: int
    ) -> tuple[bool, str]:
        """校验能否操作某一张具体图片（群级管理员只能碰本群采集的图）。"""
        scope = self._manage_scope(event)
        if scope == "none":
            return False, self._admin_denied_hint(event)
        row = self._db_pick(char, seq, True)
        if row is None:
            return False, f"「{char}」没有 id={seq} 的图片。"
        if scope == "local" and str(row["group_id"] or "") != str(event.get_group_id() or ""):
            return False, "⛔ 群级管理员只能管理本群采集的图片（这张来自其他群）。"
        return True, ""

    def _add_denied_hint(self, event: AstrMessageEvent) -> str:
        mode = self._group_mode(event)
        if mode == "post":
            return (
                "⛔ 本群是「发图群」：只有管理员能往图库添加图片，"
                "其他成员可以用 /发图 或 /分类名 请求图片。"
            )
        if mode == "none":
            return (
                "⛔ 本群未配置权限模式（默认仅管理员可操作）。\n"
                "管理员可把本群加入插件配置的「白名单群」或「发图群」以放开权限。"
            )
        return "⛔ 你没有权限执行该操作。"

    def _fetch_denied_hint(self, event: AstrMessageEvent) -> str:
        return (
            f"⛔ 本群权限模式为「{self._group_mode(event)}」，发图仅限管理员。\n"
            "管理员可把本群加入「白名单群」或「发图群」以放开请求。"
        )

    def _admin_denied_hint(self, event: AstrMessageEvent) -> str:
        if not to_str_list(self._cfg("plugin_admins", [])) and not to_str_list(
            self._cfg("group_admins", [])
        ):
            return (
                "⛔ 本插件尚未配置任何管理员。\n"
                "请在 WebUI 插件配置里填「全局管理员」（UID，用 /sid 查看）"
                "或「群级管理员」（群号:UID）。\n"
                "该权限与 AstrBot 全局管理员互相独立，避免权限越界。"
            )
        return (
            f"⛔ 你的 ID（{event.get_sender_id()}）没有管理权限。\n"
            "全库操作（分类增删改、解除指纹拉黑）仅限全局管理员；"
            "群级管理员只能管理本群采集的图片。"
        )

    def _woken_by_prefix(self, event: AstrMessageEvent) -> bool:
        """原始消息是否以本机器人唤醒前缀开头（即「斜杠域」）。

        WakingCheckStage 会把唤醒前缀从 event.message_str 剥离，
        但 message_obj.message_str 保留原文，可用它还原触发形态。
        """
        try:
            prefixes = self.context.get_config(umo=event.unified_msg_origin).get(
                "wake_prefix", ["/"]
            )
        except Exception:
            prefixes = ["/"]
        raw = (getattr(event.message_obj, "message_str", "") or "").strip()
        return any(raw.startswith(str(p)) for p in prefixes if p)

    def _woken_by_at(self, event: AstrMessageEvent) -> bool:
        self_id = str(event.get_self_id())
        for comp in event.get_messages():
            if isinstance(comp, At) and str(getattr(comp, "qq", "")) in {self_id, "all"}:
                return True
        return False

    def _is_bot_addressed(self, event: AstrMessageEvent) -> bool:
        """这条消息是否是「发给机器人的」而非「群聊内容」。

        判据二选一：
        1. 命中了 ``@filter.command`` 注册的指令 —— ``WakingCheckStage`` 会把 CommandFilter
           解析出的参数写入 ``handlers_parsed_params``；AstrBot 内置插件也用这个标记来
           「跳过指令消息、不当作群聊上下文」。
        2. 原始文本以唤醒前缀开头（即斜杠域），例如 ``/随便说点什么`` 这类会交给 LLM 的消息。

        被动采集必须排除这两类，否则 ``/加图 分类名`` 携带的图片会被同一插件的采集器
        再收一份到默认分类（一条消息同时被指令与采集器处理）。
        """
        if event.get_extra("handlers_parsed_params", {}):
            return True
        return self._woken_by_prefix(event)

    # ----------------------------- 静默 ----------------------------- #
    def _is_collect_group(self, event: AstrMessageEvent) -> bool:
        """是否属于显式声明的「偷图群」。

        这里要求 collect_groups 非空：留空在采集语义上是「所有群」，
        若据此判定静默会退化成全局静默，故留空返回 False。
        """
        allow = to_str_list(self._cfg("collect_groups", []))
        if not allow:
            return False
        return (
            str(event.get_group_id() or "") in allow
            or event.unified_msg_origin in allow
        )

    def _collect_sender_filter(self) -> tuple[str, set[str]]:
        """采集发送者过滤配置，返回 (mode, ids)。

        ``mode`` 取 ``blacklist`` | ``whitelist``；黑名单模式下 ids 会并入旧键
        ``skip_senders``，使 v1.3 的配置继续可用。
        """
        mode = str(self._cfg("collect_sender_mode", "blacklist")).strip().lower()
        if mode not in {"blacklist", "whitelist"}:
            mode = "blacklist"
        ids = set(to_str_list(self._cfg("collect_senders", [])))
        if mode == "blacklist":
            ids |= set(to_str_list(self._cfg("skip_senders", [])))
        return mode, ids

    def _sender_allowed(self, event: AstrMessageEvent) -> bool:
        """是否允许采集这条消息的发送者。

        白名单模式下列表为空 = 谁都不采（安全默认，与「空管理员名单=全员拒绝」一致）；
        黑名单模式下列表为空 = 全部放行。

        只作用于**自动采集**：``/加图`` 等指令的权限由群模式决定，与此无关。
        """
        mode, ids = self._collect_sender_filter()
        uid = str(event.get_sender_id())
        if mode == "whitelist":
            return uid in ids
        return uid not in ids

    def _in_silent_group(self, event: AstrMessageEvent) -> bool:
        if self._in_id_list(event, "silent_groups", scope="group"):
            return True
        return to_bool(self._cfg("auto_silent_in_collect_groups", True), True) and self._is_collect_group(event)

    def _suppress_text(self, event: AstrMessageEvent) -> bool:
        return self._in_silent_group(event)

    def _suppress_image(self, event: AstrMessageEvent) -> bool:
        if not self._in_silent_group(event):
            return False
        return str(self._cfg("silent_scope", "text_only")) == "all"

    # -------------------------------- 数据层 -------------------------------- #
    def _connect(self, busy_timeout_sec: float = 30.0) -> sqlite3.Connection:
        """开一个连接。

        ``busy_timeout_sec`` 是 SQLite 的**锁等待**上限（默认 30 s）。跨插件的
        ``ingest_files`` 会传一个更小的值：调用方有自己的时间预算，让它在锁上干等 30 s
        只会造成"调用方已超时放弃、写入却稍后落地"的对账不一致（v1.7.0 对抗审核实测）。
        宁可早点抛 ``database is locked``，让调用方按失败记账。
        """
        conn = sqlite3.connect(self.db_path, timeout=max(0.1, float(busy_timeout_sec)))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        conn = None
        try:
            # _connect() 自己也可能抛（库文件损坏、路径不可写）—— 必须一起兜住。
            conn = self._connect()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS images (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    char_name   TEXT    NOT NULL,
                    seq         INTEGER NOT NULL,
                    rel_path    TEXT    NOT NULL,
                    ext         TEXT    NOT NULL,
                    sha256      TEXT    NOT NULL,
                    size_bytes  INTEGER NOT NULL DEFAULT 0,
                    group_id    TEXT    NOT NULL DEFAULT '',
                    uploader_id TEXT    NOT NULL DEFAULT '',
                    source      TEXT    NOT NULL DEFAULT 'manual',
                    blocked     INTEGER NOT NULL DEFAULT 0,
                    created_at  INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (char_name, seq),
                    UNIQUE (char_name, sha256)
                );
                CREATE INDEX IF NOT EXISTS idx_images_char ON images (char_name);
                CREATE INDEX IF NOT EXISTS idx_images_sha ON images (sha256);
                CREATE TABLE IF NOT EXISTS categories (
                    name       TEXT PRIMARY KEY,
                    note       TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS blocked_hashes (
                    sha256     TEXT PRIMARY KEY,
                    reason     TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tag_index (
                    key        TEXT PRIMARY KEY,
                    canonical  TEXT NOT NULL,
                    kind       TEXT NOT NULL DEFAULT 'tag',
                    alias_text TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL DEFAULT 0,
                    note       TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_tag_index_canonical
                    ON tag_index (canonical);
                """
            )
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(images)")}
            if "blocked" not in cols:
                try:
                    conn.execute(
                        "ALTER TABLE images ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0"
                    )
                    logger.info("[image_pool] 已迁移 images 表：新增 blocked 列")
                except Exception as e:
                    # 单列迁移失败不能中止插件构造：老库其余功能仍可用。
                    logger.error(f"[image_pool] images 表迁移 blocked 列失败: {e}")
            conn.commit()
        except Exception as e:
            # 库被锁 / 损坏 / 只读时绝不能让异常逃出 __init__：
            # AstrBot 实例化插件只对 TypeError 兜底（star_manager.py），其余异常
            # 会把整个插件记入 failed_plugin_dict 并清理状态 ⇒ 指令/WebUI/采集全挂。
            logger.error(
                f"[image_pool] 数据库初始化失败: {e}；插件继续加载，图库功能可能不可用。"
            )
            logger.error(traceback.format_exc())
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        try:
            self._db_tag_index_sync()
        except Exception as e:
            logger.error(f"[image_pool] tag_index 自检失败: {e}")
            logger.error(traceback.format_exc())

    # ---------------------------- 标签索引（别名） ---------------------------- #
    @staticmethod
    def _ensure_category(conn: sqlite3.Connection, name: str) -> bool:
        """建分类（幂等）并同步索引里的正式名行。返回 False 表示名字不可用作分类。

        所有"顺手建分类"的地方都必须走这里，否则 ``tag_index`` 会缺行，
        别名解析就查不到这个 tag（保持"索引与 categories 一一对应"这条不变量）。

        **这里是三道闸的最后一道**：``/加图 <新名>``（``tag_auto_create`` 默认开）、
        WebUI 重新分类、以及跨插件的 ``ingest_files`` 都会走到这里，只拦 ``/图库 新建`` 是不够的：

        1. 保留字（``图库`` / ``help`` …）—— 否则建出永远解析不到的僵尸分类；
        2. **已被登记为别名** —— 否则同一串字符既是别名又是正式名，``/hx`` 这类
           路由会静默改道，且该分类在 ``tag_index`` 里没有 ``kind='tag'`` 行。

        **折叠成对分类（``Hatsune`` / ``HATSUNE``）不在这里拒绝**：文件系统不区分大小写时
        它们共用同一个物理目录，但 ``_db_add`` 已经做到"**绝不覆盖已存在的文件**" ——
        它按磁盘上的实际空位分配文件名，所以两个分类的文件编号天然错开，
        不会互相覆盖（v1.7.0 独立验证的反例 F-1 就是这么关掉的）。
        一刀切拒绝写入只会把升级用户的两个分类**同时冻结**，比按空位分配更差。

        调用方必须把 False 当失败处理。
        """
        raw = str(name or "")
        # 自称"最后一道闸"就得自己挡住这些：空名、纯空白、`.` / `..`、带首尾空白的名字，
        # 以及**含零宽字符**的名字 —— `\u200b` 这类 `str.strip()` 不去，建出来的分类
        # 名字不可见、用户敲 `初音` 也解析不到 `初音\u200b`（K-N7 / M-N6）。
        # 真实调用方都会先 normalize，这里不该依赖它们（G-4 / F-I-5）。
        if (
            not raw.strip()
            or raw in {".", ".."}
            or raw != raw.strip()
            or any(ch in raw for ch in "\u200b\u200c\u200d\ufeff")
        ):
            return False
        if (name_key(name) or name) in RESERVED_KEYS:
            return False
        key = name_key(name) or name
        taken = conn.execute(
            "SELECT canonical, kind FROM tag_index WHERE key=?", (key,)
        ).fetchone()
        # (3) 折叠键被**别名**占用：正式名与别名互斥，否则同一个词会有两种解析结果
        if taken and taken["kind"] == KIND_ALIAS and taken["canonical"] != name:
            logger.warning(
                f"[image_pool] 分类名「{name}」不可用：已被"
                f"「{taken['canonical']}」登记为别名"
            )
            return False
        # 折叠键被**另一个分类**占用（文件系统区分它们，例如 Linux 上的大小写变体；
        # 或 Windows 上的成对分类）：不能复用同一个键，否则本分类没有 kind='tag' 行、
        # 别名与短指令解析都查不到它。与 _db_tag_index_sync 一样改用合成键。
        if taken and taken["kind"] == KIND_TAG and taken["canonical"] != name:
            base, suffix = key, 2
            while conn.execute(
                "SELECT 1 FROM tag_index WHERE key=?", (f"{base}#{suffix}",)
            ).fetchone():
                suffix += 1
            key = f"{base}#{suffix}"
        conn.execute(
            "INSERT OR IGNORE INTO categories (name, created_at) VALUES (?,?)",
            (name, int(time.time())),
        )
        conn.execute(
            "INSERT OR IGNORE INTO tag_index (key, canonical, kind, created_at) "
            "VALUES (?,?,?,?)",
            (key, name, KIND_TAG, int(time.time())),
        )
        return True

    def _db_tag_index_sync(self) -> dict:
        """启动自检：回填缺失的正式名行、清理悬空行、报告大小写折叠冲突。

        只做**修复与告警**，不自动合并"仅大小写不同"的分类 ——
        合并意味着改目录名，而 Windows 上大小写改名本身就可能失败。
        """
        conn = self._connect()
        fixed = removed = 0
        collisions: list[list[str]] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            cats = [r["name"] for r in conn.execute("SELECT name FROM categories")]
            keys = {r["key"] for r in conn.execute("SELECT key FROM tag_index")}
            tagged = {
                r["canonical"]
                for r in conn.execute(
                    "SELECT canonical FROM tag_index WHERE kind=?", (KIND_TAG,)
                )
            }
            for name in cats:
                if name in tagged:
                    continue
                k = name_key(name) or name
                if k in keys:
                    # 折叠键已被另一个"仅大小写不同"的分类占用（v1.6.0 前允许建出）。
                    # 先用分类名本身当键；若连它也被占用（例如 "hatsune" 与 "HATSUNE"
                    # 并存时，折叠键就是 "hatsune"），则用带后缀的合成键 ——
                    # 目的只是让"一分类一行"的不变量成立；解析走精确名，不依赖这个键。
                    k = name
                if k in keys:
                    base, suffix = k, 2
                    while f"{base}#{suffix}" in keys:
                        suffix += 1
                    k = f"{base}#{suffix}"
                    logger.warning(
                        f"[image_pool] 分类「{name}」的索引键与折叠冲突的另一个分类重名，"
                        f"已用合成键「{k}」占位（解析仍按精确名优先）。"
                    )
                cur = conn.execute(
                    "INSERT OR IGNORE INTO tag_index (key, canonical, kind, created_at) "
                    "VALUES (?,?,?,?)",
                    (k, name, KIND_TAG, int(time.time())),
                )
                keys.add(k)
                tagged.add(name)
                if cur.rowcount:
                    fixed += 1
            known = set(cats)
            for r in conn.execute("SELECT key, canonical FROM tag_index").fetchall():
                if r["canonical"] not in known:
                    conn.execute("DELETE FROM tag_index WHERE key=?", (r["key"],))
                    removed += 1
            conn.commit()
            groups: dict[str, list[str]] = {}
            for name in cats:
                groups.setdefault((name_key(name) or name), []).append(name)
            collisions = [v for v in groups.values() if len(v) > 1]
        except Exception as e:
            conn.rollback()
            logger.warning(f"[image_pool] tag_index 自检失败: {e}")
        finally:
            conn.close()
        if fixed or removed:
            logger.info(f"[image_pool] tag_index 自检：补 {fixed} 行，清 {removed} 行悬空")
        for group in collisions:
            logger.warning(
                "[image_pool] 检测到仅大小写不同、折叠后同键的分类："
                f"{'、'.join(group)}。文件系统不区分大小写，它们会共用同一个目录，"
                "建议合并为一个分类并改用别名表达大小写变体。"
            )
        return {"fixed": fixed, "removed": removed, "collisions": collisions}

    @staticmethod
    def _drop_tag_index_rows(conn: sqlite3.Connection, name: str) -> None:
        """分类消失时清索引：正式名行 + 指向它的别名行。

        别名描述的是一个已经死掉的 tag，留着只会把请求引向不存在的东西
        （迁移走的是图，不是身份）。
        """
        conn.execute(
            "DELETE FROM tag_index WHERE (kind=? AND canonical=?) OR (kind=? AND canonical=?)",
            (KIND_TAG, name, KIND_ALIAS, name),
        )
        # 注意：**不能**再按 `key = name_key(name)` 删一行 —— 折叠冲突库里
        # （"Hatsune" 与 "HATSUNE" 并存）那个键属于另一个分类的正式名行，
        # 删掉会让幸存分类的解析失效、别名登记报"目标分类不存在"。

    # ---------------------------- 解析（别名 -> 正式名） ---------------------------- #
    def _db_resolve_tag(self, raw: str) -> dict:
        """解析一个用户输入的标签名。

        顺序：归一化 → **精确匹配分类名** → 折叠键查 tag_index → 未命中（附只读建议）。
        正式名与别名共用一张表且键唯一，所以这里**结构上不可能**出现
        「正式名和别名冲突」，写入期还会再拒一次（见 ``_db_alias_add``）。
        """
        norm = normalize_name(raw)
        if not norm:
            return {"canonical": None, "via": "invalid", "suggestions": []}
        if norm.casefold() in RESERVED_KEYS:
            return {"canonical": None, "via": "reserved", "suggestions": []}
        conn = self._connect()
        try:
            # 精确匹配必须排在折叠键之前：老库里可能存在"仅大小写不同"的两个分类
            # （v1.6.0 之前允许建出），而折叠键只会命中最先建的那个。
            # 先查折叠键会把 /HATSUNE 引到 Hatsune 上 —— purge 于是删错分类的图。
            exact = conn.execute(
                "SELECT name FROM categories WHERE name=?", (norm,)
            ).fetchone()
            if exact:
                row = conn.execute(
                    "SELECT canonical, kind FROM tag_index WHERE key=?",
                    (norm.casefold(),),
                ).fetchone()
                if row and row["canonical"] == exact["name"]:
                    via = "alias" if row["kind"] == KIND_ALIAS else "canonical"
                    return {"canonical": row["canonical"], "via": via, "suggestions": []}
                # 精确命中与折叠键指向**不同**分类（老库"仅大小写不同"的两个分类）：
                # 必须尊重用户输入的字面精确名，否则 purge 会删错分类的图。
                return {"canonical": exact["name"], "via": "canonical-exact", "suggestions": []}
            row = conn.execute(
                "SELECT canonical, kind FROM tag_index WHERE key=?", (norm.casefold(),)
            ).fetchone()
            if row:
                via = "alias" if row["kind"] == KIND_ALIAS else "canonical"
                return {"canonical": row["canonical"], "via": via, "suggestions": []}
            names = [r["name"] for r in conn.execute("SELECT name FROM categories")]
            return {
                "canonical": None,
                "via": "miss",
                "suggestions": suggest_tags(norm, names),
            }
        finally:
            conn.close()

    def _db_resolve_many(self, names: list[str]) -> dict[str, str]:
        """批量解析，返回 {输入名: 正式名}；非法/保留字/未命中的输入名不出现。"""
        out: dict[str, str] = {}
        if not names:
            return out
        conn = self._connect()
        try:
            for raw in names:
                norm = normalize_name(raw)
                if not norm or norm.casefold() in RESERVED_KEYS or raw in out:
                    continue
                # 与 _db_resolve_tag 保持同一顺序：精确匹配优先，再走折叠键。
                exact = conn.execute(
                    "SELECT name FROM categories WHERE name=?", (norm,)
                ).fetchone()
                if exact:
                    out[raw] = exact["name"]
                    continue
                row = conn.execute(
                    "SELECT canonical FROM tag_index WHERE key=?", (norm.casefold(),)
                ).fetchone()
                if row:
                    out[raw] = row["canonical"]
        finally:
            conn.close()
        return out

    def _db_tag_names(self) -> list[str]:
        conn = self._connect()
        try:
            return [
                r["name"] for r in conn.execute("SELECT name FROM categories ORDER BY name")
            ]
        finally:
            conn.close()

    # ---------------------------- 别名增删查 ---------------------------- #
    def _db_alias_add(
        self, alias: str, target: str, force: bool = False
    ) -> tuple[bool, str]:
        name, err = check_alias_name(alias)
        if err:
            return False, err
        akey = name.casefold()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 目标解析与 _db_resolve_tag 同序：先按精确分类名，再回落到折叠键。
            # 否则折叠冲突库里给「HATSUNE」登记别名会挂到「Hatsune」上。
            tnorm = normalize_name(target)
            trow = None
            if tnorm:
                exact_cat = conn.execute(
                    "SELECT name FROM categories WHERE name=?", (tnorm,)
                ).fetchone()
                if exact_cat:
                    trow = conn.execute(
                        "SELECT canonical, kind FROM tag_index WHERE kind=? AND canonical=?",
                        (KIND_TAG, exact_cat["name"]),
                    ).fetchone()
            if trow is None:
                trow = conn.execute(
                    "SELECT canonical, kind FROM tag_index WHERE key=?",
                    (name_key(target) or "",),
                ).fetchone()
            if trow is None:
                conn.rollback()
                return False, f"目标分类「{target}」不存在。别名只能指向已有分类。"
            if trow["kind"] == KIND_ALIAS:
                conn.rollback()
                return False, "别名不能指向另一个别名（不支持链式），请填正式分类名。"
            canonical = trow["canonical"]
            if akey == (name_key(canonical) or ""):
                conn.rollback()
                return False, "别名与目标分类是同一个词，无需登记。"

            exist = conn.execute(
                "SELECT canonical, kind FROM tag_index WHERE key=?", (akey,)
            ).fetchone()
            prev = ""
            if exist:
                if exist["kind"] == KIND_TAG:
                    conn.rollback()
                    return False, (
                        f"「{name}」是真实分类名，不能被占用为别名"
                        "（正式名与别名互斥）。"
                    )
                if exist["canonical"] == canonical:
                    conn.rollback()
                    return False, f"别名「{name}」已经指向「{canonical}」，无需重复登记。"
                if not force:
                    conn.rollback()
                    return False, (
                        f"别名「{name}」已被「{exist['canonical']}」占用。\n"
                        "一个别名只能指向一个分类（否则同一句话会落到不同 tag）。\n"
                        f"如要改指向：/图库 别名 加 {name} {canonical} force"
                    )
                prev = exist["canonical"]
            else:
                cap = max(1, int(self._cfg("max_aliases_per_tag", 20) or 20))
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM tag_index WHERE kind=? AND canonical=?",
                    (KIND_ALIAS, canonical),
                ).fetchone()[0]
                if cnt >= cap:
                    conn.rollback()
                    return False, f"「{canonical}」的别名已达上限（{cap} 个）。"

            conn.execute(
                "INSERT OR REPLACE INTO tag_index "
                "(key, canonical, kind, alias_text, created_at, note) VALUES (?,?,?,?,?,?)",
                (
                    akey, canonical, KIND_ALIAS, name, int(time.time()),
                    f"taken over from {prev}" if prev else "",
                ),
            )
            conn.commit()
            if prev:
                return True, f"已把别名「{name}」从「{prev}」改指到「{canonical}」。"
            return True, f"已登记别名「{name}」→「{canonical}」。"
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def _db_alias_delete(self, alias: str) -> tuple[bool, str]:
        norm = normalize_name(alias)
        if not norm:
            return False, "别名非法。"
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT canonical, kind FROM tag_index WHERE key=?", (norm.casefold(),)
            ).fetchone()
            if row is None:
                conn.rollback()
                return False, f"没有登记过别名「{norm}」。"
            if row["kind"] == KIND_TAG:
                conn.rollback()
                return False, f"「{norm}」是正式分类名，不是别名（删分类请用 /图库 删除）。"
            conn.execute("DELETE FROM tag_index WHERE key=?", (norm.casefold(),))
            conn.commit()
            return True, f"已删除别名「{norm}」（原指向「{row['canonical']}」）。"
        finally:
            conn.close()

    def _db_alias_list(self, target: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            if str(target or "").strip():
                key = name_key(target) or ""
                row = conn.execute(
                    "SELECT canonical FROM tag_index WHERE key=?", (key,)
                ).fetchone()
                canonical = row["canonical"] if row else normalize_name(target)
                rows = conn.execute(
                    "SELECT alias_text, canonical, created_at FROM tag_index "
                    "WHERE kind=? AND canonical=? ORDER BY created_at",
                    (KIND_ALIAS, canonical),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT alias_text, canonical, created_at FROM tag_index "
                    "WHERE kind=? ORDER BY canonical, created_at",
                    (KIND_ALIAS,),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _db_alias_adopt(self, wrong: str, right: str) -> tuple[bool, int, str]:
        """收编：把「错名」分类里的图搬进正式名，再把「错名」登记为它的别名。

        两步、两个事务（各复用已测过的实现），**非原子**是刻意的取舍：
            1. ``_db_category_delete(wrong, "move", right)`` —— 迁图；
            2. ``_db_alias_add(wrong, right, force=True)`` —— 登记别名。
        若第 2 步失败，图已迁走但别名没建，重试一次即可（不会重复搬图）。

        **折叠成对分类是例外**（``name_key`` 相同）：别名与正式名互斥，登记别名必然失败，
        所以只做"搬家 + 删源分类"，不登记别名 —— 折叠键本来就解析到 canonical。
        这正是 ``_db_category_delete`` 拒绝折叠冲突时给用户指的那条路：
        在此之前那条建议是死的（用户既删不掉、也合不了，v1.7.0 独立验证的 F-3/D4）。
        """
        w, r = normalize_name(wrong), normalize_name(right)
        if not w or not r:
            return False, 0, "分类名非法。"
        if w == r:
            return False, 0, "错名与正式名是同一个分类。"
        info = self._db_resolve_tag(r)
        if info["via"] == "alias":
            return False, 0, "正式名不能填别名，请填真实的分类名。"
        if info["canonical"] is None:
            return False, 0, f"目标分类「{r}」不存在。先用 /加图 或 /图库 新建 建好它再收编。"
        canonical = info["canonical"]

        if name_key(w) == name_key(r):
            src = self._db_resolve_tag(w)
            if src["canonical"] is None or src["via"] == "alias":
                return False, 0, f"「{w}」不是独立的分类，无需合并。"
            moved, err = self._db_category_delete(
                src["canonical"], "move", canonical, force_merge=True
            )
            if err:
                return False, 0, err
            return (
                True,
                moved,
                f"已把「{src['canonical']}」的 {moved} 张图并入「{canonical}」并删除原分类"
                "（两者在文件系统里同名，别名与正式名互斥，因此不登记别名）。",
            )

        moved = 0
        src = self._db_resolve_tag(w)
        if src["canonical"] is not None and src["via"] != "alias":
            moved, err = self._db_category_delete(src["canonical"], "move", canonical)
            if err:
                return False, 0, err
        ok, msg = self._db_alias_add(w, canonical, force=True)
        if not ok:
            return False, moved, msg
        prefix = f"已把「{w}」的 {moved} 张图并入「{canonical}」，" if moved else ""
        return True, moved, prefix + msg

    def _db_add(
        self,
        char: str,
        items: list[dict],
        group_id: str,
        uploader: str,
        source: str,
        busy_timeout_sec: float = 30.0,
    ) -> tuple[int, int]:
        """写入图片（同步，经 to_thread 调用）。返回 (added, skipped)。

        ``busy_timeout_sec`` 透传给 ``_connect``；``ingest_files`` 会传一个更小的值，
        避免跨插件调用在锁上干等到超出调用方预算（见 ``_connect`` 的说明）。

        **失败时不留下孤儿文件**：文件是随写随落盘的，事务回滚却不会把文件收回去 ——
        批次中途失败（例如目标路径被一个同名文件/目录占住）会留下"没有索引行"的图，
        列表看不见、`/删图` 删不到、清理也不管。所以这里记下本次写入的路径与内容指纹，
        回滚时一并删掉（v1.7.0 对抗审核实测过这条路径）。

        清理有两条硬约束（独立验证的反例 F-2 / F-3）：

        * **只删本次真正新建的文件**：写入前若目标已存在就顺延到空位（见下方），
          所以本次写入的一定是新建文件，不会覆盖别人的图；
        * **删除前核对内容指纹**：采样与回滚之间可能有别的写入者占了这个路径，
          内容对不上就不删（TOCTOU 守卫）。
        """
        added = skipped = 0
        written: list[tuple[Path, str, int]] = []
        conn = self._connect(busy_timeout_sec)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not self._ensure_category(conn, char):
                conn.rollback()
                logger.warning(
                    f"[image_pool] 分类名「{char}」不可用（内置指令名 / 与已有分类仅大小写不同 / "
                    "已被登记为别名），拒绝创建"
                )
                return 0, len(items)
            for it in items:
                banned = conn.execute(
                    "SELECT 1 FROM blocked_hashes WHERE sha256=?", (it["sha256"],)
                ).fetchone()
                if banned:
                    skipped += 1
                    continue
                dup = conn.execute(
                    "SELECT id, seq, rel_path FROM images WHERE char_name=? AND sha256=?",
                    (char, it["sha256"]),
                ).fetchone()
                if dup:
                    # 已有同一张图。若它的文件还在，按幂等跳过；若不在了
                    # （被手工删除、迁移失败），就地修复这一行 ——
                    # 否则孤儿行会永久挡住这张图重新收录。
                    if self._abs(dup["rel_path"]).exists():
                        skipped += 1
                        continue
                    rel = f"{char}/{int(dup['seq']):06d}{it['ext']}"
                    dest = self.pool_dir / rel
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        # 原子独占创建：被占住（折叠冲突 / 手工放置 / 外部写入）就跳过，
                        # 不覆盖 —— 否则回滚也无法还原别人的内容。
                        if not self._create_exclusive(dest, data=it["data"]):
                            logger.warning(f"[image_pool] 修复目标已被占住，跳过: {rel}")
                            skipped += 1
                            continue
                    except Exception as e:
                        logger.warning(f"[image_pool] 修复丢失文件失败 {rel}: {e}")
                        skipped += 1
                        continue
                    written.append((dest, it["sha256"], len(it["data"])))
                    conn.execute(
                        "UPDATE images SET rel_path=?, ext=?, size_bytes=?, group_id=?, "
                        "uploader_id=?, source=? WHERE id=?",
                        (
                            rel, it["ext"], len(it["data"]), group_id or "",
                            uploader or "", source, dup["id"],
                        ),
                    )
                    logger.info(f"[image_pool] 已修复索引指向的丢失文件: {rel}")
                    added += 1
                    continue
                seq = conn.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM images WHERE char_name=?",
                    (char,),
                ).fetchone()[0]
                # **绝不覆盖已存在的文件**：seq 只在本分类内递增，而折叠成对分类
                # （或外部留下的同名文件）可能已经占了同一个物理路径。
                # 顺延到空位；连空位都找不到（极端占位）就跳过这一张，绝不覆盖。
                seq, dest = self._next_free_dest(char, seq, it["ext"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                # 原子独占创建：检查与写入之间被别的写入者抢占时，"xb" 会失败而不是覆盖。
                # 抢到了就顺延到下一个编号重试（独立验证 K-N6：先检查再写不是原子的）。
                wrote = False
                for _ in range(10000):
                    if dest.exists() and not dest.is_file():
                        break
                    try:
                        wrote = self._create_exclusive(dest, data=it["data"])
                    except OSError:
                        raise
                    if wrote:
                        break
                    seq += 1
                    dest = self.pool_dir / char / f"{seq:06d}{it['ext']}"
                if not wrote:
                    logger.warning(f"[image_pool] 找不到可用文件名，跳过: {dest}")
                    skipped += 1
                    continue
                rel = f"{char}/{seq:06d}{it['ext']}"
                written.append((dest, it["sha256"], len(it["data"])))
                conn.execute(
                    "INSERT INTO images (char_name, seq, rel_path, ext, sha256, "
                    "size_bytes, group_id, uploader_id, source, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        char, seq, rel, it["ext"], it["sha256"], len(it["data"]),
                        group_id or "", uploader or "", source, int(time.time()),
                    ),
                )
                added += 1
            conn.commit()
        except Exception:
            conn.rollback()
            # 回滚不会收回已经落盘的文件 —— 不删就是"没有索引行的孤儿图"。
            # 只删**本次真正新建**的文件（写入走的是 O_EXCL，路径是我们自己占的）：
            #   * 内容与本次写入一致 → 删；
            #   * 内容**短于**本次要写的长度 → 说明写到一半失败（ENOSPC 之类），也要删
            #     （独立验证 M-N3：半截文件留在池子里，列表看不见、清理不管）；
            #   * 长度够但内容不一致 → 采样后被别的写入者改写，保留（TOCTOU 守卫，F-3）。
            for path, sha, size in written:
                try:
                    if not path.is_file():
                        continue
                    stat = path.stat()
                    if stat.st_size >= size and hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                        continue
                    path.unlink(missing_ok=True)
                except Exception as e:
                    logger.warning(f"[image_pool] 回滚后清理孤儿文件失败 {path}: {e}")
            raise
        finally:
            conn.close()
        return added, skipped

    # ------------------------- 跨插件公开接口（v1.7.0） ------------------------- #
    async def ingest_files(
        self,
        paths,
        *,
        tag: str,
        group_id: str = "",
        uploader: str = "",
        source: str = "x",
    ) -> tuple[int, int]:
        """公开入库接口：供其他插件调用（如 astrbot_plugin_x_monitor）。返回 ``(added, skipped)``。

        与 ``/加图`` **共用同一条写入路径**（``_db_add``），因此天然继承全部既有语义：
        ``_ensure_category``（``categories`` + ``tag_index`` 同步，含保留字 / 折叠冲突 / 别名占用三道闸）、
        seq 分配、``(char_name, sha256)`` 去重、``blocked_hashes`` 全局指纹黑名单、``MAX_FILE_BYTES`` 上限。

        **不变量**：正常返回时 ``added + skipped == 取到的条目数`` —— 单个 ``str``/``Path``
        算 1 条，空字符串会被当成 1 条无效路径（不是 0 条，字面 ``len("")`` 口径不适用）。
        ``skipped`` 由这几类构成 —— 路径为 None / 读不出 / 空文件、超过 ``MAX_FILE_BYTES``、
        非图片（魔数与后缀都不认）、指纹黑名单命中、同 tag 内 sha256 重复、
        分类名被拒（保留字、与已有分类折叠后同名、已被登记为别名）。

        **基础设施失败会抛异常**（不吞）：库被锁 / 只读 / 损坏、``pool/<tag>`` 被同名文件占住、
        目标目录不可写等。这类失败**不能**伪装成 ``skipped``（那会把"写不进去"说成"重复"），
        调用方必须自己兜住并按失败记账。单条文件的问题才计入 ``skipped``。

        **调用方绝不要自己去写 ``index.sqlite3``**：漏掉 ``_ensure_category`` 会让
        ``tag_index`` 缺行 —— 该 tag 的别名/短指令解析（``_db_resolve_tag``）永远查不到它
        （``/图库 列表`` 会从 ``images`` 兜底显示名字，所以"看得见但够不到"，
        比"看不见"更难排查）。

        ``tag`` 归一化后为空时**拒绝入库**（返回 ``(0, len(paths))``），
        绝不回落到 ``DEFAULT_NAMESPACE``：那会把外部插件的图混进「未分类」，
        与用户自己采集的图再也分不开。

        魔数不认的文件**只**接受明确的图片后缀（``IMAGE_EXTS``）—— 绝不能靠后缀兜底，
        否则视频（``.mp4``）会被存成 ``.jpg``。

        ``paths`` 接受：路径列表 / 单个 ``str`` / 单个 ``Path`` / 生成器等可迭代对象
        （单值会被当成"一张图"，**不会**被逐字符迭代）。
        """
        if paths is None:
            return (0, 0)
        if isinstance(paths, (str, bytes, Path)):
            targets = [paths]
        else:
            try:
                targets = list(paths)
            except TypeError:
                logger.warning(f"[image_pool] ingest_files 的 paths 不是可迭代对象: {paths!r}")
                return (0, 0)
        if not targets:
            return (0, 0)

        clean = normalize_name(str(tag or ""))
        if not clean:
            logger.warning(
                f"[image_pool] ingest_files 收到非法分类名 {tag!r}，已拒绝入库（{len(targets)} 张）"
            )
            return (0, len(targets))

        items: list[dict] = []
        bad = 0
        for raw in targets:
            if raw is None:
                bad += 1
                continue
            try:
                path = Path(raw)
            except Exception:
                logger.warning(f"[image_pool] ingest_files 跳过非法路径: {raw!r}")
                bad += 1
                continue
            try:
                data = await asyncio.to_thread(path.read_bytes)
            except Exception as e:
                logger.warning(f"[image_pool] ingest_files 读取失败 {path}: {e}")
                bad += 1
                continue
            if not data or len(data) > MAX_FILE_BYTES:
                logger.info(
                    f"[image_pool] ingest_files 跳过（空文件或超过 {MAX_FILE_BYTES} 字节）: {path}"
                )
                bad += 1
                continue
            ext = sniff_ext(data[:16])
            if ext is None:
                suffix = path.suffix.lower()
                if suffix not in IMAGE_EXTS:
                    logger.info(f"[image_pool] ingest_files 跳过非图片文件: {path}")
                    bad += 1
                    continue
                ext = suffix
            items.append(
                {"data": data, "ext": ext, "sha256": hashlib.sha256(data).hexdigest()}
            )

        if not items:
            return (0, bad)
        added, skipped = await asyncio.to_thread(
            self._db_add,
            clean,
            items,
            group_id or "",
            uploader or "",
            source or "x",
            INGEST_BUSY_TIMEOUT_SEC,
        )
        return (added, skipped + bad)

    def _db_pick(
        self, char: str, seq: int | None, include_blocked: bool = False
    ) -> sqlite3.Row | None:
        """按 (分类, id) 精确定位一张图；``seq=None`` 视为未指定，返回 None。

        随机取样一律走 ``_db_pick_many``（``ORDER BY RANDOM()``）。这里曾经还有一条
        "COUNT + ORDER BY seq LIMIT 1 OFFSET ?" 的随机分支，但没有任何调用者会传
        ``seq=None``（调用点全部带具体 id），文档却把它写成取样实现、
        测试也只断言那条死路径 —— 已删除。
        """
        if seq is None:
            return None
        cond = "" if include_blocked else " AND blocked=0"
        conn = self._connect()
        try:
            return conn.execute(
                f"SELECT * FROM images WHERE char_name=? AND seq=?{cond}",
                (char, seq),
            ).fetchone()
        finally:
            conn.close()

    def _db_pick_many(
        self, char: str | None, count: int, include_blocked: bool = False
    ) -> list[sqlite3.Row]:
        """随机取 count 张；char 为 None 表示跨全库。ORDER BY RANDOM 天然不重复。"""
        cond = "" if include_blocked else " AND blocked=0"
        where = "WHERE 1=1" + cond + (" AND char_name=?" if char else "")
        params: list = [] if not char else [char]
        params.append(max(1, int(count)))
        conn = self._connect()
        try:
            return conn.execute(
                f"SELECT * FROM images {where} ORDER BY RANDOM() LIMIT ?", params
            ).fetchall()
        finally:
            conn.close()

    def _db_transfer(
        self, items: list[tuple[str, int]], target: str, mode: str
    ) -> tuple[int, int, str]:
        """把图片移动/复制到目标分类（WebUI 重新分类）。

        与「删除分类时迁移」的差异：目标已存在同一张图时，这里 **跳过并保留源记录**
        （幂等、可解释、不静默删用户数据）；分类迁移则删除源行以避免孤儿数据。
        返回 (done, skipped, err)。
        """
        done = skipped = 0
        io_failed = 0
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not self._ensure_category(conn, target):
                conn.rollback()
                return 0, 0, f"目标分类名「{target}」不可用（内置指令名 / 与已有分类折叠冲突 / 已被登记为别名）"
            # 引用索引**一次建好**（O(n)），不要在每个条目里做全表比对 ——
            # 后者在 BEGIN IMMEDIATE 里会把写锁按住几秒到几分钟（K-N5）。
            refs = self._reference_index(conn)
            next_seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM images WHERE char_name=?", (target,)
            ).fetchone()[0]
            tdir = self.pool_dir / target
            tdir.mkdir(parents=True, exist_ok=True)

            for char, seq in items:
                row = conn.execute(
                    "SELECT * FROM images WHERE char_name=? AND seq=?", (char, seq)
                ).fetchone()
                if row is None or char == target:
                    skipped += 1
                    continue
                dup = conn.execute(
                    "SELECT 1 FROM images WHERE char_name=? AND sha256=?",
                    (target, row["sha256"]),
                ).fetchone()
                if dup:
                    skipped += 1
                    continue

                next_seq, dest = self._next_free_dest(
                    target, next_seq + 1, Path(row["rel_path"]).suffix
                )
                if dest.exists() and not dest.is_file():
                    # `_next_free_dest` 只在**被目录占住**时返回已存在的目录：
                    # 必须按失败处理，绝不能交给 `shutil.copy2` —— 它对"目标是目录"的语义是
                    # **把文件复制进目录**（静默"成功"，再写一条指向目录的 DB 行）。
                    # 独立验证的反例 F-I-2 就是这么绕过另外两条路径的守卫的。
                    logger.warning(f"[image_pool] {mode} 目标路径被目录占住，跳过: {dest}")
                    skipped += 1
                    io_failed += 1
                    continue
                new_rel = f"{target}/{next_seq:06d}{Path(row['rel_path']).suffix}"
                src = self.pool_dir / row["rel_path"]
                try:
                    if src.exists():
                        # 文件被**别的索引行**共用时（折叠成对分类共用目录、或旧版覆盖 bug
                        # 留下的"两行→同一文件"）只能**复制**，不能 rename ——
                        # rename 会把另一行指向的文件也搬走，让它变成悬空行（F-I-4）。
                        shared = refs.get(self._ref_key(row["rel_path"]), 0) > 1
                        if mode == "copy" or shared:
                            ok = self._create_exclusive(dest, src=src)
                        else:
                            ok = self._move_exclusive(src, dest)
                        if not ok:
                            # 目标被并发写入者抢走 → 顺延到下一个编号重试一次即可；
                            # 这里直接按失败处理（调用方可重试），但绝不覆盖。
                            raise FileExistsError(f"目标已被占住: {dest}")
                except Exception as e:
                    # 文件没搬成功就跳过这一项（DB 不动）。单独计数：
                    # 并进 skipped 会让 WebUI 把它说成"已在目标分类或不存在"。
                    logger.warning(f"[image_pool] {mode} 文件失败 {row['rel_path']}: {e}")
                    skipped += 1
                    io_failed += 1
                    continue

                if mode == "move":
                    conn.execute("DELETE FROM images WHERE id=?", (row["id"],))
                    key = self._ref_key(row["rel_path"])
                    refs[key] = refs.get(key, 1) - 1
                conn.execute(
                    "INSERT INTO images (char_name, seq, rel_path, ext, sha256, "
                    "size_bytes, group_id, uploader_id, source, blocked, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        target, next_seq, new_rel, row["ext"], row["sha256"],
                        row["size_bytes"], row["group_id"], row["uploader_id"],
                        row["source"], row["blocked"], row["created_at"],
                    ),
                )
                done += 1
            conn.commit()
            if mode == "move":
                for char in {c for c, _ in items}:
                    remove_dir_if_empty(self.pool_dir / char)
            return done, skipped, (
                f"{io_failed} 张的文件操作失败，已跳过（详见日志）" if io_failed else ""
            )
        except Exception as e:
            conn.rollback()
            return 0, 0, str(e)
        finally:
            conn.close()

    def _db_images_page(
        self, char: str, offset: int, limit: int, include_blocked: bool = True
    ) -> tuple[list[sqlite3.Row], int]:
        cond = "" if include_blocked else " AND blocked=0"
        conn = self._connect()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM images WHERE char_name=?{cond}", (char,)
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT seq, rel_path, ext, size_bytes, sha256, blocked, source, group_id, created_at "
                f"FROM images WHERE char_name=?{cond} ORDER BY seq LIMIT ? OFFSET ?",
                (char, max(1, int(limit)), max(0, int(offset))),
            ).fetchall()
            return rows, total
        finally:
            conn.close()

    def _db_drop(self, char: str, seq: int) -> str | None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT rel_path FROM images WHERE char_name=? AND seq=?", (char, seq)
            ).fetchone()
            if not row:
                conn.rollback()
                return None
            conn.execute("DELETE FROM images WHERE char_name=? AND seq=?", (char, seq))
            conn.commit()
            return row["rel_path"]
        finally:
            conn.close()

    def _db_rows_by_hash(
        self, sha256: str, char: str | None = None
    ) -> list[sqlite3.Row]:
        """按图片内容（sha256）找记录；``char=None`` 表示跨所有分类找。

        这是「按图定位」的基础：id 只是分类内的序号，sha256 才是图片内容本身，
        同一张图可以合法地存在于多个 tag 下（多 tag 即多行）。
        """
        conn = self._connect()
        try:
            if char:
                cur = conn.execute(
                    "SELECT * FROM images WHERE sha256=? AND char_name=? "
                    "ORDER BY char_name, seq",
                    (sha256, char),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM images WHERE sha256=? ORDER BY char_name, seq",
                    (sha256,),
                )
            return cur.fetchall()
        finally:
            conn.close()

    def _db_block_hash(
        self, sha256: str, reason: str = "", apply_to_existing: bool = True
    ) -> int:
        n = 0
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO blocked_hashes (sha256, reason, created_at) "
                "VALUES (?,?,?)",
                (sha256, reason or "", int(time.time())),
            )
            if apply_to_existing:
                cur = conn.execute("UPDATE images SET blocked=1 WHERE sha256=?", (sha256,))
                n = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        return n

    def _db_unblock_hash(self, sha256: str) -> int:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM blocked_hashes WHERE sha256=?", (sha256,))
            cur = conn.execute("UPDATE images SET blocked=0 WHERE sha256=?", (sha256,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def _db_blocked_hashes(self, limit: int = 200) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT sha256, reason, created_at FROM blocked_hashes "
                "ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        finally:
            conn.close()

    # ---- 分类管理 ---- #
    def _db_category_create(self, name: str, note: str = "") -> tuple[bool, str]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if (name_key(name) or name) in RESERVED_KEYS:
                conn.rollback()
                return False, f"「{name}」是内置指令名（保留字），不能用作分类名。"
            if conn.execute("SELECT 1 FROM categories WHERE name=?", (name,)).fetchone():
                conn.rollback()
                return False, "分类已存在"
            taken = conn.execute(
                "SELECT canonical, kind FROM tag_index WHERE key=?",
                (name_key(name) or name,),
            ).fetchone()
            if taken:
                conn.rollback()
                if taken["kind"] == KIND_ALIAS:
                    return (
                        False,
                        f"「{name}」已被「{taken['canonical']}」登记为别名，不能再作分类名。\n"
                        "（正式名与别名互斥：否则同一个词会出现两种解析结果。）",
                    )
                return (
                    False,
                    f"「{taken['canonical']}」已存在 —— 仅大小写不同也视为同一个分类，"
                    "因为文件系统不区分大小写。",
                )
            conn.execute(
                "INSERT INTO categories (name, note, created_at) VALUES (?,?,?)",
                (name, note or "", int(time.time())),
            )
            self._ensure_category(conn, name)
            conn.commit()
            return True, ""
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def _db_category_rename(self, old: str, new: str) -> tuple[int, str]:
        if old == new:
            return 0, "新名和旧名相同"
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 源分类必须真的存在：否则下面会照样把目录改名，留下"行指向不存在的分类"
            # （独立验证 M-N4：`/图库 改名 Src Src2` 返回 (0,'') 却把目录搬走了）。
            if not conn.execute(
                "SELECT 1 FROM categories WHERE name=?", (old,)
            ).fetchone() and not conn.execute(
                "SELECT 1 FROM images WHERE char_name=? LIMIT 1", (old,)
            ).fetchone():
                conn.rollback()
                return 0, f"分类「{old}」不存在。"
            if (name_key(new) or new) in RESERVED_KEYS:
                conn.rollback()
                return 0, f"「{new}」是内置指令名（保留字），不能用作分类名。"
            occupied = conn.execute(
                "SELECT 1 FROM categories WHERE name=?", (new,)
            ).fetchone() or conn.execute(
                "SELECT 1 FROM images WHERE char_name=? LIMIT 1", (new,)
            ).fetchone()
            if occupied:
                conn.rollback()
                return 0, f"「{new}」已存在"
            taken = conn.execute(
                "SELECT canonical, kind FROM tag_index WHERE key=?",
                (name_key(new) or new,),
            ).fetchone()
            if taken:
                conn.rollback()
                if taken["kind"] == KIND_ALIAS:
                    return 0, (
                        f"「{new}」已被「{taken['canonical']}」登记为别名，不能用作分类名。"
                        "如要改指向，请用 /图库 别名 改。"
                    )
                return 0, f"「{taken['canonical']}」已存在（仅大小写不同也视为同一个分类）。"

            rows = conn.execute(
                "SELECT id, rel_path FROM images WHERE char_name=? ORDER BY seq", (old,)
            ).fetchall()

            src = self.pool_dir / old
            dst = self.pool_dir / new
            if src.exists():
                if dst.exists() and not any(dst.iterdir()):
                    try:
                        dst.rmdir()
                    except OSError:
                        pass
                try:
                    src.rename(dst)
                except OSError as e:
                    conn.rollback()
                    return 0, f"目录改名失败: {e}"

            for r in rows:
                conn.execute(
                    "UPDATE images SET char_name=?, rel_path=? WHERE id=?",
                    (new, f"{new}/{Path(r['rel_path']).name}", r["id"]),
                )
            conn.execute("DELETE FROM categories WHERE name=?", (old,))
            # 正式名行先删（键变了），再建新的；最后让别名跟随改名
            conn.execute(
                "DELETE FROM tag_index WHERE kind=? AND canonical=?",
                (KIND_TAG, old),
            )
            self._ensure_category(conn, new)
            conn.execute(
                "UPDATE tag_index SET canonical=? WHERE kind=? AND canonical=?",
                (new, KIND_ALIAS, old),
            )
            conn.commit()
            return len(rows), ""
        except Exception as e:
            conn.rollback()
            return 0, str(e)
        finally:
            conn.close()

    def _fold_collision_siblings(self, conn: sqlite3.Connection, name: str) -> list[str]:
        """同一折叠键下的其它分类名。

        这些分类在文件系统上（Windows / macOS 不区分大小写）**共用同一个目录**，
        所以任何"删/搬这个分类的文件"的动作都会连带影响对方 —— 必须先合并。
        """
        k = name_key(name) or name
        return [
            r["name"]
            for r in conn.execute("SELECT name FROM categories")
            if r["name"] != name and (name_key(r["name"]) or r["name"]) == k
        ]

    def _db_category_delete(
        self,
        name: str,
        mode: str = "move",
        target: str = DEFAULT_NAMESPACE,
        force_merge: bool = False,
    ) -> tuple[int, str]:
        """mode: purge=连图删除；move=图片迁移到 target 分类。

        ``force_merge=True`` 只给"折叠成对分类合并"用（见 ``_db_alias_adopt``）：
        跳过折叠冲突的拒绝，也跳过目标分类的可用性检查 —— 目标恰恰就是与源
        **共用同一个物理目录**的那个分类，按普通规则它也会被判"折叠冲突"。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 引用索引一次建好（O(n)）：删/搬文件前要问"还有谁在用它"
            refs = self._reference_index(conn)
            siblings = self._fold_collision_siblings(conn, name)
            if siblings and not force_merge:
                conn.rollback()
                return 0, (
                    f"「{name}」与「{'、'.join(siblings)}」仅大小写不同，"
                    "在文件系统上共用同一个目录：删除或迁移会连带毁掉对方的图片文件。\n"
                    "请先用 /图库 别名 收编 <错名> <正式名> 合并成一个分类，再操作。"
                )
            rows = conn.execute(
                "SELECT id, rel_path, sha256, ext, size_bytes FROM images "
                "WHERE char_name=? ORDER BY seq",
                (name,),
            ).fetchall()
            n = len(rows)

            if mode == "purge":
                conn.execute("DELETE FROM images WHERE char_name=?", (name,))
                conn.execute("DELETE FROM categories WHERE name=?", (name,))
                self._drop_tag_index_rows(conn, name)
                conn.commit()
                # 只删本分类索引指向的文件，不做整目录 rmtree：
                # 目录里可能有别的来源留下的文件，整删无法解释也无法恢复。
                # **而且还有别的索引行在用时不能删**（折叠成对分类共用目录 / 旧版覆盖残留）：
                # 删了会让那行变成"索引指向不存在文件"（独立验证 K-N1 实测 purge 制造悬空行）。
                for r in rows:
                    key = self._ref_key(r["rel_path"])
                    refs[key] = refs.get(key, 1) - 1
                    if refs.get(key, 0) > 0:
                        continue
                    try:
                        self._abs(r["rel_path"]).unlink(missing_ok=True)
                    except Exception as e:
                        logger.warning(
                            f"[image_pool] purge 删除文件失败 {r['rel_path']}: {e}"
                        )
                remove_dir_if_empty(self.pool_dir / name)
                return n, ""

            if target == name:
                conn.rollback()
                return 0, "目标分类不能是自身"

            if force_merge:
                if not conn.execute(
                    "SELECT 1 FROM categories WHERE name=?", (target,)
                ).fetchone():
                    conn.rollback()
                    return 0, f"目标分类「{target}」不存在"
            elif not self._ensure_category(conn, target):
                conn.rollback()
                return 0, f"目标分类名「{target}」不可用（内置指令名 / 与已有分类折叠冲突 / 已被登记为别名）"
            next_seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM images WHERE char_name=?", (target,)
            ).fetchone()[0]
            tdir = self.pool_dir / target
            tdir.mkdir(parents=True, exist_ok=True)
            skipped = 0
            removed_dup = 0
            adopted = 0
            #: 事务提交后才执行的 unlink（dup 分支）与"失败时要放回去"的文件操作（move 分支）
            pending_unlink: list[Path] = []
            file_undo: list[tuple[Path, Path, bool]] = []

            for r in rows:
                dup_row = conn.execute(
                    "SELECT id, rel_path FROM images WHERE char_name=? AND sha256=?",
                    (target, r["sha256"]),
                ).fetchone()
                src = self.pool_dir / r["rel_path"]
                suffix = Path(r["rel_path"]).suffix
                if dup_row:
                    # 整个分类要消失，留源行会变孤儿 → 这里删除源行（与 _db_transfer 的跳过策略不同）。
                    # **先确认目标那一行真的可用**：目标行指向的文件若不存在，源文件就是磁盘上
                    # 唯一一份，删掉它等于全库丢图（独立验证 K-N2 实测 `(0,'')` 却丢图）。
                    dup_path = self._abs(str(dup_row["rel_path"]))
                    key = self._ref_key(r["rel_path"])
                    # 目标那行是否**真的可用**：文件不存在、或被一个同名目录占住，
                    # 都算"不可用" —— 这时源文件是磁盘上唯一一份（M 的复现：只判 exists()
                    # 会让目录占位走 else 分支，把唯一实体 unlink 掉）。
                    if not dup_path.is_file() and src.exists():
                        # 目标行悬空 → **把源文件搬进目标分类自己的目录**再"过继"给它。
                        # 只改 rel_path 是不够的：文件还留在源分类目录里，而 `/图库 改名`
                        # 是按 char_name 重写 rel_path + 只改整个目录名 —— 过继行随后就变成
                        # 悬空行（独立验证 O-N1 实测 `/发图` 报"图片文件都丢失了"，而文件
                        # 其实还躺在旧目录里）。搬过来之后"行的文件就在自己分类目录下"
                        # 这条不变量重新成立。
                        tseq, tdest = self._next_free_dest(target, next_seq + 1, suffix)
                        if tdest.exists() and not tdest.is_file():
                            logger.warning(f"[image_pool] 过继目标被目录占住，跳过: {tdest}")
                            skipped += 1
                            continue
                        try:
                            if refs.get(key, 0) > 1:
                                ok = self._create_exclusive(tdest, src=src)
                                copied = True
                            else:
                                ok = self._move_exclusive(src, tdest)
                                copied = False
                            if not ok:
                                raise FileExistsError(f"目标已被占住: {tdest}")
                            file_undo.append((src, tdest, copied))
                        except Exception as e:
                            logger.warning(f"[image_pool] 过继文件失败，保留在源分类 {r['rel_path']}: {e}")
                            skipped += 1
                            continue
                        next_seq = tseq
                        new_rel = f"{target}/{tseq:06d}{suffix}"
                        conn.execute(
                            "UPDATE images SET rel_path=?, seq=?, ext=?, size_bytes=? WHERE id=?",
                            (new_rel, tseq, r["ext"], r["size_bytes"], dup_row["id"]),
                        )
                        refs[key] = refs.get(key, 1) - 1
                        refs[self._ref_key(new_rel)] = refs.get(self._ref_key(new_rel), 0) + 1
                        logger.info(
                            f"[image_pool] 目标分类里的同图记录指向的文件不存在，"
                            f"已把 {r['rel_path']} 搬成 {new_rel} 过继给它（不丢图）"
                        )
                        conn.execute("DELETE FROM images WHERE id=?", (r["id"],))
                        adopted += 1
                        continue
                    conn.execute("DELETE FROM images WHERE id=?", (r["id"],))
                    refs[key] = refs.get(key, 1) - 1
                    # **unlink 推迟到 commit 之后**：事务中途失败时行会回滚，文件却收不回来，
                    # 那行就变成永久悬空（独立验证 M-N2）。删文件是"行已经没了"的收尾动作，
                    # 必须在行真正落库之后做 —— 与 purge / 去重 / 清理三条路径一致。
                    if src.exists() and refs.get(key, 0) <= 0:
                        pending_unlink.append(src)
                    removed_dup += 1
                    continue
                # 与 _db_add / _db_transfer 同一条规则：**绝不覆盖已存在的文件**。
                next_seq, dest = self._next_free_dest(target, next_seq + 1, suffix)
                if dest.exists() and not dest.is_file():
                    # 被目录占住：按失败处理（P1-4 语义），绝不 rename/copy 上去
                    logger.warning(f"[image_pool] 迁移目标路径被目录占住，跳过: {dest}")
                    skipped += 1
                    continue
                new_rel = f"{target}/{next_seq:06d}{suffix}"
                if src.exists():
                    try:
                        key = self._ref_key(r["rel_path"])
                        if refs.get(key, 0) > 1:
                            # 文件被别的索引行共用 → 只能复制，rename 会让那行悬空（F-I-4）
                            ok = self._create_exclusive(dest, src=src)
                            copied = True
                        else:
                            ok = self._move_exclusive(src, dest)
                            copied = False
                        if not ok:
                            raise FileExistsError(f"目标已被占住: {dest}")
                        file_undo.append((src, dest, copied))
                    except Exception as e:
                        # 文件没搬成功就不能改 DB 行：否则回执说"迁移成功 N 张"，
                        # 实际同时制造"列表看得见、文件不在"与"文件在、列表看不见"。
                        logger.warning(
                            f"[image_pool] 迁移文件失败，保留在原分类 {r['rel_path']}: {e}"
                        )
                        skipped += 1
                        continue
                    refs[key] = refs.get(key, 1) - 1
                conn.execute(
                    "UPDATE images SET char_name=?, rel_path=?, seq=? WHERE id=?",
                    (target, new_rel, next_seq, r["id"]),
                )
                refs[self._ref_key(new_rel)] = refs.get(self._ref_key(new_rel), 0) + 1
            # 过继的那张**也算"已移入目标"**（它现在确实在目标分类下可用），
            # 否则回执会少报（独立验证 O-N5：救回 1 张却报"0 张图片移入"）。
            moved = n - skipped - removed_dup
            adopted_note = (
                f"，{adopted} 张就地救回（目标原有同图记录的文件已丢失）" if adopted else ""
            )
            if skipped:
                # 还有图留在原分类：不能删 categories 行，否则这些图在指令路径下
                # 再也取不到（列表仍会显示它们，但名字解析已经失效）。
                conn.commit()
                for path in pending_unlink:
                    try:
                        path.unlink(missing_ok=True)
                    except Exception as e:
                        logger.warning(f"[image_pool] 删除文件失败 {path}: {e}")
                dup_note = f"，{removed_dup} 张因目标已存在同图而去重" if removed_dup else ""
                return moved, (
                    f"{skipped} 张图片搬移失败，已留在原分类「{name}」（详见日志）；"
                    f"其余 {moved} 张已移入「{target}」{dup_note}{adopted_note}"
                )
            conn.execute("DELETE FROM categories WHERE name=?", (name,))
            # 迁走的是图，不是身份：别名一并清掉，避免把请求引向已不存在的分类
            self._drop_tag_index_rows(conn, name)
            conn.commit()
            for path in pending_unlink:
                try:
                    path.unlink(missing_ok=True)
                except Exception as e:
                    logger.warning(f"[image_pool] 删除文件失败 {path}: {e}")
            remove_dir_if_empty(self.pool_dir / name)
            return moved, ""
        except Exception as e:
            conn.rollback()
            # **把已经动过的文件放回去**：rename/copy 发生在 commit 之前（否则行会指向
            # 不存在的文件），但事务失败时行会回滚 —— 不撤销文件操作，原行就永久悬空
            # （独立验证 M-N2 用触发器在真实 SQL 路径上复现）。
            for src, dest, copied in reversed(file_undo):
                try:
                    if copied:
                        dest.unlink(missing_ok=True)
                    elif dest.exists() and not src.exists():
                        if not self._move_exclusive(dest, src):
                            logger.warning(f"[image_pool] 回滚文件操作失败（目标已存在）: {dest}")
                except Exception as ue:
                    logger.warning(f"[image_pool] 回滚文件操作失败 {dest} -> {src}: {ue}")
            return 0, str(e)
        finally:
            conn.close()

    def _db_category_list(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT char_name AS name, COUNT(*) AS cnt, SUM(blocked) AS blocked_cnt "
                "FROM images GROUP BY char_name"
            ).fetchall()
            stats = {r["name"]: (r["cnt"], r["blocked_cnt"] or 0) for r in rows}
            alias_rows = conn.execute(
                "SELECT canonical, COUNT(*) AS n FROM tag_index WHERE kind=? GROUP BY canonical",
                (KIND_ALIAS,),
            ).fetchall()
            aliases = {r["canonical"]: r["n"] for r in alias_rows}
            cats = conn.execute("SELECT name, note FROM categories ORDER BY created_at").fetchall()
            out = []
            for c in cats:
                cnt, blocked = stats.pop(c["name"], (0, 0))
                out.append({
                    "name": c["name"], "note": c["note"], "cnt": cnt,
                    "blocked": blocked, "aliases": aliases.get(c["name"], 0),
                })
            for name, (cnt, blocked) in stats.items():
                out.append({
                    "name": name, "note": "", "cnt": cnt,
                    "blocked": blocked, "aliases": aliases.get(name, 0),
                })
            out.sort(key=lambda x: (-x["cnt"], x["name"]))
            return out
        finally:
            conn.close()

    def _db_overview(self) -> dict:
        conn = self._connect()
        try:
            img = conn.execute(
                "SELECT COUNT(*) AS total, SUM(blocked) AS blocked, "
                "COALESCE(SUM(size_bytes),0) AS bytes FROM images"
            ).fetchone()
            names = {r[0] for r in conn.execute("SELECT name FROM categories")}
            names |= {r[0] for r in conn.execute("SELECT DISTINCT char_name FROM images")}
            bans = conn.execute("SELECT COUNT(*) FROM blocked_hashes").fetchone()[0]
            return {
                "images": img["total"] or 0,
                "blocked": img["blocked"] or 0,
                "bytes": img["bytes"] or 0,
                "categories": len(names),
                "blocked_hashes": bans,
            }
        finally:
            conn.close()

    def _abs(self, rel_path: str) -> Path:
        return self.pool_dir / rel_path

    @staticmethod
    def _same_file(a: Path, b: Path) -> bool:
        """两个路径是否指向**同一个物理文件**。

        折叠成对分类（``Hatsune`` / ``HATSUNE``）在 Windows 上共用同一个目录，
        于是"源"与"目标"可能本来就是同一个文件 —— 这时不能再 rename（会抛
        FileExistsError 或自我覆盖），直接改 DB 行即可。
        """
        try:
            return a.exists() and b.exists() and os.path.samefile(a, b)
        except OSError:
            return False

    @staticmethod
    def _ref_key(rel: str) -> str:
        """兼容旧调用点：只有名字可依据时回落到 normcase 键（见 ``_ref_key`` 实例方法）。"""
        return os.path.normcase(str(rel))

    def _ref_key(self, rel: str):
        """索引行的**文件身份键**：优先 ``(st_dev, st_ino)``，文件不存在时回落到名字键。

        Windows 上 ``st_ino`` 对**硬链接与 junction** 都收敛到同一值，而 ``os.path.normcase``
        只折叠大小写 —— 实测 `mklink /J pool\\B pool\\A`（普通用户就能建）之后，
        A/B 两行指向同一个物理文件却拿到不同的名字键，于是 purge / `/删图` 会把对方
        唯一一份图删掉（独立验证 M-N1）。名字键仍保留，用于"文件已经不在"的行。
        """
        try:
            st = os.stat(self._abs(rel))
            return ("ino", st.st_dev, st.st_ino)
        except OSError:
            return ("name", os.path.normcase(str(rel)))

    def _reference_index(self, conn: sqlite3.Connection) -> dict:
        """``身份键 -> 行数``，**一次 O(n)**，供"删/搬文件之前先看还有谁在引用"。

        刻意不用 ``os.path.samefile`` 两两比对：那是 O(n²)，而且逐条发生在
        ``BEGIN IMMEDIATE`` 事务里 —— 会按住写锁几秒到几分钟（独立验证 K-N5 实测：
        10k 行时每条 ≈2s，move 60 条持锁 117s、200 条 465s，并发入库等 32.9s 后
        ``database is locked``）。身份键用 ``(st_dev, st_ino)``（覆盖硬链接 / junction）。
        """
        counts: dict = {}
        for row in conn.execute("SELECT rel_path FROM images").fetchall():
            key = self._ref_key(str(row["rel_path"]))
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _create_exclusive(self, dest: Path, data: bytes | None = None, src: Path | None = None) -> bool:
        """以 ``O_CREAT|O_EXCL`` 语义写入或复制，**绝不覆盖**已存在的文件。返回是否成功。

        "先检查再写"不是原子的：检查通过到真正写入之间，别的写入者（另一个插件实例、
        外部进程、并发的另一条指令）可能刚好占了这个路径 —— 那时 ``write_bytes`` /
        ``copy2`` 会直接覆盖它（独立验证 K-N6 用打桩确定性复现）。``"xb"`` 让内核替我们做。
        返回 ``False`` 仅表示"目标已存在"（调用方顺延）；其它 ``OSError`` 照常抛出。
        """
        try:
            with open(dest, "xb") as out:
                if data is not None:
                    out.write(data)
                elif src is not None:
                    with open(src, "rb") as inp:
                        shutil.copyfileobj(inp, out)
                else:
                    return False
        except FileExistsError:
            return False
        except Exception:
            # **写入中途失败（磁盘满等）时，刚建出来的那个半截文件要自己收掉**：
            # 调用方是"写成功之后"才把路径记进清理名单的，异常在这里抛出时它根本不知道
            # 有这个文件，回滚清理就漏掉了它（独立验证 O-A4：ENOSPC 打桩后池子里留下
            # 无索引行的半截文件）。
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        if src is not None:
            try:
                shutil.copystat(src, dest)
            except OSError:
                pass
        return True

    @staticmethod
    def _move_exclusive(src: Path, dest: Path) -> bool:
        """把 ``src`` 改名到 ``dest``，**绝不覆盖**已存在的目标。返回是否成功。

        先用 ``"xb"`` 原子占位，再 ``os.replace`` 覆盖**自己占的**那个空文件 ——
        Windows 的 ``rename`` 遇到已存在目标会抛错、POSIX 的 ``rename`` 会直接覆盖，
        两者都不能单独用来实现"绝不覆盖"。
        """
        try:
            with open(dest, "xb"):
                pass
        except FileExistsError:
            return False
        try:
            os.replace(src, dest)
        except Exception:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return True

    def _next_free_dest(
        self, target: str, start_seq: int, suffix: str
    ) -> tuple[int, Path]:
        """在 ``pool/<target>/`` 下找下一个**可写**的文件名，返回 ``(seq, 路径)``。

        **这是"绝不覆盖"的唯一实现**：三条写入路径（``_db_add``、``_db_transfer``、
        ``_db_category_delete``）都必须走它。折叠成对分类共用同一个物理目录，
        而各自的 ``seq`` 是在自己的 ``char_name`` 里独立递增的，两边必然算出同一个编号 ——
        不看磁盘占用就直接写，后写的会盖掉前一个 tag 的图，而 DB 里两行 sha256 各记一份
        ⇒ ``/发图`` 发错图（v1.7.0 独立验证的 F-1 / G-1 / G-2 都是这个形态）。

        规则：

        * 候选路径**不存在** → 用它；
        * 候选是**文件** → 顺延到下一个编号；
        * 候选是**目录**（结构性损坏）→ 原样返回，让调用方的写入/改名去失败 ——
          保持"搬不动就不能改 DB 行、不能回执成功"的既有语义（P1-4）。
        """
        seq = int(start_seq)
        for _ in range(100000):
            dest = self.pool_dir / target / f"{seq:06d}{suffix}"
            if not dest.exists() or not dest.is_file():
                return seq, dest
            seq += 1
        # 10 万个编号全被占：返回最后一个候选，由调用方按"写不进去"处理，绝不覆盖。
        return seq, self.pool_dir / target / f"{seq:06d}{suffix}"

    # ---- 保留期与清理 ---- #
    def _db_pool_stats(self) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(size_bytes),0) AS bytes, "
                "COALESCE(SUM(CASE WHEN source='auto' THEN 1 ELSE 0 END),0) AS auto_cnt, "
                "COALESCE(SUM(CASE WHEN source='auto' THEN size_bytes ELSE 0 END),0) AS auto_bytes "
                "FROM images"
            ).fetchone()
            return {
                "total": row["total"] or 0,
                "bytes": row["bytes"] or 0,
                "auto": row["auto_cnt"] or 0,
                "auto_bytes": row["auto_bytes"] or 0,
            }
        finally:
            conn.close()

    def _db_dedupe_namespace(
        self, namespace: str, targets: list[str] | None = None
    ) -> dict:
        """清理「采集落点」分类里与其它 tag 重复的采集记录。

        语义（v1.5.0 收紧，方向与 v1.3.0 一致，但**范围被锁死**）：
            * 只有 ``namespace``（采集落点，默认「未分类」）里的行会被删除；
              其它 tag 一行都不动 —— 多人图在 A/B/C 的合法多 tag 记录天然安全。
            * 删除条件：该行 sha256 在 ``targets`` 任一 tag 下也存在。
            * 只删 ``source='auto'``（潜水采集）；人工 ``/加图`` 的一律保留，
              计入 ``kept_manual`` 便于解释"为什么它还在"。
            * ``targets=None`` = 与所有现有 tag 依次比对一次。

        返回 ``{"removed", "kept_auto", "kept_manual", "scanned", "per_tag"}``。
        """
        conn = self._connect()
        victims: list[str] = []
        per_tag: dict[str, int] = {}
        kept_auto = kept_manual = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            if targets is None:
                targets = [
                    r["name"]
                    for r in conn.execute(
                        "SELECT name FROM categories WHERE name<>?", (namespace,)
                    ).fetchall()
                ]
            targets = [t for t in targets if t and t != namespace]

            rows = conn.execute(
                "SELECT id, rel_path, sha256, source FROM images WHERE char_name=?",
                (namespace,),
            ).fetchall()
            for r in rows:
                hit = None
                for t in targets:
                    if conn.execute(
                        "SELECT 1 FROM images WHERE char_name=? AND sha256=? LIMIT 1",
                        (t, r["sha256"]),
                    ).fetchone():
                        hit = t
                        break
                if hit is None:
                    if r["source"] == "auto":
                        kept_auto += 1
                    continue
                if r["source"] != "auto":
                    kept_manual += 1
                    continue
                conn.execute("DELETE FROM images WHERE id=?", (r["id"],))
                victims.append(r["rel_path"])
                per_tag[hit] = per_tag.get(hit, 0) + 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        for rel in victims:
            try:
                # 还有别的索引行在用时不能删（折叠成对分类共用目录 / 旧版覆盖残留），
                # 否则会把那行变成"索引指向不存在文件"（独立验证 K-N3）。
                if self._file_has_other_reference(rel):
                    continue
                (self.pool_dir / rel).unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"[image_pool] 去重删除文件失败 {rel}: {e}")
        remove_dir_if_empty(self.pool_dir / namespace)
        return {
            "removed": len(victims),
            "kept_auto": kept_auto,
            "kept_manual": kept_manual,
            "scanned": len(rows),
            "per_tag": per_tag,
        }

    def _db_sweep_retention(self) -> dict:
        """按保留期与容量上限清理自动采集的图片（同步，经 to_thread 调用）。

        ``retention_days`` / ``max_pool_mb`` 任一 > 0 才生效；
        ``retention_source='auto'``（默认）只清理采集来的图，人工收录的不动；
        ``retention_exempt`` 里的分类名永久豁免。

        返回 {"expired", "trimmed", "freed_bytes", "enabled"}。
        """
        days = int(self._cfg("retention_days", 0) or 0)
        max_mb = int(self._cfg("max_pool_mb", 0) or 0)
        result = {"expired": 0, "trimmed": 0, "freed_bytes": 0, "enabled": False}
        if days <= 0 and max_mb <= 0:
            return result
        result["enabled"] = True

        source = str(self._cfg("retention_source", "auto")).strip().lower()
        if source not in ("auto", "all"):
            # fail-close：非法值（'aut' / '' / 'x'）绝不能回落成 WHERE 1=1，
            # 那会连人工收录的图一起删。宁可这次不清理。
            logger.warning(
                "[image_pool] retention_source 取值非法（%r），合法值是 auto / all；"
                "为避免误删人工收录的图片，本次不执行保留期清理。",
                source,
            )
            result["enabled"] = False
            result["invalid_source"] = source
            return result
        # 豁免分类也过别名解析（管理员在配置里写昵称也能生效）
        exempt = set()
        for item in to_str_list(self._cfg("retention_exempt", [])):
            info = self._db_resolve_tag(item)
            exempt.add(info["canonical"] or normalize_name(item) or item)

        conn = self._connect()
        victims: list[str] = []
        freed = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            where = "source='auto'" if source == "auto" else "1=1"
            rows = conn.execute(
                f"SELECT id, rel_path, size_bytes, created_at, char_name, source "
                f"FROM images WHERE {where} ORDER BY created_at ASC"
            ).fetchall()

            expired_ids: set[int] = set()
            if days > 0:
                cutoff = int(time.time()) - days * 86400
                for r in rows:
                    if r["created_at"] < cutoff and r["char_name"] not in exempt:
                        expired_ids.add(r["id"])

            trim_ids: set[int] = set()
            if max_mb > 0:
                limit = max_mb * 1024 * 1024
                candidates = [
                    r for r in rows
                    if r["id"] not in expired_ids and r["char_name"] not in exempt
                ]
                total = sum(int(r["size_bytes"] or 0) for r in candidates)
                for r in candidates:  # created_at ASC → 最旧的先淘汰
                    if total <= limit:
                        break
                    trim_ids.add(r["id"])
                    total -= int(r["size_bytes"] or 0)

            doomed = expired_ids | trim_ids
            for r in rows:
                if r["id"] in expired_ids:
                    result["expired"] += 1
                elif r["id"] in trim_ids:
                    result["trimmed"] += 1
                if r["id"] in doomed:
                    conn.execute("DELETE FROM images WHERE id=?", (r["id"],))
                    victims.append(r["rel_path"])
                    freed += int(r["size_bytes"] or 0)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        touched: set[str] = set()
        for rel in victims:
            touched.add(Path(rel).parent.name)
            try:
                # **自动清理更要小心**：还有别的索引行在用这个文件时不能删 ——
                # 保留期清理是定时自动触发的，删掉"另一个分类唯一一份图"用户根本没机会拦
                # （独立验证 K-N4 实测）。
                if self._file_has_other_reference(rel):
                    logger.info(f"[image_pool] 该文件仍被其它索引行引用，清理时保留: {rel}")
                    continue
                (self.pool_dir / rel).unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"[image_pool] 清理删除文件失败 {rel}: {e}")
        for name in touched:
            remove_dir_if_empty(self.pool_dir / name)

        result["freed_bytes"] = freed
        return result

    async def _maybe_sweep(self) -> None:
        """按 ``retention_sweep_hours`` 节流地跑一次保留期清理。

        选择惰性触发（随消息到来检查）而不是后台常驻任务：不需要管理异步任务生命周期，
        不会在插件重载时留下野任务，群聊活跃时天然就会执行。
        """
        if int(self._cfg("retention_days", 0) or 0) <= 0 and int(self._cfg("max_pool_mb", 0) or 0) <= 0:
            return
        interval = max(1, int(self._cfg("retention_sweep_hours", 6) or 6)) * 3600
        now = time.time()
        if now - getattr(self, "_sweep_at", 0.0) < interval:
            return
        self._sweep_at = now
        try:
            res = await asyncio.to_thread(self._db_sweep_retention)
            if res.get("expired") or res.get("trimmed"):
                logger.info(
                    "[image_pool] 保留期清理：超时 "
                    f"{res['expired']} 张，超额 {res['trimmed']} 张，"
                    f"释放 {res['freed_bytes'] / 1024:.1f} KB"
                )
        except Exception as e:
            logger.warning(f"[image_pool] 保留期清理失败: {e}")

    # ------------------------------ 图片读取 ------------------------------ #
    async def _image_to_item(self, comp) -> dict | None:
        """把一个 Image 组件转成待入库的字典（含 sha256）；失败返回 None。"""
        try:
            local = await comp.convert_to_file_path()
        except Exception as e:
            logger.warning(f"[image_pool] 图片获取失败: {e}")
            return None
        path = Path(local)
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except Exception as e:
            logger.warning(f"[image_pool] 读取本地图片失败: {e}")
            return None
        if not data or len(data) > MAX_FILE_BYTES:
            return None
        ext = sniff_ext(data[:16]) or (
            path.suffix.lower() if path.suffix.lower() in IMAGE_EXTS else ".jpg"
        )
        return {"data": data, "ext": ext, "sha256": hashlib.sha256(data).hexdigest()}

    async def _reply_images(self, event: AstrMessageEvent, reply) -> list:
        """取被引用消息里的 Image 组件列表（取不到则返回空）。

        先看 AstrBot 是否已把被引用消息解析进 ``Reply.chain``；为空时再尝试
        通过协议端 ``get_msg`` 回拉。
        """
        if not to_bool(self._cfg("allow_reply_collect", True), True):
            return []
        chain = list(getattr(reply, "chain", None) or [])
        if chain:
            return [c for c in chain if isinstance(c, Image)]
        return await self._fetch_reply_images(event, reply)

    async def _fetch_reply_images(self, event: AstrMessageEvent, reply) -> list:
        """链为空时用协议端接口回拉被引用消息；任何失败都静默降级为空列表。

        ``Reply`` 只有 ``id``（没有 message_id），且能否回拉取决于协议端实现与
        消息是否已过期 —— 所以这里必须全程兜底，不能让它影响 /加图 主流程。
        """
        mid = getattr(reply, "id", None)
        action = getattr(getattr(event, "bot", None), "call_action", None)
        if not mid or not callable(action):
            return []
        try:
            data = await action("get_msg", message_id=mid)
        except Exception as e:
            logger.info(f"[image_pool] 回拉引用消息失败（协议端不支持或消息已过期）: {e}")
            return []
        segments = data.get("message") if isinstance(data, dict) else None
        if not isinstance(segments, list):
            return []
        out = []
        for seg in segments:
            if not isinstance(seg, dict) or str(seg.get("type")) != "image":
                continue
            payload = seg.get("data") or {}
            if not isinstance(payload, dict):
                continue
            url = payload.get("url") or payload.get("file")
            if not url:
                continue
            url = str(url)
            out.append(Image.fromURL(url) if url.startswith("http") else Image(file=url))
        return out

    async def _quoted_hashes(self, event: AstrMessageEvent) -> list[str]:
        """取本消息引用的图片内容指纹（sha256 列表，去重保序）。

        用于 `/删图` `/移动` 的"按引用图定位"形态。取不到（无引用、协议端不
        支持回拉、消息过期）返回空列表，由调用方给用法提示。
        """
        hashes: list[str] = []
        for comp in event.get_messages():
            if not isinstance(comp, Reply):
                continue
            for quoted in await self._reply_images(event, comp):
                item = await self._image_to_item(quoted)
                if item and item["sha256"] not in hashes:
                    hashes.append(item["sha256"])
        return hashes

    async def _collect_images(
        self, event: AstrMessageEvent, limit: int, include_reply: bool = False
    ) -> list[dict]:
        """收集待入库的图片。

        ``include_reply=True`` 时额外展开引用（reply）消息里的图片。
        该开关只在 ``/加图`` 指令路径启用，被动采集**不**启用：否则任何一条群消息
        都能诱导机器人去取任意 message_id，把一个内容采集功能变成协议端取图接口。
        """
        items: list[dict] = []

        async def take(comp) -> bool:
            """尝试收录一个组件；返回是否继续（未达上限）。"""
            if len(items) >= limit:
                return False
            item = await self._image_to_item(comp)
            if item:
                items.append(item)
            return len(items) < limit

        for comp in event.get_messages():
            if isinstance(comp, Image):
                if not await take(comp):
                    break
            elif include_reply and isinstance(comp, Reply):
                for quoted in await self._reply_images(event, comp):
                    if not await take(quoted):
                        break
                if len(items) >= limit:
                    break
        return items

    async def _resolve_paths(self, rows: list[sqlite3.Row]) -> list[Path]:
        """过滤失效记录，返回仍然存在的文件路径。

        **只报告，不删记录**：文件"不存在"可能只是瞬时状态（挂载点掉线、
        权限异常、同步目录未就绪），而删掉索引行会连带丢掉
        source / group_id / created_at / sha256 等无法重建的元数据。
        孤儿行的回收交给显式操作（``/图库 清理``）或人工处理。
        """
        paths: list[Path] = []
        for row in rows:
            path = self._abs(row["rel_path"])
            if path.exists():
                paths.append(path)
            else:
                logger.warning(
                    f"[image_pool] 索引指向的文件不存在，本次跳过（不删记录）: "
                    f"{row['rel_path']}"
                )
        return paths

    # ============================== 指令：加图 ============================== #
    @filter.command("加图", alias={"addimg", "add_image"})
    async def add_image(self, event: AstrMessageEvent, char_name: GreedyStr) -> None:
        """收录图片到 1~N 个分类。用法：/加图 分类名 [分类名 ...]（支持引用消息里的图片）"""
        if not self._can(event, "add"):
            if not self._suppress_text(event):
                yield event.plain_result(self._add_denied_hint(event))
            event.stop_event()
            return

        quiet = self._suppress_text(event)
        cap = int(self._cfg("max_tags_per_add", 5) or 5)
        raw_tags, dropped = parse_tags(str(char_name or ""), cap)
        if not raw_tags:
            if not quiet:
                yield event.plain_result(
                    "用法：/加图 分类名 [分类名 ...]\n"
                    "图片需与该指令在同一条消息内（或引用一条含图消息），分类名不要含空格。"
                )
            event.stop_event()
            return

        # 别名 → 正式名；未命中的按 tag_auto_create 决定新建或拒绝（别名在此**之前**生效，
        # 所以打错字命中别名时不会再新建一个分类）
        plan = await asyncio.to_thread(self._plan_tags, raw_tags)
        tags = plan["canonical"] + plan["new"]
        if not tags:
            if not quiet:
                msg = "❌ 这些分类名都不可用：" + "、".join(plan["rejected"])
                if plan.get("reserved"):
                    msg += "\n（" + "、".join(
                        f"「{r}」" for r in plan["reserved"]
                    ) + " 是内置指令名，不能用作分类名。）"
                tips = await asyncio.to_thread(self._suggestion_text, plan["misses"])
                if tips:
                    msg += "\n" + tips
                elif not to_bool(self._cfg("tag_auto_create", True), True):
                    msg += "\n（已关闭 tag_auto_create，不会自动新建分类。）"
                yield event.plain_result(msg)
            event.stop_event()
            return

        limit = int(self._cfg("max_images_per_msg", 9) or 9)
        items = await self._collect_images(event, limit, include_reply=True)
        if not items:
            if not quiet:
                yield event.plain_result(
                    f"❌ 未检测到可用图片。\n"
                    f"方式一：把图片和 `/加图 {tags[0]}` 放在同一条消息里发送。\n"
                    f"方式二：引用一条含图片的消息，再发 `/加图 {tags[0]}`。"
                )
            event.stop_event()
            return

        results: list[tuple[str, int, int, str]] = []
        for tag in tags:
            try:
                added, skipped = await asyncio.to_thread(
                    self._db_add, tag, items, event.get_group_id(),
                    event.get_sender_id(), "manual",
                )
                results.append((tag, added, skipped, ""))
            except Exception as e:
                logger.exception("[image_pool] 写入失败")
                results.append((tag, 0, 0, str(e)))

        if not quiet:
            report = self._format_add_report(tags, items, results, dropped)
            extras = []
            if plan["new"]:
                extras.append(await asyncio.to_thread(self._new_tag_hint, plan["new"]))
            if plan["rejected"]:
                extras.append(await asyncio.to_thread(self._suggestion_text, plan["rejected"]))
            extras = [e for e in extras if e]
            yield event.plain_result(report + ("\n" + "\n".join(extras) if extras else ""))
        event.stop_event()

    @staticmethod
    def _format_add_report(
        tags: list[str],
        items: list[dict],
        results: list[tuple[str, int, int, str]],
        dropped: int = 0,
    ) -> str:
        """收录回执：单分类保持旧文案，多分类给一张紧凑账单。"""
        if len(results) == 1:
            tag, added, skipped, err = results[0]
            if err:
                return f"❌ 「{tag}」写入失败：{err}"
            if added == 0 and skipped and skipped == len(items):
                # "一张都没进"有两种完全不同的原因，以前一律报成"跳过重复"：
                # 分类名被闸门拒绝时用户会以为图已经在了，其实一张没进（独立验证 F-6）。
                return (
                    f"⚠️ 「{tag}」没有收录任何图片（{skipped} 张被跳过）。"
                    "可能是这些图重复或已被屏蔽；也可能是这个分类名不可用"
                    "（内置指令名 / 与已有分类在文件系统里同名 / 已被登记为别名）——"
                    "具体原因见机器人日志。"
                )
            msg = f"✅ 已为「{tag}」收录 {added} 张图片"
            if skipped:
                msg += f"，跳过重复或已屏蔽 {skipped} 张"
            return msg
        lines = [f"✅ 已收录 {len(items)} 张图片到 {len(results)} 个分类："]
        for tag, added, skipped, err in results:
            if err:
                lines.append(f"· {tag}：❌ {err}")
                continue
            line = f"· {tag}：{added} 张"
            if skipped:
                line += f"（跳过重复或已屏蔽 {skipped} 张）"
            lines.append(line)
        if dropped:
            lines.append(f"（分类数超出上限，已忽略 {dropped} 个）")
        return "\n".join(lines)

    # ============================== 指令：发图 ============================== #
    @filter.command("发图", alias={"随机图", "random"})
    async def send_random(self, event: AstrMessageEvent, arg: str | None = None) -> None:
        """从全部图片中随机发图。用法：/发图 [数量]，如 /发图 3、/发图 x5"""
        res, _ = await self._serve_many(event, None, None, parse_count(arg, 1), False)
        if res is not None:
            yield res
        event.stop_event()

    # ============================== 指令：来图 ============================== #
    @filter.command("来图", alias={"randomimg", "random_img"})
    async def fetch_image(
        self, event: AstrMessageEvent, char_name: str, arg: str | None = None
    ) -> None:
        """显式出图：/来图 分类名 [id]；多张用 /来图 分类名 x3"""
        seq, count, ok = parse_fetch_arg(arg)
        if not ok:
            if not self._suppress_text(event):
                yield event.plain_result(
                    "参数无法识别。用法：/来图 分类名 [id]；\n"
                    "多张请写 /来图 分类名 x3 或 /来图 分类名 3张。"
                )
            event.stop_event()
            return
        res, _ = await self._serve_many(event, char_name, seq, count, False)
        if res is not None:
            yield res
        event.stop_event()

    @filter.regex(r"^(?P<name>[^\s/]+)(?:\s+(?P<arg>\S+))?$")
    async def name_router(self, event: AstrMessageEvent) -> None:
        """动态路由：/分类名 [id|x数量]。

        默认仅在本机器人唤醒前缀域内（默认 `/`）生效；
        裸分类名需显式开启 bare_name_enable，且不适用于与 MaiBot 共存的群。
        """
        if not self._woken_by_prefix(event):
            bare_ok = to_bool(self._cfg("bare_name_enable", False), False) and self._group_allowed(
                event, "bare_name_groups"
            )
            if not (bare_ok and not self._woken_by_at(event)):
                return

        raw = (event.message_str or "").strip()
        m = NAME_ARG_RE.match(raw)
        if not m:
            return
        name = normalize_name(m.group("name"))
        if not name or name.casefold() in RESERVED_KEYS:
            return

        seq, count, ok = parse_fetch_arg(m.group("arg"))
        if not ok:
            return  # 参数不认识 → 交给 LLM 兜底
        # 传原始名，由 _serve_many 统一解析别名（保持"解析只有一个入口"）
        res, served = await self._serve_many(event, m.group("name"), seq, count, True)
        if res is not None:
            yield res
            event.stop_event()
            return
        if served:
            # served=True 且没有结果 = 图片被静默吞掉（silent_scope=all）。
            # 这里必须收口：否则 ProcessStage 会照调 LLM 补一条文本回复，
            # "全静默"承诺被打破（同配置下 /来图、/发图 是真静默）。
            event.stop_event()
            return
        if not to_bool(self._cfg("fallback_llm_on_miss", True), True):
            event.stop_event()

    async def _serve_many(
        self,
        event: AstrMessageEvent,
        char_raw: str | None,
        seq: int | None,
        count: int,
        quiet_miss: bool,
    ):
        """出图核心。返回 (MessageEventResult | None, served)。不负责 stop_event。

        char_raw 为 None 表示跨全库随机（/发图）。
        """
        if not self._can(event, "fetch"):
            if quiet_miss or self._suppress_text(event):
                return None, False
            return event.plain_result(self._fetch_denied_hint(event)), False
        if not self._group_allowed(event, "reply_groups"):
            return None, False
        if self._suppress_image(event):
            return None, True

        char_raw = normalize_name(char_raw) if char_raw else None
        char = None
        if char_raw:
            info = await asyncio.to_thread(self._db_resolve_tag, char_raw)
            char = info["canonical"] or char_raw
        if char_raw and not char:
            if quiet_miss or self._suppress_text(event):
                return None, False
            return event.plain_result("分类名非法。"), False

        # 指定 id 优先于数量
        if seq is not None:
            rows = [r for r in [await asyncio.to_thread(self._db_pick, char, seq)] if r]
            if not rows:
                if quiet_miss or self._suppress_text(event):
                    return None, False
                return event.plain_result(f"「{char}」没有 id={seq} 的图片。"), False
        else:
            want = max(1, int(count))
            hard_cap = max(1, int(self._cfg("max_batch_fetch", 10) or 10))
            if want > hard_cap:
                logger.info(f"[image_pool] 单次出图请求 {want} 张，已按上限截断为 {hard_cap}")
                want = hard_cap
            rows = await asyncio.to_thread(self._db_pick_many, char, want)
            if not rows:
                if quiet_miss or self._suppress_text(event):
                    return None, False
                if char:
                    return event.plain_result(
                        f"图库里还没有「{char}」，先用 /加图 {char} 收录一些吧。"
                    ), False
                return event.plain_result("图库还是空的，先用 /加图 分类名 收录吧。"), False

        paths = await self._resolve_paths(rows)
        if not paths:
            if quiet_miss or self._suppress_text(event):
                return None, False
            return event.plain_result("图片文件都丢失了，请重新收录。"), False

        at_comp = At(qq=event.get_sender_id()) if to_bool(self._cfg("at_sender", True), True) else None
        logger.info(
            f"[image_pool] 出图 {char or '全库'} x{len(paths)} -> {event.unified_msg_origin}"
        )

        if len(paths) == 1:
            chain = ([at_comp] if at_comp else []) + [Image.fromFileSystem(str(paths[0]))]
            return event.chain_result(chain), True

        return await self._send_batch(event, paths, at_comp), True

    async def _send_batch(self, event: AstrMessageEvent, paths: list[Path], at_comp):
        """多图发送。默认合并转发（聊天记录）；失败则降级为单条多图 / 逐条。"""
        mode = str(self._cfg("batch_send_mode", "forward")).strip().lower()

        if mode == "forward":
            try:
                nick = str(self._cfg("forward_nickname", "图库") or "图库")
                uin = str(event.get_self_id())
                nodes = [
                    Node(content=[Image.fromFileSystem(str(p))], name=nick, uin=uin)
                    for p in paths
                ]
                chain = ([at_comp] if at_comp else []) + [Nodes(nodes)]
                await event.send(MessageChain(chain=chain))
                return None
            except Exception as e:
                logger.warning(f"[image_pool] 合并转发失败，降级为单条多图: {e}")
                mode = "chain"

        if mode == "separate":
            for p in paths:
                chain = ([at_comp] if at_comp else []) + [Image.fromFileSystem(str(p))]
                await event.send(MessageChain(chain=chain))
            return None

        chain = ([at_comp] if at_comp else []) + [
            Image.fromFileSystem(str(p)) for p in paths
        ]
        return event.chain_result(chain)

    # ============================ 指令组：图库整理 ============================ #
    @filter.command_group("图库")
    def pool_group(self) -> None:
        """图库整理指令组：/图库 列表|新建|改名|删除|屏蔽|解屏|已屏蔽|去重未分类|清理"""

    @pool_group.command("列表")
    async def pool_list(self, event: AstrMessageEvent) -> None:
        """列出所有分类与图片数量"""
        cats = await asyncio.to_thread(self._db_category_list)
        if not self._suppress_text(event):
            if not cats:
                yield event.plain_result("图库还是空的，先用 /加图 分类名 收录吧。")
            else:
                lines = []
                for c in cats[: int(self._cfg("list_limit", 30) or 30)]:
                    extra = f"，已屏蔽 {c['blocked']}" if c["blocked"] else ""
                    if c.get("aliases"):
                        extra += f"，别名 {c['aliases']} 个"
                    lines.append(f"· {c['name']}：{c['cnt']} 张{extra}")
                yield event.plain_result("📚 图库分类：\n" + "\n".join(lines))
        event.stop_event()

    @pool_group.command("新建")
    async def pool_create(self, event: AstrMessageEvent, name: str) -> None:
        """新建分类：/图库 新建 分类名"""
        if not self._is_global_admin(event):
            # 分类是全局命名空间：增删改只给全局管理员。
            # whitelist_allow_manage 只放开"本群采集的图片"的删/屏蔽（见 _manage_scope），
            # 绝不能顺带把分类级操作也放开（README/schema/开发指南三处都这么承诺）。
            if not self._suppress_text(event):
                yield event.plain_result(self._admin_denied_hint(event))
            event.stop_event()
            return
        cat = normalize_name(name)
        if not cat:
            if not self._suppress_text(event):
                yield event.plain_result('分类名非法（不可含空格与 / \\ : * ? " < > |）')
            event.stop_event()
            return
        ok, err = await asyncio.to_thread(self._db_category_create, cat)
        if not self._suppress_text(event):
            yield event.plain_result(f"✅ 已创建分类「{cat}」" if ok else f"未创建：{err}")
        event.stop_event()

    @pool_group.command("改名")
    async def pool_rename(self, event: AstrMessageEvent, old: str, new: str) -> None:
        """分类改名（目录与索引一起改）：/图库 改名 旧名 新名"""
        if not self._is_global_admin(event):
            # 分类是全局命名空间：增删改只给全局管理员。
            # whitelist_allow_manage 只放开"本群采集的图片"的删/屏蔽（见 _manage_scope），
            # 绝不能顺带把分类级操作也放开（README/schema/开发指南三处都这么承诺）。
            if not self._suppress_text(event):
                yield event.plain_result(self._admin_denied_hint(event))
            event.stop_event()
            return
        # 旧名先解析别名（支持 /图库 改名 昵称 新名）；新名保持字面量，
        # 由 _db_category_rename 去拒绝"撞别名/仅大小写不同"的情况
        info = await asyncio.to_thread(self._db_resolve_tag, old)
        a, b = info["canonical"] or normalize_name(old), normalize_name(new)
        if not a or not b:
            if not self._suppress_text(event):
                yield event.plain_result("用法：/图库 改名 旧名 新名")
            event.stop_event()
            return
        moved, err = await asyncio.to_thread(self._db_category_rename, a, b)
        if not self._suppress_text(event):
            yield event.plain_result(
                f"❌ {err}" if err else f"✅ 「{a}」→「{b}」，迁移 {moved} 张"
            )
        event.stop_event()

    @pool_group.command("删除")
    async def pool_delete(self, event: AstrMessageEvent, mode: str, name: str) -> None:
        """删除分类：/图库 删除 move 分类名（图片移入默认分类）或 purge（连图删除）"""
        if not self._is_global_admin(event):
            # 分类是全局命名空间：增删改只给全局管理员。
            # whitelist_allow_manage 只放开"本群采集的图片"的删/屏蔽（见 _manage_scope），
            # 绝不能顺带把分类级操作也放开（README/schema/开发指南三处都这么承诺）。
            if not self._suppress_text(event):
                yield event.plain_result(self._admin_denied_hint(event))
            event.stop_event()
            return
        action = str(mode).strip().lower()
        info = await asyncio.to_thread(self._db_resolve_tag, name)
        cat = info["canonical"] or normalize_name(name)
        if action not in {"move", "purge"} or not cat:
            if not self._suppress_text(event):
                yield event.plain_result("用法：/图库 删除 <move|purge> 分类名")
            event.stop_event()
            return
        if action == "purge" and not to_bool(self._cfg("allow_purge", False), False):
            if not self._suppress_text(event):
                yield event.plain_result(
                    "已禁用 purge（连图删除）。如确需，请在插件配置中打开 allow_purge。"
                )
            event.stop_event()
            return
        target = await asyncio.to_thread(
            self._cfg_tag, "default_namespace", DEFAULT_NAMESPACE
        )
        n, err = await asyncio.to_thread(self._db_category_delete, cat, action, target)
        if not self._suppress_text(event):
            if err:
                yield event.plain_result(f"❌ {err}")
            elif action == "purge":
                yield event.plain_result(f"🗑️ 已删除分类「{cat}」及其 {n} 张图片")
            else:
                yield event.plain_result(f"✅ 已删除分类「{cat}」，{n} 张图片移入「{target}」")
        event.stop_event()

    @pool_group.command("屏蔽")
    async def pool_block(self, event: AstrMessageEvent, name: str, seq: int) -> None:
        """屏蔽某张图（不再随机出现 + 指纹拉黑）：/图库 屏蔽 分类 id"""
        info = await asyncio.to_thread(self._db_resolve_tag, name)
        cat = info["canonical"] or normalize_name(name)
        if not cat:
            if not self._suppress_text(event):
                yield event.plain_result("用法：/图库 屏蔽 分类名 id")
            event.stop_event()
            return
        ok, hint = self._can_manage_image(event, cat, seq)
        if not ok:
            if not self._suppress_text(event):
                yield event.plain_result(hint)
            event.stop_event()
            return
        row = await asyncio.to_thread(self._db_pick, cat, seq, True)
        affected = await asyncio.to_thread(self._db_block_hash, row["sha256"], "manual block")
        if not self._suppress_text(event):
            yield event.plain_result(
                f"🚫 已屏蔽「{cat}」id={seq}（指纹拉黑，同期命中共 {affected} 张）"
            )
        event.stop_event()

    @pool_group.command("解屏")
    async def pool_unblock(self, event: AstrMessageEvent, name: str, seq: int) -> None:
        """解除屏蔽：/图库 解屏 分类 id（指纹是全局的，仅全局管理员可解）"""
        if not self._is_global_admin(event):
            if not self._suppress_text(event):
                yield event.plain_result(
                    "⛔ 指纹黑名单是全局的，解除屏蔽仅限全局管理员（避免群级管理员放开别人拉黑的图）。"
                )
            event.stop_event()
            return
        info = await asyncio.to_thread(self._db_resolve_tag, name)
        cat = info["canonical"] or normalize_name(name)
        row = await asyncio.to_thread(self._db_pick, cat, seq, True) if cat else None
        if row is None:
            if not self._suppress_text(event):
                yield event.plain_result("用法：/图库 解屏 分类名 id")
            event.stop_event()
            return
        affected = await asyncio.to_thread(self._db_unblock_hash, row["sha256"])
        if not self._suppress_text(event):
            yield event.plain_result(f"✅ 已解除屏蔽「{cat}」id={seq}（恢复 {affected} 张）")
        event.stop_event()

    @pool_group.command("已屏蔽")
    async def pool_blocked_list(self, event: AstrMessageEvent) -> None:
        """查看指纹黑名单"""
        if not self._is_global_admin(event):
            if not self._suppress_text(event):
                yield event.plain_result(self._admin_denied_hint(event))
            event.stop_event()
            return
        rows = await asyncio.to_thread(self._db_blocked_hashes, 50)
        if not self._suppress_text(event):
            if not rows:
                yield event.plain_result("指纹黑名单为空。")
            else:
                lines = [f"· {r['sha256'][:16]}…（{r['reason'] or '无理由'}）" for r in rows]
                yield event.plain_result("🚫 指纹黑名单：\n" + "\n".join(lines))
        event.stop_event()

    @pool_group.command("去重未分类")
    async def pool_dedupe_namespace(
        self, event: AstrMessageEvent, name: str | None = None
    ) -> None:
        """清掉采集落点里已在其它 tag 出现的采集图：/图库 去重未分类 [采集落点分类名]"""
        if not self._is_global_admin(event):
            if not self._suppress_text(event):
                yield event.plain_result(self._admin_denied_hint(event))
            event.stop_event()
            return
        base = await asyncio.to_thread(
            self._cfg_tag, "collect_namespace", DEFAULT_NAMESPACE
        )
        asked = None
        if str(name or "").strip():
            info = await asyncio.to_thread(self._db_resolve_tag, name)
            asked = info["canonical"] or normalize_name(str(name))
        if asked and asked != base:
            # 只允许作用于采集落点。否则会把某个真实 tag 里"与别处重复"的图删掉——
            # 多人图本来就该同时挂在多个 tag 下，那样删是数据损失。
            if not self._suppress_text(event):
                yield event.plain_result(
                    f"⛔ 去重只作用于采集落点分类「{base}」，不能指定其它分类（{asked}）。\n"
                    "理由：一张多人图可能合法地同时属于多个 tag，"
                    "对普通分类执行「与别处重复就去掉」会直接删掉这些多 tag 记录。\n"
                    f"需要清理其它落点请改 collect_namespace，或用 /图库 删除 处理整个分类。"
                )
            event.stop_event()
            return

        res = await asyncio.to_thread(self._db_dedupe_namespace, base, None)
        if not self._suppress_text(event):
            if res["removed"]:
                detail = "、".join(
                    f"{k} {v} 张"
                    for k, v in sorted(
                        res["per_tag"].items(), key=lambda kv: -kv[1]
                    )[:8]
                )
                msg = (
                    f"🧹 「{base}」去重完成：清掉 {res['removed']} 张已在其它 tag 出现的采集图。\n"
                    f"· 命中：{detail}\n"
                    f"· 保留：无重复的采集图 {res['kept_auto']} 张"
                )
                if res["kept_manual"]:
                    msg += f"；人工收录的重复记录 {res['kept_manual']} 张（不动）"
                yield event.plain_result(msg + "。")
            else:
                msg = f"「{base}」没有需要去重的采集图（现有 {res['scanned']} 张"
                msg += f"，无重复的 {res['kept_auto']} 张"
                if res["kept_manual"]:
                    msg += f"，人工收录的重复记录 {res['kept_manual']} 张（保留）"
                yield event.plain_result(msg + "）。")
        event.stop_event()

    # ------------------------------ 指令：别名 ------------------------------ #
    @pool_group.group("别名")
    def alias_group(self) -> None:
        """别名管理：/图库 别名 列表|加|删|改|收编"""

    @alias_group.command("列表")
    async def alias_list(
        self, event: AstrMessageEvent, target: str | None = None
    ) -> None:
        """列出别名：/图库 别名 列表 [分类名]"""
        rows = await asyncio.to_thread(self._db_alias_list, target)
        if not self._suppress_text(event):
            if not rows:
                yield event.plain_result(
                    "还没有登记任何别名。\n"
                    "用 /图库 别名 加 <别名> <分类名> 登记，例如：/图库 别名 加 hx 初音未来"
                )
            else:
                lines = [f"· {r['alias_text']} → {r['canonical']}" for r in rows[:60]]
                more = f"\n（共 {len(rows)} 条，只显示前 60 条）" if len(rows) > 60 else ""
                yield event.plain_result("🏷️ 别名：\n" + "\n".join(lines) + more)
        event.stop_event()

    @alias_group.command("加")
    async def alias_add(
        self, event: AstrMessageEvent, alias: str, target: str, force: str | None = None
    ) -> None:
        """登记别名（可选 force 接管）：/图库 别名 加 <别名> <分类名> [force]"""
        if not self._is_global_admin(event):
            if not self._suppress_text(event):
                yield event.plain_result(self._admin_denied_hint(event))
            event.stop_event()
            return
        hard = str(force or "").strip().lower() in {"force", "-f", "1", "true", "yes"}
        ok, msg = await asyncio.to_thread(self._db_alias_add, alias, target, hard)
        if not self._suppress_text(event):
            yield event.plain_result(("✅ " if ok else "⛔ ") + msg)
        event.stop_event()

    @alias_group.command("删")
    async def alias_del(self, event: AstrMessageEvent, alias: str) -> None:
        """删除别名：/图库 别名 删 <别名>"""
        if not self._is_global_admin(event):
            if not self._suppress_text(event):
                yield event.plain_result(self._admin_denied_hint(event))
            event.stop_event()
            return
        ok, msg = await asyncio.to_thread(self._db_alias_delete, alias)
        if not self._suppress_text(event):
            yield event.plain_result(("✅ " if ok else "⛔ ") + msg)
        event.stop_event()

    @alias_group.command("改")
    async def alias_set(
        self, event: AstrMessageEvent, alias: str, target: str
    ) -> None:
        """改别名指向：/图库 别名 改 <别名> <新分类名>"""
        if not self._is_global_admin(event):
            if not self._suppress_text(event):
                yield event.plain_result(self._admin_denied_hint(event))
            event.stop_event()
            return
        ok, msg = await asyncio.to_thread(self._db_alias_add, alias, target, True)
        if not self._suppress_text(event):
            yield event.plain_result(("✅ " if ok else "⛔ ") + msg)
        event.stop_event()

    @alias_group.command("收编")
    async def alias_adopt(
        self, event: AstrMessageEvent, wrong: str, right: str
    ) -> None:
        """把错分类收编成别名并搬家：/图库 别名 收编 <错名> <正式名>"""
        if not self._is_global_admin(event):
            if not self._suppress_text(event):
                yield event.plain_result(self._admin_denied_hint(event))
            event.stop_event()
            return
        ok, _moved, msg = await asyncio.to_thread(self._db_alias_adopt, wrong, right)
        if not self._suppress_text(event):
            yield event.plain_result(("✅ " if ok else "⛔ ") + msg)
        event.stop_event()

    @pool_group.command("清理")
    async def pool_sweep(self, event: AstrMessageEvent) -> None:
        """立即执行一次保留期/容量清理：/图库 清理"""
        if not self._is_global_admin(event):
            if not self._suppress_text(event):
                yield event.plain_result(self._admin_denied_hint(event))
            event.stop_event()
            return
        stats = await asyncio.to_thread(self._db_pool_stats)
        res = await asyncio.to_thread(self._db_sweep_retention)
        self._sweep_at = time.time()
        if not self._suppress_text(event):
            if not res["enabled"]:
                if res.get("invalid_source") is not None:
                    # 配置非法时是 fail-close（宁可不清，也不误删人工图），
                    # 必须说清原因，否则用户会以为是"没有东西可清"。
                    yield event.plain_result(
                        f"⚠️ 配置项 retention_source 取值非法（{res['invalid_source']}），"
                        "合法值只有 auto / all。为避免误删人工收录的图片，本次不执行清理。\n"
                        f"当前图库 {stats['total']} 张，其中采集图 {stats['auto']} 张，"
                        f"占用 {stats['bytes'] / 1024 / 1024:.1f} MB。"
                    )
                else:
                    yield event.plain_result(
                        "未启用保留策略（retention_days 与 max_pool_mb 都是 0），无需清理。\n"
                        f"当前图库 {stats['total']} 张，其中采集图 {stats['auto']} 张，"
                        f"占用 {stats['bytes'] / 1024 / 1024:.1f} MB。"
                    )
            else:
                yield event.plain_result(
                    f"🧹 清理完成：超时删除 {res['expired']} 张，容量淘汰 {res['trimmed']} 张，"
                    f"释放 {res['freed_bytes'] / 1024 / 1024:.2f} MB。\n"
                    f"剩余 {stats['total']} 张（采集图 {stats['auto']} 张）。"
                )
        event.stop_event()

    # ============================== 指令：删图 ============================== #
    @filter.command("删图", alias={"delimg"})
    async def delete_image(
        self, event: AstrMessageEvent, arg1: str | None = None, arg2: str | None = None
    ) -> None:
        """删图。用法：/删图 分类名 id ｜ /删图 [分类名] + 引用一条含图片的消息"""
        quiet = self._suppress_text(event)
        char, seq, err = parse_delete_args(arg1, arg2)
        if err:
            if not quiet:
                yield event.plain_result(err)
            event.stop_event()
            return
        if char:  # 允许用别名指代分类
            info = await asyncio.to_thread(self._db_resolve_tag, char)
            char = info["canonical"] or char

        if seq is not None:
            ok, hint = self._can_manage_image(event, char, seq)
            if not ok:
                if not quiet:
                    yield event.plain_result(hint)
                event.stop_event()
                return
            rel = await asyncio.to_thread(self._db_drop, char, seq)
            if not quiet:
                yield event.plain_result(
                    f"🗑️ 已删除「{char}」id={seq}。" if rel is not None else "该图片已被移除。"
                )
            if rel is not None:
                await self._unlink_rel(rel)
            event.stop_event()
            return

        # 未指定 id：按引用图片的内容（sha256）定位要删的记录
        if self._manage_scope(event) == "none":
            if not quiet:
                yield event.plain_result(self._admin_denied_hint(event))
            event.stop_event()
            return
        hashes = await self._quoted_hashes(event)
        if not hashes:
            if not quiet:
                yield event.plain_result(self._delete_usage())
            event.stop_event()
            return

        removed = denied = missing = 0
        cats: set[str] = set()
        for h in hashes:
            rows = await asyncio.to_thread(self._db_rows_by_hash, h, char)
            if not rows:
                missing += 1
                continue
            for row in rows:
                c, s = str(row["char_name"]), int(row["seq"])
                ok, _hint = await asyncio.to_thread(self._can_manage_image, event, c, s)
                if not ok:
                    denied += 1
                    continue
                rel = await asyncio.to_thread(self._db_drop, c, s)
                if rel is None:
                    continue
                await self._unlink_rel(rel)
                removed += 1
                cats.add(c)
        if not quiet:
            yield event.plain_result(
                self._format_drop_report(char, removed, denied, missing, cats)
            )
        event.stop_event()

    @staticmethod
    def _delete_usage() -> str:
        return (
            "用法：\n"
            "· /删图 分类名 id —— 删除某个分类下的指定图片\n"
            "· /删图 [分类名] + 引用一条含图片的消息 —— 按图片内容删除\n"
            "  不填分类名 = 在所有分类里找同一张图（一张图可因多 tag 存在多行）"
        )

    @staticmethod
    def _format_drop_report(
        char: str | None,
        removed: int,
        denied: int,
        missing: int,
        cats: set[str],
    ) -> str:
        scope = f"「{char}」" if char else "全部分类"
        if removed:
            head = f"🗑️ 已删除 {removed} 张（在{scope}，涉及 {len(cats)} 个分类"
            head += "：" + "、".join(sorted(cats)[:8]) if len(cats) <= 8 else ""
            head += "）。"
        else:
            head = f"❌ 没有删除任何图片（在{scope}未命中可删记录）。"
        notes = []
        if missing:
            notes.append(f"· 有 {missing} 张引用图不在图库中")
        if denied:
            notes.append("· 有图片因权限不足未删（群级管理员只能管本群采集的图）")
        return "\n".join([head] + notes)

    async def _unlink_rel(self, rel: str) -> None:
        """删除一张图对应的文件 —— **但还有别的索引行指向同一个物理文件时不删**。

        折叠成对分类共用目录（或旧版覆盖 bug 留下的"两行 → 同一文件"）时，
        删掉一行不代表这个文件没人在用：删了会让另一行变成"索引指向不存在文件"，
        直到 `/发图` 那一刻才发现（独立验证 F-I-4）。
        """
        try:
            if await asyncio.to_thread(self._file_has_other_reference, rel):
                logger.info(f"[image_pool] 该文件仍被其它索引行引用，只删记录不删文件: {rel}")
                return
            await asyncio.to_thread(self._abs(rel).unlink, True)
        except Exception as e:
            logger.warning(f"[image_pool] 删除文件失败 {rel}: {e}")

    def _file_has_other_reference(self, rel: str) -> bool:
        """是否还有**其它**索引行指向同一个文件（调用时本行已从表里删除）。

        走 ``_reference_index``（一次 O(n)），不再两两 ``samefile`` ——
        后者在 10k 行库上单次 `/删图` 要 2.25s，100k 行外推 ≈20s（独立验证 K-N5）。
        """
        src = self._abs(rel)
        if not src.exists():
            return False
        conn = self._connect()
        try:
            return self._reference_index(conn).get(self._ref_key(rel), 0) > 0
        finally:
            conn.close()

    # ============================== 指令：移动 ============================== #
    @filter.command("移动", alias={"movetag", "moveto"})
    async def move_image(
        self,
        event: AstrMessageEvent,
        arg1: str | None = None,
        arg2: str | None = None,
        arg3: str | None = None,
    ) -> None:
        """换 tag：/移动 目标分类 源分类 id ｜ /移动 目标分类 [源分类] + 引用一条含图消息"""
        quiet = self._suppress_text(event)
        target, src, seq, err = parse_move_args(arg1, arg2, arg3)
        if err:
            if not quiet:
                yield event.plain_result(err)
            event.stop_event()
            return
        # 目标与源都允许用别名；目标未命中时保持字面量（沿用"移动会自动建目标分类"的既有行为）
        info = await asyncio.to_thread(self._db_resolve_tag, target)
        target = info["canonical"] or target
        if src:
            info = await asyncio.to_thread(self._db_resolve_tag, src)
            src = info["canonical"] or src

        if seq is not None:
            ok, hint = await asyncio.to_thread(self._can_manage_image, event, src, seq)
            if not ok:
                if not quiet:
                    yield event.plain_result(hint)
                event.stop_event()
                return
            done, skipped, merr = await asyncio.to_thread(
                self._db_transfer, [(src, seq)], target, "move"
            )
            if not quiet:
                if merr:
                    yield event.plain_result(f"❌ 移动失败：{merr}")
                elif done:
                    yield event.plain_result(f"✅ 已把「{src}」id={seq} 换 tag 到「{target}」。")
                else:
                    yield event.plain_result(
                        f"「{target}」已有同一张图，未重复添加，源图仍留在「{src}」。"
                    )
            event.stop_event()
            return

        # 未指定 id：按引用图片的内容定位
        if self._manage_scope(event) == "none":
            if not quiet:
                yield event.plain_result(self._admin_denied_hint(event))
            event.stop_event()
            return
        hashes = await self._quoted_hashes(event)
        if not hashes:
            if not quiet:
                yield event.plain_result(self._move_usage())
            event.stop_event()
            return

        pairs: list[tuple[str, int]] = []
        denied = missing = 0
        for h in hashes:
            rows = await asyncio.to_thread(self._db_rows_by_hash, h, src)
            if not rows:
                missing += 1
                continue
            for row in rows:
                c, s = str(row["char_name"]), int(row["seq"])
                if c == target:
                    continue  # 已经在目标分类
                ok, _hint = await asyncio.to_thread(self._can_manage_image, event, c, s)
                if not ok:
                    denied += 1
                    continue
                pairs.append((c, s))
        if not pairs:
            if not quiet:
                notes = []
                if missing:
                    notes.append(f"· 有 {missing} 张引用图不在图库中（或已在「{target}」）")
                if denied:
                    notes.append("· 有图片因权限不足未移动")
                yield event.plain_result(
                    f"❌ 没有可移动的图片。\n" + "\n".join(notes)
                    if notes
                    else f"❌ 没有可移动的图片（引用的图可能已经在「{target}」里）。"
                )
            event.stop_event()
            return

        done, skipped, merr = await asyncio.to_thread(
            self._db_transfer, pairs, target, "move"
        )
        if not quiet:
            if merr:
                yield event.plain_result(f"❌ 移动失败：{merr}")
            else:
                msg = f"✅ 已把 {done} 张图换 tag 到「{target}」"
                if skipped:
                    msg += f"，{skipped} 张因目标已有同一张图跳过（源图保留）"
                msg += "。"
                notes = []
                if missing:
                    notes.append(f"· 有 {missing} 张引用图不在图库中")
                if denied:
                    notes.append("· 有图片因权限不足未移动")
                yield event.plain_result("\n".join([msg] + notes))
        event.stop_event()

    @staticmethod
    def _move_usage() -> str:
        return (
            "用法：\n"
            "· /移动 目标分类 源分类 id —— 把「源分类」下 id 这张图换 tag 到「目标分类」\n"
            "· /移动 目标分类 [源分类] + 引用一条含图片的消息 —— 把引用的图换 tag\n"
            "  不填源分类 = 在所有分类里找。换 tag = 源分类不再保留该图（加 tag 请用复制）"
        )

    # ============================ 被动采集（偷图） ============================ #
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def passive_collect(self, event: AstrMessageEvent) -> None:
        """潜水采集：只入库、不回复、不产出任何结果。

        该监听不会把 is_at_or_wake_command 置为 True，
        因此群内普通消息不会触发 LLM 回复。

        同时承担保留期清理的惰性触发点（与 auto_collect 开关无关）。
        """
        await self._maybe_sweep()

        if not to_bool(self._cfg("auto_collect", False), False):
            return
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return
        if not self._group_allowed(event, "collect_groups"):
            return
        if not self._sender_allowed(event):
            return
        if self._woken_by_at(event) and not to_bool(self._cfg("collect_at_bot_reply", False), False):
            return  # 被 @ 时不抢答，交由 LLM / 指令处理
        if to_bool(self._cfg("collect_skip_commands", True), True) and self._is_bot_addressed(event):
            # 指令 / 斜杠域消息是"发给机器人的"，不是群聊内容：
            # 排除它可以避免 /加图 的图片被采集器再收一份到默认分类。
            return

        namespace = await asyncio.to_thread(
            self._cfg_tag, "collect_namespace", DEFAULT_NAMESPACE
        )
        imgs = [c for c in event.get_messages() if isinstance(c, Image)]
        if imgs:
            limit = int(self._cfg("max_images_per_msg", 9) or 9)
            items = await self._collect_images(event, limit)
            if items:
                try:
                    added, _ = await asyncio.to_thread(
                        self._db_add, namespace, items, event.get_group_id(),
                        event.get_sender_id(), "auto",
                    )
                    if added:
                        logger.info(
                            f"[image_pool] 采集 {added} 张 <- 群 {event.get_group_id()}"
                        )
                except Exception as e:
                    logger.warning(f"[image_pool] 采集写入失败: {e}")

        if to_bool(self._cfg("collect_faces", False), False):
            faces = [str(c.id) for c in event.get_messages() if isinstance(c, Face)]
            if faces:
                await asyncio.to_thread(self._log_faces, event.get_group_id(), faces)

    def _log_faces(self, group_id: str, face_ids: list[str]) -> None:
        """QQ 表情只有 id、消息里没有图片本体；此处仅登记，便于后续按 id 取资源。"""
        try:
            with self.face_log.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {"ts": int(time.time()), "group_id": group_id or "", "faces": face_ids},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception as e:
            logger.warning(f"[image_pool] 表情登记失败: {e}")

    # ============================ WebUI 管理页 API ============================ #
    def _register_web_api(self) -> None:
        if not WEB_API_AVAILABLE:
            logger.warning(
                "[image_pool] 当前 AstrBot 不支持插件 Pages（需 >= v4.24.1），"
                "已跳过 WebUI 管理页 API；指令功能仍可用。"
            )
            return
        routes = [
            ("overview", self.api_overview, ["GET"], "图库概览"),
            ("categories", self.api_categories, ["GET"], "分类列表"),
            ("categories/create", self.api_category_create, ["POST"], "新建分类"),
            ("categories/rename", self.api_category_rename, ["POST"], "分类改名"),
            ("categories/delete", self.api_category_delete, ["POST"], "删除分类"),
            ("aliases", self.api_aliases, ["GET"], "别名列表"),
            ("aliases/add", self.api_alias_add, ["POST"], "新增/接管别名"),
            ("aliases/delete", self.api_alias_delete, ["POST"], "删除别名"),
            ("thumbs", self.api_thumbs, ["GET"], "缩略图分页"),
            ("images/delete", self.api_image_delete, ["POST"], "删除图片"),
            ("images/block", self.api_image_block, ["POST"], "屏蔽/解屏图片"),
            ("images/transfer", self.api_image_transfer, ["POST"], "移动/复制到其他分类"),
            ("hashes/unblock", self.api_hash_unblock, ["POST"], "解除指纹拉黑"),
            ("raw", self.api_raw, ["GET"], "下载原图"),
        ]
        for route, handler, methods, desc in routes:
            try:
                self.context.register_web_api(
                    f"/{PLUGIN_NAME}/{route}", handler, methods, desc
                )
            except Exception as e:  # pragma: no cover
                logger.warning(f"[image_pool] 注册 Web API {route} 失败: {e}")

    THUMB_CACHE_MAX_ENTRIES = 240
    THUMB_CACHE_MAX_BYTES = 24 * 1024 * 1024

    def _thumb_cache(self) -> dict:
        """缩略图缓存的惰性状态（离线自检用 __new__ 造实例，不跑 __init__）。

        ``api_thumbs`` 每行都走 ``asyncio.to_thread``，多个并发请求会真的并发
        读写同一份缓存 —— 所以取值、写入与淘汰都必须在同一把锁下完成，
        否则 ``pop(next(iter(cache)))`` 会抛 KeyError（→ 500），字节计数也会丢更新。
        """
        state = getattr(self, "_thumb_state_data", None)
        if state is None:
            with _THUMB_INIT_LOCK:
                state = getattr(self, "_thumb_state_data", None)
                if state is None:
                    state = {"cache": {}, "bytes": 0, "lock": threading.Lock()}
                    self._thumb_state_data = state
        return state

    def _thumb_data_uri(self, path: Path) -> str | None:
        """生成缩略图 data URI；Pillow 不可用时退化为原图 base64（限 1.5MB）。

        结果按 (路径, mtime_ns, 大小, thumb_size) 缓存并设条数/字节双上限：
        WebUI 每次翻页、勾选、改配置都会整页重取，不缓存时 60 张 1024px
        缩略图要串行重算 2 秒以上（实测 2453 ms / 12.48 MB）。
        """
        if not path.exists():
            return None
        size = int(self._cfg("thumb_size", 320) or 320)
        try:
            st = path.stat()
            key = (str(path), int(st.st_mtime_ns), int(st.st_size), size)
        except Exception:
            key = None
        state = self._thumb_cache()
        if key is not None:
            with state["lock"]:
                hit = state["cache"].get(key)
            if hit is not None:
                return hit or None
        uri = self._thumb_render(path, size)
        if key is not None:
            with state["lock"]:
                # 并发首访同一个 key 时，后到者不能重复累加字节数，
                # 否则记账虚高 → 提前淘汰（缓存白建）。
                if key not in state["cache"]:
                    state["cache"][key] = uri or ""
                    state["bytes"] += len(uri) if uri else 0
                while state["cache"] and (
                    len(state["cache"]) > self.THUMB_CACHE_MAX_ENTRIES
                    or state["bytes"] > self.THUMB_CACHE_MAX_BYTES
                ):
                    oldest = next(iter(state["cache"]))
                    state["bytes"] -= len(state["cache"].pop(oldest))
        return uri

    def _thumb_render(self, path: Path, size: int) -> str | None:
        try:
            from PIL import Image as PILImage

            with PILImage.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((size, size))
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=70)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception:
            pass
        try:
            data = path.read_bytes()
            if len(data) > 1_500_000:
                return None
            mime = MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")
            return f"data:{mime};base64," + base64.b64encode(data).decode()
        except Exception:
            return None

    def _parse_transfer_items(self, raw) -> list[tuple[str, int]]:
        items: list[tuple[str, int]] = []
        if not isinstance(raw, list):
            return items
        for it in raw[:500]:
            if not isinstance(it, dict):
                continue
            cat = normalize_name(str(it.get("category", "")))
            seq = it.get("seq")
            if cat and isinstance(seq, int):
                items.append((cat, seq))
        return items

    async def api_overview(self):
        return json_response(await asyncio.to_thread(self._db_overview))

    async def api_categories(self):
        return json_response({"items": await asyncio.to_thread(self._db_category_list)})

    async def api_category_create(self):
        payload = as_dict(await request.json(default={}))
        name = normalize_name(str(payload.get("name", "")))
        if not name:
            return error_response("分类名非法", status_code=400)
        ok, err = await asyncio.to_thread(
            self._db_category_create, name, str(payload.get("note", ""))
        )
        if not ok:
            return error_response(err, status_code=409)
        return json_response({"created": name})

    async def api_category_rename(self):
        payload = as_dict(await request.json(default={}))
        old = normalize_name(str(payload.get("old", "")))
        new = normalize_name(str(payload.get("new", "")))
        if not old or not new:
            return error_response("参数非法", status_code=400)
        info = await asyncio.to_thread(self._db_resolve_tag, old)
        old = info["canonical"] or old
        moved, err = await asyncio.to_thread(self._db_category_rename, old, new)
        if err:
            return error_response(err, status_code=409)
        return json_response({"moved": moved, "name": new})

    async def api_category_delete(self):
        payload = as_dict(await request.json(default={}))
        name = normalize_name(str(payload.get("name", "")))
        mode = str(payload.get("mode", "move")).strip().lower()
        target = normalize_name(
            str(payload.get("target") or self._cfg("default_namespace", DEFAULT_NAMESPACE))
        ) or DEFAULT_NAMESPACE
        if not name or mode not in {"move", "purge"}:
            return error_response("参数非法", status_code=400)
        if mode == "purge" and not to_bool(self._cfg("allow_purge", False), False):
            return error_response("已禁用 purge，请在插件配置中打开 allow_purge", status_code=403)
        # WebUI 与指令行为保持一致：都解析别名（按图定位用 sha256，不走别名）
        info = await asyncio.to_thread(self._db_resolve_tag, name)
        name = info["canonical"] or name
        info = await asyncio.to_thread(self._db_resolve_tag, target)
        target = info["canonical"] or target
        n, err = await asyncio.to_thread(self._db_category_delete, name, mode, target)
        if err:
            return error_response(err, status_code=409)
        return json_response({"removed": n, "mode": mode, "target": target})

    async def api_aliases(self):
        target = str(request.query.get("target", "") or "")
        return json_response(
            {"items": await asyncio.to_thread(self._db_alias_list, target or None)}
        )

    async def api_alias_add(self):
        payload = as_dict(await request.json(default={}))
        alias = str(payload.get("alias", ""))
        target = str(payload.get("target", ""))
        force = to_bool(payload.get("force", False), False)
        if not alias.strip() or not target.strip():
            return error_response("参数非法（alias/target）", status_code=400)
        ok, msg = await asyncio.to_thread(self._db_alias_add, alias, target, force)
        if not ok:
            return error_response(msg, status_code=409)
        return json_response({"ok": True, "message": msg})

    async def api_alias_delete(self):
        payload = as_dict(await request.json(default={}))
        alias = str(payload.get("alias", ""))
        if not alias.strip():
            return error_response("参数非法（alias）", status_code=400)
        ok, msg = await asyncio.to_thread(self._db_alias_delete, alias)
        if not ok:
            return error_response(msg, status_code=409)
        return json_response({"ok": True, "message": msg})

    async def api_thumbs(self):
        name = normalize_name(str(request.query.get("category", "") or ""))
        if not name:
            return error_response("缺少 category", status_code=400)
        info = await asyncio.to_thread(self._db_resolve_tag, name)
        name = info["canonical"] or name
        offset = request.query.get("offset", 0, type=int) or 0
        limit = min(max(1, request.query.get("limit", 24, type=int) or 24), 60)
        include_blocked = bool(request.query.get("include_blocked", 1, type=int))
        rows, total = await asyncio.to_thread(
            self._db_images_page, name, offset, limit, include_blocked
        )
        items = []
        for r in rows:
            items.append(
                {
                    "seq": r["seq"],
                    "blocked": bool(r["blocked"]),
                    "sha256": r["sha256"],
                    "size": r["size_bytes"],
                    "source": r["source"],
                    "group_id": r["group_id"],
                    "thumb": await asyncio.to_thread(
                        self._thumb_data_uri, self._abs(r["rel_path"])
                    ),
                }
            )
        # 回传服务端实际生效的 limit：前端必须用它做翻页步进与分页文案，
        # 否则请求 96 而服务端只给 60 时会静默跳过 36 张（曾经就是这样）。
        return json_response(
            {"total": total, "offset": offset, "limit": limit, "items": items}
        )

    async def api_image_delete(self):
        payload = as_dict(await request.json(default={}))
        name = normalize_name(str(payload.get("category", "")))
        seq = payload.get("seq")
        if not name or not isinstance(seq, int):
            return error_response("参数非法", status_code=400)
        info = await asyncio.to_thread(self._db_resolve_tag, name)
        name = info["canonical"] or name
        rel = await asyncio.to_thread(self._db_drop, name, seq)
        if rel is None:
            return error_response("图片不存在", status_code=404)
        try:
            await asyncio.to_thread(self._abs(rel).unlink, True)
        except Exception:
            pass
        return json_response({"deleted": {"category": name, "seq": seq}})

    async def api_image_block(self):
        payload = as_dict(await request.json(default={}))
        name = normalize_name(str(payload.get("category", "")))
        seq = payload.get("seq")
        blocked = to_bool(payload.get("blocked", True), True)
        if not name or not isinstance(seq, int):
            return error_response("参数非法", status_code=400)
        info = await asyncio.to_thread(self._db_resolve_tag, name)
        name = info["canonical"] or name
        row = await asyncio.to_thread(self._db_pick, name, seq, True)
        if row is None:
            return error_response("图片不存在", status_code=404)
        if blocked:
            affected = await asyncio.to_thread(
                self._db_block_hash, row["sha256"], "webui block"
            )
        else:
            affected = await asyncio.to_thread(self._db_unblock_hash, row["sha256"])
        return json_response({"blocked": blocked, "affected": affected})

    async def api_image_transfer(self):
        """重新分类：把选中的图片移动/复制到目标分类。"""
        payload = as_dict(await request.json(default={}))
        items = self._parse_transfer_items(payload.get("items"))
        target = normalize_name(str(payload.get("target", "")))
        mode = str(payload.get("mode", "move")).strip().lower()
        if not items or not target or mode not in {"move", "copy"}:
            return error_response("参数非法（items/target/mode）", status_code=400)
        info = await asyncio.to_thread(self._db_resolve_tag, target)
        target = info["canonical"] or target
        done, skipped, err = await asyncio.to_thread(self._db_transfer, items, target, mode)
        if err:
            return error_response(err, status_code=409)
        return json_response(
            {"done": done, "skipped": skipped, "target": target, "mode": mode}
        )

    async def api_hash_unblock(self):
        payload = as_dict(await request.json(default={}))
        sha = str(payload.get("sha256", "")).strip().lower()
        if len(sha) != 64:
            return error_response("sha256 非法", status_code=400)
        affected = await asyncio.to_thread(self._db_unblock_hash, sha)
        return json_response({"affected": affected})

    async def api_raw(self):
        name = normalize_name(str(request.query.get("category", "") or ""))
        seq = request.query.get("seq", None, type=int)
        if not name or seq is None:
            return error_response("参数非法", status_code=400)
        info = await asyncio.to_thread(self._db_resolve_tag, name)
        name = info["canonical"] or name
        row = await asyncio.to_thread(self._db_pick, name, seq, True)
        if row is None:
            return error_response("图片不存在", status_code=404)
        path = self._abs(row["rel_path"])
        if not path.exists():
            return error_response("文件不存在", status_code=404)
        return file_response(
            path,
            filename=f"{name}_{seq}{path.suffix}",
            content_type=MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream"),
        )

    # --------------------------------- 收尾 --------------------------------- #
    async def terminate(self) -> None:
        logger.info("[image_pool] 插件已停止")
