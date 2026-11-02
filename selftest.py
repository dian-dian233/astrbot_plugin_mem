#!/usr/bin/env python3
"""离线自测：假 astrbot 桩 + 假 embedder/reranker，**不打任何模型接口**。

    PYTHONDONTWRITEBYTECODE=1 python3 selftest.py --offline

覆盖：
  1. add → search 命中
  2. 同槽二次写 → 旧记录 valid_to 被填、search 只出新值
  3. forget 生效
  4. 超长 / 敏感词被拒
  5. 库损坏 → 友好降级（不抛 traceback）
  6. 非白名单写被拦、读放行；管理员 / 群白名单放行
  7. auto_inject_n=0 时确实不注入；n>0 时注入且每条截 200 字
  8. embedding 404/超时 → 退关键词，结果仍正确
  9. rerank 超时 → 退回向量排序
 10. 模型名变化 → 关键词兜底不崩
 11. embedding 缓存命中（数调用次数）
  + 参数解析单测、export/import 合并、db_path 默认位置

v0.3.0 新增（记忆生命周期 / RRF / consolidate）：
 12. plan 过 TTL 后不参与检索（假时钟注入 now）
 13. fact 30 天后仍在、但 decay 把分数压低
 14. linear / step / exp 三种衰减公式数值
 15. access_count 越高 decay 越慢，到 access_cap 封顶
 16. 衰减回合后 access_count 按 access_halve 回落
 17. importance >= protected_importance 的记录不衰减
 18. RRF：两路都命中的分数高于单路命中
 19. 某一路为空 → 只用另一路，结果不为空
 20. agreement_bonus：两路名次越接近加成越大
 21. consolidate：--dry-run 不改库；真跑后条数下降、importance 取 max、access 求和
 22. 老库（v0.2 表结构）自动补列，历史数据可读可检索
 23. plan 类型识别（线索词 / --type 覆盖）与 TTL 落库

v0.4.0 新增（从 LivingMemory 搬家 /mem import-lm）：
 24. 假 livingmemory.db（schema 照抄对方 storage/atom_store.py）端到端：读 5 条 → 导入 3 条
 25. 跳过：status=archived、内容重复；同槽新者胜、旧的不许盖新的
 26. 切分规则：长谓词优先 / 1<idx≤24 / 长叙事退回「遗留记忆 · 内容 · 原文」
 27. atom_type → mtype、decay_type → decay_type、reinforcement_count → access_count
 28. epoch 浮点 ↔ ISO 字符串（也兼容本来就是 ISO 的列）
 29. --dry-run 不改库、--limit 截断、缺列表也能导入、只读句柄写不进去
 30. 自动探测顺序 + 递归深度上限 + 跳过 site-packages；找不到库不抛 traceback
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_NAME = "astrbot_plugin_mem"

# ── 假 astrbot 桩 ──────────────────────────────────────────────────────

STUB_FILES = {
    "__init__.py": """
import logging
logger = logging.getLogger("astrbot")
""",
    "api/__init__.py": """
from astrbot import logger
__all__ = ["logger"]
""",
    "api/event/__init__.py": """
class AstrMessageEvent:
    pass

from astrbot.api.event import filter  # noqa: E402,F401
__all__ = ["AstrMessageEvent", "filter"]
""",
    "api/event/filter/__init__.py": '''
"""桩：只记录装饰器元数据，行为和真框架一致（函数原样返回）。"""


def command(name=None, alias=None, **kwargs):
    def deco(fn):
        fn.__command_name__ = name
        return fn
    return deco


def llm_tool(name=None, **kwargs):
    def deco(fn):
        fn.__llm_tool_name__ = name or fn.__name__
        return fn
    return deco


def on_llm_request(**kwargs):
    def deco(fn):
        fn.__on_llm_request__ = True
        return fn
    return deco
''',
    "api/provider/__init__.py": """
from dataclasses import dataclass, field


@dataclass
class ProviderRequest:
    prompt: str | None = None
    system_prompt: str = ""
    session_id: str = ""
    contexts: list = field(default_factory=list)
""",
    "api/star/__init__.py": """
class Context:
    pass


class Star:
    def __init__(self, context, config=None):
        self.context = context
        self.config = config or {}


def register(*args, **kwargs):
    def deco(cls):
        return cls
    return deco
""",
    "core/__init__.py": "",
    "core/utils/__init__.py": "",
    "core/utils/astrbot_path.py": """
import os


def get_astrbot_data_path() -> str:
    return os.environ.get("STUB_ASTRBOT_DATA", os.path.join(os.getcwd(), "data"))
