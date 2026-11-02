"""AstrBot 全局记忆插件：任何会话（群聊 / 私聊 / 换号）共用同一份记忆。

指令（前缀 `/` 或 `!`，唤醒前缀由框架剥掉，这里再兜一层）：

    /mem add <主语> <谓词> <内容> [--scope group:123]
    /mem search <关键词> [数量]
    /mem list [数量]
    /mem forget <关键词>
    /mem stats
    /mem export [文件路径]
    /mem import <文件路径>

LLM 工具：memory_add / memory_search（`@filter.llm_tool`）。

注入策略：默认 `auto_inject_n = 0`，**不往 prompt 里塞任何记忆**；配成 N>0 才每轮
注入 top-N 条、每条截 200 字。原因见 README「为什么默认不注入」。

API 出处（都已 grep 过 AstrBot 4.29.3 源码，不凭记忆）：
    - Star / Context：astrbot/api/star/__init__.py
    - command / llm_tool / on_llm_request：astrbot/api/event/filter/__init__.py
    - 指令 handler 不声明参数，参数自己从 event.message_str 剥（避免
      `_empty() takes no arguments`，见 SKILL.md 坑①）
    - Context.get_provider_by_id：astrbot/core/star/context.py:456（同步方法）
    - provider_manager.embedding_provider_insts / rerank_provider_insts：
      astrbot/core/provider/manager.py:70-72（加载时填充于 847/856 行）
    - EmbeddingProvider.get_embedding / get_dim：astrbot/core/provider/provider.py:336/346
    - RerankProvider.rerank(query, documents, top_n)：provider.py:431
    - RerankResult(index, relevance_score)：astrbot/core/provider/entities.py:609
    - ProviderRequest.system_prompt：astrbot/core/provider/entities.py:212
    - 事件方法 is_admin / get_sender_id / get_group_id / plain_result：
      astrbot/core/platform/astr_message_event.py:319/254/246/455
"""

from __future__ import annotations

import os
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .retrieve import Retriever
from .store import Store, StoreError, resolve_db_path

MAX_VALUE = 500
MAX_NAME = 40

CMD_PREFIX = re.compile(r"^[/!！]?\s*(?:mem|记忆)\b\s*", re.IGNORECASE)
SCOPE_FLAG = re.compile(r"(?:^|\s)--scope\s+(\S+)\s*$", re.IGNORECASE)

# 凭证/私钥特征串：命中就拒写（凭证走配置项，不许进记忆库）
SENSITIVE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{6,}"
    r"|gho_[A-Za-z0-9]{6,}"
    r"|ibssh_[A-Za-z0-9]{4,}"
    r"|AKIA[0-9A-Z]{6,}"
    r"|-----BEGIN)",
    re.IGNORECASE,
)

HELP = """长期记忆（所有会话共享同一份）
/mem add <主语> <谓词> <内容>   写事实（同槽自动覆盖旧值）
/mem search <关键词> [数量]     检索（向量 + 关键词 + 重排）
/mem list [数量]                最近记录
/mem forget <关键词>            作废（保留历史，不物理删）
/mem stats                      条数 / provider 状态 / 最近更新
/mem export [路径]              导出 JSON（默认 ~/mem-export.json）
/mem import <路径>              导入并合并（同槽 updated_at 新者胜）
写操作限管理员 + allowed_users / allowed_groups；读对所有人生效。"""

USAGE = {
    "add": "/mem add <主语> <谓词> <内容> [--scope group:123]",
    "search": "/mem search <关键词> [数量]",
    "list": "/mem list [数量]",
    "forget": "/mem forget <关键词>",
    "export": "/mem export [文件路径]",
    "import": "/mem import <文件路径>",
}

MODE_CN = {
    "vector+rerank": "向量 + 重排",
    "vector": "仅向量（重排不可用）",
    "keyword+rerank": "仅关键词 + 重排（向量不可用）",
    "keyword": "仅关键词（向量/重排都不可用）",
}


class ArgError(Exception):
    """参数错误 / 权限拒绝 / 校验不过：直接回给用户看的那句话。"""


# ── 纯函数：参数解析与校验（可单测） ──────────────────────────────────


def strip_prefix(text: str) -> str:
    """剥掉 `/mem`、`！记忆` 之类前缀，拿到子指令和参数。"""
    raw = (text or "").strip()
    return CMD_PREFIX.sub("", raw, count=1).strip()


