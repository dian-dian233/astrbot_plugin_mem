"""AstrBot 全局记忆插件：任何会话（群聊 / 私聊 / 换号）共用同一份记忆。

指令（前缀 `/` 或 `!`，唤醒前缀由框架剥掉，这里再兜一层）：

    /mem add <主语> <谓词> <内容> [--scope group:123] [--type plan] [--importance 0.9]
    /mem search <关键词> [数量]
    /mem list [数量]
    /mem forget <关键词>
    /mem stats
    /mem dedupe [--dry-run]
    /mem consolidate [--dry-run]
    /mem export [文件路径]
    /mem import <文件路径>
    /mem import-lm [--dry-run] [--path <db>] [--limit N]   从 LivingMemory 插件直接搬家

LLM 工具：memory_add / memory_search（`@filter.llm_tool`）。

写入降噪（v0.2.0，规范见 dd-home/memory-policy.md）：一句判断句、≤`max_value_len`（默认
120）字、拒叙事体（`reject_narrative`）、同槽覆盖优先于追加、同主语同义（cos ≥
`dedupe_threshold`）也走覆盖。检索输出按 (subject, predicate) 只留最新一条，噪声地板
`min_score`。

搬家（v0.4.0，实现在 livingmemory.py）：`/mem import-lm` 自己找 LivingMemory 的库，
**只读**打开、切三元组、分批写进来，用户不用导文件。

记忆生命周期（v0.3.0，实现在 lifecycle.py）：每条记录带 mtype / importance / ttl_days /
decay_type / last_accessed_at / access_count。四种类型 fact(3650d exp) / preference(365d exp) /
event(30d exp) / plan(2d step)。检索时 `final = base * decay * (0.5 + 0.5*importance)`，
decay 按年龄算、按 access_count 强化（`access_cap` 封顶），`importance >= protected_importance`
的记录不衰减；plan 过 TTL 直接不参与检索（不再像 v0.2 那样拒写）。

检索链路（v0.3.0）：向量 top-k + 关键词 BM25 top-k → RRF 融合（`rrf_k`）→ rerank →
decay 加权 → 截断。某一路为空只用另一路，不拿空路稀释。

注入策略：默认 `auto_inject_n = 0`，**不往 prompt 里塞任何记忆**；配成 N>0 才每轮
注入 top-N 条、按主语去重、每条截 200 字且总量不超 `inject_char_cap`（默认 600）。
原因见 README「为什么默认不注入」。

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
    - 配置项 type: float 合法：astrbot/dashboard/services/config_service.py:61
    - LLM 工具参数由 docstring Args 生成（可选参数靠函数默认值兜底）：
      astrbot/core/star/register/star_handler.py:586-670
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .retrieve import (
    EMBED_TIMEOUT,
    Retriever,
    cosine,
    pack_vec,
    unpack_vec,
    value_hash,
)
from .lifecycle import (
    DEFAULT_ACCESS_CAP,
    DEFAULT_ACCESS_HALVE,
    DEFAULT_PROTECTED_IMPORTANCE,
    DEFAULT_TTL_DAYS,
    infer_mtype,
    is_expired,
    normalize_mtype,
    ttl_for,
)
from .livingmemory import LivingMemoryError, format_not_found, format_report, probe, run_import
from .store import Store, StoreError, astrbot_data_dirs, resolve_db_path
from .textnorm import normalize_text

DEFAULT_MAX_VALUE_LEN = 120  # 单条内容上限（v0.1.0 是硬编码 500，现在可配）
MAX_VALUE = DEFAULT_MAX_VALUE_LEN  # 兼容旧引用
MAX_NAME = 40  # 主语 / 谓词各自上限
DEFAULT_DEDUPE_THRESHOLD = 0.95  # 写入相似去重阈值
DEFAULT_MIN_SCORE = 0.2  # 检索噪声地板（和 retrieve.DEFAULT_MIN_SCORE 一致）
DEFAULT_INJECT_CHAR_CAP = 600  # 注入内容总字数上限
DEFAULT_RRF_K = 60  # RRF 融合参数（和 retrieve.DEFAULT_RRF_K 一致）
DEFAULT_AGREEMENT_BONUS = 0.02  # 两路共识加成
DEFAULT_MTYPE = "fact"  # default_mtype 的兜底值

CMD_PREFIX = re.compile(r"^[/!！]?\s*(?:mem|记忆)\b\s*", re.IGNORECASE)
SCOPE_FLAG = re.compile(r"(?:^|\s)--scope\s+(\S+)\s*$", re.IGNORECASE)

# 叙事体开头：以这些词开头的 value 是「我昨晚跟他讲了……」这类过程复述，不是事实。
NARRATIVE_PREFIXES = ("我", "他", "她", "今天", "昨天", "昨晚", "刚才", "后来", "然后")

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
/mem add <主语> <谓词> <内容>   写事实（同槽 / 同义自动覆盖；--type 指定类型）
/mem search <关键词> [数量]     检索（向量 + 关键词 + 重排）
/mem list [数量]                最近记录
/mem forget <关键词>            作废（保留历史，不物理删）
/mem stats                      条数 / provider 状态 / 最近更新
/mem dedupe [--dry-run]         扫同槽重复，只留最新（--dry-run 只报告不改库）
/mem consolidate [--dry-run]    同主语 + 语义相近的多条合并成一条（保留最完整）
/mem export [路径]              导出 JSON（默认 ~/mem-export.json）
/mem import <路径>              导入并合并（同槽 updated_at 新者胜）
/mem import-lm [--dry-run]      从 LivingMemory 插件搬家（自动找库，不用你导文件）
类型：fact 长期 / preference 一年 / event 一个月 / plan 两天（过期不参与检索）
写操作限管理员 + allowed_users / allowed_groups；读对所有人生效。"""