""",
}


def install_stub(root: str) -> None:
    for rel, content in STUB_FILES.items():
        path = os.path.join(root, "astrbot", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content.lstrip("\n"))
    sys.path.insert(0, root)


def load_plugin():
    """按包名导入 main.py（插件内部是相对导入，必须走包）。"""
    if os.path.basename(HERE) != PKG_NAME:
        tmp = tempfile.mkdtemp(prefix="memrepo_")
        os.symlink(HERE, os.path.join(tmp, PKG_NAME), target_is_directory=True)
        sys.path.insert(0, tmp)
    else:
        sys.path.insert(0, os.path.dirname(HERE))
    import importlib

    return importlib.import_module(f"{PKG_NAME}.main")


# ── 假 provider / 事件 ─────────────────────────────────────────────────


class FakeRerankResult:
    """形状对齐 astrbot/core/provider/entities.py:609 的 RerankResult。"""

    __slots__ = ("index", "relevance_score")

    def __init__(self, index: int, relevance_score: float):
        self.index = index
        self.relevance_score = relevance_score


class FakeEmbedder:
    """内容 hash 生成确定性向量：字符相同的文本余弦更高（够测链路）。"""

    def __init__(self, dim: int = 32, pid: str = "fake-embed", model: str = "fake-embed-v1"):
        self.dim = dim
        self.model = model
        self.provider_config = {"id": pid}
        self.calls = 0
        self.fail: Exception | None = None

    async def get_embedding(self, text: str) -> list[float]:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        vec = [0.0] * self.dim
        for ch in str(text):
            vec[ord(ch) % self.dim] += 1.0
        return vec

    def get_dim(self) -> int:
        return self.dim


class VectorEmbedder:
    """按文本查表返回固定向量：用来精确构造余弦（测去重阈值 / 噪声地板边界）。

    查不到就用 default 向量。比 FakeEmbedder 的字符直方图可控，能凑出「cos 正好 0.97」。
    """

    def __init__(self, table=None, default=(1.0, 0.0, 0.0), pid="fake-vec", model="fake-vec-v1"):
        self.table = {str(k): [float(x) for x in v] for k, v in dict(table or {}).items()}
        self.default = [float(x) for x in default]
        self.model = model
        self.provider_config = {"id": pid}
        self.calls = 0
        self.fail: Exception | None = None

    async def get_embedding(self, text: str) -> list[float]:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        vec = self.table.get(str(text))
        return list(vec) if vec is not None else list(self.default)

    def get_dim(self) -> int:
        return len(self.default)


class FakeReranker:
    """按字符重合打分，模拟 rerank。"""

    def __init__(self, pid: str = "fake-rerank", model: str = "fake-rerank-v1"):
        self.model = model
        self.provider_config = {"id": pid}
        self.calls = 0
        self.fail: Exception | None = None

    async def rerank(self, query: str, documents: list[str], top_n: int | None = None):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        qset = set(str(query))
        scored = []
        for idx, doc in enumerate(documents):
            overlap = len(qset & set(doc)) / max(1, len(qset))
            scored.append((idx, round(overlap, 6)))
        scored.sort(key=lambda p: p[1], reverse=True)
        results = [FakeRerankResult(i, s) for i, s in scored]
        return results[:top_n] if top_n else results


class FakeProviderManager:
    def __init__(self, embed=None, rerank=None):
        self.embedding_provider_insts = [embed] if embed else []
        self.rerank_provider_insts = [rerank] if rerank else []


class FakeContext:
    def __init__(self, embed=None, rerank=None):
        self.provider_manager = FakeProviderManager(embed, rerank)
        self._by_id = {}
        for prov in (embed, rerank):
            if prov is not None:
                self._by_id[prov.provider_config["id"]] = prov

    def get_provider_by_id(self, provider_id):
        return self._by_id.get(provider_id)


class FakeEvent:
    def __init__(self, text: str, *, sender="10001", group="", admin=False):
        self.message_str = text
        self._sender = sender
        self._group = group
        self._admin = admin
        self.unified_msg_origin = f"test:FriendMessage:{sender}"

    def plain_result(self, text):
        return ("plain", text)

    def is_admin(self):
        return self._admin

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return self._group

    def get_platform_id(self):
        return "test"


# ── 测试脚手架 ─────────────────────────────────────────────────────────


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def ok(self, name: str, condition, detail: str = "") -> bool:
        if isinstance(condition, str) and isinstance(name, bool):  # 防手滑写反
            name, condition = condition, name
        if condition:
            self.passed += 1
            print(f"PASS  {name}")
            return True
        self.failed += 1
        print(f"FAIL  {name}" + (f"  ← {detail}" if detail else ""))
        return False

    def eq(self, actual, expected, name: str) -> bool:
        return self.ok(actual == expected, name, f"期望 {expected!r}，实际 {actual!r}")


async def collect(agen) -> str:
    parts = []
    async for item in agen:
        if isinstance(item, tuple) and len(item) == 2 and item[0] == "plain":
            parts.append(item[1])
        else:
            parts.append(str(item))
    return "\n".join(parts)


class Harness:
    def __init__(self, main_mod, tmp, *, db_name="mem.db", embed=True, rerank=True,
                 embedder=None, allowed_users=("10001",), allowed_groups=(), allow_all=False,
                 auto_inject_n=0, **conf_overrides):
        self.tmp = tmp
        self.embedder = embedder if embedder is not None else (FakeEmbedder() if embed else None)
        self.reranker = FakeReranker() if rerank else None
        self.ctx = FakeContext(self.embedder, self.reranker)
        self.conf = {
            "db_path": os.path.join(tmp, db_name),
            "scope": "global",
            "embed_provider_id": "",
            "rerank_provider_id": "",
            "recall_k": 30,
            "rerank_k": 5,
            "auto_inject_n": auto_inject_n,
            "allow_all": allow_all,
            "allowed_users": list(allowed_users),
            "allowed_groups": list(allowed_groups),
            # v0.2.0 新增（和 _conf_schema.json 默认值保持一致）
            "max_value_len": 120,
            "reject_narrative": True,
            "dedupe_threshold": 0.95,
            "min_score": 0.2,
            "inject_char_cap": 600,
            # v0.3.0 新增（和 _conf_schema.json 默认值保持一致）
            "default_mtype": "fact",
            "ttl_fact": 3650,
            "ttl_preference": 365,
            "ttl_event": 30,
            "ttl_plan": 2,
            "rrf_k": 60,
            "agreement_bonus": 0.02,
            "protected_importance": 0.9,
            "access_cap": 10,
            "access_halve": 0.5,
        }
        self.conf.update(conf_overrides)
        self.plugin = main_mod.MemPlugin(self.ctx, self.conf)

    @property
    def store(self):
        return self.plugin._get_store()

    async def cmd(self, text: str, **event_kwargs) -> str:
        return await collect(self.plugin.mem(FakeEvent(text, **event_kwargs)))


# ── 各条测试 ───────────────────────────────────────────────────────────


async def test_parse_units(main_mod, chk: Checker) -> None:
    chk.eq(main_mod.strip_prefix("mem add a b c"), "add a b c", "解析：剥 /mem 前缀")
    chk.eq(main_mod.strip_prefix("！记忆 search 星星"), "search 星星", "解析：剥中文别名前缀")
    chk.eq(main_mod.strip_prefix("/mem"), "", "解析：只有指令名")
    subject, predicate, value, scope = main_mod.parse_add("点点 喜欢 看星星")
    chk.eq((subject, predicate, value, scope), ("点点", "喜欢", "看星星", ""), "解析：add 三段")
    subject, predicate, value, scope = main_mod.parse_add("点点 喜欢 看星星 --scope group:123")
    chk.eq(scope, "group:123", "解析：--scope 分池")
    chk.eq(value, "看星星", "解析：--scope 不黏进内容")
    chk.eq(main_mod.parse_search("星星 3"), ("星星", 3), "解析：search 带数量")
    chk.eq(main_mod.parse_search("星星"), ("星星", 0), "解析：search 缺省数量")
    try:
        main_mod.parse_add("只有一个词")
        chk.ok("解析：缺参抛 ArgError", False)
    except main_mod.ArgError:
        chk.ok("解析：缺参抛 ArgError", True)
    chk.ok("校验：超长被拒", bool(main_mod.validate_fact("a", "b", "c" * 501)))
    chk.ok("校验：sk- 被拒", bool(main_mod.validate_fact("a", "b", "sk-abcdefgh123456")))
    chk.ok("校验：gho_ 被拒", bool(main_mod.validate_fact("a", "b", "gho_abcdefghijkl")))
    chk.ok("校验：私钥头被拒", bool(main_mod.validate_fact("a", "b", "-----BEGIN RSA")))
    chk.eq(main_mod.validate_fact("点点", "喜欢", "看星星"), "", "校验：正常内容放行")
    chk.eq(main_mod.clip("x" * 300, 200), "x" * 199 + "…", "工具：clip 截断")


def test_handler_signatures(main_mod, chk: Checker) -> None:
    """对着框架的解析规则检查 handler 签名（坑①：*args / **kwargs 会被当类型调用）。"""
    import inspect

    sig = inspect.signature(main_mod.MemPlugin.mem)
    kinds = {p.name: p.kind for p in sig.parameters.values()}
    chk.eq(list(kinds), ["self", "event"], "指令 handler 只收 self + event（没有 *args）")
    chk.ok("参数里没有 VAR_POSITIONAL / VAR_KEYWORD",
           all(k not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
               for k in kinds.values()), str(kinds))
    chk.eq(getattr(main_mod.MemPlugin.mem, "__command_name__", None), "mem", "指令名注册为 mem")

    for tool_name in ("memory_add", "memory_search"):
        fn = getattr(main_mod.MemPlugin, tool_name)
        chk.eq(getattr(fn, "__llm_tool_name__", None), tool_name, f"LLM 工具注册名 {tool_name}")
        params = list(inspect.signature(fn).parameters.values())
        chk.eq([p.name for p in params][:2], ["self", "event"], f"{tool_name} 前两个参数是 self/event")
        chk.ok(f"{tool_name} 参数都有类型注解",
               all(p.annotation is not inspect.Parameter.empty for p in params[2:]), str(params))

    hook = main_mod.MemPlugin.inject_memory
    chk.ok("inject_memory 挂了 on_llm_request 钩子", bool(getattr(hook, "__on_llm_request__", False)))
    chk.eq([p.name for p in inspect.signature(hook).parameters.values()], ["self", "event", "req"],
           "注入钩子签名 (self, event, req)")


async def test_add_search_overwrite_forget(main_mod, tmp, chk: Checker) -> None:
    h = Harness(main_mod, tmp, db_name="basic.db")

    r = await h.cmd("mem")
    chk.ok("/mem 无参数 → 帮助", "/mem add" in r and "/mem search" in r, r)

    r = await h.cmd("mem add 点点 喜欢 看星星")
    chk.ok("add 写入成功", "已记住" in r and "看星星" in r, r)
    await h.cmd("mem add 小满 也喜欢 看星星")  # 两条候选，rerank 才会真的跑

    r = await h.cmd("mem add 点点 喜欢")
    chk.ok("add 缺参 → 友好报错", "参数不够" in r and "Traceback" not in r, r)

    r = await h.cmd("mem search 星星")
    chk.ok("search 命中", "看星星" in r, r)
    chk.ok("search 走向量 + 重排", "向量 + 重排" in r, r)

    old_id = [row for row in h.store.active_rows() if row["predicate"] == "喜欢"][0]["id"]
    r = await h.cmd("mem add 点点 喜欢 看月亮")
    chk.ok("同槽覆盖提示", "作废" in r, r)
    old = h.store.get(old_id)
    chk.ok("旧记录 valid_to 被填", bool(old.get("valid_to")), str(old))
    chk.ok("旧记录 superseded_by 指向新记录", bool(old.get("superseded_by")), str(old))

    r = await h.cmd("mem search 点点")
    chk.ok("覆盖后只出新值", "看月亮" in r and "看星星" not in r, r)
    chk.eq(h.store.stats()["active"], 2, "覆盖后有效记录 2 条（另一条没被误伤）")

    r = await h.cmd("mem forget 月亮")
    chk.ok("forget 生效", "已作废 1 条" in r, r)
    chk.eq(h.store.stats()["active"], 1, "forget 只作废命中的那条")
    r = await h.cmd("mem search 月亮")
    chk.ok("forget 后检索不到", "没检索到" in r, r)

    r = await h.cmd("mem forget 不存在的东西")
    chk.ok("forget 无命中友好提示", "没找到" in r, r)


async def test_rejections(main_mod, tmp, chk: Checker) -> None:
    h = Harness(main_mod, tmp, db_name="reject.db")
    r = await h.cmd("mem add 点点 喜欢 " + "长" * 501)
    chk.ok("超长内容拒写", "内容太长" in r, r)
    r = await h.cmd("mem add " + "长" * 41 + " 喜欢 看星星")
    chk.ok("超长主语拒写", "主语太长" in r, r)
    r = await h.cmd("mem add 服务器 密钥 sk-abcdefgh123456")
    chk.ok("敏感串拒写（sk-）", "拒绝写入" in r, r)
    r = await h.cmd("mem add 服务器 令牌 AKIAABCDEFGH1234")
    chk.ok("敏感串拒写（AKIA）", "拒绝写入" in r, r)
    chk.eq(h.store.stats()["active"], 0, "被拒的都没落库")


async def test_corrupt_db(main_mod, tmp, chk: Checker) -> None:
    bad = os.path.join(tmp, "broken.db")
    with open(bad, "wb") as fh:
        fh.write(b"this is definitely not a sqlite database" * 200)
    h = Harness(main_mod, tmp, db_path=bad)
    r = await h.cmd("mem list")
    chk.ok("库损坏 → list 回「记忆库读不了」", "记忆库读不了" in r, r)
    chk.ok("库损坏 → 没有 traceback", "Traceback" not in r, r)
    r = await h.cmd("mem add 点点 喜欢 看星星")
    chk.ok("库损坏 → add 也友好降级", "记忆库读不了" in r, r)
    r = await h.cmd("mem search 星星")
    chk.ok("库损坏 → search 也友好降级", "记忆库读不了" in r, r)


async def test_permissions(main_mod, tmp, chk: Checker) -> None:
    h = Harness(main_mod, tmp, db_name="perm.db", allowed_users=(), allowed_groups=())
    r = await h.cmd("mem add 小明 喜欢 苹果", sender="99999")
    chk.ok("非白名单写被拦", "没权限写记忆" in r, r)
    chk.eq(h.store.stats()["active"], 0, "被拦后没写进库")

    r = await h.cmd("mem list", sender="99999")
    chk.ok("非白名单读放行", "记忆库还是空的" in r, r)

    r = await h.cmd("mem add 小明 喜欢 苹果", sender="99999", admin=True)
    chk.ok("管理员写放行", "已记住" in r, r)

    h2 = Harness(main_mod, tmp, db_name="perm2.db", allowed_users=(), allowed_groups=("88888",))
    r = await h2.cmd("mem add 群友 喜欢 摸鱼", sender="77777", group="88888")
    chk.ok("群白名单写放行", "已记住" in r, r)

    h3 = Harness(main_mod, tmp, db_name="perm3.db", allowed_users=("12345",))
    r = await h3.cmd("mem add 白名单用户 喜欢 睡觉", sender="12345")
    chk.ok("用户白名单写放行", "已记住" in r, r)

    h4 = Harness(main_mod, tmp, db_name="perm4.db", allowed_users=(), allow_all=True)
    r = await h4.cmd("mem add 路人 喜欢 围观", sender="55555")
    chk.ok("allow_all 全开写放行", "已记住" in r, r)


async def test_injection(main_mod, tmp, chk: Checker) -> None:
    from astrbot.api.provider import ProviderRequest

    off = Harness(main_mod, tmp, db_name="inject_off.db", auto_inject_n=0)
    await off.cmd("mem add 点点 喜欢 看星星")
    req = ProviderRequest(system_prompt="你是点点。")
    await off.plugin.inject_memory(FakeEvent("星星好看吗"), req)
    chk.eq(req.system_prompt, "你是点点。", "auto_inject_n=0 → 一点记忆都不注入")
    chk.ok("auto_inject_n=0 → prompt 里没有记忆段", "长期记忆" not in req.system_prompt)

    on = Harness(main_mod, tmp, db_name="inject_on.db", auto_inject_n=2, max_value_len=500)
    # ↑ max_value_len 显式放宽到 500：这条 302 字的值是历史数据形状，用来测注入裁剪
    #   （v0.2.0 默认 120 字会直接拒写；导入/旧库仍可能有超长值）
    await on.cmd("mem add 点点 喜欢 看星星")
    await on.cmd("mem add 点点 口头禅 星星" + "雨" * 300)
    req2 = ProviderRequest(system_prompt="你是点点。")
    await on.plugin.inject_memory(FakeEvent("星星"), req2)
    chk.ok("auto_inject_n>0 → 注入记忆段", "[长期记忆" in req2.system_prompt, req2.system_prompt)
    chk.ok("注入保留原 prompt", req2.system_prompt.startswith("你是点点。"), req2.system_prompt)
    chk.ok("注入每条截到 200 字", "雨" * 200 not in req2.system_prompt and "…" in req2.system_prompt,
           req2.system_prompt[-300:])


async def test_degradation(main_mod, tmp, chk: Checker) -> None:
    h = Harness(main_mod, tmp, db_name="degrade.db")
    await h.cmd("mem add 点点 喜欢 看星星")
    await h.cmd("mem add 点点 讨厌 下雨")

    h.embedder.fail = RuntimeError("HTTP 404 Not Found")
    r = await h.cmd("mem search 星星")
    chk.ok("embedding 404 → 关键词兜底仍命中", "看星星" in r, r)
    chk.ok("embedding 404 → 模式标成仅关键词", "仅关键词" in r, r)
    h.embedder.fail = asyncio.TimeoutError("embedding timed out")
    r = await h.cmd("mem search 星星")
    chk.ok("embedding 超时 → 关键词兜底仍命中", "看星星" in r, r)
    h.embedder.fail = None

    h.reranker.fail = asyncio.TimeoutError("rerank timed out")
    r = await h.cmd("mem search 星星")
    chk.ok("rerank 超时 → 结果照常返回", "看星星" in r, r)
    chk.ok("rerank 超时 → 标成仅向量", "仅向量" in r, r)
    h.reranker.fail = None

    # 模型名变化 → 缓存全部失效 → 现算失败 → 关键词兜底
    h.embedder.model = "fake-embed-v2"
    h.embedder.fail = RuntimeError("model fake-embed-v2 not found")
    r = await h.cmd("mem search 星星")
    chk.ok("换模型名 + 调用失败 → 关键词兜底不崩", "看星星" in r and "仅关键词" in r, r)
    h.embedder.model = "fake-embed-v1"
    h.embedder.fail = None

    # 一个 provider 都没有
    bare = Harness(main_mod, tmp, db_name="bare.db", embed=False, rerank=False)
    await bare.cmd("mem add 点点 喜欢 看星星")
    r = await bare.cmd("mem search 星星")
    chk.ok("没配 provider → 纯关键词照常检索", "看星星" in r and "仅关键词" in r, r)
    r = await bare.cmd("mem stats")
    chk.ok("stats 标出 embedding 未配置", "embedding：未配置" in r, r)


async def test_llm_tools(main_mod, tmp, chk: Checker) -> None:
    h = Harness(main_mod, tmp, db_name="tools.db")
    r = await h.plugin.memory_add(FakeEvent(""), "点点", "喜欢", "看星星")
    chk.ok("工具 memory_add 写入成功", "已记住" in r, r)
    r = await h.plugin.memory_search(FakeEvent(""), "星星")
    chk.ok("工具 memory_search 命中", "看星星" in r, r)
    r = await h.plugin.memory_add(FakeEvent(""), "服务器", "密码", "sk-abcdefgh123456")
    chk.ok("工具 memory_add 也拦敏感串", "拒绝写入" in r, r)

    blocked = Harness(main_mod, tmp, db_name="tools_perm.db", allowed_users=(), allowed_groups=())
    r = await blocked.plugin.memory_add(FakeEvent("", sender="99999"), "小明", "喜欢", "苹果")
    chk.ok("工具 memory_add 非白名单被拦", "没权限写记忆" in r, r)
    r = await blocked.plugin.memory_search(FakeEvent("", sender="99999"), "苹果")
    chk.ok("工具 memory_search 读放行", "没有相关记忆" in r, r)


async def test_vector_cache(main_mod, tmp, chk: Checker) -> None:
    h = Harness(main_mod, tmp, db_name="cache.db")
    await h.cmd("mem add 点点 喜欢 看星星")
    await h.cmd("mem add 点点 讨厌 下雨")
    h.embedder.calls = 0
    await h.cmd("mem search 星星")
    first = h.embedder.calls
    chk.ok("首次检索调了 embedding", first >= 3, f"calls={first}")
    await h.cmd("mem search 星星")
    delta = h.embedder.calls - first
    chk.eq(delta, 1, "第二次只算查询向量（条目向量全部命中缓存）")


async def test_export_import(main_mod, tmp, chk: Checker) -> None:
    h = Harness(main_mod, tmp, db_name="io.db")
    await h.cmd("mem add 点点 喜欢 看星星")
    path = os.path.join(tmp, "export.json")
    r = await h.cmd(f"mem export {path}")
    chk.ok("export 落盘", os.path.exists(path) and "已导出 1 条" in r, r)
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    chk.eq(len(payload["items"]), 1, "export 文件里有 1 条")

    r = await h.cmd(f"mem import {path}")
    chk.ok("重复导入全部跳过", "新增 0" in r and "跳过 1" in r, r)

    newer = {
        "items": [
            {
                "scope": "global",
                "subject": "点点",
                "predicate": "喜欢",
                "value": "看雪花",
                "updated_at": "2099-01-01T00:00:00+08:00",
            }
        ]
    }
    newer_path = os.path.join(tmp, "newer.json")
    with open(newer_path, "w", encoding="utf-8") as fh:
        json.dump(newer, fh, ensure_ascii=False)
    r = await h.cmd(f"mem import {newer_path}")
    chk.ok("更新的记录覆盖旧槽", "覆盖 1" in r, r)
    r = await h.cmd("mem search 雪花")
    chk.ok("导入的新值可检索", "看雪花" in r, r)

    old_path = os.path.join(tmp, "older.json")
    with open(old_path, "w", encoding="utf-8") as fh:
        json.dump({"items": [{"subject": "点点", "predicate": "喜欢", "value": "看星星",
                              "updated_at": "2000-01-01T00:00:00+08:00"}]}, fh, ensure_ascii=False)
    r = await h.cmd(f"mem import {old_path}")
    chk.ok("更旧的记录被跳过", "跳过 1" in r, r)
    chk.eq(h.store.stats()["active"], 1, "合并后仍只有 1 条有效")


async def test_write_hygiene(main_mod, tmp, chk: Checker) -> None:
    """写入降噪：限长 120 / 拒叙事体 / 归一化。"""
    h = Harness(main_mod, tmp, db_name="hygiene.db")
    r = await h.cmd("mem add 点点 键值 " + "星" * 121)
    chk.ok("121 字拒写", "内容太长" in r and "≤120" in r, r)
    r = await h.cmd("mem add 点点 键值 " + "星" * 120)
    chk.ok("120 字通过", "已记住" in r, r)

    r = await h.cmd("mem add 点点 聊天 我今天跟他聊了记忆")
    chk.ok("叙事体「我今天…」拒写", "像日记" in r and "已记住" not in r, r)
    r = await h.cmd("mem add 点点 存储 点点的记忆库在 sqlite")
    chk.ok("判断句「点点的记忆库在 sqlite」放行", "已记住" in r, r)
    chk.eq(h.store.stats()["active"], 2, "被拒的没落库（只留 120 字 + 判断句 2 条）")

    chk.eq(
        main_mod.normalize_text("  点点  住在\u3000friend-box "),
        "点点 住在 friend-box",
        "归一化：全角空格→半角、连续空白压一个、去首尾",
    )
    chk.eq(
        main_mod.normalize_text("点点，住在 friend-box。"),
        "点点，住在 friend-box。",
        "归一化：中文标点不动",
    )

    r = await h.plugin.memory_add(FakeEvent(""), "  点点  ", "住在", " friend-box\u3000A ")
    chk.ok("工具写入也归一化", "已记住" in r, r)
    row = [x for x in h.store.active_rows() if x["predicate"] == "住在"][0]
    chk.eq(row["subject"], "点点", "主语 strip 后落库")
    chk.eq(row["value"], "friend-box A", "value 全角空格转半角后落库")


async def test_write_dedupe(main_mod, tmp, chk: Checker) -> None:
    """写入相似去重：同 scope + 同 subject，cos ≥ 0.95 走覆盖。"""
    # cos([1,0,0], [0.97,0.243,0]) ≈ 0.97 ≥ 0.95；cos([1,0,0], [0.8,0.6,0]) = 0.8 < 0.95
    emb = VectorEmbedder({
        "点点的记忆库在 sqlite": [1.0, 0.0, 0.0],
        "点点的记忆库跑 sqlite": [0.97, 0.243, 0.0],
        "点点的记忆库在 mysql": [0.8, 0.6, 0.0],
    })
    h = Harness(main_mod, tmp, db_name="dedupe_w.db", embedder=emb, rerank=False)
    r1 = await h.cmd("mem add 点点 存储 点点的记忆库在 sqlite")
    chk.ok("第一条正常写入", "已记住" in r1, r1)
    old_id = int(r1.split("#")[1].split(" ")[0])

    r2 = await h.cmd("mem add 点点 位置 点点的记忆库跑 sqlite")
    chk.ok("同主语 cos 0.97 → 覆盖（提示作废）", "已作废" in r2, r2)
    chk.eq(h.store.stats()["active"], 1, "相似去重后只剩 1 条")
    left = h.store.active_rows()[0]
    chk.eq(left["value"], "点点的记忆库跑 sqlite", "留下的是新值")
    old = h.store.get(old_id)
    chk.ok(
        "旧记录 valid_to + superseded_by 都填了",
        bool(old.get("valid_to")) and int(old.get("superseded_by") or 0) == int(left["id"]),
        str(old),
    )

    r3 = await h.cmd("mem add 点点 库 点点的记忆库在 mysql")
    chk.ok("cos 0.8 < 0.95 → 不合并", "已作废" not in r3 and h.store.stats()["active"] == 2, r3)

    no_emb = Harness(main_mod, tmp, db_name="dedupe_noemb.db", embed=False, rerank=False)
    r = await no_emb.cmd("mem add 点点 喜欢 看星星")
    chk.ok(
        "没有 embedding provider → 写入照常（去重跳过）",
        "已记住" in r and no_emb.store.stats()["active"] == 1,
        r,
    )

    bad_emb = VectorEmbedder({})
    bad_emb.fail = RuntimeError("HTTP 500 embedding down")
    broken = Harness(main_mod, tmp, db_name="dedupe_broken.db", embedder=bad_emb, rerank=False)
    r = await broken.cmd("mem add 点点 喜欢 看月亮")
    chk.ok(
        "embedding 挂了也不阻塞写入",
        "已记住" in r and broken.store.stats()["active"] == 1,
        r,
    )

    off = Harness(main_mod, tmp, db_name="dedupe_off.db", embedder=emb, rerank=False,
                  dedupe_threshold=0)
    await off.cmd("mem add 点点 存储 点点的记忆库在 sqlite")
    r = await off.cmd("mem add 点点 位置 点点的记忆库跑 sqlite")
    chk.ok("dedupe_threshold=0 → 关闭相似去重", "已作废" not in r, r)
    chk.eq(off.store.stats()["active"], 2, "关闭后两条并存")

    # 新值向量要回填缓存：否则同主语的 N 次写入会退化成 O(N²) 次 embedding 调用
    emb2 = VectorEmbedder({
        "版本甲": [1.0, 0.0, 0.0],
        "版本乙": [0.0, 0.0, 1.0],
        "版本丙": [0.0, 1.0, 0.0],
    })
    cache = Harness(main_mod, tmp, db_name="dedupe_cache.db", embedder=emb2, rerank=False)
    for tag, val in (("甲", "版本甲"), ("乙", "版本乙"), ("丙", "版本丙")):
        await cache.cmd(f"mem add 机器 第{tag}版 {val}")
    chk.eq(emb2.calls, 3, "同主语写 3 条只调 3 次 embedding（旧值命中缓存）")


async def test_search_hygiene(main_mod, tmp, chk: Checker) -> None:
    """检索净化：(subject, predicate) 去重 + updated_at 展示 + 作废标注。"""
    h = Harness(main_mod, tmp, db_name="search_clean.db", rerank=False)
    ids = []
    for scope, val in (("global", "版本一"), ("group:1", "版本二"), ("user:1", "版本三")):
        ids.append(h.store.add(scope, "部署", "版本", val, source="test")["id"])
    with sqlite3.connect(h.store.path) as conn:  # 同一秒写入时间戳会撞，手动拉开
        conn.execute(
            "UPDATE mem SET updated_at=? WHERE id=?", ("2099-01-01T00:00:00+08:00", ids[-1])
        )

    r = await h.cmd("mem search 版本")
    chk.eq(r.count("#"), 1, "同 (主语,谓词) 三条历史 → 输出只 1 条")
    chk.ok("留的是 updated_at 最新那条", "版本三" in r and "版本一" not in r, r)
    chk.ok("结果带更新时间", "更新 2099-01-01 00:00" in r, r)
    chk.ok(
        "作废记录标注「已作废」",
        "已作废" in main_mod.format_row(
            {"id": 9, "scope": "global", "subject": "a", "predicate": "b", "value": "c",
             "valid_to": "2026-01-01T00:00:00+08:00", "updated_at": "2026-01-01T00:00:00+08:00"}
        ),
    )


async def test_min_score(main_mod, tmp, chk: Checker) -> None:
    """噪声地板可配：min_score=0.9 时弱相关被滤掉。"""
    emb = VectorEmbedder({
        "月亮": [1.0, 0.0, 0.0],
        "小满 也喜欢 看星星": [0.3, 0.954, 0.0],  # 余弦 0.3：关键词一个没中
    })
    loose = Harness(main_mod, tmp, db_name="minscore.db", embedder=emb, rerank=False)
    await loose.cmd("mem add 小满 也喜欢 看星星")
    r = await loose.cmd("mem search 月亮")
    chk.ok("min_score=0.2 → 余弦 0.3 的弱相关还留着", "看星星" in r, r)

    strict = Harness(main_mod, tmp, db_name="minscore.db", embedder=emb, rerank=False,
                     min_score=0.9)
    r = await strict.cmd("mem search 月亮")
    chk.ok("min_score=0.9 → 弱相关被滤掉", "没检索到" in r, r)


async def test_dedupe_cmd(main_mod, tmp, chk: Checker) -> None:
    """/mem dedupe [--dry-run]：扫全库同槽重复。"""
    h = Harness(main_mod, tmp, db_name="dedupe_cmd.db", rerank=False)
    path = h.store.path
    with sqlite3.connect(path) as conn:  # 直接造 v0.1.0 遗留的同槽重复（add 本身不会重复）
        for val, ts in (
            ("旧甲", "2026-01-01T00:00:00+08:00"),
            ("旧乙", "2026-02-01T00:00:00+08:00"),
            ("新丙", "2026-03-01T00:00:00+08:00"),
        ):
            conn.execute(
                "INSERT INTO mem(scope,subject,predicate,value,confidence,source,"
                "created_at,updated_at,valid_from) VALUES(?,?,?,?,?,?,?,?,?)",
                ("global", "服务器", "系统", val, 0.8, "test", ts, ts, ts),
            )
    chk.eq(h.store.stats()["active"], 3, "先造 3 条同槽重复")

    r = await h.cmd("mem dedupe --dry-run")
    chk.ok("--dry-run 报告「扫描 3 条，可合并 1 组」", "扫描 3 条，可合并 1 组" in r, r)
    chk.ok("--dry-run 明说没改库", "dry-run）没改库" in r, r)
    chk.eq(h.store.stats()["active"], 3, "--dry-run 不改库")

    r = await h.cmd("mem dedupe")
    chk.ok("真跑：作废 2 条", "已作废 2 条" in r, r)
    chk.eq(h.store.stats()["active"], 1, "真跑后只剩 1 条有效")
    chk.eq(h.store.active_rows()[0]["value"], "新丙", "保留 updated_at 最新的那条")

    r = await h.cmd("mem dedupe")
    chk.ok("再跑一次：无可合并", "可合并 0 组" in r, r)
    r = await h.cmd("mem dedupe --oops")
    chk.ok("不认识参数 → 友好报错", "不认识的参数" in r and "Traceback" not in r, r)


async def test_inject_cap(main_mod, tmp, chk: Checker) -> None:
    """注入：按 subject 去重 + inject_char_cap 按 updated_at 倒序砍。"""
    from astrbot.api.provider import ProviderRequest

    h = Harness(main_mod, tmp, db_name="inject_cap.db", auto_inject_n=5,
                inject_char_cap=120, rerank=False)
    await h.cmd("mem add 甲机 状态 星" + "A" * 100)
    await h.cmd("mem add 乙机 状态 星" + "B" * 100)
    req = ProviderRequest(system_prompt="你是点点。")
    await h.plugin.inject_memory(FakeEvent("星"), req)
    chk.ok("超 inject_char_cap → 丢掉较旧的那条", "A" * 20 not in req.system_prompt,
           req.system_prompt)
    chk.ok("最新的那条保留", "B" * 20 in req.system_prompt, req.system_prompt)

    by_subject = Harness(main_mod, tmp, db_name="inject_subj.db", auto_inject_n=3, rerank=False)
    await by_subject.cmd("mem add 点点 喜欢 看星星")
    await by_subject.cmd("mem add 点点 讨厌 下雨")
    await by_subject.cmd("mem add 小满 喜欢 看月亮")
    req2 = ProviderRequest(system_prompt="你是点点。")
    await by_subject.plugin.inject_memory(FakeEvent("喜欢"), req2)
    block = req2.system_prompt.split("[长期记忆")[-1]
    chk.eq(block.count("- "), 2, "注入按 subject 去重：同主语只留 1 条")
    chk.ok("另一个主语照常注入", "小满" in block, block)


async def test_db_path(main_mod, tmp, chk: Checker) -> None:
    store = main_mod.resolve_db_path
    data_dir = os.path.join(tmp, "astrbot_data")
    os.makedirs(data_dir, exist_ok=True)
    old_env = os.environ.get("STUB_ASTRBOT_DATA")
    os.environ["STUB_ASTRBOT_DATA"] = data_dir
    try:
        path = store("")
    finally:
        if old_env is None:
            os.environ.pop("STUB_ASTRBOT_DATA", None)
        else:
            os.environ["STUB_ASTRBOT_DATA"] = old_env
    chk.eq(os.path.dirname(path), os.path.realpath(data_dir), "db_path 留空 → AstrBot data 目录")
    chk.ok("db_path 留空 → 文件名 mem.db", path.endswith("mem.db"), path)

    inside = store(os.path.join(HERE, "data", "mem.db"))
    chk.ok("db_path 指插件目录 → 拒绝并改默认", not inside.startswith(os.path.realpath(HERE) + os.sep), inside)


# ── v0.3.0：记忆生命周期 / RRF / consolidate ──────────────────────────


def _mod(name: str):
    """按包名取兄弟模块（load_plugin 之后它们已经在 sys.modules 里）。"""
    return sys.modules[f"{PKG_NAME}.{name}"]


async def test_lifecycle_plan_ttl(main_mod, tmp, chk: Checker) -> None:
    """12. plan 类型 TTL=2 天，过期不参与检索（假时钟注入 now）。"""
    h = Harness(main_mod, tmp, db_name="life_plan.db", rerank=False)
    r = await h.cmd("mem add 点点 待办 明天交周报")
    chk.ok("线索词「明天」→ 判为 plan", "[plan]" in r, r)
    row = h.store.active_rows()[0]
    chk.eq(row["mtype"], "plan", "plan 类型落库")
    chk.eq(float(row["ttl_days"]), 2.0, "plan 默认 TTL 2 天")
    chk.eq(str(row["decay_type"]), "step", "plan 用 step 衰减")

    now = datetime.now(timezone.utc).astimezone()
    retriever = h.plugin._build_retriever(h.store)
    today = await retriever.search("周报", now=now)
    chk.ok("plan 当天能检索到", bool(today.rows), str(today.rows))
    later = await retriever.search("周报", now=now + timedelta(days=3))
    chk.ok("plan 3 天后不再出现在检索结果", not later.rows, str(later.rows))
    chk.eq(h.store.stats()["active"], 1, "过期不物理删（只是不参与检索）")


async def test_lifecycle_fact_decay(main_mod, tmp, chk: Checker) -> None:
    """13. fact 30 天后仍在，但 decay 把分数压低。"""
    h = Harness(main_mod, tmp, db_name="life_fact.db", rerank=False)
    await h.cmd("mem add 服务器 部署在 阿里云")
    row = h.store.active_rows()[0]
    chk.eq(row["mtype"], "fact", "默认类型是 fact")
    chk.eq(float(row["ttl_days"]), 3650.0, "fact 默认 TTL 3650 天")
    chk.eq(str(row["decay_type"]), "exp", "fact 用 exp 衰减")

    short = Harness(main_mod, tmp, db_name="life_fact30.db", rerank=False, ttl_fact=30)
    await short.cmd("mem add 服务器 部署在 阿里云")
    now = datetime.now(timezone.utc).astimezone()
    got = await short.plugin._build_retriever(short.store).search(
        "阿里云", now=now + timedelta(days=30)
    )
    chk.ok("fact 30 天后仍在（exp 只降权不剔除）", bool(got.rows), str(got.rows))
    decay = float(got.rows[0]["decay"]) if got.rows else 0.0
    chk.ok("TTL 30 天、放了 30 天 → decay ≈ 0.25", abs(decay - 0.25) < 0.02, str(decay))
    chk.ok(
        "decay 把分数压到 1 以下",
        bool(got.rows) and float(got.rows[0]["score"]) < 1.0,
        str(got.rows[:1]),
    )


def test_lifecycle_decay_formulas(main_mod, chk: Checker) -> None:
    """14. linear / step / exp 三种衰减公式数值。"""
    lc = _mod("lifecycle")
    chk.eq(round(lc.decay_factor("linear", 10, 5), 6), 0.5, "linear：过一半 TTL → 0.5")
    chk.eq(round(lc.decay_factor("linear", 10, 20), 6), 0.0, "linear：过 TTL → 0（不会负数）")
    chk.eq(lc.decay_factor("step", 2, 2), 1.0, "step：TTL 内 → 1.0")
    chk.eq(lc.decay_factor("step", 2, 2.5), 0.05, "step：过 TTL → 0.05")
    chk.eq(round(lc.decay_factor("exp", 10, 5), 6), 0.5, "exp：半衰期 = TTL/2 → 0.5")
    chk.eq(round(lc.decay_factor("exp", 10, 10), 6), 0.25, "exp：一个 TTL 后 → 0.25")


def test_lifecycle_access_boost(main_mod, chk: Checker) -> None:
    """15. access_count 越高 decay 越慢，到 access_cap 封顶。"""
    lc = _mod("lifecycle")
    base = lc.decay_factor("exp", 30, 15)  # 0.5
    d0 = lc.effective_decay(base, 0, 10)
    d5 = lc.effective_decay(base, 5, 10)
    d10 = lc.effective_decay(base, 10, 10)
    d99 = lc.effective_decay(base, 99, 10)
    chk.eq(round(d0, 6), 0.5, "没被访问过 → 原衰减")
    chk.ok(d5 > d0, "access_count 提高 → decay 变慢")
    chk.eq(round(d5, 6), 0.75, "5/10 次 → 0.5 + 0.5*0.5")
    chk.eq(round(d10, 6), 1.0, "到 access_cap → 不再衰减")
    chk.eq(round(d99, 6), 1.0, "超过 access_cap 不再变慢")


async def test_lifecycle_access_halve(main_mod, tmp, chk: Checker) -> None:
    """16. 衰减回合后 access_count 按 access_halve 回落（小时级节流）。"""
    h = Harness(main_mod, tmp, db_name="life_halve.db", rerank=False)
    hit = h.store.add("global", "甲机", "状态", "在线")["id"]
    idle = h.store.add("global", "乙机", "状态", "离线")["id"]
    with sqlite3.connect(h.store.path) as conn:
        conn.execute("UPDATE mem SET access_count=8 WHERE id=?", (hit,))
        conn.execute("UPDATE mem SET access_count=3 WHERE id=?", (idle,))

    skipped = h.store.apply_decay_pass()
    chk.ok("一小时内重复调用 → 跳过（meta 记时间戳）", bool(skipped["skipped"]), str(skipped))

    report = h.store.apply_decay_pass(force=True)
    chk.eq(report["updated"], 2, "两条非保护记录都回落")
    chk.eq(int(h.store.get(hit)["access_count"]), 4, "8 → 4（×0.5）")
    chk.eq(int(h.store.get(idle)["access_count"]), 1, "3 → 1（整数截断）")


def test_lifecycle_protected(main_mod, chk: Checker) -> None:
    """17. importance >= protected_importance 的记录不衰减。"""
    lc = _mod("lifecycle")
    now = datetime.now(timezone.utc).astimezone()
    row = {
        "importance": 0.95,
        "decay_type": "exp",
        "ttl_days": 10,
        "created_at": (now - timedelta(days=30)).isoformat(),
        "access_count": 0,
    }
    old_score, old_decay = lc.final_score(1.0, row, now=now)
    fresh_score, _ = lc.final_score(1.0, dict(row, created_at=now.isoformat()), now=now)
    chk.eq(old_decay, 1.0, "importance 0.95 → decay 不生效")
    chk.ok(abs(old_score - fresh_score) < 1e-9, "放 30 天与当天分数完全一致")

    plain_score, plain_decay = lc.final_score(1.0, dict(row, importance=0.5), now=now)
    chk.ok(plain_decay < 0.1, "同条件 importance 0.5 → 衰减到 0.1 以下")
    chk.ok(plain_score < old_score, "普通记录分数被压低")

    expired_plan = {
        "importance": 0.95, "decay_type": "step", "ttl_days": 2,
        "created_at": (now - timedelta(days=9)).isoformat(),
    }
    chk.ok(
        not lc.is_expired(expired_plan, now=now),
        "高 importance 的 plan 也不会因过期被剔除",
    )

def test_rrf_dual_route(main_mod, chk: Checker) -> None:
    """18. RRF：两路都命中 > 只有单路命中。"""
    ret = _mod("retrieve")
    fused = ret.rrf_fuse([1, 2], [1, 3], k=60, agreement_bonus=0.0)
    chk.ok(fused[1]["score"] > fused[2]["score"], "双路第 1 > 单路（向量）第 2")
    chk.ok(fused[1]["score"] > fused[3]["score"], "双路第 1 > 单路（关键词）第 2")
    chk.ok(abs(fused[1]["score"] - 2.0 / 61) < 1e-9, "双路都排第 1 → 1/61 + 1/61")
    chk.eq(fused[2]["vec_rank"], 1, "向量路名次记录正确")
    chk.eq(fused[2]["kw_rank"], None, "没走关键词路 → kw_rank 为空")


async def test_rrf_empty_route(main_mod, tmp, chk: Checker) -> None:
    """19. 某一路为空 → 只用另一路，不拿空路稀释。"""
    ret = _mod("retrieve")
    solo = ret.rrf_fuse([], [7, 8], k=60, agreement_bonus=0.02)
    chk.ok(7 in solo and 8 in solo, "关键词单路也能融合出结果")
    chk.ok(solo[7]["score"] > solo[8]["score"], "单路按名次给分")
    chk.eq(solo[7]["vec_rank"], None, "空路没有 rank，不贡献也不稀释")
    chk.ok(abs(solo[7]["score"] - 1.0 / 61) < 1e-9, "空路时分数就是 1/(k+1)")
    chk.eq(ret.rrf_fuse([], [], k=60), {}, "两路全空 → 空结果")

    h = Harness(main_mod, tmp, db_name="solo_route.db", embed=False, rerank=False)
    await h.cmd("mem add 点点 喜欢 看星星")
    r = await h.cmd("mem search 星星")
    chk.ok("端到端：向量路空 → 关键词路照样出结果", "看星星" in r, r)
    chk.ok("融合信息标明向量路 0 条", "向量 0" in r, r)


def test_rrf_agreement_bonus(main_mod, chk: Checker) -> None:
    """20. agreement_bonus：两路都中且名次接近 → 分数更高。"""
    ret = _mod("retrieve")
    off = ret.rrf_fuse([1, 2, 3], [3, 2, 1], k=60, agreement_bonus=0.0)
    on = ret.rrf_fuse([1, 2, 3], [3, 2, 1], k=60, agreement_bonus=0.02)
    chk.ok(on[2]["score"] > off[2]["score"], "名次差距 0 → 拿到加成")
    chk.ok(on[1]["score"] > off[1]["score"], "名次差距 2 → 也有加成")
    gap0 = on[2]["score"] - off[2]["score"]
    gap2 = on[1]["score"] - off[1]["score"]
    chk.ok(gap0 > gap2, f"名次越接近加成越大（{gap0:.4f} > {gap2:.4f}）")
    chk.ok(abs(gap0 - 0.02) < 1e-9, "gap=0 时加满 agreement_bonus")


async def test_consolidate(main_mod, tmp, chk: Checker) -> None:
    """21. consolidate：--dry-run 不改库；真跑后合并 + importance max + access 求和。"""
    emb = VectorEmbedder({
        "看星星": [1.0, 0.0, 0.0],
        "看星星和月亮": [0.99, 0.14, 0.0],
        "看星星呀": [0.98, 0.2, 0.0],
    })
    h = Harness(main_mod, tmp, db_name="consolidate.db", embedder=emb, rerank=False)
    first = h.store.add("global", "点点", "喜欢", "看星星", importance=0.4)["id"]
    keeper = h.store.add("global", "点点", "爱好", "看星星和月亮", importance=0.8)["id"]
    third = h.store.add("global", "点点", "偏好", "看星星呀", importance=0.6)["id"]
    with sqlite3.connect(h.store.path) as conn:
        for mem_id, count in ((first, 1), (keeper, 2), (third, 3)):
            conn.execute("UPDATE mem SET access_count=? WHERE id=?", (count, mem_id))
    chk.eq(h.store.stats()["active"], 3, "先造 3 条同主语相近记录")

    r = await h.cmd("mem consolidate --dry-run")
    chk.ok("--dry-run 报告「可合并 1 组」", "可合并 1 组" in r, r)
    chk.ok("--dry-run 明说没改库", "没改库" in r, r)
    chk.eq(h.store.stats()["active"], 3, "--dry-run 不改库")
    chk.ok(not h.store.get(first)["valid_to"], "--dry-run 不填 valid_to")

    r = await h.cmd("mem consolidate")
    chk.ok("真跑：作废 2 条", "已作废 2 条" in r, r)
    chk.eq(h.store.stats()["active"], 1, "真跑后只剩 1 条有效")
    kept = h.store.get(keeper)
    chk.ok(not kept["valid_to"], "保留行没被作废")
    chk.eq(kept["value"], "看星星和月亮", "保留 value 最完整（最长）的那条")
    chk.eq(float(kept["importance"]), 0.8, "importance 取组内 max")
    chk.eq(int(kept["access_count"]), 6, "access_count 求和 1+2+3")
    chk.ok(bool(h.store.get(first)["valid_to"]), "其余记录填 valid_to")
    chk.eq(int(h.store.get(first)["superseded_by"]), keeper, "superseded_by 指向保留行")

    r = await h.cmd("mem consolidate")
    chk.ok("再跑一次 → 可合并 0 组", "可合并 0 组" in r, r)


async def test_migration_from_v02(main_mod, tmp, chk: Checker) -> None:
    """22. 老库（v0.2 六列 schema）启动自动补列，历史数据可读可检索。"""
    path = os.path.join(tmp, "legacy_v02.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
CREATE TABLE mem(
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL, subject TEXT NOT NULL, predicate TEXT NOT NULL,
  value TEXT NOT NULL, confidence REAL DEFAULT 0.8, source TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  valid_from TEXT NOT NULL, valid_to TEXT, superseded_by INTEGER
);
CREATE INDEX ix_mem_slot ON mem(scope, subject, predicate, valid_to);
CREATE TABLE vec(
  mem_id INTEGER NOT NULL, model TEXT NOT NULL, dim INTEGER NOT NULL,
  vec BLOB NOT NULL, text_hash TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(mem_id, model)
);
INSERT INTO mem(scope,subject,predicate,value,created_at,updated_at,valid_from)
VALUES('global','老服务器','系统','v0.2 留下的数据',
       '2025-01-01T00:00:00+08:00','2025-01-01T00:00:00+08:00','2025-01-01T00:00:00+08:00');
"""
    )
    conn.commit()
    conn.close()

    h = Harness(main_mod, tmp, db_path=path, rerank=False)
    chk.ok("缺的列都被补上", "mtype" in h.store.migrated_columns, str(h.store.migrated_columns))
    row = h.store.active_rows()[0]
    chk.eq(row["mtype"], "fact", "老数据默认 fact")
    chk.eq(float(row["ttl_days"]), 3650.0, "老数据默认 TTL 3650 天")
    chk.eq(int(row["access_count"]), 0, "老数据 access_count=0")
    chk.eq(float(row["importance"]), 0.5, "老数据 importance=0.5")

    r = await h.cmd("mem search 老服务器")
    chk.ok("迁移后老数据照样能检索", "v0.2 留下的数据" in r, r)
    r = await h.cmd("mem stats")
    chk.ok("stats 显示类型分布", "类型：" in r and "fact" in r, r)


