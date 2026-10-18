"""离线自检：用桩模块替换 astrbot，验证纯逻辑与数据层可跑。

用法: python tests/offline_check.py

注意：每个用例组用独立 tag 隔离 DB 与图片目录，避免互相污染。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
# 临时目录刻意用 mkdir(parents=True) 建在插件目录内，而不是 tempfile.mkdtemp()：
# Windows 沙箱下 mkdtemp 生成的目录自身不可写（再建子目录直接 PermissionError），
# 会让自检在第一批用例就崩掉，一条断言都跑不到。父目录可用 IMAGEPOOL_TEST_TMP 覆盖。
TMP = Path(os.environ.get("IMAGEPOOL_TEST_TMP") or (PLUGIN_DIR / ".test_tmp"))
TMP = TMP / f"run_{int(time.time())}_{os.getpid()}"
TMP.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# 桩：astrbot.*
# --------------------------------------------------------------------------- #
class _Comp:
    """通用消息组件替身。"""

    def __init__(self, *a, **k):
        self.args, self.kwargs = a, k
        # 关键：把 kwargs 落成属性，否则 Reply(id=..., chain=[...]) 读不到 chain。
        for key, value in k.items():
            setattr(self, key, value)

    @classmethod
    def fromFileSystem(cls, path=None, **kw):
        obj = cls()
        obj.path = path
        return obj

    @classmethod
    def fromURL(cls, url=None, **kw):
        obj = cls()
        obj.url = url
        return obj

    async def convert_to_file_path(self):
        # 真实实现会把 url/file 统一解析成本地路径；离线只支持本地形态。
        local = getattr(self, "path", None) or getattr(self, "file", None)
        if local:
            return local
        raise RuntimeError("no local path")

    def __repr__(self):
        return f"<{type(self).__name__} {self.args or self.kwargs}>"


class _Dummy(_Comp):
    pass


def _dec_factory(*a, **k):
    def deco(fn):
        fn.__astrbot_filter__ = True
        return fn

    return deco


class _Group:
    """模拟 RegisteringCommandable：可级联注册（group() 必须返回新的可注册对象）。

    真实实现里 ``@grp.group("子组")`` 返回的是新的 ``RegisteringCommandable``，
    而不是被装饰的函数 —— 若桩返回函数，第二层 ``@子组.command(...)`` 就会 AttributeError。
    """

    def __init__(self, parents=()):
        self.parents = tuple(parents)

    def command(self, name, alias=None):
        def deco(fn):
            fn.__astrbot_filter__ = True
            return fn

        return deco

    def group(self, name):
        def deco(fn):
            fn.__astrbot_filter__ = True
            return _Group(self.parents + (name,))

        return deco

    def custom_filter(self, *a, **k):
        return _dec_factory(*a, **k)

    def __call__(self, fn):
        return self


class _PermissionType:
    ADMIN = "ADMIN"
    SHARED_GROUP_ADMIN = "SHARED_GROUP_ADMIN"


class _EventMessageType:
    ALL = "ALL"
    GROUP_MESSAGE = "GROUP_MESSAGE"
    PRIVATE_MESSAGE = "PRIVATE_MESSAGE"


class _Filter:
    permission_type = staticmethod(_dec_factory)
    command = staticmethod(_dec_factory)
    command_group = staticmethod(lambda *a, **k: _Group())
    regex = staticmethod(_dec_factory)
    event_message_type = staticmethod(_dec_factory)
    platform_adapter_type = staticmethod(_dec_factory)
    on_llm_request = staticmethod(_dec_factory)
    PermissionType = _PermissionType
    EventMessageType = _EventMessageType


class _MessageType:
    GROUP_MESSAGE = "GroupMessage"
    FRIEND_MESSAGE = "FriendMessage"


class _MessageChain:
    def __init__(self, chain=None, **kw):
        self.chain = chain if chain is not None else []


class StarTools:
    @classmethod
    def get_data_dir(cls, plugin_name=None):
        d = TMP / str(plugin_name)
        d.mkdir(parents=True, exist_ok=True)
        return d


class Star:
    def __init__(self, context=None):
        self.context = context


class _Request:
    query = types.SimpleNamespace(get=lambda *a, **k: None)

    async def json(self, default=None):
        return default or {}


def install_stubs() -> None:
    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    api = mod(
        "astrbot.api",
        logger=types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            error=lambda *a, **k: None, exception=lambda *a, **k: None,
            debug=lambda *a, **k: None,
        ),
        AstrBotConfig=dict,
    )
    mod("astrbot", api=api)
    mod("astrbot.api.event", AstrMessageEvent=_Dummy, filter=_Filter,
        MessageChain=_MessageChain)
    # 每个组件必须是**独立的类**：插件靠 isinstance 区分 Image / Reply 等，
    # 若共用一个替身类，isinstance(comp, Image) 会对 Reply 也成立，分支判定全错。
    comp_names = (
        "Plain", "At", "AtAll", "Image", "Face", "Reply", "Record", "Video",
        "File", "Forward", "Node", "Nodes", "Json", "Poke",
    )
    comps = {name: type(f"_Stub{name}", (_Comp,), {}) for name in comp_names}
    mod("astrbot.api.message_components", **comps)
    mod("astrbot.api.platform", MessageType=_MessageType)
    mod("astrbot.api.star", Context=_Dummy, Star=Star, StarTools=StarTools,
        register=lambda *a, **k: (lambda cls: cls))
    mod("astrbot.api.web", json_response=lambda d=None: d,
        error_response=lambda *a, **k: None,
        file_response=lambda *a, **k: None, request=_Request())
    mod("astrbot.core")
    mod("astrbot.core.message")
    mod("astrbot.core.message.components", **comps)
    mod("astrbot.core.star")
    mod("astrbot.core.star.filter")
    mod("astrbot.core.star.filter.command", GreedyStr=type("GreedyStr", (str,), {}))


def load_plugin():
    install_stubs()
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_image_pool.main", PLUGIN_DIR / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# 测试数据与替身
# --------------------------------------------------------------------------- #
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89" + b"\x00" * 64
)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeEvent:
    def __init__(self, uid="u1", gid="100", is_group=True, raw="", messages=None,
                 message_str=None):
        self._uid, self._gid, self._is_group = uid, gid, is_group
        self._raw = raw
        self._messages = list(messages or [])
        # 真实 AstrBot 里 event.message_str 是普通实例属性，且唤醒阶段会把前缀剥掉，
        # 而 message_obj.message_str 保留原文。默认两者相同，需要时显式区分。
        self.message_str = raw if message_str is None else message_str
        self._extras: dict = {}
        self.stopped = False
        self.result = None

    def get_sender_id(self):
        return self._uid

    def get_group_id(self):
        return self._gid

    @property
    def unified_msg_origin(self):
        if self._is_group:
            return f"aiocqhttp:GroupMessage:{self._gid}"
        return f"aiocqhttp:FriendMessage:{self._uid}"

    def get_messages(self):
        return self._messages

    def get_message_type(self):
        return "GroupMessage" if self._is_group else "FriendMessage"

    def get_self_id(self):
        return "bot1"

    @property
    def message_obj(self):
        return types.SimpleNamespace(message_str=self._raw, message=self._messages)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return dict(self._extras)
        return self._extras.get(key, default)

    def plain_result(self, text):
        self.result = text
        return text

    def stop_event(self):
        self.stopped = True


class BatchEvent(FakeEvent):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.sent = []
        self.result = None

    def get_self_id(self):
        return "bot1"

    async def send(self, chain):
        self.sent.append(chain)

    def chain_result(self, chain):
        self.result = chain
        return chain


def new_plugin(m, tag="main", **cfg):
    """按 tag 隔离 DB 与图片目录。"""
    plugin = m.ImagePoolPlugin.__new__(m.ImagePoolPlugin)
    plugin.config = cfg
    plugin.base_dir = TMP / tag
    plugin.pool_dir = TMP / tag / "pool"
    plugin.db_path = TMP / tag / "index.sqlite3"
    plugin.face_log = TMP / tag / "faces.jsonl"
    plugin.pool_dir.mkdir(parents=True, exist_ok=True)
    plugin._init_db()
    return plugin


def drive(agen):
    """把指令 handler（async generator）跑完，返回（产出结果列表, 事件）。

    指令 handler 都是 ``async def ... yield`` 形态，直接调用只拿到生成器；
    这里统一驱动，顺便校验"回复后必须 stop_event"的约定。
    """
    async def _run():
        return [item async for item in agen]

    return asyncio.run(_run())


def main() -> int:
    m = load_plugin()
    failures = []

    def check(label, got, want):
        ok = got == want
        print(f"{'PASS' if ok else 'FAIL'}  {label}: {got!r}")
        if not ok:
            failures.append(f"{label}: got={got!r} want={want!r}")

    # ================================================ 纯函数：文件头嗅探
    check("sniff png", m.sniff_ext(PNG[:16]), ".png")
    check("sniff jpg", m.sniff_ext(JPEG[:16]), ".jpg")
    check("sniff gif", m.sniff_ext(GIF[:16]), ".gif")
    check("sniff webp", m.sniff_ext(WEBP[:16]), ".webp")
    check("sniff junk", m.sniff_ext(b"hello world!!!!!"[:16]), None)

    # ================================================ 纯函数：名称归一化
    check("name plain", m.normalize_name("初音未来"), "初音未来")
    check("name fullwidth", m.normalize_name("ＡＢＣ"), "ABC")
    check("name traversal", m.normalize_name("../../etc/passwd"), "etcpasswd")
    check("name illegal", m.normalize_name('a/b\\c:d*e'), "abcde")
    check("name empty", m.normalize_name("   "), None)
    check("name dots", m.normalize_name(".."), None)
    check("name long", len(m.normalize_name("角" * 100)), m.MAX_NAME_LEN)

    # ================================================ 需求4：参数消歧
    check("arg 裸数字=指定id", m.parse_fetch_arg("3"), (3, 1, True))
    check("arg x3=数量3", m.parse_fetch_arg("x3"), (None, 3, True))
    check("arg *5=数量5", m.parse_fetch_arg("*5"), (None, 5, True))
    check("arg ×2=数量2", m.parse_fetch_arg("×2"), (None, 2, True))
    check("arg 3张=数量3", m.parse_fetch_arg("3张"), (None, 3, True))
    check("arg 空=随机1张", m.parse_fetch_arg(None), (None, 1, True))
    check("arg 无法识别", m.parse_fetch_arg("abc"), (None, 1, False))
    check("count 裸数字", m.parse_count("4"), 4)
    check("count x4", m.parse_count("x4"), 4)
    check("count 4张", m.parse_count("4张"), 4)
    check("count 缺省", m.parse_count(None, 2), 2)
    check("count 垃圾值回落默认", m.parse_count("abc", 3), 3)

    rx = m.NAME_ARG_RE
    g = rx.match("初音未来")
    check("route 无参", (g.group("name"), g.group("arg")), ("初音未来", None))
    g = rx.match("初音未来 3")
    check("route id", (g.group("name"), g.group("arg")), ("初音未来", "3"))
    g = rx.match("初音未来 x3")
    check("route 数量", (g.group("name"), g.group("arg")), ("初音未来", "x3"))
    # 带参指令虽被正则匹配，但参数校验不通过 → 路由直接返回（由指令 handler 处理）
    check(
        "route 指令带参被参数校验拦下",
        m.parse_fetch_arg(rx.match("加图 初音未来").group("arg"))[2],
        False,
    )
    check("reserved 含发图", "发图" in m.RESERVED_NAMES, True)
    check("reserved 含图库", "图库" in m.RESERVED_NAMES, True)

    # ================================================ 需求1：群级三档权限模式
    pm = new_plugin(
        m, tag="mode",
        open_groups=["gOpen"], post_groups=["gPost"],
        default_group_mode="none", private_mode="post",
    )
    check("mode 白名单群", pm._group_mode(FakeEvent(gid="gOpen")), "whitelist")
    check("mode 发图群", pm._group_mode(FakeEvent(gid="gPost")), "post")
    check("mode 未配置→none", pm._group_mode(FakeEvent(gid="gOther")), "none")
    check("mode 私聊→private_mode", pm._group_mode(FakeEvent(is_group=False)), "post")

    pm.config["default_group_mode"] = "post"
    check("mode 默认模式可改", pm._group_mode(FakeEvent(gid="gOther")), "post")
    pm.config["private_mode"] = "bad-value"
    check("mode 私聊非法值回落 post", pm._group_mode(FakeEvent(is_group=False)), "post")
    pm.config["default_group_mode"] = "bad-value"
    check("mode 群非法值回落 none", pm._group_mode(FakeEvent(gid="gOther")), "none")
    pm.config["default_group_mode"] = "none"

    check("cap 白名单群可加图", pm._can(FakeEvent(gid="gOpen"), "add"), True)
    check("cap 白名单群可发图", pm._can(FakeEvent(gid="gOpen"), "fetch"), True)
    check("cap 白名单群默认不可管理", pm._can(FakeEvent(gid="gOpen"), "manage"), False)
    check("cap 发图群不可加图", pm._can(FakeEvent(gid="gPost"), "add"), False)
    check("cap 发图群可发图", pm._can(FakeEvent(gid="gPost"), "fetch"), True)
    check("cap 发图群不可管理", pm._can(FakeEvent(gid="gPost"), "manage"), False)
    check("cap 未配置不可加图", pm._can(FakeEvent(gid="gOther"), "add"), False)
    check("cap 未配置不可发图", pm._can(FakeEvent(gid="gOther"), "fetch"), False)

    pm.config["whitelist_allow_manage"] = True
    check("cap 白名单群放开可管理", pm._can(FakeEvent(gid="gOpen"), "manage"), True)
    check("cap 放开不影响发图群", pm._can(FakeEvent(gid="gPost"), "manage"), False)
    pm.config["whitelist_allow_manage"] = False

    # ================================================ 需求2：管理员分层与群隔离
    pa = new_plugin(
        m, tag="perm",
        plugin_admins=["root"], group_admins=["100:ga1", "*:ga2"],
    )
    check("admin 全局管理员", pa._is_global_admin(FakeEvent("root", "100")), True)
    check("admin 群级管理员(本群)", pa._is_group_admin(FakeEvent("ga1", "100")), True)
    check("admin 群级管理员跨群不生效", pa._is_group_admin(FakeEvent("ga1", "200")), False)
    check("admin 通配群级", pa._is_group_admin(FakeEvent("ga2", "999")), True)
    check("admin 普通成员", pa._is_global_admin(FakeEvent("u9", "100")), False)

    check("scope 全局→all", pa._manage_scope(FakeEvent("root", "100")), "all")
    check("scope 群级→local", pa._manage_scope(FakeEvent("ga1", "100")), "local")
    check("scope 普通成员→none", pa._manage_scope(FakeEvent("u9", "100")), "none")

    pa.config.update({"open_groups": ["gOpen"], "whitelist_allow_manage": True})
    check("scope 白名单放开→local", pa._manage_scope(FakeEvent("u9", "gOpen")), "local")

    pa._db_add("甲组", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "auto")
    pa._db_add("乙组", [{"data": JPEG, "ext": ".jpg", "sha256": sha(JPEG)}], "200", "u2", "auto")
    check("scope 全局管理员可管他群图",
          pa._can_manage_image(FakeEvent("root", "100"), "乙组", 1)[0], True)
    check("scope 群级管理员可管本群图",
          pa._can_manage_image(FakeEvent("ga1", "100"), "甲组", 1)[0], True)
    ok, hint = pa._can_manage_image(FakeEvent("ga1", "100"), "乙组", 1)
    check("scope 群级管理员越权被拒", ok, False)
    check("scope 越权提示说明原因", "本群" in hint, True)
    check("scope 普通成员被拒",
          pa._can_manage_image(FakeEvent("u9", "100"), "甲组", 1)[0], False)
    check("scope 图片不存在提示",
          pa._can_manage_image(FakeEvent("root", "100"), "甲组", 99)[0], False)

    # ================================================ 静默（沿用 v1.1.0 语义）
    pq = new_plugin(
        m, tag="silent",
        collect_groups=["100"], silent_groups=["999"],
        auto_silent_in_collect_groups=True, silent_scope="text_only",
    )
    check("silent 偷图群抑制文本", pq._suppress_text(FakeEvent(gid="100")), True)
    check("silent 偷图群 text_only 仍发图", pq._suppress_image(FakeEvent(gid="100")), False)
    check("silent 显式静默群", pq._suppress_text(FakeEvent(gid="999")), True)
    check("silent 普通群不静默", pq._suppress_text(FakeEvent(gid="777")), False)
    pq.config["silent_scope"] = "all"
    check("silent all 连图也不发", pq._suppress_image(FakeEvent(gid="100")), True)
    pq.config.update({"collect_groups": [], "silent_groups": [], "silent_scope": "text_only"})
    check("silent 未配偷图群不全局静默", pq._suppress_text(FakeEvent(gid="100")), False)

    # ================================================ 提示语
    ph = new_plugin(m, tag="hint", post_groups=["gPost"])
    check("hint 发图群文案", "发图群" in ph._add_denied_hint(FakeEvent(gid="gPost")), True)
    check("hint 未配置文案", "未配置" in ph._add_denied_hint(FakeEvent(gid="gY")), True)
    check("hint 发图被拒含模式名", "none" in ph._fetch_denied_hint(FakeEvent(gid="gY")), True)
    check("hint 管理员提示含独立性说明",
          "独立" in ph._admin_denied_hint(FakeEvent(gid="gY")), True)

    # ================================================ 数据层
    p = new_plugin(m, tag="data", plugin_admins=["root"])
    items = [
        {"data": PNG, "ext": ".png", "sha256": sha(PNG)},
        {"data": JPEG, "ext": ".jpg", "sha256": sha(JPEG)},
    ]
    check("db add", p._db_add("初音未来", items, "100", "u1", "manual"), (2, 0))
    check("db add 自动建分类", p._db_category_create("初音未来"), (False, "分类已存在"))
    check("db dedupe", p._db_add("初音未来", items, "100", "u1", "manual"), (0, 2))

    row = p._db_pick("初音未来", 2)
    check("db pick by id -> seq", row["seq"], 2)
    check("db pick file exists", (p.pool_dir / row["rel_path"]).exists(), True)
    check("db pick seq=None 不再随机（改走 pick_many）", p._db_pick("初音未来", None), None)
    check("db random 覆盖两张",
          {r["seq"] for _ in range(30) for r in p._db_pick_many("初音未来", 1)}, {1, 2})
    check("db pick missing id", p._db_pick("初音未来", 99), None)
    check("db drop", p._db_drop("初音未来", 1), "初音未来/000001.png")
    check("db seq 不复用", p._db_add("初音未来", items, "100", "u1", "manual")[0], 1)
    check("db 新图拿到 seq3", p._db_pick("初音未来", 3)["seq"], 3)

    # ------------------------------------------ 需求3/4：_db_pick_many（全库/限类随机）
    got = p._db_pick_many("初音未来", 10)
    check("pick_many 上限=实际张数", len(got), 2)
    check("pick_many 不重复", len({r["seq"] for r in got}), 2)
    check("pick_many 单张", len(p._db_pick_many("初音未来", 1)), 1)
    check("pick_many 限定分类", {r["char_name"] for r in p._db_pick_many("初音未来", 5)}, {"初音未来"})
    check("pick_many 空分类", p._db_pick_many("不存在", 3), [])
    p._db_add("另一组", [{"data": GIF, "ext": ".gif", "sha256": sha(GIF)}], "300", "u3", "auto")
    check("pick_many 全库跨分类", {r["char_name"] for r in p._db_pick_many(None, 50)},
          {"初音未来", "另一组"})

    # ------------------------------------------------------ 屏蔽：指纹黑名单
    check("block 屏蔽已入库同图数", p._db_block_hash(sha(PNG), "test"), 1)
    check("block 随机不再返回",
          {r["seq"] for _ in range(30) for r in p._db_pick_many("初音未来", 1)}, {2})
    check("block pick_many 也排除", {r["seq"] for r in p._db_pick_many("初音未来", 10)}, {2})
    check("block pick_many 全库也排除",
          all(r["sha256"] != sha(PNG) for r in p._db_pick_many(None, 50)), True)
    check("block 指定 id 默认被挡", p._db_pick("初音未来", 3), None)
    check("block include_blocked 可查", p._db_pick("初音未来", 3, True)["seq"], 3)
    check("block 拉黑后拒绝再收录", p._db_add("初音未来", items[:1], "100", "u1", "auto"), (0, 1))
    check("unblock 恢复张数", p._db_unblock_hash(sha(PNG)), 1)
    check("unblock 随机恢复",
          {r["seq"] for _ in range(30) for r in p._db_pick_many("初音未来", 1)}, {2, 3})

    rows, total = p._db_images_page("初音未来", 0, 10)
    check("page total", total, 2)
    check("page 含 group_id 字段", "group_id" in rows[0].keys(), True)

    # ================================================ 需求5/6：重新分类 move/copy
    p._db_category_create("B组")
    p._db_add("B组", [{"data": WEBP, "ext": ".webp", "sha256": sha(WEBP)}], "300", "u3", "auto")
    check("transfer 前置 B组 1 张", p._db_pick("B组", 1) is not None, True)

    check("transfer copy 成功", p._db_transfer([("初音未来", 2)], "B组", "copy"), (1, 0, ""))
    check("transfer copy 源仍在", p._db_pick("初音未来", 2) is not None, True)
    check("transfer copy 目标已加", p._db_pick("B组", 2) is not None, True)
    # 需求6 断言：同一 sha256 同时挂在两个分类 → 一张图可多 tag
    check("tag 同图多分类共存",
          p._db_pick("初音未来", 2)["sha256"] == p._db_pick("B组", 2)["sha256"], True)

    check("transfer move 成功", p._db_transfer([("初音未来", 3)], "B组", "move"), (1, 0, ""))
    check("transfer move 源已移出", p._db_pick("初音未来", 3), None)
    check("transfer move 目标已入", p._db_pick("B组", 3) is not None, True)
    check("transfer move 原文件已搬走",
          (p.pool_dir / "初音未来" / "000003.png").exists(), False)
    # 需求6 断言：move 之后该图不再属于原分类
    check("tag move 后原分类无此 sha256",
          any(r["sha256"] == sha(PNG) for r in p._db_images_page("初音未来", 0, 50)[0]), False)

    check("transfer 目标重复→跳过且保留源",
          p._db_transfer([("初音未来", 2)], "B组", "move"), (0, 1, ""))
    check("transfer 跳过后源仍在", p._db_pick("初音未来", 2) is not None, True)
    check("transfer 不存在的图→跳过", p._db_transfer([("不存在", 1)], "B组", "move")[:2], (0, 1))
    check("transfer 同分类→跳过", p._db_transfer([("B组", 1)], "B组", "move")[:2], (0, 1))
    check("transfer 到新分类", p._db_transfer([("B组", 1)], "新分类", "move")[0], 1)
    check("transfer 自动建目标分类",
          "新分类" in [c["name"] for c in p._db_category_list()], True)
    check("transfer 空 items 无影响", p._db_transfer([], "B组", "copy")[:2], (0, 0))
    check("transfer 批量两项", p._db_transfer([("B组", 2)], "新分类", "copy")[0], 1)

    # ================================================ 分类：创建/改名/删除
    check("cat create", p._db_category_create("风景"), (True, ""))
    check("cat rename 空分类", p._db_category_rename("风景", "风景照"), (0, ""))
    check("cat rename 冲突保护", p._db_category_rename("风景照", "B组"), (0, "「B组」已存在"))
    check("cat rename 同名保护", p._db_category_rename("B组", "B组"), (0, "新名和旧名相同"))
    check("cat delete 目标不能是自身", p._db_category_delete("B组", "move", "B组"),
          (0, "目标分类不能是自身"))
    check("cat delete purge", p._db_category_delete("风景照", "purge", "未分类")[1], "")
    check("cat purge 目录已删", (p.pool_dir / "风景照").exists(), False)

    ov = p._db_overview()
    check("overview 分类数>=2", ov["categories"] >= 2, True)
    check("overview 黑名单数", ov["blocked_hashes"], 0)

    # ================================================ 老库自动迁移
    conn = sqlite3.connect(TMP / "legacy.sqlite3")
    conn.execute(
        "CREATE TABLE images (id INTEGER PRIMARY KEY AUTOINCREMENT, char_name TEXT NOT NULL,"
        " seq INTEGER NOT NULL, rel_path TEXT NOT NULL, ext TEXT NOT NULL, sha256 TEXT NOT NULL,"
        " size_bytes INTEGER NOT NULL DEFAULT 0, group_id TEXT NOT NULL DEFAULT '',"
        " uploader_id TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'manual',"
        " created_at INTEGER NOT NULL DEFAULT 0, UNIQUE (char_name, seq), UNIQUE (char_name, sha256))"
    )
    conn.commit()
    conn.close()
    legacy = new_plugin(m, tag="legacy")
    legacy.db_path = TMP / "legacy.sqlite3"
    legacy._init_db()
    c = sqlite3.connect(TMP / "legacy.sqlite3")
    cols = {r[1] for r in c.execute("PRAGMA table_info(images)")}
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    check("migration blocked 列已补齐", "blocked" in cols, True)
    check("migration categories 表已建", "categories" in tables, True)
    check("migration blocked_hashes 表已建", "blocked_hashes" in tables, True)

    # ================================================ 缩略图
    check("thumb 不存在文件返回 None", p._thumb_data_uri(p.pool_dir / "nope.png"), None)
    sample = p._db_pick("B组", 2, True)
    check("thumb 存在文件返回 data URI",
          str(p._thumb_data_uri(p.pool_dir / sample["rel_path"])).startswith("data:image/"), True)

    # ================================================ 需求4：批量发送形态
    p2 = new_plugin(m, tag="data", batch_send_mode="chain", at_sender=False,
                    forward_nickname="图库")
    paths = [
        p2.pool_dir / r["rel_path"]
        for r in p2._db_images_page("B组", 0, 50, True)[0]
    ]
    paths = [x for x in paths if x.exists()]
    check("batch 前置：至少 2 张", len(paths) >= 2, True)

    ev = BatchEvent()
    res = asyncio.run(p2._send_batch(ev, paths, None))
    check("batch chain 模式返回结果", res is not None, True)
    check("batch chain 未直接发送", ev.sent, [])

    p2.config["batch_send_mode"] = "forward"
    ev = BatchEvent()
    res = asyncio.run(p2._send_batch(ev, paths, None))
    check("batch forward 直接发送 1 次", len(ev.sent), 1)
    check("batch forward 返回 None", res, None)
    check("batch forward 用 Nodes 包裹", len(ev.sent[0].chain), 1)

    p2.config["batch_send_mode"] = "separate"
    ev = BatchEvent()
    res = asyncio.run(p2._send_batch(ev, paths, None))
    check("batch separate 逐条发送", len(ev.sent), len(paths))
    check("batch separate 返回 None", res, None)

    check("collect no image", asyncio.run(p2._collect_images(FakeEvent(), 5)), [])

    # ================================================ 修复：发给机器人的消息不进采集
    ImageComp = sys.modules["astrbot.api.message_components"].Image
    src_dir = TMP / "collect_src"
    src_dir.mkdir(parents=True, exist_ok=True)
    png_file = src_dir / "a.png"
    png_file.write_bytes(PNG)
    jpg_file = src_dir / "b.jpg"
    jpg_file.write_bytes(JPEG)

    pc = new_plugin(
        m, tag="collect",
        auto_collect=True, collect_groups=["100"], collect_namespace="未分类",
        collect_skip_commands=True, retention_days=0, max_pool_mb=0,
    )

    def img_event(raw, path, command=False):
        ev = FakeEvent(gid="100", raw=raw, messages=[ImageComp.fromFileSystem(str(path))])
        if command:
            ev.set_extra("handlers_parsed_params", {"add_image": {}})
        return ev

    check("bot_addressed 指令消息", pc._is_bot_addressed(img_event("/加图 甲", png_file, True)), True)
    check("bot_addressed 斜杠域消息", pc._is_bot_addressed(img_event("/随便聊聊", png_file)), True)
    check("bot_addressed 普通群聊", pc._is_bot_addressed(img_event("", png_file)), False)

    asyncio.run(pc.passive_collect(img_event("/加图 甲", png_file, True)))
    check("采集：指令消息被跳过（不再误入默认分类）", pc._db_pick("未分类", 1, True), None)

    asyncio.run(pc.passive_collect(img_event("/随便聊聊", png_file)))
    check("采集：斜杠域消息被跳过", pc._db_pick("未分类", 1, True), None)

    asyncio.run(pc.passive_collect(img_event("", png_file)))
    check("采集：普通群聊消息正常入库", pc._db_pick("未分类", 1, True) is not None, True)

    # 回归对照：关掉开关即复现原 bug
    pc.config["collect_skip_commands"] = False
    asyncio.run(pc.passive_collect(img_event("/加图 甲", jpg_file, True)))
    check("采集：关闭开关后指令图被误收（复现原 bug）",
          pc._db_pick("未分类", 2, True) is not None, True)
    pc.config["collect_skip_commands"] = True

    # 历史数据修复：/图库 去重未分类
    pc._db_add("甲", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "manual")
    res = pc._db_dedupe_namespace("未分类")
    check("去重未分类：删除重复采集图", res["removed"], 1)
    check("去重未分类：保留无重复的采集图", res["kept_auto"], 1)
    check("去重未分类：重复项已消失", pc._db_pick("未分类", 1, True), None)
    check("去重未分类：目标分类的记录不动", pc._db_pick("甲", 1, True) is not None, True)
    check("去重未分类：命中来源可追溯", res["per_tag"], {"甲": 1})
    check("去重未分类：幂等（再跑一次无删除）",
          pc._db_dedupe_namespace("未分类")["removed"], 0)

    # ================================================ 保留期与容量清理
    def age(plugin, char, seq, days):
        conn = sqlite3.connect(plugin.db_path)
        conn.execute(
            "UPDATE images SET created_at=? WHERE char_name=? AND seq=?",
            (int(time.time()) - days * 86400, char, seq),
        )
        conn.commit()
        conn.close()

    def size_of(plugin, char, seq, nbytes):
        conn = sqlite3.connect(plugin.db_path)
        conn.execute(
            "UPDATE images SET size_bytes=? WHERE char_name=? AND seq=?",
            (nbytes, char, seq),
        )
        conn.commit()
        conn.close()

    pr = new_plugin(
        m, tag="retain",
        retention_days=30, retention_source="auto", retention_exempt=["精选"],
        max_pool_mb=0, retention_sweep_hours=6,
    )
    pr._db_add("未分类", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "auto")
    pr._db_add("未分类", [{"data": JPEG, "ext": ".jpg", "sha256": sha(JPEG)}], "100", "u1", "auto")
    pr._db_add("精选", [{"data": GIF, "ext": ".gif", "sha256": sha(GIF)}], "100", "u1", "auto")
    pr._db_add("人工", [{"data": WEBP, "ext": ".webp", "sha256": sha(WEBP)}], "100", "u1", "manual")
    age(pr, "未分类", 1, 40)
    age(pr, "未分类", 2, 1)
    age(pr, "精选", 1, 40)
    age(pr, "人工", 1, 40)

    res = pr._db_sweep_retention()
    check("保留期：删除超时采集图", res["expired"], 1)
    check("保留期：未超时的不删", pr._db_pick("未分类", 2, True) is not None, True)
    check("保留期：豁免分类不删", pr._db_pick("精选", 1, True) is not None, True)
    check("保留期：人工收录不删", pr._db_pick("人工", 1, True) is not None, True)
    check("保留期：超时行已消失", pr._db_pick("未分类", 1, True), None)
    check("保留期：文件已删除", (pr.pool_dir / "未分类" / "000001.png").exists(), False)
    check("保留期：报告释放字节", res["freed_bytes"] > 0, True)
    check("保留期：stats 统计采集图", pr._db_pool_stats()["auto"], 2)

    # 关闭时不做任何事
    pz = new_plugin(m, tag="retain_off", retention_days=0, max_pool_mb=0)
    pz._db_add("未分类", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "auto")
    age(pz, "未分类", 1, 400)
    off = pz._db_sweep_retention()
    check("保留期：关闭时不清理", off["enabled"], False)
    check("保留期：关闭时数据保留", pz._db_pick("未分类", 1, True) is not None, True)

    # 容量淘汰：最旧优先
    pt = new_plugin(m, tag="cap", retention_days=0, max_pool_mb=1, retention_source="auto")
    for i, (data, ext) in enumerate(
        [(PNG, ".png"), (JPEG, ".jpg"), (GIF, ".gif")], start=1
    ):
        pt._db_add("未分类", [{"data": data, "ext": ext, "sha256": sha(data)}], "100", "u1", "auto")
        age(pt, "未分类", i, 10 - i)  # seq1 最旧
        size_of(pt, "未分类", i, 500_000)  # 3 × 500KB = 1.5MB > 1MB
    cap = pt._db_sweep_retention()
    check("容量：按最旧优先淘汰", cap["trimmed"], 1)
    check("容量：淘汰的是最旧一条", pt._db_pick("未分类", 1, True), None)
    check("容量：较新的保留", pt._db_pick("未分类", 3, True) is not None, True)

    # 惰性清理的节流
    pr._sweep_at = 0.0
    asyncio.run(pr._maybe_sweep())
    check("节流：首次执行后写入时间戳", pr._sweep_at > 0, True)
    pr._db_add("未分类", [{"data": WEBP, "ext": ".webp", "sha256": sha(WEBP)}], "100", "u1", "auto")
    age(pr, "未分类", 3, 90)
    asyncio.run(pr._maybe_sweep())
    check("节流：间隔内不重复清理", pr._db_pick("未分类", 3, True) is not None, True)

    # ================================================ 采集发送者：黑白名单双模式
    gif_file = src_dir / "c.gif"
    gif_file.write_bytes(GIF)

    pm2 = new_plugin(m, tag="sender", collect_sender_mode="whitelist", collect_senders=["u1"])
    check("sender 白名单：命中", pm2._sender_allowed(FakeEvent("u1")), True)
    check("sender 白名单：未命中", pm2._sender_allowed(FakeEvent("u2")), False)
    pm2.config["collect_sender_mode"] = "BAD"
    check("sender 非法模式回落 blacklist", pm2._collect_sender_filter()[0], "blacklist")
    pm2.config.update({"collect_sender_mode": "blacklist", "collect_senders": ["u2"]})
    check("sender 黑名单：命中被拦", pm2._sender_allowed(FakeEvent("u2")), False)
    check("sender 黑名单：未命中放行", pm2._sender_allowed(FakeEvent("u1")), True)
    pm2.config.update({"collect_senders": [], "skip_senders": ["u3"]})
    check("sender 黑名单：旧键 skip_senders 并入", pm2._sender_allowed(FakeEvent("u3")), False)
    check("sender 黑名单：空名单放行", pm2._sender_allowed(FakeEvent("u9")), True)
    pm2.config.update({"collect_sender_mode": "whitelist", "collect_senders": [],
                       "skip_senders": ["u3"]})
    check("sender 白名单：空名单谁都不采（安全默认）",
          pm2._sender_allowed(FakeEvent("u9")), False)
    check("sender 白名单：不消费旧黑名单键", pm2._sender_allowed(FakeEvent("u3")), False)

    # 与 passive_collect 的联动
    pw = new_plugin(
        m, tag="sender_live",
        auto_collect=True, collect_groups=["100"], collect_namespace="未分类",
        collect_skip_commands=True, collect_sender_mode="whitelist", collect_senders=["u1"],
        retention_days=0, max_pool_mb=0,
    )

    def chat_event(uid, path):
        return FakeEvent(uid=uid, gid="100", raw="", messages=[ImageComp.fromFileSystem(str(path))])

    asyncio.run(pw.passive_collect(chat_event("u1", png_file)))
    check("采集白名单：名单内成员被采集", pw._db_pick("未分类", 1, True) is not None, True)
    asyncio.run(pw.passive_collect(chat_event("u2", jpg_file)))
    check("采集白名单：名单外成员被跳过", pw._db_pick("未分类", 2, True), None)

    pw.config.update({"collect_sender_mode": "blacklist", "collect_senders": [], "skip_senders": ["u2"]})
    asyncio.run(pw.passive_collect(chat_event("u2", jpg_file)))
    check("采集黑名单：旧键 skip_senders 生效", pw._db_pick("未分类", 2, True), None)
    pw.config["skip_senders"] = []
    asyncio.run(pw.passive_collect(chat_event("u2", jpg_file)))
    check("采集黑名单：空名单全部放行", pw._db_pick("未分类", 2, True) is not None, True)

    pw.config.update({"collect_sender_mode": "whitelist", "collect_senders": []})
    asyncio.run(pw.passive_collect(chat_event("u9", gif_file)))
    check("采集白名单：空名单不采集任何人", pw._db_pick("未分类", 3, True), None)

    # ================================================ /加图 支持引用消息
    ReplyComp = sys.modules["astrbot.api.message_components"].Reply
    quoted = ImageComp.fromFileSystem(str(png_file))
    reply = ReplyComp(id="1001", chain=[quoted])
    pv = new_plugin(m, tag="reply", allow_reply_collect=True)

    ev = FakeEvent(gid="100", raw="/加图 甲", messages=[reply])
    items = asyncio.run(pv._collect_images(ev, 9, include_reply=True))
    check("引用：/加图 路径可收录引用图", len(items), 1)
    check("引用：收录到的是被引用的那张", items[0]["sha256"], sha(PNG))

    items = asyncio.run(pv._collect_images(ev, 9, include_reply=False))
    check("引用：被动采集路径不展开引用", items, [])

    pv.config["allow_reply_collect"] = False
    items = asyncio.run(pv._collect_images(ev, 9, include_reply=True))
    check("引用：开关关闭时不收录", items, [])
    pv.config["allow_reply_collect"] = True

    # 同一消息里既有直发图又有引用图 → 两张都收
    both = FakeEvent(
        gid="100", raw="/加图 甲",
        messages=[ImageComp.fromFileSystem(str(jpg_file)), ReplyComp(id="1002", chain=[quoted])],
    )
    check("引用：直发图与引用图同时收录",
          len(asyncio.run(pv._collect_images(both, 9, include_reply=True))), 2)
    check("引用：受 limit 约束",
          len(asyncio.run(pv._collect_images(both, 1, include_reply=True))), 1)

    # chain 为空 → 走协议端 get_msg 回拉
    class BotEvent(FakeEvent):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.calls = []
            self.bot = types.SimpleNamespace(call_action=self._call_action)

        async def _call_action(self, action, **kw):
            self.calls.append((action, kw))
            return {"message": [{"type": "image", "data": {"file": str(png_file)}}]}

    empty_reply = ReplyComp(id="2002", chain=[])
    ev = BotEvent(gid="100", messages=[empty_reply])
    items = asyncio.run(pv._collect_images(ev, 9, include_reply=True))
    check("引用：chain 为空时回拉协议端", len(items), 1)
    check("引用：调用 get_msg 并带上 id", ev.calls, [("get_msg", {"message_id": "2002"})])

    # 协议端不支持 / 消息过期 → 静默降级
    class DeadBotEvent(FakeEvent):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.bot = types.SimpleNamespace(call_action=self._boom)

        async def _boom(self, action, **kw):
            raise RuntimeError("no such message")

    ev = DeadBotEvent(gid="100", messages=[ReplyComp(id="3003", chain=[])])
    check("引用：回拉失败静默降级", asyncio.run(pv._collect_images(ev, 9, include_reply=True)), [])

    ev = FakeEvent(gid="100", messages=[ReplyComp(id="4004", chain=[])])
    check("引用：无协议端时不报错", asyncio.run(pv._collect_images(ev, 9, include_reply=True)), [])

    # ================================================ v1.5.0 参数解析（多 tag / 删图 / 移动）
    check("tags：空白分隔", m.parse_tags("A B C", 5), (["A", "B", "C"], 0))
    check("tags：逗号顿号加号", m.parse_tags("甲,乙、丙+丁", 5), (["甲", "乙", "丙", "丁"], 0))
    check("tags：去重且保序", m.parse_tags("A A B A", 5), (["A", "B"], 0))
    check("tags：超上限丢弃并计数", m.parse_tags("A B C D", 2), (["A", "B"], 2))
    check("tags：防路径穿越", m.parse_tags("../../etc A", 5), (["etc", "A"], 0))
    check("tags：空输入", m.parse_tags("   ", 5), ([], 0))
    check("tags：全角归一", m.parse_tags("ＡＢ 甲", 5), (["AB", "甲"], 0))

    check("del：分类名+id", m.parse_delete_args("甲", "3"), ("甲", 3, ""))
    check("del：仅分类名（引用形态）", m.parse_delete_args("甲", None), ("甲", None, ""))
    check("del：全空（引用形态）", m.parse_delete_args(None, None), (None, None, ""))
    check("del：裸数字判为歧义", m.parse_delete_args("3", None)[2] != "", True)
    check("del：id 非数字报错", m.parse_delete_args("甲", "x")[2] != "", True)

    check("move：目标+源+id", m.parse_move_args("目标", "源", "7"), ("目标", "源", 7, ""))
    check("move：仅目标（引用形态）", m.parse_move_args("目标", None, None), ("目标", None, None, ""))
    check("move：目标+源（引用形态）", m.parse_move_args("目标", "源", None), ("目标", "源", None, ""))
    check("move：缺源分类报错", m.parse_move_args("目标", "3", None)[3] != "", True)
    check("move：缺目标报错", m.parse_move_args(None, None, None)[3] != "", True)
    check("move：id 非数字报错", m.parse_move_args("目标", "源", "x")[3] != "", True)
    # AstrBot 的 CommandFilter：参数无注解默认值时按"默认值"处理，纯数字会被转成 int
    check("del：id 以 int 传入也能解析", m.parse_delete_args("甲", 3), ("甲", 3, ""))
    check("move：id 以 int 传入也能解析", m.parse_move_args("目标", "源", 3), ("目标", "源", 3, ""))

    # ================================================ /加图：多分类 tag
    vdir = TMP / "v15_src"
    vdir.mkdir(parents=True, exist_ok=True)
    f_png, f_jpg, f_gif, f_webp = (
        vdir / "a.png", vdir / "b.jpg", vdir / "c.gif", vdir / "d.webp",
    )
    f_png.write_bytes(PNG)
    f_jpg.write_bytes(JPEG)
    f_gif.write_bytes(GIF)
    f_webp.write_bytes(WEBP)

    def img(path):
        return ImageComp.fromFileSystem(str(path))

    def reply_to(paths, mid="9001"):
        return ReplyComp(id=mid, chain=[img(p) for p in paths])

    ptag = new_plugin(m, tag="multitag", plugin_admins=["u1"],
                      max_tags_per_add=5, max_images_per_msg=9)
    ev = FakeEvent(gid="100", raw="/加图 A B C", messages=[img(f_png)])
    out = drive(ptag.add_image(ev, "A B C"))
    check("多tag：单图落进 3 个分类",
          [ptag._db_pick(c, 1) is not None for c in ("A", "B", "C")], [True, True, True])
    check("多tag：回复是账单形态", "3 个分类" in (out[0] if out else ""), True)
    check("多tag：每分类各存一份文件",
          [(ptag.pool_dir / c / "000001.png").exists() for c in ("A", "B", "C")],
          [True, True, True])
    check("多tag：回复后停止事件", ev.stopped, True)

    # 引用多图 + 多 tag（多人图）：4 张图分别落到 3 个分类，重复的按分类各自去重
    ev = FakeEvent(gid="100", raw="/加图 A B C",
                   messages=[reply_to([f_png, f_jpg, f_gif, f_webp])])
    out = drive(ptag.add_image(ev, "A B C"))
    check("多tag：引用的多张图全部落入每个分类",
          [ptag._db_images_page(c, 0, 99, True)[1] for c in ("A", "B", "C")], [4, 4, 4])
    check("多tag：重复图在分类内被跳过", "跳过" in (out[0] if out else ""), True)

    # 上限：超出的分类被忽略
    pcap = new_plugin(m, tag="multitag_cap", plugin_admins=["u1"], max_tags_per_add=2)
    ev = FakeEvent(gid="100", raw="/加图 A B C", messages=[img(f_png)])
    out = drive(pcap.add_image(ev, "A B C"))
    check("多tag：超上限只落前 N 个",
          [pcap._db_pick("A", 1) is not None, pcap._db_pick("B", 1) is not None,
           pcap._db_pick("C", 1) is not None], [True, True, False])
    check("多tag：超上限有提示", "已忽略 1 个" in (out[0] if out else ""), True)

    # 单分类保持旧文案；空格写法不再被吞成一个分类
    pone = new_plugin(m, tag="multitag_one", plugin_admins=["u1"])
    out = drive(pone.add_image(
        FakeEvent(gid="100", raw="/加图 甲", messages=[img(f_png)]), "甲"))
    check("多tag：单分类仍是旧文案", out[0], "✅ 已为「甲」收录 1 张图片")
    drive(pone.add_image(
        FakeEvent(gid="100", raw="/加图 甲 乙", messages=[img(f_jpg)]), "甲 乙"))
    check("多tag：空格分隔不再粘连成一个分类", pone._db_pick("甲乙", 1, True), None)
    check("多tag：空格分隔各建一个分类", pone._db_pick("乙", 1, True) is not None, True)

    # ================================================ /删图：按引用图片定位
    pdel = new_plugin(m, tag="delreply", plugin_admins=["u1"])
    pdel._db_add("甲", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "manual")
    pdel._db_add("乙", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "manual")
    pdel._db_add("乙", [{"data": JPEG, "ext": ".jpg", "sha256": sha(JPEG)}], "100", "u1", "manual")

    out = drive(pdel.delete_image(FakeEvent(gid="100", messages=[reply_to([f_png])]), "甲"))
    check("删图引用：限定分类时只删该类",
          [pdel._db_pick("甲", 1, True) is None, pdel._db_pick("乙", 1, True) is not None],
          [True, True])

    ev = FakeEvent(gid="100", messages=[reply_to([f_png])])
    out = drive(pdel.delete_image(ev))
    check("删图引用：不填分类跨全库删同内容图", pdel._db_pick("乙", 1, True), None)
    check("删图引用：回复报出张数", "已删除 1 张" in (out[0] if out else ""), True)
    check("删图引用：文件一并删除", (pdel.pool_dir / "乙" / "000001.png").exists(), False)
    check("删图引用：无关的图不受影响", pdel._db_pick("乙", 2, True) is not None, True)
    check("删图引用：回复后停止事件", ev.stopped, True)

    out = drive(pdel.delete_image(FakeEvent(gid="100", messages=[reply_to([f_webp])])))
    check("删图引用：引用图不在图库时提示未删", "没有删除任何图片" in (out[0] if out else ""), True)
    check("删图引用：统计不在图库的引用图", "1 张引用图不在图库中" in (out[0] if out else ""), True)

    out = drive(pdel.delete_image(FakeEvent(gid="100", messages=[])))
    check("删图引用：没有引用时给用法", "引用" in (out[0] if out else ""), True)

    out = drive(pdel.delete_image(FakeEvent(gid="100"), "甲", "x"))
    check("删图：id 非数字给提示", "应是图片 id" in (out[0] if out else ""), True)

    pint = new_plugin(m, tag="delint", plugin_admins=["u1"])
    pint._db_add("甲", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "manual")
    drive(pint.delete_image(FakeEvent(gid="100"), "甲", 1))
    check("删图：id 以 int 传入（AstrBot 的默认转换）", pint._db_pick("甲", 1, True), None)

    pnod = new_plugin(m, tag="delno")
    pnod._db_add("甲", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "manual")
    out = drive(pnod.delete_image(FakeEvent(gid="100", messages=[reply_to([f_png])])))
    check("删图引用：无管理权限被拒", "⛔" in (out[0] if out else ""), True)
    check("删图引用：无权限时不删数据", pnod._db_pick("甲", 1, True) is not None, True)

    # ================================================ /移动：换 tag
    pmv = new_plugin(m, tag="movetag", plugin_admins=["u1"])
    pmv._db_add("源", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "manual")
    pmv._db_add("源", [{"data": JPEG, "ext": ".jpg", "sha256": sha(JPEG)}], "100", "u1", "manual")
    out = drive(pmv.move_image(FakeEvent(gid="100", raw="/移动 目标 源 1"), "目标", "源", "1"))
    check("移动：指定 id 换 tag（源行消失）", pmv._db_pick("源", 1, True), None)
    check("移动：指定 id 换 tag（目标落库）", pmv._db_pick("目标", 1, True) is not None, True)
    check("移动：源分类其它图不动", pmv._db_pick("源", 2, True) is not None, True)
    check("移动：文件已搬走", (pmv.pool_dir / "源" / "000001.png").exists(), False)
    check("移动：目标文件就位", (pmv.pool_dir / "目标" / "000001.png").exists(), True)
    check("移动：回复后停止事件", out and "换 tag" in out[0], True)

    pmv._db_add("旧目标", [{"data": JPEG, "ext": ".jpg", "sha256": sha(JPEG)}], "100", "u1", "manual")
    out = drive(pmv.move_image(FakeEvent(gid="100"), "旧目标", "源", "2"))
    check("移动：目标已有同图则跳过", pmv._db_pick("源", 2, True) is not None, True)
    check("移动：跳过时给出解释", "源图仍留" in (out[0] if out else ""), True)

    # 引用形态：不填 id，按图片内容把它的每一行都换到目标 tag
    prev = new_plugin(m, tag="movereply", plugin_admins=["u1"])
    prev._db_add("甲", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "manual")
    prev._db_add("乙", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "manual")
    prev._db_add("乙", [{"data": JPEG, "ext": ".jpg", "sha256": sha(JPEG)}], "100", "u1", "manual")
    out = drive(prev.move_image(FakeEvent(gid="100", messages=[reply_to([f_png])]), "归档"))
    check("移动引用：命中的第一份换到目标 tag", prev._db_pick("归档", 1, True) is not None, True)
    check("移动引用：同图在别处仍保留多 tag 记录（不静默删）",
          [prev._db_pick("甲", 1, True), prev._db_pick("乙", 1, True)].count(None), 1)
    check("移动引用：跳过重复的那份并说明", "跳过" in (out[0] if out else ""), True)
    check("移动引用：无关图不动", prev._db_pick("乙", 2, True) is not None, True)

    out = drive(prev.move_image(
        FakeEvent(gid="100", messages=[reply_to([f_jpg])]), "归档", "乙"))
    check("移动引用：指定源分类缩小范围",
          [prev._db_pick("乙", 2, True) is None, prev._db_pick("归档", 2, True) is not None],
          [True, True])

    out = drive(prev.move_image(
        FakeEvent(gid="100", messages=[reply_to([f_jpg])]), "归档", "甲"))
    check("移动引用：源分类里没这张图时提示无可移动",
          "没有可移动的图片" in (out[0] if out else ""), True)

    pnm = new_plugin(m, tag="moveno")
    pnm._db_add("甲", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "manual")
    out = drive(pnm.move_image(FakeEvent(gid="100", messages=[reply_to([f_png])]), "目标"))
    check("移动引用：无管理权限被拒", "⛔" in (out[0] if out else ""), True)
    check("移动引用：无权限时不改数据", pnm._db_pick("甲", 1, True) is not None, True)

    # ================================================ 去重未分类：只清采集落点，方向不能反
    pdd = new_plugin(m, tag="dedupe2", plugin_admins=["u1"])
    pdd._db_add("未分类", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "auto")
    pdd._db_add("未分类", [{"data": JPEG, "ext": ".jpg", "sha256": sha(JPEG)}], "100", "u1", "auto")
    pdd._db_add("未分类", [{"data": GIF, "ext": ".gif", "sha256": sha(GIF)}], "100", "u1", "manual")
    pdd._db_add("甲", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "manual")
    pdd._db_add("甲", [{"data": WEBP, "ext": ".webp", "sha256": sha(WEBP)}], "100", "u1", "auto")
    pdd._db_add("乙", [{"data": GIF, "ext": ".gif", "sha256": sha(GIF)}], "100", "u1", "manual")
    pdd._db_add("乙", [{"data": WEBP, "ext": ".webp", "sha256": sha(WEBP)}], "100", "u2", "manual")

    res = pdd._db_dedupe_namespace("未分类")
    check("去重：只删采集落点里的重复采集图", res["removed"], 1)
    check("去重：无重复的采集图保留", res["kept_auto"], 1)
    check("去重：人工收录的重复项保留并计数", res["kept_manual"], 1)
    check("去重：命中来源可追溯", res["per_tag"], {"甲": 1})
    check("去重：未分类里的重复项已删", pdd._db_pick("未分类", 1, True), None)
    check("去重：未分类里的独有项还在", pdd._db_pick("未分类", 2, True) is not None, True)
    check("去重：未分类里的人工项还在", pdd._db_pick("未分类", 3, True) is not None, True)
    check("去重：普通 tag 内部的跨 tag 重复一律不动（多人图安全）",
          [pdd._db_pick("甲", 2, True) is not None, pdd._db_pick("乙", 2, True) is not None],
          [True, True])
    check("去重：幂等（再跑一次无删除）", pdd._db_dedupe_namespace("未分类")["removed"], 0)

    # 多人图场景：同一张图合法地同时属于 A/B/C，去重不得动它们
    pmulti = new_plugin(m, tag="dedupe_multi")
    for c in ("A", "B", "C"):
        pmulti._db_add(c, [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "manual")
    pmulti._db_add("未分类", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "auto")
    res = pmulti._db_dedupe_namespace("未分类")
    check("去重：多人图在 A/B/C 的多 tag 记录全保留",
          [pmulti._db_pick(c, 1, True) is not None for c in ("A", "B", "C")],
          [True, True, True])
    check("去重：只清掉采集池里的那一份", res["removed"], 1)

    # 命令层面：不允许把普通 tag 当清理目标
    out = drive(pdd.pool_dedupe_namespace(FakeEvent(gid="100", raw="/图库 去重未分类 甲"), "甲"))
    check("去重：拒绝把普通 tag 当清理目标", "⛔" in (out[0] if out else ""), True)
    check("去重：拒绝时不产生删除", pdd._db_pick("甲", 2, True) is not None, True)

    out = drive(pdd.pool_dedupe_namespace(FakeEvent(gid="100")))
    check("去重：无参数 = 与所有现有 tag 依次比对",
          ("去重完成" in (out[0] if out else "")) or ("没有需要去重" in (out[0] if out else "")),
          True)

    out = drive(pdd.pool_dedupe_namespace(FakeEvent(gid="100"), "未分类"))
    check("去重：显式传采集落点照常执行",
          ("去重完成" in (out[0] if out else "")) or ("没有需要去重" in (out[0] if out else "")),
          True)

    # ================================================ v1.6.0 别名：纯函数
    check("key：大小写折叠", m.name_key("HATSUNE"), "hatsune")
    check("key：全角折叠", m.name_key("ＡＢＣ"), "abc")
    check("key：非法名返回 None", m.name_key("   "), None)

    check("alias 校验：正常", m.check_alias_name("hx"), ("hx", ""))
    check("alias 校验：含空格拒绝", m.check_alias_name("初音 未来")[0], None)
    check("alias 校验：含斜杠拒绝", m.check_alias_name("a/b")[0], None)
    check("alias 校验：纯数字拒绝", m.check_alias_name("3")[0], None)
    check("alias 校验：全角数字也拒绝", m.check_alias_name("３")[0], None)
    check("alias 校验：保留字拒绝", m.check_alias_name("发图")[0], None)
    check("alias 校验：保留字大小写也拒绝", m.check_alias_name("Help")[0], None)
    check("alias 校验：空白拒绝", m.check_alias_name("  ")[0], None)

    check("建议：前缀命中", m.suggest_tags("初音未", ["初音未来", "巡音"]), ["初音未来"])
    check("建议：折叠相同优先", m.suggest_tags("HATSUNE", ["Hatsune", "别的"]), ["Hatsune"])
    check("建议：差太远就沉默", m.suggest_tags("abc", ["初音未来"]), [])

    # ================================================ v1.6.0 别名：数据层
    def one(data, ext):
        return {"data": data, "ext": ext, "sha256": sha(data)}

    pal = new_plugin(m, tag="alias", plugin_admins=["u1"])
    pal._db_add("初音未来", [one(PNG, ".png")], "100", "u1", "manual")
    pal._db_add("巡音", [one(JPEG, ".jpg")], "100", "u1", "manual")

    check("索引：正式名自动入表", pal._db_resolve_tag("初音未来")["via"], "canonical")
    check("索引：正式名折叠命中", pal._db_resolve_tag("初音未来")["canonical"], "初音未来")

    ok, _msg = pal._db_alias_add("hx", "初音未来")
    check("别名：登记成功", ok, True)
    check("解析：命中别名",
          pal._db_resolve_tag("hx"),
          {"canonical": "初音未来", "via": "alias", "suggestions": []})
    check("解析：别名大小写不敏感", pal._db_resolve_tag("HX")["canonical"], "初音未来")
    check("解析：未命中附建议", pal._db_resolve_tag("初音未末")["suggestions"], ["初音未来"])
    check("解析：保留字不给别名机会", pal._db_resolve_tag("发图")["via"], "reserved")
    check("别名：列表", [(r["alias_text"], r["canonical"]) for r in pal._db_alias_list()],
          [("hx", "初音未来")])

    check("别名：重复登记被拒", pal._db_alias_add("hx", "初音未来")[0], False)
    check("别名：目标不存在被拒", pal._db_alias_add("zz", "不存在")[0], False)
    check("别名：不能指向别名（禁链式）", pal._db_alias_add("hx2", "hx")[0], False)
    check("别名：与目标同词被拒", pal._db_alias_add("巡音", "巡音")[0], False)
    check("别名：占用真实分类名被拒", pal._db_alias_add("巡音", "初音未来")[0], False)

    ok2, msg2 = pal._db_alias_add("hx", "巡音")
    check("别名：冲突默认拒绝", ok2, False)
    check("别名：冲突回显占用者", "初音未来" in msg2, True)
    ok3, msg3 = pal._db_alias_add("hx", "巡音", force=True)
    check("别名：force 可接管", ok3, True)
    check("别名：接管后改指向", pal._db_resolve_tag("hx")["canonical"], "巡音")
    check("别名：接管回显旧目标", "初音未来" in msg3, True)

    check("别名：删不掉正式名", pal._db_alias_delete("巡音")[0], False)
    check("别名：删别名成功", pal._db_alias_delete("hx")[0], True)
    check("解析：删后回落未命中", pal._db_resolve_tag("hx")["via"], "miss")

    pal.config["max_aliases_per_tag"] = 1
    pal._db_alias_add("a1", "初音未来")
    check("别名：超过上限被拒", pal._db_alias_add("a2", "初音未来")[0], False)
    pal.config["max_aliases_per_tag"] = 20

    pal._db_alias_add("vip", "巡音")
    check("建分类：撞别名被拒", pal._db_category_create("VIP")[0], False)
    check("建分类：撞别名时说明原因", "别名" in pal._db_category_create("VIP")[1], True)
    check("建分类：撞正式名被拒（仅大小写不同也拒）",
          pal._db_category_create("巡音")[0], False)

    # ================================================ v1.6.0 别名：生命周期联动
    prn = new_plugin(m, tag="alias_rename", plugin_admins=["u1"])
    prn._db_add("旧名", [one(PNG, ".png")], "100", "u1", "manual")
    prn._db_alias_add("old", "旧名")
    moved, rerr = prn._db_category_rename("旧名", "新名")
    check("改名：图片跟着走", moved, 1)
    check("改名：无错误", rerr, "")
    check("改名：别名跟随指向", prn._db_resolve_tag("old")["canonical"], "新名")
    check("改名：旧正式名不再命中", prn._db_resolve_tag("旧名")["via"], "miss")
    prn._db_alias_add("taken", "新名")
    check("改名：目标撞别名被拒", prn._db_category_rename("新名", "taken")[1] != "", True)

    prd = new_plugin(m, tag="alias_delete", plugin_admins=["u1"])
    prd._db_add("待删", [one(PNG, ".png")], "100", "u1", "manual")
    prd._db_alias_add("gone", "待删")
    prd._db_category_delete("待删", "move", "未分类")
    check("删分类：别名一并消失", prd._db_alias_list(), [])

    pad = new_plugin(m, tag="alias_adopt", plugin_admins=["u1"])
    pad._db_add("初音未来", [one(PNG, ".png")], "100", "u1", "manual")
    pad._db_add("初音未末", [one(JPEG, ".jpg")], "100", "u1", "manual")
    aok, amoved, _amsg = pad._db_alias_adopt("初音未末", "初音未来")
    check("收编：成功", aok, True)
    check("收编：迁移张数", amoved, 1)
    check("收编：图片已并入", pad._db_images_page("初音未来", 0, 99, True)[1], 2)
    check("收编：错分类已消失", pad._db_pick("初音未末", 1, True), None)
    check("收编：错名成为别名", pad._db_resolve_tag("初音未末")["canonical"], "初音未来")
    check("收编：目标不存在时报错", pad._db_alias_adopt("x", "不存在")[0], False)

    # ================================================ v1.6.0 别名：指令接线
    pwire = new_plugin(m, tag="alias_wire", plugin_admins=["u1"])
    pwire._db_add("初音未来", [one(PNG, ".png")], "100", "u1", "manual")
    pwire._db_alias_add("hx", "初音未来")

    out = drive(pwire.add_image(
        FakeEvent(gid="100", raw="/加图 hx", messages=[img(f_jpg)]), "hx"))
    check("接线：/加图 用别名落进正式名", pwire._db_pick("初音未来", 2) is not None, True)
    check("接线：别名不会生成同名分类",
          "hx" in [c["name"] for c in pwire._db_category_list()], False)
    check("接线：回执报的是正式名", "初音未来" in out[0], True)

    out = drive(pwire.add_image(
        FakeEvent(gid="100", raw="/加图 初音未来 hx", messages=[img(f_gif)]), "初音未来 hx"))
    check("接线：别名+正式名指向同一 tag → 只写一次",
          "已为「初音未来」收录 1 张图片" in out[0], True)
    check("接线：不会退化成两份账单", "个分类" in out[0], False)

    out = drive(pwire.delete_image(FakeEvent(gid="100"), "hx", 1))
    check("接线：/删图 支持别名", pwire._db_pick("初音未来", 1, True), None)

    pstrict = new_plugin(m, tag="alias_strict", plugin_admins=["u1"], tag_auto_create=False)
    pstrict._db_add("初音未来", [one(PNG, ".png")], "100", "u1", "manual")
    out = drive(pstrict.add_image(
        FakeEvent(gid="100", raw="/加图 初音未末", messages=[img(f_jpg)]), "初音未末"))
    check("严格模式：拒绝静默新建", "都不可用" in out[0], True)
    check("严格模式：给出相似建议", "你是不是想找「初音未来」" in out[0], True)
    check("严格模式：确实没建新分类",
          [c["name"] for c in pstrict._db_category_list()], ["初音未来"])
    check("严格模式：也没有写入",
          pstrict._db_images_page("初音未来", 0, 99, True)[1], 1)

    pstrict._db_alias_add("hx", "初音未来")
    out = drive(pstrict.add_image(
        FakeEvent(gid="100", raw="/加图 hx", messages=[img(f_jpg)]), "hx"))
    check("严格模式：别名命中照样可用",
          pstrict._db_images_page("初音未来", 0, 99, True)[1], 2)

    pcfgw = new_plugin(m, tag="alias_cfg", collect_namespace="hx", plugin_admins=["u1"])
    pcfgw._db_add("初音未来", [one(PNG, ".png")], "100", "u1", "manual")
    pcfgw._db_alias_add("hx", "初音未来")
    check("配置：collect_namespace 支持别名",
          pcfgw._cfg_tag("collect_namespace", "未分类"), "初音未来")
    check("配置：解析不到时回落字面量",
          pcfgw._cfg_tag("unknown_key", "别的"), "别的")

    # 指令级
    out = drive(pwire.alias_add(FakeEvent(gid="100"), "hx3", "初音未来"))
    check("指令：别名加成功", out[0].startswith("✅"), True)
    out = drive(pwire.alias_list(FakeEvent(gid="100")))
    check("指令：列表含新别名", "hx3 → 初音未来" in out[0], True)
    out = drive(pwire.alias_list(FakeEvent(gid="100"), "初音未来"))
    check("指令：按目标过滤", "hx3 → 初音未来" in out[0], True)
    out = drive(pwire.alias_set(FakeEvent(gid="100"), "hx3", "巡音"))
    check("指令：改指向（目标不存在则拒绝）", out[0].startswith("⛔"), True)
    out = drive(pwire.alias_del(FakeEvent(gid="100"), "hx3"))
    check("指令：别名删成功", out[0].startswith("✅"), True)
    pnoadm = new_plugin(m, tag="alias_noadm")
    out = drive(pnoadm.alias_add(FakeEvent(gid="100"), "a", "b"))
    check("指令：无权限被拒", "⛔" in out[0], True)
    out = drive(pnoadm.alias_del(FakeEvent(gid="100"), "a"))
    check("指令：无权限不能删", "⛔" in out[0], True)

    # 一致性：kind='tag' 行必须与 categories 一一对应，且没有悬空别名
    def index_ok(plugin):
        conn = sqlite3.connect(plugin.db_path)
        cats = {r[0] for r in conn.execute("SELECT name FROM categories")}
        rows = conn.execute("SELECT key, canonical, kind FROM tag_index").fetchall()
        conn.close()
        dangling = [r for r in rows if r[1] not in cats]
        return not dangling and {r[1] for r in rows if r[2] == "tag"} == cats

    check("一致性：索引与 categories 一一对应且无悬空", index_ok(pwire), True)
    check("一致性：删除分类后仍一致", index_ok(prd), True)
    check("一致性：收编后仍一致", index_ok(pad), True)
    conn = sqlite3.connect(pwire.db_path)
    missing_rows = conn.execute(
        "SELECT name FROM categories WHERE name NOT IN "
        "(SELECT canonical FROM tag_index WHERE kind='tag')"
    ).fetchall()
    conn.close()
    check("一致性：每个分类都有正式名行", missing_rows, [])

    # ================================================ 配置 Schema 与代码一致性
    # WebUI 渲染规则（源码核实 dashboard/src/components/shared/ConfigItemRenderer.vue）：
    #   bool -> v-switch；string + options -> v-select（labels 对齐 options）；
    #   int + slider -> 滑块（值会被钳制到 [min,max]）；说明文字放 hint，不要塞进 description。
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    check("schema：每项都有 type 与 description",
          all("type" in v and "description" in v for v in schema.values()), True)
    check("schema：description 一律简短（≤ 12 字）",
          [k for k, v in schema.items() if len(v["description"]) > 12], [])
    check("schema：每项都提供 hint 承载细节",
          [k for k, v in schema.items() if not v.get("hint")], [])
    check("schema：hint 比 description 长（细节确实搬到了 hint）",
          [k for k, v in schema.items() if len(v.get("hint", "")) <= len(v["description"])], [])
    check("schema：bool 项类型正确",
          sorted(k for k, v in schema.items() if v["type"] == "bool"),
          sorted(["whitelist_allow_manage", "auto_silent_in_collect_groups", "at_sender",
                  "bare_name_enable", "fallback_llm_on_miss", "allow_purge", "auto_collect",
                  "collect_skip_commands", "collect_at_bot_reply", "collect_faces",
                  "allow_reply_collect", "tag_auto_create", "alias_suggest_on_miss"]))

    for key, options in {
        "default_group_mode": sorted(m.VALID_MODES),
        "private_mode": sorted(m.VALID_MODES),
        "silent_scope": ["all", "text_only"],
        "batch_send_mode": ["chain", "forward", "separate"],
        "retention_source": ["all", "auto"],
        "collect_sender_mode": ["blacklist", "whitelist"],
    }.items():
        item = schema[key]
        check(f"schema：{key} 渲染为下拉（options 与代码一致）",
              sorted(item.get("options", [])), options)
        check(f"schema：{key} labels 与 options 数量对齐",
              len(item.get("labels", [])), len(item["options"]))
        check(f"schema：{key} 默认值在 options 内",
              item.get("default") in item["options"], True)

    for key in ("max_batch_fetch", "max_images_per_msg", "max_tags_per_add",
                "max_aliases_per_tag", "list_limit", "thumb_size"):
        sl = schema[key].get("slider") or {}
        check(f"schema：{key} 滑块区间合法",
              sl.get("min", 1) < sl.get("max", 0) and sl.get("step", 0) > 0, True)
        check(f"schema：{key} 默认值在滑块区间内",
              sl.get("min", 0) <= schema[key]["default"] <= sl.get("max", 0), True)

    # ================================================ 回归：2026-10-09 对抗式审查修复
    src = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")

    # ---- P0-1 分类级操作只给全局管理员（whitelist_allow_manage 不得放大） ----
    pfix = new_plugin(
        m, tag="fix_p01",
        plugin_admins=["root"], open_groups=["gOpen"], whitelist_allow_manage=True,
    )
    member_ev = FakeEvent(uid="u9", gid="gOpen")
    admin_ev = FakeEvent(uid="root", gid="gOpen")
    drive(pfix.pool_create(member_ev, "偷偷建"))
    check("P0-1 白名单普通成员不能新建分类", "偷偷建" in pfix._db_tag_names(), False)
    drive(pfix.pool_create(admin_ev, "正常建"))
    check("P0-1 全局管理员仍能新建分类", "正常建" in pfix._db_tag_names(), True)
    drive(pfix.pool_rename(member_ev, "正常建", "被改名"))
    check("P0-1 白名单普通成员不能改名分类", "被改名" in pfix._db_tag_names(), False)
    drive(pfix.pool_delete(member_ev, "move", "正常建"))
    check("P0-1 白名单普通成员不能删除分类", "正常建" in pfix._db_tag_names(), True)
    check("P0-1 图片级管理权仍在（只限本群）",
          pfix._manage_scope(FakeEvent(uid="u9", gid="gOpen")), "local")

    # ---- P0-2 数据库不可用不得让插件加载失败 ----
    pbad = new_plugin(m, tag="fix_p02")
    pbad.db_path = pbad.base_dir / "as_dir"
    pbad.db_path.mkdir(parents=True, exist_ok=True)
    try:
        pbad._init_db()
        p02_ok = True
    except Exception:
        p02_ok = False
    check("P0-2 库路径不可用时 _init_db 不抛异常", p02_ok, True)

    # ---- P0-3 Web API 请求体必须强制成 dict ----
    check("P0-3 as_dict 数组归一为空 dict", m.as_dict([1, 2]), {})
    check("P0-3 as_dict 字符串归一为空 dict", m.as_dict("x"), {})
    check("P0-3 as_dict 数字归一为空 dict", m.as_dict(5), {})
    check("P0-3 as_dict null 归一为空 dict", m.as_dict(None), {})
    check("P0-3 as_dict 对象原样返回", m.as_dict({"a": 1}), {"a": 1})
    check("P0-3 每个 request.json 都被 as_dict 包住",
          src.count("await request.json(default={})"),
          src.count("as_dict(await request.json(default={}))"))

    # ---- P0-4 分页：服务端 limit 是权威 ----
    html = (PLUGIN_DIR / "pages" / "manage" / "index.html").read_text(encoding="utf-8")
    js = (PLUGIN_DIR / "pages" / "manage" / "app.js").read_text(encoding="utf-8")
    check("P0-4 thumbs 回传生效 limit", '"limit": limit' in src, True)
    check("P0-4 前端不再提供超过 60 的页大小", 'value="96"' in html, False)
    check("P0-4 前端用服务端 limit 校正步进", "data.limit" in js, True)

    # ---- P0-5 老库折叠同键：精确匹配优先 ----
    pc5 = new_plugin(m, tag="fix_p05")
    conn5 = pc5._connect()
    conn5.execute("INSERT INTO categories (name, created_at) VALUES (?,?)", ("Hatsune", 0))
    conn5.execute("INSERT INTO categories (name, created_at) VALUES (?,?)", ("HATSUNE", 0))
    conn5.execute(
        "INSERT OR IGNORE INTO tag_index (key, canonical, kind, created_at) VALUES (?,?,?,?)",
        ("hatsune", "Hatsune", m.KIND_TAG, 0),
    )
    conn5.commit()
    conn5.close()
    check("P0-5 精确名优先于折叠键", pc5._db_resolve_tag("HATSUNE")["canonical"], "HATSUNE")
    check("P0-5 冲突时标记 canonical-exact",
          pc5._db_resolve_tag("HATSUNE")["via"], "canonical-exact")
    check("P0-5 折叠键仍作兜底", pc5._db_resolve_tag("hatsune")["canonical"], "Hatsune")
    check("P0-5 正常分类仍走 canonical",
          pc5._db_resolve_tag("Hatsune")["via"], "canonical")
    pc5._db_tag_index_sync()
    conn5 = pc5._connect()
    keys5 = {r["key"] for r in conn5.execute("SELECT key FROM tag_index")}
    conn5.close()
    check("P0-5 自检补齐折叠冲突分类的 tag 行", "HATSUNE" in keys5, True)
    # 折叠键恰好等于其中一个分类名本身（"hatsune" vs "HATSUNE"）：合成键兜底
    pc5b = new_plugin(m, tag="fix_p05b")
    conn5b = pc5b._connect()
    conn5b.execute("INSERT INTO categories (name, created_at) VALUES (?,?)", ("hatsune", 0))
    conn5b.execute("INSERT INTO categories (name, created_at) VALUES (?,?)", ("HATSUNE", 0))
    conn5b.commit()
    conn5b.close()
    pc5b._db_tag_index_sync()
    conn5b = pc5b._connect()
    tagrows5 = conn5b.execute(
        "SELECT canonical FROM tag_index WHERE kind=?", (m.KIND_TAG,)
    ).fetchall()
    conn5b.close()
    check("P0-5 折叠键重名时两个分类都拿到 tag 行",
          sorted(r["canonical"] for r in tagrows5), ["HATSUNE", "hatsune"])

    # ---- P1-1 布尔归一化（"false" 不得反向打开安全开关） ----
    check("P1-1 to_bool('false') 为假", m.to_bool("false"), False)
    check("P1-1 to_bool('FALSE') 为假", m.to_bool("FALSE"), False)
    check("P1-1 to_bool('0') 为假", m.to_bool("0"), False)
    check("P1-1 to_bool('off') 为假", m.to_bool("off"), False)
    check("P1-1 to_bool('true') 为真", m.to_bool("true"), True)
    check("P1-1 to_bool('1') 为真", m.to_bool("1"), True)
    check("P1-1 to_bool(None, True) 走默认", m.to_bool(None, True), True)
    check("P1-1 to_bool('maybe', True) 走默认", m.to_bool("maybe", True), True)
    check("P1-1 全文不再有裸 bool(self._cfg(...)) 形态",
          re.findall(r"(?<!to_)bool\(self\._cfg\(", src), [])
    check("P1-1 to_bool('') 走默认值", m.to_bool("", True), True)
    check("P1-1 to_bool('  ') 走默认值", m.to_bool("  ", True), True)
    pstr = new_plugin(m, tag="fix_p11", plugin_admins=["root"], allow_purge="false")
    pstr._db_add("丙组", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "auto")
    out11 = drive(pstr.pool_delete(FakeEvent(uid="root", gid="100"), "purge", "丙组"))
    check("P1-1 配置写 'false' 时 purge 仍被禁用", "丙组" in pstr._db_tag_names(), True)
    check("P1-1 'false' 下回执说明已禁用", "禁用" in "".join(str(x) for x in out11), True)

    # ---- P1-2 群号与用户 ID 两个命名空间不得混用 ----
    pids = new_plugin(m, tag="fix_p12", plugin_admins=["100"], open_groups=["999"])
    check("P1-2 群号填进 plugin_admins 不再提权整群",
          pids._is_global_admin(FakeEvent(uid="u9", gid="100")), False)
    check("P1-2 用户 ID 填进 plugin_admins 仍有效",
          pids._is_global_admin(FakeEvent(uid="100", gid="777")), True)
    check("P1-2 用户 ID 填进 open_groups 不再跨群生效",
          new_plugin(m, tag="fix_p12b", open_groups=["u7"])._group_mode(
              FakeEvent(uid="u7", gid="999")), "none")
    check("P1-2 群号填进 open_groups 正常生效",
          pids._group_mode(FakeEvent(uid="u7", gid="999")), "whitelist")
    check("P1-2 群 UMO 填进 plugin_admins 不授权该群成员",
          new_plugin(m, tag="fix_p12c",
                     plugin_admins=["aiocqhttp:GroupMessage:999"])._is_global_admin(
              FakeEvent(uid="100", gid="999")), False)
    check("P1-2 私聊 UMO 仍可授权本人",
          new_plugin(m, tag="fix_p12d",
                     plugin_admins=["aiocqhttp:FriendMessage:100"])._is_global_admin(
              FakeEvent(uid="100", is_group=False, gid=None)), True)

    # ---- P1-3 全静默下事件必须收口（否则 LLM 补一条文本） ----
    psil = new_plugin(m, tag="fix_p13", open_groups=["100"], silent_groups=["100"],
                      silent_scope="all")
    psil._db_add("甲", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "auto")
    ev13 = FakeEvent(uid="u1", gid="100", raw="/甲", message_str="甲")
    out13 = drive(psil.name_router(ev13))
    check("P1-3 全静默下 /分类名 不产出", out13, [])
    check("P1-3 全静默下事件被收口", ev13.stopped, True)
    psil.config["silent_scope"] = "text_only"
    ev13b = BatchEvent(uid="u1", gid="100", raw="/甲", message_str="甲")
    out13b = drive(psil.name_router(ev13b))
    check("P1-3 对照组：text_only 仍出图", len(out13b) > 0, True)
    check("P1-3 对照组：事件同样收口", ev13b.stopped, True)

    # ---- P1-4 分类迁移失败不得回执成功、不得改 DB 行 ----
    pmv = new_plugin(m, tag="fix_p14")
    pmv._db_category_create("源组")
    pmv._db_category_create("目标组")
    pmv._db_add("源组", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "auto")
    row14 = pmv._db_pick("源组", 1)
    (pmv.pool_dir / "目标组" / Path(row14["rel_path"]).name).mkdir(parents=True)
    n14, err14 = pmv._db_category_delete("源组", "move", "目标组")
    check("P1-4 搬移失败不再回执成功", err14 != "", True)
    check("P1-4 搬移失败时保留原分类", "源组" in pmv._db_tag_names(), True)
    check("P1-4 搬移失败时图片仍在原分类", pmv._db_pick("源组", 1) is not None, True)
    check("P1-4 搬移计数不含失败项", n14, 0)

    # ---- P1-5 文件缺失不得永久销毁索引行 ----
    prp = new_plugin(m, tag="fix_p15")
    prp._db_add("甲组", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "auto")
    rows15 = prp._db_images_page("甲组", 0, 10)[0]
    for r15 in rows15:
        prp._abs(r15["rel_path"]).unlink()
    kept15 = asyncio.run(prp._resolve_paths(rows15))
    check("P1-5 文件缺失时不返回路径", kept15, [])
    check("P1-5 文件缺失时保留索引行", prp._db_pick("甲组", 1) is not None, True)

    # ---- P1-6 retention_source 非法值 fail-close ----
    pret = new_plugin(m, tag="fix_p16", retention_days=1, retention_source="aut")
    pret._db_add("人工组", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}],
                 "100", "u1", "manual")
    conn16 = pret._connect()
    conn16.execute("UPDATE images SET created_at=0")
    conn16.commit()
    conn16.close()
    res16 = pret._db_sweep_retention()
    check("P1-6 非法 retention_source 不启用清理", res16.get("enabled"), False)
    check("P1-6 非法值下人工图保留", pret._db_pick("人工组", 1) is not None, True)
    pret.config["retention_source"] = "all"
    pret._db_sweep_retention()
    check("P1-6 对照组：合法值 all 才会清理", pret._db_pick("人工组", 1), None)
    psweep = new_plugin(m, tag="fix_p16b", retention_days=1, retention_source="bogus",
                        plugin_admins=["root"])
    out16 = drive(psweep.pool_sweep(FakeEvent(uid="root", gid="100")))
    check("P1-6 /图库 清理 会说明非法配置原因",
          "retention_source" in "".join(str(x) for x in out16), True)

    # ---- P1-7 _db_pick 不再有随机分支（用**有图**的分类，旧实现会返回 Row） ----
    check("P1-7 _db_pick(seq=None) 返回 None（随机一律走 pick_many）",
          p._db_pick("初音未来", None), None)
    check("P1-7 对照组：带 id 仍能取到",
          p._db_pick("初音未来", 2) is not None, True)

    # ---- P1-8 缩略图缓存：真命中（不重新渲染）+ 失效 + 并发安全 + 记账一致 ----
    pth = new_plugin(m, tag="fix_p18")
    pth._db_add("甲", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "auto")
    row18 = pth._db_images_page("甲", 0, 10)[0][0]
    f18 = pth._abs(row18["rel_path"])
    st18 = pth._thumb_cache()
    calls = {"n": 0}
    real_render = pth._thumb_render

    def _counting_render(path, size):
        calls["n"] += 1
        return real_render(path, size)

    pth._thumb_render = _counting_render
    u18a = pth._thumb_data_uri(f18)
    check("P1-8 首次生成并写入缓存", len(st18["cache"]), 1)
    check("P1-8 首次确实渲染了一次", calls["n"], 1)
    check("P1-8 二次调用命中缓存（未重新渲染）",
          (pth._thumb_data_uri(f18), calls["n"]), (u18a, 1))
    check("P1-8 缓存未重复占用", len(st18["cache"]), 1)
    _st = f18.stat()
    os.utime(f18, (_st.st_atime + 10, _st.st_mtime + 10))
    pth._thumb_data_uri(f18)
    check("P1-8 文件 mtime 变化后重新渲染", calls["n"], 2)
    check("P1-8 mtime 变化后缓存两条", len(st18["cache"]), 2)
    th_errs = []

    def _thumb_hammer():
        try:
            for _ in range(40):
                pth._thumb_data_uri(f18)
        except Exception as e:  # noqa: BLE001 - 这里就是要抓任何并发异常
            th_errs.append(repr(e))

    ths = [threading.Thread(target=_thumb_hammer) for _ in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    check("P1-8 8 线程并发访问缓存不抛异常", th_errs, [])
    check("P1-8 并发后字节记账与内容一致",
          st18["bytes"], sum(len(v) for v in st18["cache"].values()))

    # ---- P1-9 保留字不能作分类名（指令与 WebUI 共用同一层） ----
    pres = new_plugin(m, tag="fix_p19")
    check("P1-9 不能新建保留字分类", pres._db_category_create("加图")[0], False)
    check("P1-9 保留字提示说明原因", "保留字" in pres._db_category_create("图库")[1], True)
    pres._db_category_create("正常组")
    check("P1-9 不能改名为保留字", pres._db_category_rename("正常组", "发图")[1] != "", True)

    # ================================================ 回归：独立对抗验证发现的第二轮问题
    # ---- N-1 保留字旁路：自动建分类（/加图）与 WebUI transfer 同样不能建保留字分类 ----
    pn1 = new_plugin(m, tag="fix_n1")
    plan_n1 = pn1._plan_tags(["图库"])
    check("N-1 _plan_tags 不把保留字放进待新建", plan_n1["new"], [])
    check("N-1 _plan_tags 把保留字放进 reserved", plan_n1.get("reserved"), ["图库"])
    check("N-1 _db_add 拒绝为保留字建分类",
          pn1._db_add("图库", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}],
                      "100", "u1", "auto"), (0, 1))
    check("N-1 保留字分类没有被建出来", "图库" in pn1._db_tag_names(), False)
    pn1._db_add("甲", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}], "100", "u1", "auto")
    check("N-1 _db_transfer 拒绝保留字目标",
          pn1._db_transfer([("甲", 1)], "图库", "move")[2] != "", True)
    check("N-1 transfer 被拒后未建出分类", "图库" in pn1._db_tag_names(), False)
    check("N-1 迁移到保留字目标也被拒",
          pn1._db_category_delete("甲", "move", "图库")[1] != "", True)
    check("N-1 指令侧 /加图 保留字不再建分类（计划层）",
          pn1._plan_tags(["加图", "help"])["new"], [])

    # ---- N-2 孤儿行不再阻塞重新收录（就地修复） ----
    pn2 = new_plugin(m, tag="fix_n2")
    it_n2 = {"data": PNG, "ext": ".png", "sha256": sha(PNG)}
    pn2._db_add("甲", [it_n2], "100", "u1", "auto")
    pn2._abs(pn2._db_pick("甲", 1)["rel_path"]).unlink()
    check("N-2 文件丢失后重新收录会修复而不是跳过",
          pn2._db_add("甲", [it_n2], "100", "u1", "auto"), (1, 0))
    row_n2 = pn2._db_pick("甲", 1)
    check("N-2 修复后文件真的回来了",
          bool(row_n2) and pn2._abs(row_n2["rel_path"]).exists(), True)
    check("N-2 文件仍在时依旧幂等跳过",
          pn2._db_add("甲", [it_n2], "100", "u1", "auto"), (0, 1))

    # ---- N-3 折叠冲突：共用目录时拒绝删除，避免连带毁掉对方文件 ----
    pn3 = new_plugin(m, tag="fix_n3")
    conn_n3 = pn3._connect()
    conn_n3.execute("INSERT INTO categories (name, created_at) VALUES (?,?)", ("Hatsune", 0))
    conn_n3.execute("INSERT INTO categories (name, created_at) VALUES (?,?)", ("HATSUNE", 0))
    conn_n3.commit()
    conn_n3.close()
    pn3._db_add("Hatsune", [{"data": PNG, "ext": ".png", "sha256": sha(PNG)}],
                "100", "u1", "auto")
    n_n3, err_n3 = pn3._db_category_delete("Hatsune", "purge")
    check("N-3 折叠冲突时拒绝 purge", err_n3 != "", True)
    check("N-3 被拒后图片仍在", pn3._db_pick("Hatsune", 1) is not None, True)
    row_n3 = pn3._db_pick("Hatsune", 1)
    check("N-3 被拒后文件仍在",
          bool(row_n3) and pn3._abs(row_n3["rel_path"]).exists(), True)

    # ---- N-4 群 UMO 入 plugin_admins + 成员 UID 撞群号，不得提权 ----
    check("N-4 群 UMO 与撞号成员都不提权",
          new_plugin(m, tag="fix_n4",
                     plugin_admins=["aiocqhttp:GroupMessage:123"])._is_global_admin(
              FakeEvent(uid="123", gid="123")), False)

    # ---- N-5 清索引不再误删折叠冲突的另一个分类 ----
    pn5 = new_plugin(m, tag="fix_n5")
    conn_n5 = pn5._connect()
    conn_n5.execute("INSERT INTO categories (name, created_at) VALUES (?,?)", ("Hatsune", 0))
    conn_n5.execute("INSERT INTO categories (name, created_at) VALUES (?,?)", ("HATSUNE", 0))
    conn_n5.commit()
    conn_n5.close()
    pn5._db_tag_index_sync()
    conn_n5 = pn5._connect()
    # 删"Hatsune"：它的折叠键 "hatsune" 正被"HATSUNE"占着 ——
    # 旧实现会顺手按 key 再删一行，把对方也删掉。
    pn5._drop_tag_index_rows(conn_n5, "Hatsune")
    left_n5 = {r["canonical"] for r in conn_n5.execute("SELECT canonical FROM tag_index")}
    conn_n5.commit()
    conn_n5.close()
    check("N-5 清索引不误删折叠冲突的另一个分类", "HATSUNE" in left_n5, True)

    # ---- N-9 目标已有同图时，去重项不计入"已移入" ----
    pn9 = new_plugin(m, tag="fix_n9")
    it_n9 = {"data": PNG, "ext": ".png", "sha256": sha(PNG)}
    pn9._db_add("源组", [it_n9], "100", "u1", "auto")
    pn9._db_add("目标组", [it_n9], "100", "u1", "auto")
    moved_n9, err_n9 = pn9._db_category_delete("源组", "move", "目标组")
    check("N-9 去重项不计入已移入", moved_n9, 0)
    check("N-9 源分类仍被清掉", "源组" in pn9._db_tag_names(), False)
    check("N-9 目标图仍在", pn9._db_pick("目标组", 1) is not None, True)

    print()
    if failures:
        print(f"❌ {len(failures)} 项失败")
        for f in failures:
            print("  -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    _code = main()
    if _code == 0:
        shutil.rmtree(TMP, ignore_errors=True)
    else:
        print(f"（保留失败现场：{TMP}）")
    raise SystemExit(_code)