USAGE = {
    "add": "/mem add <主语> <谓词> <内容> [--scope group:123] [--type fact|preference|event|plan]",
    "search": "/mem search <关键词> [数量]",
    "list": "/mem list [数量]",
    "forget": "/mem forget <关键词>",
    "dedupe": "/mem dedupe [--dry-run]",
    "consolidate": "/mem consolidate [--dry-run]",
    "export": "/mem export [文件路径]",
    "import": "/mem import <文件路径>",
    "import-lm": "/mem import-lm [--dry-run] [--path <livingmemory.db>] [--limit N]",
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


def _take_flag(text: str, name: str) -> tuple[str, str]:
    """从参数尾部摘掉一个 `--name value` / `--name=value`，返回 (值, 剩下的文本)。"""
    pattern = re.compile(rf"(?:^|\s)--{re.escape(name)}(?:[=\s]+)(\S+)", re.IGNORECASE)
    match = pattern.search(text or "")
    if not match:
        return "", (text or "")
    rest = f"{(text or '')[: match.start()]} {(text or '')[match.end():]}".strip()
    return match.group(1).strip(), rest


def parse_add_ex(rest: str) -> tuple[str, str, str, str, str, str]:
    """`<主语> <谓词> <内容> [--scope x] [--type plan] [--importance 0.9]`

    → (subject, predicate, value, scope, mtype, importance)。
    mtype / importance 留空表示「没显式指定」，由调用方推断 / 取默认。
    """
    rest = (rest or "").strip()
    flags: dict[str, str] = {}
    for name in ("scope", "type", "importance"):
        value, rest = _take_flag(rest, name)
        if value:
            flags[name] = value
    parts = re.split(r"\s+", rest, maxsplit=2)
    if len(parts) < 3 or not parts[0].strip() or not parts[1].strip() or not parts[2].strip():
        raise ArgError("参数不够：要主语、谓词、内容三段。")
    return (
        normalize_text(parts[0]),
        normalize_text(parts[1]),
        normalize_text(parts[2]),
        flags.get("scope", ""),
        flags.get("type", ""),
        flags.get("importance", ""),
    )


def parse_add(rest: str) -> tuple[str, str, str, str]:
    """兼容 v0.2 的四元组接口（自测直接调用它）。"""
    subject, predicate, value, scope, _mtype, _importance = parse_add_ex(rest)
    return subject, predicate, value, scope


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


def parse_import_lm(rest: str) -> dict:
    """`[--dry-run] [--path <db>] [--limit N]` → {"dry_run", "path", "limit"}。

    多余参数直接报错（不静默吞掉）：拼错 `--dryrun` 时用户得知道没生效。
    """
    text = str(rest or "").strip()
    dry_run = bool(re.search(r"(?:^|\s)--dry-run\b", text, re.IGNORECASE))
    if dry_run:
        text = re.sub(r"(?:^|\s)--dry-run\b", " ", text, flags=re.IGNORECASE)
    path, text = _take_flag(text, "path")
    limit_raw, text = _take_flag(text, "limit")
    limit = 0
    if limit_raw:
        if not limit_raw.isdigit():
            raise ArgError(f"--limit 要正整数，收到「{limit_raw}」。")
        limit = int(limit_raw)
        if limit < 1:
            raise ArgError("--limit 要 ≥ 1；不限制就别写这个参数。")
    leftover = text.strip()
    if leftover:
        raise ArgError(
            f"多余的参数「{leftover}」；用法：/mem import-lm [--dry-run] [--path <db>] [--limit N]"
        )
    return {"dry_run": dry_run, "path": path, "limit": limit}


def validate_fact(
    subject: str,
    predicate: str,
    value: str,
    *,
    max_value_len: int = DEFAULT_MAX_VALUE_LEN,
    reject_narrative: bool = True,
    max_name: int = MAX_NAME,
) -> str:
    """写入前校验。返回空串表示通过，否则返回给用户看的原因。"""
    if len(subject) > max_name:
        return f"主语太长（{len(subject)} 字，上限 {max_name}）。"
    if len(predicate) > max_name:
        return f"谓词太长（{len(predicate)} 字，上限 {max_name}）。"
    try:
        limit = int(max_value_len)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_VALUE_LEN
    if limit > 0 and len(value) > limit:
        return (
            f"内容太长（{len(value)} 字，上限 {limit}）。\n"
            f"改成一句事实，≤{limit} 字：只留「谁 / 什么 · 是什么」，过程别记。"
        )
    if reject_narrative:
        head = str(value or "").lstrip()
        for prefix in NARRATIVE_PREFIXES:
            if head.startswith(prefix):
                return (
                    f"这条像日记（以「{prefix}」开头），不是一句事实。\n"
                    "改成「主语 + 谓词 + 值」的判断句，例如「框架长期记忆有会话隔离开关」。\n"
                    "（要关掉这项检查：把配置 reject_narrative 设为 false）"
                )
    if SENSITIVE.search(f"{subject}\n{predicate}\n{value}"):
        return "这条看起来含密钥/私钥/令牌，拒绝写入。凭证请放插件配置项，不要进记忆库。"
    return ""


def clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def short_ts(value) -> str:
    """ISO 时间串 → `YYYY-MM-DD HH:MM`；解析不了就原样返回（不抛错）。"""
    raw = str(value or "")
    try:
        return datetime.fromisoformat(raw).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return raw or "?"


def format_row(row: dict, *, with_score: bool = False) -> str:
    tag = f"  [已作废 {row['valid_to']}]" if row.get("valid_to") else ""
    mtype = str(row.get("mtype") or "fact")
    line = (
        f"#{row.get('id')} [{row.get('scope')}] [{mtype}] "
        f"{row.get('subject')} · {row.get('predicate')} = {row.get('value')}{tag}"
    )
    meta = f"更新 {short_ts(row.get('updated_at'))}"
    decay = row.get("decay")
    if decay is not None:
        try:
            meta = f"衰减 {float(decay):.2f} · {meta}"
        except (TypeError, ValueError):
            pass
    if with_score and row.get("score") is not None:
        meta = f"分 {row['score']} · {meta}"
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

    def _conf_float(self, key: str, default: float) -> float:
        try:
            return float(self.conf.get(key, default))
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

    def _ttl_overrides(self) -> dict:
        """四种类型的 TTL 配置 → {"fact": 3650, ...}。"""
        return {
            "fact": self._conf_float("ttl_fact", DEFAULT_TTL_DAYS["fact"]),
            "preference": self._conf_float("ttl_preference", DEFAULT_TTL_DAYS["preference"]),
            "event": self._conf_float("ttl_event", DEFAULT_TTL_DAYS["event"]),
            "plan": self._conf_float("ttl_plan", DEFAULT_TTL_DAYS["plan"]),
        }

    def _ttl_for(self, mtype: str) -> float:
        return ttl_for(mtype, self._ttl_overrides())

    @staticmethod
    def _data_dirs() -> list[str]:
        """AstrBot data 目录候选（只返回存在的），给「找 LivingMemory 的库」用。"""
        return astrbot_data_dirs()

    # -- 存储 --
    def _get_store(self) -> Store:
        if self._store is not None:
            return self._store
        path = resolve_db_path(self._conf_str("db_path"), warn=logger.warning)
        store = Store(path, warn=logger.warning)  # 失败抛 StoreError，由调用方转成「记忆库读不了」
        if store.migrated_columns:
            logger.info(f"[mem] 老库已补列：{', '.join(store.migrated_columns)}")
        self._store_path = path
        self._store = store
        logger.info(f"[mem] 记忆库：{path}")
        # 衰减回合：非保护记录的 access_count 按 access_halve 回落（默认每小时最多一次），
        # 防止历史热点靠访问次数永远霸榜。失败只 warning，不影响使用。
        try:
            stats = store.apply_decay_pass(
                access_halve=self._conf_float("access_halve", DEFAULT_ACCESS_HALVE),
                protected_importance=self._conf_float(
                    "protected_importance", DEFAULT_PROTECTED_IMPORTANCE
                ),
            )
            if not stats.get("skipped") and stats.get("updated"):
                logger.info(f"[mem] 衰减回合：{stats['updated']} 条 access_count 已回落")
        except StoreError as e:
            logger.warning(f"[mem] 衰减回合跳过：{e}")
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
            min_score=self._conf_float("min_score", DEFAULT_MIN_SCORE),
            rrf_k=self._conf_int("rrf_k", DEFAULT_RRF_K),
            agreement_bonus=self._conf_float("agreement_bonus", DEFAULT_AGREEMENT_BONUS),
            access_cap=self._conf_int("access_cap", DEFAULT_ACCESS_CAP),
            protected_importance=self._conf_float(
                "protected_importance", DEFAULT_PROTECTED_IMPORTANCE
            ),
            logger=logger,
        )

    # ── 写入：归一化 → 校验 → 相似去重 → 落库 ────────────────────────
    def _validate(self, subject: str, predicate: str, value: str) -> str:
        return validate_fact(
            subject,
            predicate,
            value,
            max_value_len=self._conf_int("max_value_len", DEFAULT_MAX_VALUE_LEN),
            reject_narrative=self._conf_bool("reject_narrative", True),
            max_name=MAX_NAME,
        )

    async def _write_fact(
        self,
        scope: str,
        subject: str,
        predicate: str,
        value: str,
        *,
        source: str,
        mtype: str = DEFAULT_MTYPE,
        importance: float | None = None,
    ) -> dict:
        """唯一写入通道：指令和 LLM 工具都走这里，保证降噪规则一致。

        mtype 决定 TTL 与衰减方式：plan 2 天 step（过期不参与检索）、event 30 天 exp、
        preference 365 天 exp、fact 3650 天 exp。
        """
        store = self._get_store()
        mtype = normalize_mtype(mtype)
        lookup = await self._similar_ids(store, scope, subject, value)
        result = store.add(
            scope,
            subject,
            predicate,
            value,
            source=source,
            supersede_ids=lookup["ids"],
            mtype=mtype,
            importance=importance,
            ttl_days=self._ttl_for(mtype),
        )
        # 新值向量回填缓存：不填的话下次写同主语要把它再算一遍，
        # N 次写入会退化成 O(N²) 次 embedding 调用。
        if lookup["vec"] and lookup["model_key"]:
            try:
                store.vec_put(
                    int(result["id"]),
                    lookup["model_key"],
                    len(lookup["vec"]),
                    pack_vec(lookup["vec"]),
                    value_hash(value),
                )
            except StoreError as e:
                logger.warning(f"[mem] 新值向量缓存写失败（#{result['id']}）：{e}")
        return result

    async def _similar_ids(self, store: Store, scope: str, subject: str, value: str) -> dict:
        """同 scope + 同主语里，值向量 cos ≥ dedupe_threshold 的记录 → 视为同一件事。

        返回 {"ids": [...], "model_key": str, "vec": [...]}；没有 embedding provider /
        调用失败 / 超时 → ids 为空（跳过检查，绝不阻塞写入）。
        """
        skip = {"ids": [], "model_key": "", "vec": None}
        threshold = self._conf_float("dedupe_threshold", DEFAULT_DEDUPE_THRESHOLD)
        if threshold <= 0:
            return skip
        pid, ep = self._embed_provider()
        if ep is None:
            return skip
        try:
            new_vec = [float(x) for x in (await self._embed_value(ep, value) or [])]
        except Exception as e:  # noqa: BLE001 去重是优化，失败不影响写入
            logger.warning(f"[mem] 相似去重跳过（新值向量失败：{type(e).__name__}: {e}）")
            return skip
        if not new_vec:
            return skip

        model_key = f"{self._model_key(pid, ep)}|value"  # 只对 value 算的向量，单独一个缓存键
        out = {"ids": [], "model_key": model_key, "vec": new_vec}
        try:
            rows = store.active_rows_by_subject(scope, subject)
        except StoreError as e:
            logger.warning(f"[mem] 相似去重跳过（读候选失败：{e}）")
            return out

        hits: list[int] = []
        for row in rows:
            if row.get("value") == value:  # 一字不差：本来就是同一件事
                hits.append(int(row["id"]))
                continue
            vec = await self._cached_value_vector(store, row, ep, model_key)
            if vec is None:
                continue
            if cosine(new_vec, vec) >= threshold:
                hits.append(int(row["id"]))
        out["ids"] = hits
        return out

    @staticmethod
    async def _embed_value(ep, text: str) -> list[float]:
        return await asyncio.wait_for(ep.get_embedding(text), timeout=EMBED_TIMEOUT)

    async def _cached_value_vector(
        self, store: Store, row: dict, ep, model_key: str
    ) -> list[float] | None:
        digest = value_hash(str(row.get("value") or ""))
        try:
            cached = store.vec_get(int(row["id"]), model_key)
        except StoreError:
            cached = None
        if cached:
            try:
                vec = unpack_vec(cached["vec"])
            except (TypeError, ValueError):
                vec = []
            if cached.get("text_hash") == digest and cached.get("dim") == len(vec) and vec:
                return vec
        try:
            vec = [float(x) for x in (await self._embed_value(ep, str(row.get("value") or "")) or [])]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[mem] 候选 #{row.get('id')} 向量算不出来，跳过比对：{e}")
            return None
        if not vec:
            return None
        try:
            store.vec_put(int(row["id"]), model_key, len(vec), pack_vec(vec), digest)
        except StoreError as e:
            logger.warning(f"[mem] 去重向量缓存写失败（#{row.get('id')}）：{e}")
        return vec

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
        if action in ("dedupe", "去重"):
            return self._cmd_dedupe(rest, event)
        if action in ("consolidate", "合并", "整理"):
            return await self._cmd_consolidate(rest, event)
        if action in ("export", "导出"):
            return self._cmd_export(rest, event)
        if action in ("import", "导入"):
            return self._cmd_import(rest, event)
        if action in ("import-lm", "import_lm", "importlm", "搬家", "迁入"):
            return self._cmd_import_lm(rest, event)
        if action in ("help", "?", "帮助"):
            return HELP
        raise ArgError(
            f"不认识的子指令「{action}」，可用：add / search / list / forget / stats / "
            "dedupe / consolidate / export / import / import-lm。"
        )

    # ── 子指令实现 ────────────────────────────────────────────────────
    async def _cmd_add(self, rest: str, event) -> str:
        ok, why = self._allow(event, write=True)
        if not ok:
            return why
        subject, predicate, value, scope, mtype_flag, importance_raw = parse_add_ex(rest)
        problem = self._validate(subject, predicate, value)
        if problem:
            return problem

        if mtype_flag:
            mtype = normalize_mtype(mtype_flag)
        else:
            mtype = infer_mtype(
                subject, predicate, value, self._conf_str("default_mtype") or DEFAULT_MTYPE
            )
        importance = None
        if importance_raw:
            try:
                importance = max(0.0, min(1.0, float(importance_raw)))
            except ValueError:
                raise ArgError(f"importance 要 0~1 的小数，收到「{importance_raw}」。") from None

        target = scope or self._scope()
        result = await self._write_fact(
            target, subject, predicate, value, source="cmd", mtype=mtype, importance=importance
        )
        line = (
            f"已记住 #{result['id']} [{target}] [{mtype}] "
            f"{subject} · {predicate} = {value}"
        )
        if result["superseded"]:
            old = "、".join(f"#{i}" for i in result["superseded"])
            line += f"\n（旧值已作废：{old}）"
        ttl = self._ttl_for(mtype)
        hint = "（过期自动不参与检索）" if mtype == "plan" else ""
        line += f"\n类型 {mtype} · TTL {ttl:g} 天{hint}"
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
        if result.fusion:
            head += f" · {result.fusion}"
        return head + "\n" + "\n".join(format_row(r, with_score=True) for r in result.rows)

    def _cmd_list(self, rest: str) -> str:
        limit = parse_limit(rest, 10)
        store = self._get_store()
        protected = self._conf_float("protected_importance", DEFAULT_PROTECTED_IMPORTANCE)
        rows = store.active_rows(limit=max(limit, limit * 3))
        fresh = [r for r in rows if not is_expired(r, protected_importance=protected)]
        total = store.stats()["active"]
        if not fresh:
            if total:
                return f"最近没有未过期的记录（{total} 条里都已过期，用 /mem export 查历史）。"
            return "记忆库还是空的。用 /mem add <主语> <谓词> <内容> 记第一条。"
        body = "\n".join(format_row(r) for r in fresh[:limit])
        return f"最近 {len(fresh[:limit])} 条（共 {total} 条有效）：\n{body}"

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

    def _cmd_dedupe(self, rest: str, event) -> str:
        tokens = [t for t in (rest or "").split() if t]
        dry_run = any(t in ("--dry-run", "--dry", "-n") for t in tokens)
        unknown = [t for t in tokens if t not in ("--dry-run", "--dry", "-n")]
        if unknown:
            raise ArgError(f"不认识的参数「{' '.join(unknown)}」，用法：{USAGE['dedupe']}")
        # --dry-run 只读不写，不要求写权限；真合并会改库，按写权限管
        if not dry_run:
            ok, why = self._allow(event, write=True)
            if not ok:
                return why
        report = self._get_store().dedupe_slots(dry_run=dry_run)
        head = f"扫描 {report['scanned']} 条，可合并 {report['mergeable_groups']} 组"
        if report["mergeable_rows"]:
            head += f"（涉及 {report['mergeable_rows']} 条重复）"
        if not report["mergeable_rows"]:
            return head + "\n库里没有同 (scope, 主语, 谓词) 重复的有效记录。"
        slots = "、".join(f"{k} → 保留 #{v}" for k, v in list(report["kept"].items())[:5])
        if report["dry_run"]:
            return (
                f"{head}\n{slots}\n"
                "（dry-run）没改库；去掉 --dry-run 才会真的作废旧记录。"
            )
        ids = "、".join(f"#{i}" for i in report["superseded"])
        return f"{head}\n已作废 {len(report['superseded'])} 条：{ids}\n{slots}"

    # ── consolidate：同 scope + 同主语、语义相近的多条合并 ────────────

    @staticmethod
    def _same_value(left: dict, right: dict) -> bool:
        return str(left.get("value") or "") == str(right.get("value") or "")

    @staticmethod
    def _close(left, right, threshold: float) -> bool:
        if left is None or right is None:
            return False
        return cosine(left, right) >= threshold

    async def _consolidation_clusters(self, store: Store, rows: list[dict],
                                      threshold: float) -> list[list[dict]]:
        """把同 (scope, subject) 的记录按值向量聚类，返回每组 ≥2 条的簇。

        没有 embedding provider / 阈值 <= 0 时只认「一字不差」，绝不瞎合并。
        """
        grouped: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            grouped.setdefault((row.get("scope") or "", row.get("subject") or ""), []).append(row)
        multi = {key: items for key, items in grouped.items() if len(items) > 1}
        if not multi:
            return []

        vec_by_id: dict[int, list[float]] = {}
        pid, embedder = self._embed_provider()
        use_cos = embedder is not None and threshold > 0
        if use_cos:
            model_key = f"{self._model_key(pid, embedder)}|value"  # 只对 value 算的向量
            for items in multi.values():
                for row in items:
                    vec = await self._cached_value_vector(store, row, embedder, model_key)
                    if vec is not None:
                        vec_by_id[int(row["id"])] = vec

        clusters: list[list[dict]] = []
        for items in multi.values():
            ordered = sorted(
                items,
                key=lambda r: (str(r.get("updated_at") or ""), int(r.get("id") or 0)),
                reverse=True,
            )
            groups: list[list[dict]] = []
            for row in ordered:
                placed = False
                for group in groups:
                    head = group[0]
                    if self._same_value(row, head) or self._close(
                        vec_by_id.get(int(row["id"])),
                        vec_by_id.get(int(head["id"])),
                        threshold,
                    ):
                        group.append(row)
                        placed = True
                        break
                if not placed:
                    groups.append([row])
            clusters.extend([g for g in groups if len(g) > 1])
        return clusters

    async def _cmd_consolidate(self, rest: str, event) -> str:
        tokens = [t for t in (rest or "").split() if t]
        dry_run = any(t in ("--dry-run", "--dry", "-n") for t in tokens)
        unknown = [t for t in tokens if t not in ("--dry-run", "--dry", "-n")]
        if unknown:
            raise ArgError(f"不认识的参数「{' '.join(unknown)}」，用法：{USAGE['consolidate']}")
        # --dry-run 只读不写，不要求写权限；真合并会改库，按写权限管
        if not dry_run:
            ok, why = self._allow(event, write=True)
            if not ok:
                return why

        store = self._get_store()
        rows = store.active_rows()
        threshold = self._conf_float("dedupe_threshold", DEFAULT_DEDUPE_THRESHOLD)
        clusters = await self._consolidation_clusters(store, rows, threshold)
        drops = sum(len(cluster) - 1 for cluster in clusters)
        head = f"扫描 {len(rows)} 条，可合并 {len(clusters)} 组"
        if drops:
            head += f"（涉及 {drops} 条重复）"
        if not clusters:
            if threshold <= 0:
                return head + "\n（dedupe_threshold=0，只合并一字不差的值）没有可合并的记录。"
            return head + "\n库里没有同 scope + 同主语且语义相近的记录。"

        plans = []
        for cluster in clusters:
            keeper = max(
                cluster,
                key=lambda r: (
                    len(str(r.get("value") or "")),
                    str(r.get("updated_at") or ""),
                    int(r.get("id") or 0),
                ),
            )
            plans.append(
                {
                    "keeper": keeper,
                    "drop": [r for r in cluster if int(r["id"]) != int(keeper["id"])],
                    "importance": max(
                        float(r.get("importance") or 0.0) for r in cluster
                    ),
                    "access_count": sum(int(r.get("access_count") or 0) for r in cluster),
                }
            )

        def summarize(plan) -> str:
            keeper = plan["keeper"]
            return (
                f"{keeper.get('scope')}|{keeper.get('subject')} → 保留 #{keeper.get('id')}"
                f"（合并 {len(plan['drop'])} 条，importance {plan['importance']:.2f}，"
                f"access {plan['access_count']}）"
            )

        lines = [summarize(plan) for plan in plans[:5]]
        if dry_run:
            return f"{head}\n" + "\n".join(lines) + "\n（dry-run）没改库；去掉 --dry-run 才会真的作废旧记录。"

        dropped_ids: list[int] = []
        for plan in plans:
            result = store.merge_rows(
                int(plan["keeper"]["id"]),
                [int(r["id"]) for r in plan["drop"]],
                importance=plan["importance"],
                access_count=plan["access_count"],
            )
            dropped_ids.extend(result["dropped"])
        ids = "、".join(f"#{i}" for i in dropped_ids)
        return f"{head}\n已作废 {len(dropped_ids)} 条：{ids}\n" + "\n".join(lines)

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
        types = st.get("types") or {}
        types_line = "、".join(f"{name} {n}" for name, n in types.items()) or "无"
        lines = [
            f"有效 {st['active']} 条 / 历史 {st['history']} 条",
            f"分池：{scopes}",
            f"类型：{types_line}",
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

    def _cmd_import_lm(self, rest: str, event) -> str:
        """从 LivingMemory 插件搬家：自己找库、只读打开、切三元组写进来。

        同步跑（和 /mem import 一致）：一次管理操作，几百上千条 sqlite 读写是毫秒级，
        不值得为它把 handler 改成线程池；`--dry-run` 能在真写之前先看数。
        """
        ok, why = self._allow(event, write=True)
        if not ok:
            return why
        opts = parse_import_lm(rest)
        store = self._get_store()
        configured = opts["path"] or self._conf_str("lm_db_path")
        found = probe(self._data_dirs(), configured=configured)
        if not found["path"]:
            return format_not_found(found["tried"])
        logger.info(f"[mem] LivingMemory 搬家：{found['path']}（来源：{found['source']}）")
        try:
            result = run_import(
                store,
                db_path=found["path"],
                scope=self._scope(),
                dry_run=opts["dry_run"],
                limit=opts["limit"],
            )
        except LivingMemoryError as e:
            logger.warning(f"[mem] LivingMemory 搬家失败：{e}")
            return f"搬家没成功：{e}\n库里一个字没写，对方的库也没动过。"
        if not result["dry_run"]:
            logger.info(
                f"[mem] 搬家完成：读 {result['read']} 条，导入 "
                f"{result['added'] + result['updated']} 条（新增 {result['added']} / "
                f"覆盖 {result['updated']}）"
            )
        return format_report(result)

    # ── LLM 工具 ──────────────────────────────────────────────────────
    @filter.llm_tool(name="memory_add")
    async def memory_add(self, event: AstrMessageEvent, subject: str, predicate: str,
                         value: str, mtype: str = ""):
        """记住一条跨会话共享的长期事实（同一主语+谓词会覆盖旧值）。

        value 必须是一句判断句：主语 + 谓词 + 值，尽量 ≤120 字；不要写日记体过程。
        mtype 决定记忆寿命：fact 长期、preference 一年、event 一个月、plan 两天。

        Args:
            subject(string): 主语，例如「点点」或「用户的服务器」
            predicate(string): 谓词，例如「喜欢」或「部署在」
            value(string): 内容（一句事实，≤120 字；以「我/他/她/今天/昨天」开头的叙事会被拒）
            mtype(string): 可选，fact / preference / event / plan；留空自动判断（含「待办/明天」等线索判 plan）
        """
        ok, why = self._allow(event, write=True)
        if not ok:
            return why
        subject = normalize_text(subject)
        predicate = normalize_text(predicate)
        value = normalize_text(value)
        problem = self._validate(subject, predicate, value)
        if problem:
            return problem
        if mtype:
            resolved = normalize_mtype(mtype)
        else:
            resolved = infer_mtype(
                subject, predicate, value, self._conf_str("default_mtype") or DEFAULT_MTYPE
            )
        try:
            result = await self._write_fact(
                self._scope(), subject, predicate, value, source="llm", mtype=resolved
            )
        except StoreError as e:
            logger.warning(f"[mem] 工具写入失败：{e}")
            return f"记忆库读不了：{e}"
        line = f"已记住 #{result['id']} [{resolved}]：{subject} · {predicate} = {value}"
        if result["superseded"]:
            old = "、".join(f"#{i}" for i in result["superseded"])
            line += f"（旧值已作废：{old}）"
        return line

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
            f"- {r['subject']} · {r['predicate']}：{r['value']}"
            + (f"（更新 {short_ts(r.get('updated_at'))}）" if r.get("updated_at") else "")
            + ("［已作废］" if r.get("valid_to") else "")
            for r in result.rows
        )

    # ── 注入钩子（默认关闭） ──────────────────────────────────────────
    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """auto_inject_n > 0 时注入 top-N 条：先按主语去重，再按 inject_char_cap 裁字。"""
        n = self._conf_int("auto_inject_n", 0)
        if n <= 0:
            return
        try:
            query = (getattr(event, "message_str", "") or "").strip()
            if not query:
                return
            store = self._get_store()
            # 多召回一些候选：按主语去重后会掉条数，只取 n 条可能不够 n
            result = await self._build_retriever(store).search(query, limit=max(n, n * 3))
            if not result.rows:
                return
            # 新→旧排序后再去重 → 每个主语只留最新那条；注入顺序也自然是新→旧，
            # 超字数时从尾部（最旧）砍。
            candidates = sorted(
                result.rows,
                key=lambda r: (str(r.get("updated_at") or ""), int(r.get("id") or 0)),
                reverse=True,
            )
            picked: list[dict] = []
            seen: set[str] = set()
            for row in candidates:
                subj = str(row.get("subject") or "")
                if subj in seen:
                    continue
                seen.add(subj)
                picked.append(row)
            picked = picked[:n]

            cap = self._conf_int("inject_char_cap", DEFAULT_INJECT_CHAR_CAP)
            lines: list[str] = []
            used = 0
            for row in picked:
                line = f"- {row['subject']} · {row['predicate']}：{clip(row['value'], 200)}"
                if cap > 0 and used + len(line) > cap:
                    break
                lines.append(line)
                used += len(line)
            if not lines:
                return
            block = "\n".join(lines)
            prompt = (getattr(req, "system_prompt", "") or "").rstrip()
            req.system_prompt = f"{prompt}\n\n[长期记忆（可能过期，仅供参考）]\n{block}".lstrip()
        except Exception as e:  # noqa: BLE001 注入失败绝不能打断聊天
            logger.warning(f"[mem] 记忆注入失败，本轮跳过：{type(e).__name__}: {e}")