async def test_mtype_resolution(main_mod, tmp, chk: Checker) -> None:
    """23. plan 识别（线索词 / --type 覆盖）与各类型 TTL 落库。"""
    h = Harness(main_mod, tmp, db_name="mtype.db", rerank=False)
    r = await h.cmd("mem add 点点 待办 明天交周报")
    chk.ok("线索词 → plan", "[plan]" in r, r)
    r = await h.cmd("mem add 点点 喜欢 看星星")
    chk.ok("普通陈述 → fact", "[fact]" in r, r)
    r = await h.cmd("mem add 点点 计划 下周一发版 --type event")
    chk.ok("--type 覆盖线索词", "[event]" in r, r)
    r = await h.cmd("mem add 点点 习惯 喝美式 --type preference")
    chk.ok("--type preference", "[preference]" in r, r)

    by_pred = {row["predicate"]: row for row in h.store.active_rows()}
    chk.eq(float(by_pred["计划"]["ttl_days"]), 30.0, "event TTL 30 天")
    chk.eq(float(by_pred["习惯"]["ttl_days"]), 365.0, "preference TTL 365 天")
    chk.eq(float(by_pred["待办"]["ttl_days"]), 2.0, "plan TTL 2 天")
    r = await h.cmd("mem add 点点 备注 随便 --type 不认识的类型")
    chk.ok("未知类型 → 回落默认 fact", "[fact]" in r, r)