def parse_add(rest: str) -> tuple[str, str, str, str]:
    """`<主语> <谓词> <内容> [--scope x]` → (subject, predicate, value, scope)。"""
    rest = (rest or "").strip()
    scope = ""
    m = SCOPE_FLAG.search(rest)
    if m:
        scope = m.group(1).strip()
        rest = rest[: m.start()].rstrip()
    parts = re.split(r"\s+", rest, maxsplit=2)
    if len(parts) < 3 or not parts[0].strip() or not parts[1].strip() or not parts[2].strip():
        raise ArgError("参数不够：要主语、谓词、内容三段。")
    return parts[0].strip(), parts[1].strip(), parts[2].strip(), scope


def parse_search(rest: str) -> tuple[str, int]:
    """`<关键词> [数量]` → (query, limit)；数量缺省为 0（由调用方取默认）。"""
    tokens = (rest or "").split()
    limit = 0
    if len(tokens) >= 2 and tokens[-1].isdigit():
        limit = int(tokens.pop())
    query = " ".join(tokens).strip()
    if not query:
        raise ArgError("要给关键词。")
    return query, limit


def parse_limit(rest: str, default: int) -> int:
    token = (rest or "").strip()
    if not token:
        return default
    if not token.isdigit():
        raise ArgError(f"数量要是正整数，收到「{token}」。")
    return max(1, min(100, int(token)))


def validate_fact(subject: str, predicate: str, value: str) -> str:
    """写入前校验。返回空串表示通过，否则返回给用户看的原因。"""
    if len(subject) > MAX_NAME:
        return f"主语太长（{len(subject)} 字，上限 {MAX_NAME}）。"
    if len(predicate) > MAX_NAME:
        return f"谓词太长（{len(predicate)} 字，上限 {MAX_NAME}）。"
    if len(value) > MAX_VALUE:
        return f"内容太长（{len(value)} 字，上限 {MAX_VALUE}）。拆小一点再记。"
    if SENSITIVE.search(f"{subject}\n{predicate}\n{value}"):
        return "这条看起来含密钥/私钥/令牌，拒绝写入。凭证请放插件配置项，不要进记忆库。"
    return ""


def clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_row(row: dict, *, with_score: bool = False) -> str:
    tag = f"  [已作废 {row['valid_to']}]" if row.get("valid_to") else ""
    line = (
        f"#{row.get('id')} [{row.get('scope')}] "
        f"{row.get('subject')} · {row.get('predicate')} = {row.get('value')}{tag}"
    )
    meta = f"更新 {row.get('updated_at') or '?'}"
    if with_score and row.get("score") is not None:
        meta = f"分 {row['score']} | {meta}"
    return f"{line}\n    ↳ {meta}"


# ── 插件 ───────────────────────────────────────────────────────────────


class MemPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.conf = dict(config or {})
        self._store: Store | None = None
        self._store_path = ""

    # -- 配置读取（AstrBotConfig 是 dict 子类，但单测里可能给普通 dict） --
    def _conf_str(self, key: str, default: str = "") -> str:
        raw = self.conf.get(key, default)
        return str(raw).strip() if raw is not None else default

    def _conf_int(self, key: str, default: int) -> int:
        try:
            return int(self.conf.get(key, default))
        except (TypeError, ValueError):
            return default

    def _conf_bool(self, key: str, default: bool = False) -> bool:
        raw = self.conf.get(key, default)
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(raw)

    def _conf_list(self, key: str) -> list[str]:
        raw = self.conf.get(key) or []
        if isinstance(raw, str):
            raw = re.split(r"[\s,，]+", raw)
        out = []
        for item in raw:
            item = str(item).strip()
            if item:
                out.append(item)
        return out

    def _scope(self) -> str:
        return self._conf_str("scope") or "global"

    # -- 存储 --
    def _get_store(self) -> Store:
        if self._store is not None:
            return self._store
        path = resolve_db_path(self._conf_str("db_path"), warn=logger.warning)
        store = Store(path)  # 失败抛 StoreError，由调用方转成「记忆库读不了」
        self._store_path = path
        self._store = store
        logger.info(f"[mem] 记忆库：{path}")
        return store

    # -- 权限 --
    def _allow(self, event, *, write: bool) -> tuple[bool, str]:
        if self._conf_bool("allow_all"):
            return True, ""
        try:
            if event.is_admin():
                return True, ""
        except Exception:  # noqa: BLE001 老版本事件对象可能没有 is_admin
            pass
        try:
            uid = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            uid = ""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001
            gid = ""
        if uid and uid in self._conf_list("allowed_users"):
            return True, ""
        if gid and gid in self._conf_list("allowed_groups"):
            return True, ""
        if not write:
            return True, ""
        return False, (
            "没权限写记忆。\n"
            f"当前：用户 {uid or '?'} / 群 {gid or '私聊'}\n"
            "解决：把上面这个号加进 allowed_users，或把群号加进 allowed_groups；"
            "也可以打开 allow_all（不建议）。读操作不受限。"
        )

    # -- provider 选择：配置指定 → 自动挑第一个 → 没有就降级 --
    def _safe_get_provider(self, provider_id: str):
        if not provider_id:
            return None
        try:
            return self.context.get_provider_by_id(provider_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[mem] 取 provider {provider_id} 失败：{e}")
            return None

    @staticmethod
    def _provider_id(prov) -> str:
        cfg = getattr(prov, "provider_config", None)
        if isinstance(cfg, dict) and cfg.get("id"):
            return str(cfg["id"])
        try:
            return str(prov.meta().id)
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _model_name(prov) -> str:
        for attr in ("model", "model_name"):
            value = getattr(prov, attr, None)
            if value:
                return str(value)
        try:
            return str(prov.get_model() or "")
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _dim(prov) -> int:
        try:
            return int(prov.get_dim())
        except Exception:  # noqa: BLE001
            return 0

    def _embed_provider(self) -> tuple[str, object | None]:
        pid = self._conf_str("embed_provider_id")
        prov = self._safe_get_provider(pid)
        if prov is not None and not hasattr(prov, "get_embedding"):
            logger.warning(f"[mem] embed_provider_id={pid} 不带 get_embedding，改为自动挑")
            prov = None
        if prov is None:
            insts = getattr(self.context.provider_manager, "embedding_provider_insts", None) or []
            prov = insts[0] if insts else None
            if prov is not None:
                pid = self._provider_id(prov) or pid
            elif pid:
                logger.warning(f"[mem] 找不到 embedding provider {pid}，检索退关键词")
        return pid, prov

    def _rerank_provider(self) -> tuple[str, object | None]:
        pid = self._conf_str("rerank_provider_id")
        prov = self._safe_get_provider(pid)
        if prov is not None and not hasattr(prov, "rerank"):
            logger.warning(f"[mem] rerank_provider_id={pid} 不带 rerank，改为自动挑")
            prov = None
        if prov is None:
            insts = getattr(self.context.provider_manager, "rerank_provider_insts", None) or []
            prov = insts[0] if insts else None
            if prov is not None:
                pid = self._provider_id(prov) or pid
        return pid, prov

    def _model_key(self, pid: str, prov) -> str:
        return f"{pid or '?'}|{self._model_name(prov) or '?'}|{self._dim(prov)}"

    def _build_retriever(self, store: Store) -> Retriever:
        ep_id, ep = self._embed_provider()
        _, rp = self._rerank_provider()
        embed = None
        if ep is not None:
            async def embed(text: str, _p=ep) -> list[float]:
                return await _p.get_embedding(text)

        rerank = None
        if rp is not None:
            async def rerank(query: str, docs: list[str], top_n=None, _p=rp):
                return await _p.rerank(query, docs, top_n)

        return Retriever(
            store,
            embed=embed,
            rerank=rerank,
            model_key=self._model_key(ep_id, ep) if ep is not None else "keyword-only",
            recall_k=self._conf_int("recall_k", 30),
            rerank_k=self._conf_int("rerank_k", 5),
            logger=logger,
        )

    # ── 指令入口 ──────────────────────────────────────────────────────
    @filter.command("mem", alias={"记忆"})
    async def mem(self, event: AstrMessageEvent):
        """长期记忆：add / search / list / forget / stats / export / import。"""
        args = strip_prefix(getattr(event, "message_str", ""))
        if not args:
            yield event.plain_result(HELP)
            return

        action, _, rest = args.partition(" ")
        action = action.strip().lower()
        rest = rest.strip()

        try:
            text = await self._dispatch(action, rest, event)
        except ArgError as e:
            usage = USAGE.get(action)
            text = f"{e}" + (f"\n用法：{usage}" if usage else "")
        except StoreError as e:
            logger.warning(f"[mem] 记忆库异常：{e}")
            text = f"记忆库读不了：{e}"
        except Exception as e:  # noqa: BLE001 指令永远不抛 traceback
            logger.warning(f"[mem] 指令处理异常：{type(e).__name__}: {e}")
            text = "记忆操作出错了，已记日志；不影响其他功能。"
        yield event.plain_result(text)

    async def _dispatch(self, action: str, rest: str, event) -> str:
        if action in ("add", "记", "记住", "写入"):
            return await self._cmd_add(rest, event)
        if action in ("search", "搜", "查", "检索"):
            return await self._cmd_search(rest, event)
        if action in ("list", "ls", "列表"):
            return self._cmd_list(rest)
        if action in ("forget", "del", "delete", "忘记", "作废"):
            return self._cmd_forget(rest, event)
        if action in ("stats", "状态", "统计"):
            return self._cmd_stats()
        if action in ("export", "导出"):
            return self._cmd_export(rest, event)
        if action in ("import", "导入"):
            return self._cmd_import(rest, event)
        if action in ("help", "?", "帮助"):
            return HELP
        raise ArgError(f"不认识的子指令「{action}」，可用：add / search / list / forget / stats / export / import。")

    # ── 子指令实现 ────────────────────────────────────────────────────
    async def _cmd_add(self, rest: str, event) -> str:
        ok, why = self._allow(event, write=True)
        if not ok:
            return why
        subject, predicate, value, scope = parse_add(rest)
        problem = validate_fact(subject, predicate, value)
        if problem:
            return problem
        result = self._get_store().add(
            scope or self._scope(), subject, predicate, value, source="cmd"
        )
        line = f"已记住 #{result['id']} [{scope or self._scope()}] {subject} · {predicate} = {value}"
        if result["superseded"]:
            old = "、".join(f"#{i}" for i in result["superseded"])
            line += f"\n（同槽旧值已作废：{old}）"
        return line

    async def _cmd_search(self, rest: str, event) -> str:
        query, limit = parse_search(rest)
        store = self._get_store()
        result = await self._build_retriever(store).search(
            query, limit=limit or self._conf_int("rerank_k", 5)
        )
        if not result.rows:
            return f"（没检索到「{query}」）"
        head = f"检索到 {len(result.rows)} 条 · {MODE_CN.get(result.mode, result.mode)}"
        return head + "\n" + "\n".join(format_row(r, with_score=True) for r in result.rows)

    def _cmd_list(self, rest: str) -> str:
        limit = parse_limit(rest, 10)
        store = self._get_store()
        rows = store.active_rows(limit=limit)
        if not rows:
            return "记忆库还是空的。用 /mem add <主语> <谓词> <内容> 记第一条。"
        total = store.stats()["active"]
        body = "\n".join(format_row(r) for r in rows)
        return f"最近 {len(rows)} 条（共 {total} 条有效）：\n{body}"

    def _cmd_forget(self, rest: str, event) -> str:
        ok, why = self._allow(event, write=True)
        if not ok:
            return why
        keyword = (rest or "").strip()
        if not keyword:
            raise ArgError("要给关键词。")
        rows = self._get_store().forget(keyword)
        if not rows:
            return f"没找到含「{keyword}」的有效记忆。"
        ids = "、".join(f"#{r['id']}" for r in rows)
        return f"已作废 {len(rows)} 条：{ids}（历史保留，可 export 查）"

    def _cmd_stats(self) -> str:
        store = self._get_store()
        ep_id, ep = self._embed_provider()
        rp_id, rp = self._rerank_provider()
        model_key = self._model_key(ep_id, ep) if ep is not None else ""
        st = store.stats(model=model_key or None)

        if ep is None:
            embed_line = "embedding：未配置 / 不可用（检索走关键词）"
        else:
            embed_line = (
                f"embedding：{ep_id or '?'}（{self._model_name(ep) or '未知模型'}，"
                f"dim {self._dim(ep) or '?'}，已缓存 {st['vec_rows']} 条向量）"
            )
        if rp is None:
            rerank_line = "rerank：未配置 / 不可用（按向量或关键词排序）"
        else:
            rerank_line = f"rerank：{rp_id or '?'}（{self._model_name(rp) or '未知模型'}）"

        scopes = "、".join(f"{name} {n} 条" for name, n in st["scopes"]) or "无"
        lines = [
            f"有效 {st['active']} 条 / 历史 {st['history']} 条",
            f"分池：{scopes}",
            embed_line,
            rerank_line,
            f"最近更新：{st['last_updated'] or '（还没有记录）'}",
            f"库文件：{st['path']}",
        ]
        return "\n".join(lines)

    def _cmd_export(self, rest: str, event) -> str:
        # 导出会落盘且含全量数据，按写权限管（比规格多一层保险，README 有说明）
        ok, why = self._allow(event, write=True)
        if not ok:
            return why
        path = rest.strip() or os.path.expanduser("~/mem-export.json")
        result = self._get_store().export_json(path)
        return f"已导出 {result['count']} 条到 {result['path']}"

    def _cmd_import(self, rest: str, event) -> str:
        ok, why = self._allow(event, write=True)
        if not ok:
            return why
        path = rest.strip()
        if not path:
            raise ArgError("要给文件路径。")
        result = self._get_store().import_json(path)
        text = (
            f"导入完成：新增 {result['added']}、覆盖 {result['updated']}、"
            f"跳过 {result['skipped']}、无效 {result['invalid']}"
        )
        if result["errors"]:
            text += "\n无效示例：" + "；".join(result["errors"])
        return text

    # ── LLM 工具 ──────────────────────────────────────────────────────
    @filter.llm_tool(name="memory_add")
    async def memory_add(self, event: AstrMessageEvent, subject: str, predicate: str, value: str):
        """记住一条跨会话共享的长期事实（同一主语+谓词会覆盖旧值）。

        Args:
            subject(string): 主语，例如「点点」或「用户的服务器」
            predicate(string): 谓词，例如「喜欢」或「部署在」
            value(string): 内容，最多 500 字
        """
        ok, why = self._allow(event, write=True)
        if not ok:
            return why
        subject = str(subject or "").strip()
        predicate = str(predicate or "").strip()
        value = str(value or "").strip()
        problem = validate_fact(subject, predicate, value)
        if problem:
            return problem
        try:
            result = self._get_store().add(
                self._scope(), subject, predicate, value, source="llm"
            )
        except StoreError as e:
            logger.warning(f"[mem] 工具写入失败：{e}")
            return f"记忆库读不了：{e}"
        return f"已记住 #{result['id']}：{subject} · {predicate} = {value}"

    @filter.llm_tool(name="memory_search")
    async def memory_search(self, event: AstrMessageEvent, query: str):
        """检索长期记忆，返回最相关的事实。

        Args:
            query(string): 检索关键词或自然语言问题
        """
        query = str(query or "").strip()
        if not query:
            return "query 不能为空。"
        try:
            store = self._get_store()
            result = await self._build_retriever(store).search(
                query, limit=self._conf_int("rerank_k", 5)
            )
        except StoreError as e:
            logger.warning(f"[mem] 工具检索失败：{e}")
            return f"记忆库读不了：{e}"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[mem] 工具检索异常：{type(e).__name__}: {e}")
            return "检索出错了，已记日志。"
        if not result.rows:
            return "（没有相关记忆）"
        return "\n".join(
            f"- {r['subject']} · {r['predicate']}：{r['value']}" for r in result.rows
        )

    # ── 注入钩子（默认关闭） ──────────────────────────────────────────
    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """auto_inject_n > 0 时，把 top-N 条记忆（每条截 200 字）追加到 system_prompt。"""
        n = self._conf_int("auto_inject_n", 0)
        if n <= 0:
            return
        try:
            query = (getattr(event, "message_str", "") or "").strip()
            if not query:
                return
            store = self._get_store()
            result = await self._build_retriever(store).search(query, limit=n)
            if not result.rows:
                return
            block = "\n".join(
                f"- {r['subject']} · {r['predicate']}：{clip(r['value'], 200)}"
                for r in result.rows
            )
            prompt = (getattr(req, "system_prompt", "") or "").rstrip()
            req.system_prompt = f"{prompt}\n\n[长期记忆（可能过期，仅供参考）]\n{block}".lstrip()
        except Exception as e:  # noqa: BLE001 注入失败绝不能打断聊天
            logger.warning(f"[mem] 记忆注入失败，本轮跳过：{type(e).__name__}: {e}")
