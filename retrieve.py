"""召回 / 重排 / 三级降级。**不 import astrbot**，embedder 和 reranker 都是注入进来的
可调用对象，所以离线单测能跑全链路。

检索链路（按规格）：

    关键词 LIKE 召回 ┐
                     ├→ 按 mem id 去重 → 向量打分 → rerank 取 top-K → 输出
    向量召回        ┘

降级链（任何一级失败只记 note，绝不给用户抛错）：

    1. 向量 + 重排都通       → mode = "vector+rerank"
    2. 向量通、重排挂        → mode = "vector"
    3. 向量挂、重排通        → mode = "keyword+rerank"
    4. 全挂 / 没配 provider   → mode = "keyword"

向量缓存：vec 表按 (mem_id, model) 存 float32；同一 model 下内容哈希没变就不重复
调 embedding。model_key 变了（换模型 / 换维度）→ 缓存全部视为过期，先按关键词出结果，
能算的再补算，算不了就只走关键词。
"""

from __future__ import annotations

import array
import asyncio
import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime

DEFAULT_RECALL_K = 30
DEFAULT_RERANK_K = 5
EMBED_TIMEOUT = 15.0  # 秒；embedding 调用超时即降级
RERANK_TIMEOUT = 20.0  # 秒；rerank 调用超时即降级
DEFAULT_MIN_SCORE = 0.2  # 噪声地板默认值（可用配置 min_score 覆盖）
MIN_VECTOR_SCORE = DEFAULT_MIN_SCORE  # 兼容旧引用；实际取 Retriever.min_score


# ── 纯函数工具（单测直接用） ───────────────────────────────────────────


def text_hash(subject: str, predicate: str, value: str) -> str:
    """内容哈希：向量缓存的校验键。"""
    raw = f"{subject}|{predicate}|{value}".encode()
    return hashlib.sha256(raw).hexdigest()


def value_hash(value: str) -> str:
    """只对 value 做哈希：写入相似去重用的是「纯 value 向量」，缓存键要和它对齐。"""
    return hashlib.sha256(f"value:{value}".encode()).hexdigest()


def _ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def newer(left, right) -> bool:
    """left 是否比 right 新（ISO 串解析失败就退回字符串比较）。"""
    a, b = _ts(left), _ts(right)
    if a is not None and b is not None:
        return a > b
    return str(left or "") > str(right or "")


def dedupe_rows(rows: list[dict], *, key=None) -> list[dict]:
    """按 key 分组，每组只留 `updated_at` 最新的一条；位置取组内首次出现处。

    默认按 `(subject, predicate)` 分组 —— 检索输出前用它，防止同一条事实的历史版本
    一起返回。注入时传 `key=lambda r: r["subject"]` 就是按主语去重。
    """
    if key is None:
        def key(row: dict):
            return (str(row.get("subject") or ""), str(row.get("predicate") or ""))

    best: dict = {}
    order: list = []
    for row in rows or []:
        slot = key(row)
        if slot not in best:
            best[slot] = row
            order.append(slot)
        elif newer(row.get("updated_at"), best[slot].get("updated_at")):
            best[slot] = row
    return [best[slot] for slot in order]


def pack_vec(vec: list[float]) -> bytes:
    """float32 打包（和 vec 表的 BLOB 对应）。"""
    return array.array("f", [float(x) for x in vec]).tobytes()


def unpack_vec(blob: bytes) -> list[float]:
    out = array.array("f")
    out.frombytes(blob)
    return out.tolist()


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度；长度不等 / 全零向量返回 0.0（不抛错）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def split_terms(query: str) -> list[str]:
    """关键词切分：空白切，去重保序；整句也留一份（中文用户常不带空格）。"""
    raw = (query or "").strip()
    if not raw:
        return []
    terms: list[str] = []
    whole = raw.lower()
    if whole:
        terms.append(whole)
    for token in raw.replace("，", " ").replace(",", " ").split():
        token = token.strip().lower()
        if token and token not in terms:
            terms.append(token)
    return terms


def row_text(row: dict) -> str:
    return f"{row.get('subject', '')} {row.get('predicate', '')} {row.get('value', '')}".strip()


def keyword_score(row: dict, terms: list[str]) -> float:
    """命中词数 / 词数，0~1。"""
    if not terms:
        return 0.0
    blob = f"{row.get('subject', '')}|{row.get('predicate', '')}|{row.get('value', '')}".lower()
    hits = sum(1 for t in terms if t in blob)
    return hits / len(terms)