# ── v0.4.0：从 LivingMemory 搬家（/mem import-lm） ─────────────────────
#
# 全程离线：自己造一个假 livingmemory.db（schema 照抄对方 storage/atom_store.py:59），
# 不打任何模型接口，也不碰真人的库。

LM_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_atoms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_memory_id INTEGER NOT NULL DEFAULT 0,
  atom_type TEXT NOT NULL DEFAULT 'unknown',
  content TEXT NOT NULL,
  entities TEXT DEFAULT '[]',
  importance REAL NOT NULL DEFAULT 0.5,
  confidence REAL NOT NULL DEFAULT 0.7,
  created_at REAL NOT NULL DEFAULT 0,
  last_accessed_at REAL NOT NULL DEFAULT 0,
  last_reinforced_at REAL,
  event_time REAL,
  ttl_days REAL NOT NULL DEFAULT 30.0,
  expires_at REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  reinforcement_count INTEGER NOT NULL DEFAULT 0,
  decay_type TEXT NOT NULL DEFAULT 'exponential',
  session_id TEXT,
  persona_id TEXT,
  metadata TEXT DEFAULT '{}'
)
"""

EPOCH = 1700000000.0          # 2023-11-14T22:13:20Z，随便挑的一个整点
EPOCH_ISO = "2023-11-14T22:13:20"


def make_lm_db(path: str, rows, *, schema: str = LM_SCHEMA, table: str = "memory_atoms") -> str:
    """造一个假的 LivingMemory 库（schema 和对方一致，列可以缺）。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(schema)
        for row in rows:
            keys = list(row)
            conn.execute(
                f"INSERT INTO {table}({','.join(keys)}) VALUES({','.join('?' * len(keys))})",
                [row[k] for k in keys],
            )
        conn.commit()
    finally:
        conn.close()
    return path


