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
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile

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
                 allowed_users=("10001",), allowed_groups=(), allow_all=False,
                 auto_inject_n=0, **conf_overrides):
        self.tmp = tmp
        self.embedder = FakeEmbedder() if embed else None
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

    on = Harness(main_mod, tmp, db_name="inject_on.db", auto_inject_n=2)
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
        await test_db_path(main_mod, tmp_root, chk)
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