def normalize_rerank_result(result) -> list[tuple[int, float]]:
    """把各家 rerank 的返回统一成 [(index, score)]。

    AstrBot 的 `RerankResult`（astrbot/core/provider/entities.py:609）字段是
    `index` / `relevance_score`；也兼容 dict 和两元组，方便离线桩。
    """
    pairs: list[tuple[int, float]] = []
    for item in result or []:
        try:
            if isinstance(item, dict):
                idx, score = item.get("index"), item.get("relevance_score", item.get("score", 0.0))
            elif isinstance(item, (tuple, list)) and len(item) >= 2:
                idx, score = item[0], item[1]
            else:
                idx, score = getattr(item, "index"), getattr(item, "relevance_score", 0.0)
            pairs.append((int(idx), float(score)))
        except (TypeError, ValueError):
            continue
    return pairs


# ── 数据结构 ───────────────────────────────────────────────────────────


@dataclass
class Candidate:
    row: dict
    kw: float = 0.0
    vec: float | None = None
    score: float = 0.0
    scored: bool = False


@dataclass
class RetrievalResult:
    rows: list[dict] = field(default_factory=list)
    mode: str = "keyword"
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.rows)


# ── 检索器 ─────────────────────────────────────────────────────────────


class Retriever:
    """一次检索 = 召回 + 向量打分 + 重排 + 降级。

    Args:
        store: store.Store 实例（有 active_rows / keyword_search / vec_get / vec_put）。
        embed: `async (text) -> list[float]`，None 表示没配 embedding。
        rerank: `async (query, docs, top_n) -> 可被 normalize_rerank_result 处理的结果`。
        model_key: 缓存键（provider id + 模型名 + 维度）；变了就等于缓存全过期。
    """

    def __init__(
        self,
        store,
        *,
        embed=None,
        rerank=None,
        model_key: str = "",
        recall_k: int = DEFAULT_RECALL_K,
        rerank_k: int = DEFAULT_RERANK_K,
        min_score: float = DEFAULT_MIN_SCORE,
        logger=None,
    ) -> None:
        self.store = store
        self.embed = embed
        self.rerank = rerank
        self.model_key = model_key or "default"
        self.recall_k = max(1, int(recall_k or DEFAULT_RECALL_K))
        self.rerank_k = max(1, int(rerank_k or DEFAULT_RERANK_K))
        try:
            self.min_score = float(min_score)
        except (TypeError, ValueError):
            self.min_score = DEFAULT_MIN_SCORE
        self.logger = logger

    # -- 日志：只 warning，绝不冒泡 --
    def _note(self, notes: list[str], message: str) -> None:
        notes.append(message)
        if self.logger is not None:
            try:
                self.logger.warning(f"[mem] {message}")
            except Exception:  # noqa: BLE001
                pass

    async def _embed_text(self, text: str) -> list[float]:
        vec = await asyncio.wait_for(self.embed(text), timeout=EMBED_TIMEOUT)
        return [float(x) for x in (vec or [])]

    async def _row_vector(self, row: dict, notes: list[str]) -> list[float] | None:
        """取某行向量：缓存命中直接用；过期/缺失就现算并落库。失败返回 None。"""
        digest = text_hash(row["subject"], row["predicate"], row["value"])
        cached = self.store.vec_get(row["id"], self.model_key)
        if cached:
            try:
                vec = unpack_vec(cached["vec"])
            except (ValueError, TypeError):
                vec = []
            if cached.get("text_hash") == digest and cached.get("dim") == len(vec) and vec:
                return vec
        text = row_text(row)
        try:
            vec = await self._embed_text(text)
        except Exception as e:  # noqa: BLE001 单条失败不影响整体
            self._note(notes, f"embedding 调用失败（#{row['id']}）：{type(e).__name__}: {e}")
            return None
        if not vec:
            self._note(notes, f"embedding 返回空向量（#{row['id']}），该条走关键词")
            return None
        try:
            self.store.vec_put(row["id"], self.model_key, len(vec), pack_vec(vec), digest)
        except Exception as e:  # noqa: BLE001 缓存写失败不影响本次结果
            self._note(notes, f"向量缓存写入失败（#{row['id']}）：{e}")
        return vec

    async def search(self, query: str, *, limit: int | None = None) -> RetrievalResult:
        top = max(1, int(limit or self.rerank_k))
        notes: list[str] = []
        query = (query or "").strip()
        if not query:
            return RetrievalResult([], "keyword", notes)

        terms = split_terms(query)
        pool: dict[int, Candidate] = {}
        try:
            for row in self.store.keyword_search(terms, limit=self.recall_k):
                pool[row["id"]] = Candidate(row=row, kw=keyword_score(row, terms))
        except Exception as e:  # noqa: BLE001
            self._note(notes, f"关键词召回失败：{e}")

        try:
            recent = self.store.active_rows(limit=self.recall_k)
        except Exception as e:  # noqa: BLE001
            self._note(notes, f"读取记忆失败：{e}")
            recent = []
        for row in recent:
            pool.setdefault(row["id"], Candidate(row=row, kw=keyword_score(row, terms)))

        if not pool:
            return RetrievalResult([], "keyword", notes)

        # ── 向量 ──────────────────────────────────────────────
        vector_ready = False
        query_vec: list[float] = []
        if self.embed is not None:
            try:
                query_vec = await self._embed_text(query)
                vector_ready = bool(query_vec)
                if not vector_ready:
                    self._note(notes, "查询向量为空，退关键词")
            except Exception as e:  # noqa: BLE001 404 / 超时 / 解析失败都走这里
                self._note(notes, f"查询 embedding 不可用（{type(e).__name__}: {e}），退关键词")

        if vector_ready:
            scored = 0
            for cand in pool.values():
                vec = await self._row_vector(cand.row, notes)
                if vec is None:
                    continue
                cand.vec = cosine(query_vec, vec)
                scored += 1
            if scored == 0:
                vector_ready = False
                self._note(notes, "没有任何一条能算出向量，整体退关键词")

        # ── 打分 ──────────────────────────────────────────────
        for cand in pool.values():
            if vector_ready and cand.vec is not None:
                cand.score = 0.75 * cand.vec + 0.25 * cand.kw
            else:
                cand.score = 0.15 * cand.kw
            cand.scored = True

        ranked = sorted(
            pool.values(),
            key=lambda c: (c.score, str(c.row.get("updated_at") or "")),
            reverse=True,
        )[: self.recall_k]

        # 噪声地板：关键词一个都没中、向量相似度又低于 min_score → 不是「相关记忆」，丢掉。
        # 不设这个的话，库里任何一条都会被当成「最相关」返回（实测 /mem search 月亮
        # 会把「小满 也喜欢 看星星」当结果），比空手而归更糟。
        # 阈值可配（min_score，默认 0.2）：调高 = 更严格，弱相关直接不给。
        ranked = [
            c
            for c in ranked
            if c.kw > 0 or (c.vec is not None and c.vec >= self.min_score)
        ]

        # ── 重排 ──────────────────────────────────────────────
        rerank_ready = False
        if self.rerank is not None and len(ranked) > 1:
            try:
                docs = [row_text(c.row) for c in ranked]
                raw = await asyncio.wait_for(
                    self.rerank(query, docs, len(docs)), timeout=RERANK_TIMEOUT
                )
                pairs = normalize_rerank_result(raw)
                if pairs:
                    ordered: list[Candidate] = []
                    seen: set[int] = set()
                    for idx, score in sorted(pairs, key=lambda p: p[1], reverse=True):
                        if 0 <= idx < len(ranked) and idx not in seen:
                            cand = ranked[idx]
                            cand.score = score
                            ordered.append(cand)
                            seen.add(idx)
                    ranked = ordered + [c for i, c in enumerate(ranked) if i not in seen]
                    rerank_ready = True
                else:
                    self._note(notes, "rerank 返回空，保持原排序")
            except Exception as e:  # noqa: BLE001 超时 / 接口挂了都走这里
                self._note(notes, f"rerank 不可用（{type(e).__name__}: {e}）")

        if vector_ready and rerank_ready:
            mode = "vector+rerank"
        elif vector_ready:
            mode = "vector"
        elif rerank_ready:
            mode = "keyword+rerank"
        else:
            mode = "keyword"

        rows = []
        for cand in ranked:
            row = dict(cand.row)
            row["score"] = round(float(cand.score), 4)
            rows.append(row)

        # 输出前净化：同 (subject, predicate) 只留 updated_at 最新的一条。
        # 同一条事实在多个 scope / 多次导入后可能有多条有效记录，一起返回会让模型
        # 同时看到新旧两个值、还分不清哪个是现在的。
        rows = dedupe_rows(rows)[:top]
        return RetrievalResult(rows, mode, notes)