def lm_rows():
    """5 条：3 条该进、1 条非活跃、1 条和第一条内容重复（切不动的那条走退回整体）。"""
    return [
        {
            "id": 1, "atom_type": "semantic", "content": "服务器的 SSH 端口是 22022",
            "importance": 0.9, "confidence": 0.8, "created_at": EPOCH,
            "last_accessed_at": EPOCH + 86400, "ttl_days": 3650.0,
            "decay_type": "exponential", "status": "active", "reinforcement_count": 3,
            "session_id": "qq:FriendMessage:1", "persona_id": "点点",
        },
        {
            "id": 2, "atom_type": "planned", "content": "明天要交周报",
            "importance": 0.6, "confidence": 0.7, "created_at": EPOCH,
            "last_accessed_at": EPOCH, "ttl_days": 2.0,
            "decay_type": "step", "status": "active", "reinforcement_count": 0,
        },
        {
            "id": 3, "atom_type": "episodic", "content": "那天风很大我们走了很久才到家",
            "importance": 0.4, "confidence": 0.6, "created_at": EPOCH,
            "last_accessed_at": EPOCH, "ttl_days": 30.0,
            "decay_type": "exponential", "status": "active",
        },
        {
            "id": 4, "atom_type": "semantic", "content": "服务器的 SSH 端口是 22022",
            "importance": 0.5, "created_at": EPOCH + 60, "ttl_days": 3650.0,
            "decay_type": "exponential", "status": "archived",
        },
        {
            "id": 5, "atom_type": "unknown", "content": "服务器的 SSH 端口是 22022",
            "importance": 0.3, "created_at": EPOCH + 120, "ttl_days": 30.0,
            "decay_type": "exponential", "status": "active",
        },
    ]


def lm_module():
    return _mod("livingmemory")


def _active(store, subject, predicate=None):
    for row in store.active_rows():
        if row["subject"] == subject and (predicate is None or row["predicate"] == predicate):
            return row
    return {}

def test_import_lm_units(main_mod, chk: Checker) -> None:
    """纯函数：切分规则 / 类型映射 / 时间转换 / 参数解析。"""
    lm = lm_module()

    chk.eq(lm.split_content("服务器的 SSH 端口是 22022"),
           ("服务器的 SSH 端口", "是", "22022", True), "切分：是 → 三元组")
    chk.eq(lm.split_content("明天要交周报"),
           ("明天", "要", "交周报", True), "切分：要 → 三元组")
    chk.eq(lm.split_content("点点住在 A 房间"),
           ("点点", "住在", "A 房间", True), "切分：长谓词「住在」优先于「住」「在」")
    chk.eq(lm.split_content("我在读书"),
           ("遗留记忆", "内容", "我在读书", False), "切分：主语只有 1 字（idx=1）不切")
    too_long = "长" * 25 + "是值"
    chk.eq(lm.split_content(too_long),
           ("遗留记忆", "内容", too_long, False), "切分：命中位置超过 24 字不切")
    chk.eq(lm.split_content("那天风很大我们走了很久才到家"),
           ("遗留记忆", "内容", "那天风很大我们走了很久才到家", False), "切分：长叙事退回整体")
    chk.eq(lm.split_content(""), ("遗留记忆", "内容", "", False), "切分：空内容也不炸")

    chk.eq(lm.to_iso(EPOCH)[:19], EPOCH_ISO, "时间：epoch 浮点 → ISO")
    chk.eq(lm.to_iso(str(EPOCH))[:19], EPOCH_ISO, "时间：纯数字串也当 epoch")
    chk.ok("时间：ISO 字符串没被当 epoch（年份不是 1970）",
           lm.to_iso("2026-10-27T06:25:00+00:00").startswith("2026"))
    chk.eq(lm.to_iso("不是时间", default="FALLBACK"), "FALLBACK", "时间：认不出来回默认值")
    chk.eq(lm.to_iso(None, default=""), "", "时间：None 不炸")

    columns = set(lm.ATOM_COLUMNS)
    planned = lm.map_atom({"atom_type": "planned", "content": "明天要交周报"}, columns)
    chk.eq(planned["mtype"], "plan", "类型：planned → plan")
    chk.eq(float(planned["ttl_days"]), 2.0, "类型：planned 缺 ttl_days 时用本插件默认 2 天")
    chk.eq(planned["decay_type"], "step", "类型：planned 缺 decay_type 时用 step")

    for atom_type, expected in (("episodic", "event"), ("semantic", "fact"),
                                ("procedural", "preference"), ("unknown", "fact"),
                                ("垃圾类型", "fact")):
        mapped = lm.map_atom({"atom_type": atom_type, "content": "点点是机器人"}, columns)
        chk.eq(mapped["mtype"], expected, f"类型：{atom_type} → {expected}")

    chk.eq(lm.map_atom({"content": "x", "status": "archived"}, columns)["skip"], "inactive",
           "跳过：status=archived")
    chk.eq(lm.map_atom({"content": "   "}, columns)["skip"], "empty", "跳过：空内容")

    chk.eq(lm.map_atom({"content": "点点住在 A", "decay_type": "linear"}, columns)["decay_type"],
           "linear", "衰减：linear 带过")
    chk.eq(lm.map_atom({"content": "点点住在 A", "decay_type": "step"}, columns)["decay_type"],
           "step", "衰减：step 带过")
    chk.eq(lm.map_atom({"content": "点点住在 A", "decay_type": "奇怪"}, columns)["decay_type"],
           "exp", "衰减：认不出来 → exp")

    dropped = lm.map_atom(
        {"content": "点点住在 A", "session_id": "s1", "persona_id": "p1"}, columns)
    chk.ok("丢弃计数：session_id 命中", dropped["dropped_session"] is True)
    chk.ok("丢弃计数：persona_id 命中", dropped["dropped_persona"] is True)
    chk.ok("丢弃：session_id 没被塞进 value",
           "s1" not in f"{dropped['subject']}{dropped['predicate']}{dropped['value']}")

    # 缺列：只有 id/content/importance 也必须能映射
    thin_cols = {"id", "content", "importance"}
    thin = lm.map_atom({"id": 1, "content": "服务器需要用 22022 端口", "importance": 0.8},
                       thin_cols)
    chk.eq(thin["mtype"], "fact", "缺列：没 atom_type 也回 fact")
    chk.eq(float(thin["ttl_days"]), 3650.0, "缺列：没 ttl_days 用默认 3650")
    chk.eq(thin["access_count"], 0, "缺列：没 reinforcement_count 当 0")
    chk.ok("缺列：没 created_at 就用现在", bool(thin["created_at"]))

    chk.eq(main_mod.parse_import_lm(""),
           {"dry_run": False, "path": "", "limit": 0}, "解析：无参数 → 全默认")
    chk.eq(main_mod.parse_import_lm("--dry-run"),
           {"dry_run": True, "path": "", "limit": 0}, "解析：--dry-run")
    chk.eq(main_mod.parse_import_lm("--path /tmp/lm.db --limit 10 --dry-run"),
           {"dry_run": True, "path": "/tmp/lm.db", "limit": 10}, "解析：三个参数一起")
    chk.eq(main_mod.parse_import_lm('--path "/app/Program Files/livingmemory.db"')["path"],
           "/app/Program Files/livingmemory.db", "解析：路径带空格可以加引号")
    chk.eq(main_mod.parse_import_lm('--path "/a b/lm.db" --dry-run'),
           {"dry_run": True, "path": "/a b/lm.db", "limit": 0}, "解析：引号路径 + 其它参数")
    for bad in ("--limit 0", "--limit abc", "--dryrun", "--path"):
        try:
            main_mod.parse_import_lm(bad)
            chk.ok(f"解析：`{bad}` 报错", False)
        except main_mod.ArgError:
            chk.ok(f"解析：`{bad}` 报错", True)

async def test_import_lm_end_to_end(main_mod, tmp, chk: Checker) -> None:
    """主链路：5 条假数据 → 导入 3、跳过 1 非活跃 / 1 重复；类型 / TTL / 时间都对。"""
    lm_path = make_lm_db(os.path.join(tmp, "lm_e2e", "livingmemory.db"), lm_rows())
    h = Harness(main_mod, tmp, db_name="mem_e2e.db", rerank=False)

    text = await h.cmd(f"mem import-lm --path {lm_path}")
    chk.ok("搬家：输出带库路径", lm_path in text, text)
    chk.ok("搬家：读入 5 条", "读入 5 条" in text, text)
    chk.ok("搬家：导入 3 条（切 2 / 退回 1）",
           "导入 3 条（切出三元组 2 / 退回整体 1）" in text, text)
    chk.ok("搬家：跳过 1 重复 + 1 非活跃",
           "跳过：已存在重复 1、非活跃 1" in text, text)
    chk.ok("搬家：输出有时耗行", "用时" in text, text)
    chk.ok("搬家：一条消息不刷屏", len(text.splitlines()) <= 8, text)

    chk.eq(h.store.stats()["active"], 3, "搬家：库里有效记录 3 条")
    chk.eq(h.store.stats()["history"], 0, "搬家：没有历史行（没走覆盖）")

    planned = _active(h.store, "明天", "要")
    chk.eq(planned.get("mtype"), "plan", "搬家：planned → mtype=plan")
    chk.eq(float(planned.get("ttl_days") or 0), 2.0, "搬家：planned 的 ttl 2 天")
    chk.eq(str(planned.get("decay_type")), "step", "搬家：planned 用 step")

    split_row = _active(h.store, "服务器的 SSH 端口", "是")
    chk.eq(split_row.get("value"), "22022", "搬家：切出三元组（值是 22022）")
    chk.eq(float(split_row.get("importance") or 0), 0.9, "搬家：importance 原样带过")
    chk.eq(int(split_row.get("access_count") or 0), 3, "搬家：reinforcement_count → access_count")
    chk.eq(str(split_row.get("created_at"))[:19], EPOCH_ISO, "搬家：epoch 转出的 ISO 时间正确")
    chk.eq(str(split_row.get("source")), "livingmemory", "搬家：来源标记为 livingmemory")

    fallback = _active(h.store, "遗留记忆", "内容")
    chk.eq(fallback.get("value"), "那天风很大我们走了很久才到家", "搬家：切不动的退回整体")

    chk.ok("搬家：session_id / persona_id 计数上报",
           "丢弃：" in text and "session_id 1 条" in text, text)

    # 对方的库一个字节都不许改
    conn = sqlite3.connect(lm_path)
    try:
        chk.eq(conn.execute("SELECT COUNT(*) FROM memory_atoms").fetchone()[0], 5,
               "搬家：对方的库行数没变")
        chk.eq(conn.execute(
            "SELECT COUNT(*) FROM memory_atoms WHERE status='archived'").fetchone()[0], 1,
            "搬家：对方的库状态没被改")
    finally:
        conn.close()

    again = await h.cmd(f"mem import-lm --path {lm_path}")
    chk.ok("搬家：二次导入全是重复", "导入 0 条" in again and "已存在重复 4" in again, again)


async def test_import_lm_dry_run(main_mod, tmp, chk: Checker) -> None:
    """--dry-run 只统计不改库（报出来的数还得和真跑一致）。"""
    lm_path = make_lm_db(os.path.join(tmp, "lm_dry", "livingmemory.db"), lm_rows())
    h = Harness(main_mod, tmp, db_name="mem_dry.db", rerank=False)

    text = await h.cmd(f"mem import-lm --path {lm_path} --dry-run")
    chk.ok("dry-run：输出标明只统计", "--dry-run" in text, text)
    chk.ok("dry-run：统计口径和真跑一致",
           "导入 3 条（切出三元组 2 / 退回整体 1）" in text, text)
    chk.eq(h.store.stats()["active"], 0, "dry-run：库里一条没多")

    await h.cmd(f"mem import-lm --path {lm_path}")
    chk.eq(h.store.stats()["active"], 3, "dry-run 之后真跑：3 条进库")


async def test_import_lm_missing_db(main_mod, tmp, chk: Checker) -> None:
    """库不存在 → 报「没找到」+ 列出搜过的位置，不抛 traceback。"""
    empty = os.path.join(tmp, "lm_missing", "empty_data")
    os.makedirs(empty, exist_ok=True)
    h = Harness(main_mod, tmp, db_name="mem_missing.db", rerank=False)
    h.plugin._data_dirs = lambda: [empty]

    text = await h.cmd("mem import-lm")
    chk.ok("找不到库：说人话", "没找到 LivingMemory 的库" in text, text)
    chk.ok("找不到库：给出 --path 补救办法", "--path" in text, text)
    chk.ok("找不到库：列出搜过的位置",
           "plugin_data/astrbot_plugin_livingmemory/livingmemory.db" in text, text)
    chk.ok("找不到库：没有 traceback", "Traceback" not in text, text)

    text = await h.cmd(f"mem import-lm --path {os.path.join(tmp, 'nope', 'livingmemory.db')}")
    chk.ok("找不到库：--path 指到不存在的文件也列出该路径",
           "nope/livingmemory.db" in text, text)


def test_import_lm_readonly(main_mod, tmp, chk: Checker) -> None:
    """只读打开：mode=ro 的句柄写不进去。"""
    lm = lm_module()
    lm_path = make_lm_db(os.path.join(tmp, "lm_ro", "livingmemory.db"), lm_rows()[:1])
    conn = lm.open_ro(lm_path)
    try:
        try:
            conn.execute("INSERT INTO memory_atoms(content) VALUES('偷偷写一条')")
            chk.ok("只读：INSERT 被拒", False)
        except sqlite3.OperationalError as e:
            chk.ok("只读：INSERT 被拒", "readonly" in str(e).lower(), str(e))
        chk.eq(conn.execute("SELECT COUNT(*) FROM memory_atoms").fetchone()[0], 1,
               "只读：读得到数据")
    finally:
        conn.close()
    try:
        lm.open_ro(os.path.join(tmp, "lm_ro", "not-here.db"))
        chk.ok("只读：库不存在抛 LivingMemoryError", False)
    except lm.LivingMemoryError:
        chk.ok("只读：库不存在抛 LivingMemoryError", True)

def test_import_lm_probe(main_mod, tmp, chk: Checker) -> None:
    """自动探测顺序：固定候选 → 递归（≤4 层，跳过 site-packages）→ 找不到。"""
    lm = lm_module()
    data = os.path.join(tmp, "lm_probe", "data")
    os.makedirs(data, exist_ok=True)

    chk.eq(lm.probe([data])["path"], "", "探测：空目录找不到")

    fixed = make_lm_db(
        os.path.join(data, "plugin_data", "astrbot_plugin_livingmemory", "livingmemory.db"),
        lm_rows()[:1])
    found = lm.probe([data])
    chk.eq(found["path"], os.path.realpath(fixed),
           "探测：命中 plugin_data/astrbot_plugin_livingmemory")
    chk.eq(found["source"], "AstrBot data 目录", "探测：来源标为 data 目录")
    chk.ok("探测：搜过的位置按顺序记下来",
           found["tried"][0].endswith("plugin_data/astrbot_plugin_livingmemory/livingmemory.db"),
           found["tried"][:1])

    configured = make_lm_db(os.path.join(tmp, "lm_probe", "manual.db"), lm_rows()[:1])
    chk.eq(lm.probe([data], configured=configured)["path"], os.path.realpath(configured),
           "探测：配置 lm_db_path 优先")
    manual_dir = os.path.join(tmp, "lm_probe_dir")
    manual = make_lm_db(os.path.join(manual_dir, "livingmemory.db"), lm_rows()[:1])
    chk.eq(lm.probe([data], configured=manual_dir)["path"], os.path.realpath(manual),
           "探测：配置给目录时自动补 livingmemory.db")

    # 只有递归才找得到的位置
    deep = os.path.join(tmp, "lm_deep", "data")
    deep_db = make_lm_db(os.path.join(deep, "a", "b", "c", "livingmemory.db"), lm_rows()[:1])
    found = lm.probe([deep])
    chk.eq(found["path"], os.path.realpath(deep_db), "探测：递归兜底找得到（深度 3）")
    chk.ok("探测：递归命中的来源写清楚", "递归" in found["source"], found["source"])

    # 超过 4 层不找
    too_deep = os.path.join(tmp, "lm_too_deep", "data")
    make_lm_db(os.path.join(too_deep, "d1", "d2", "d3", "d4", "d5", "livingmemory.db"),
               lm_rows()[:1])
    chk.eq(lm.probe([too_deep])["path"], "", "探测：超过 4 层不递归")

    # site-packages 里同名的是依赖自带的，不是运行时数据
    skipped = os.path.join(tmp, "lm_skip", "data")
    make_lm_db(os.path.join(skipped, "site-packages", "x", "livingmemory.db"), lm_rows()[:1])
    chk.eq(lm.probe([skipped])["path"], "", "探测：跳过 site-packages")

    tried = lm.probe([skipped])["tried"]
    chk.ok("探测：找不到时 tried 里有全部固定候选",
           all(p in tried for p in lm.candidate_paths([skipped])), tried)


async def test_import_lm_missing_columns(main_mod, tmp, chk: Checker) -> None:
    """缺列场景：只有 id/content/importance 也要能导入，不整批失败。"""
    schema = """
    CREATE TABLE IF NOT EXISTS memory_atoms (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      content TEXT NOT NULL,
      importance REAL DEFAULT 0.5
    )
    """
    lm_path = make_lm_db(
        os.path.join(tmp, "lm_thin", "livingmemory.db"),
        [{"content": "服务器需要用 22022 端口"}, {"content": "点点喜欢喝乌龙茶"}],
        schema=schema)
    h = Harness(main_mod, tmp, db_name="mem_thin.db", rerank=False)

    text = await h.cmd(f"mem import-lm --path {lm_path}")
    chk.ok("缺列：导入不崩", "导入 2 条" in text, text)
    chk.ok("缺列：报告里说明补了几列", "缺 " in text and "列" in text, text)
    chk.eq(h.store.stats()["active"], 2, "缺列：两条都进库")
    row = _active(h.store, "服务器", "需要")
    chk.eq(row.get("mtype"), "fact", "缺列：没有 atom_type → fact")
    chk.eq(float(row.get("ttl_days") or 0), 3650.0, "缺列：没有 ttl_days → 3650")
    chk.ok("缺列：没有 created_at 也写了时间", bool(row.get("created_at")))


async def test_import_lm_override(main_mod, tmp, chk: Checker) -> None:
    """同槽：新值覆盖旧值（留历史）；比库里现有记录旧的不许盖。"""
    lm_path = make_lm_db(
        os.path.join(tmp, "lm_slot", "livingmemory.db"),
        [
            {"atom_type": "semantic", "content": "点点住在 A 房间", "created_at": EPOCH},
            {"atom_type": "semantic", "content": "点点住在 B 房间", "created_at": EPOCH + 600},
        ])

    h = Harness(main_mod, tmp, db_name="mem_slot.db", rerank=False)
    text = await h.cmd(f"mem import-lm --path {lm_path}")
    chk.ok("同槽：报出覆盖 1 条", "覆盖同槽旧值 1 条" in text, text)
    chk.eq(h.store.stats()["active"], 1, "同槽：有效记录只留 1 条")
    chk.eq(h.store.stats()["history"], 1, "同槽：旧值进历史（不物理删）")
    chk.eq(_active(h.store, "点点", "住在").get("value"), "B 房间", "同槽：新者胜")

    # 库里已有一条更「新」的同槽记录 → 老数据不许盖掉它
    h2 = Harness(main_mod, tmp, db_name="mem_slot2.db", rerank=False)
    h2.store.add("global", "点点", "住在", "C 房间", mtype="fact",
                 created_at="2099-01-01T00:00:00+00:00")
    text = await h2.cmd(f"mem import-lm --path {lm_path} --limit 1")
    chk.ok("同槽：比现有记录旧的不覆盖", "旧于现有 1" in text, text)
    chk.eq(_active(h2.store, "点点", "住在").get("value"), "C 房间", "同槽：现有新值没被冲掉")


async def test_import_lm_batch_state(main_mod, tmp, chk: Checker) -> None:
    """跨批次：620 条（>1 批 500）时 dry-run 的数字必须和真跑一模一样。

    踩过的坑：dry-run 每批回滚，跨批次去重状态不传出去 → 第二批看不到第一批写过的内容，
    报出来的「重复」比真跑少。`Store.import_records(state=...)` 就是为了这个。
    """
    rows = []
    for i in range(620):
        content = f"用户{i}的偏好是深色主题"
        if i == 5:
            content = "重复的一条事实是同一个值"
        if i == 508:  # 跨过 500 边界，且和 #5 内容完全一样
            content = "重复的一条事实是同一个值"
        rows.append({"atom_type": "semantic", "content": content,
                     "created_at": EPOCH + i, "ttl_days": 3650.0})
    lm_path = make_lm_db(os.path.join(tmp, "lm_batch", "livingmemory.db"), rows)

    dry = Harness(main_mod, tmp, db_name="mem_batch_dry.db", rerank=False)
    dry_text = await dry.cmd(f"mem import-lm --path {lm_path} --dry-run")
    chk.eq(dry.store.stats()["active"], 0, "跨批次：dry-run 一条没写")

    real = Harness(main_mod, tmp, db_name="mem_batch_real.db", rerank=False)
    real_text = await real.cmd(f"mem import-lm --path {lm_path}")
    chk.eq(real.store.stats()["active"], 619, "跨批次：620 条里 1 条重复 → 619 条")

    def numbers(text):
        return [line for line in text.splitlines() if line.startswith(("读入", "跳过", "表："))]

    chk.eq(numbers(dry_text), numbers(real_text), "跨批次：dry-run 的统计和真跑完全一致")
    chk.ok("跨批次：重复被认出来（跨 500 边界）", "已存在重复 1" in real_text, real_text)


async def test_import_lm_limit(main_mod, tmp, chk: Checker) -> None:
    """--limit N 只读前 N 条。"""
    lm_path = make_lm_db(os.path.join(tmp, "lm_limit", "livingmemory.db"), lm_rows())
    h = Harness(main_mod, tmp, db_name="mem_limit.db", rerank=False)
    text = await h.cmd(f"mem import-lm --path {lm_path} --limit 2")
    chk.ok("limit：只读 2 条", "读入 2 条" in text, text)
    chk.ok("limit：报告里说明截断", "--limit 2" in text, text)
    chk.eq(h.store.stats()["active"], 2, "limit：只进了 2 条")


# ── 入口 ───────────────────────────────────────────────────────────────


async def run_all() -> int:
    chk = Checker()
    tmp_root = tempfile.mkdtemp(prefix="mem_selftest_")

    stub_root = os.path.join(tmp_root, "stub")
    os.makedirs(stub_root, exist_ok=True)
    install_stub(stub_root)
    main_mod = load_plugin()

    try:
        await test_parse_units(main_mod, chk)
        test_handler_signatures(main_mod, chk)
        await test_add_search_overwrite_forget(main_mod, tmp_root, chk)
        await test_rejections(main_mod, tmp_root, chk)
        await test_corrupt_db(main_mod, tmp_root, chk)
        await test_permissions(main_mod, tmp_root, chk)
        await test_injection(main_mod, tmp_root, chk)
        await test_degradation(main_mod, tmp_root, chk)
        await test_llm_tools(main_mod, tmp_root, chk)
        await test_vector_cache(main_mod, tmp_root, chk)
        await test_export_import(main_mod, tmp_root, chk)
        await test_write_hygiene(main_mod, tmp_root, chk)
        await test_write_dedupe(main_mod, tmp_root, chk)
        await test_search_hygiene(main_mod, tmp_root, chk)
        await test_min_score(main_mod, tmp_root, chk)
        await test_dedupe_cmd(main_mod, tmp_root, chk)
        await test_inject_cap(main_mod, tmp_root, chk)
        await test_db_path(main_mod, tmp_root, chk)
        # v0.3.0：生命周期 / RRF / consolidate / 迁移
        await test_lifecycle_plan_ttl(main_mod, tmp_root, chk)
        await test_lifecycle_fact_decay(main_mod, tmp_root, chk)
        test_lifecycle_decay_formulas(main_mod, chk)
        test_lifecycle_access_boost(main_mod, chk)
        await test_lifecycle_access_halve(main_mod, tmp_root, chk)
        test_lifecycle_protected(main_mod, chk)
        test_rrf_dual_route(main_mod, chk)
        await test_rrf_empty_route(main_mod, tmp_root, chk)
        test_rrf_agreement_bonus(main_mod, chk)
        await test_consolidate(main_mod, tmp_root, chk)
        await test_migration_from_v02(main_mod, tmp_root, chk)
        await test_mtype_resolution(main_mod, tmp_root, chk)
        # v0.4.0：从 LivingMemory 搬家
        test_import_lm_units(main_mod, chk)
        await test_import_lm_end_to_end(main_mod, tmp_root, chk)
        await test_import_lm_dry_run(main_mod, tmp_root, chk)
        await test_import_lm_missing_db(main_mod, tmp_root, chk)
        test_import_lm_readonly(main_mod, tmp_root, chk)
        test_import_lm_probe(main_mod, tmp_root, chk)
        await test_import_lm_missing_columns(main_mod, tmp_root, chk)
        await test_import_lm_override(main_mod, tmp_root, chk)
        await test_import_lm_limit(main_mod, tmp_root, chk)
        await test_import_lm_batch_state(main_mod, tmp_root, chk)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    total = chk.passed + chk.failed
    print(f"\n{chk.passed}/{total} 通过" + ("" if not chk.failed else f"，{chk.failed} 条失败"))
    return 0 if chk.failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="astrbot_plugin_mem 离线自测")
    parser.add_argument("--offline", action="store_true", help="不联网、不打模型接口（唯一模式）")
    parser.parse_args()
    return asyncio.run(run_all())


if __name__ == "__main__":
    sys.exit(main())
