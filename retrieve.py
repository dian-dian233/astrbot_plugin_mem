"""召回 / 融合 / 重排 / 衰减加权。**不 import astrbot**，embedder 和 reranker 都是注入进来的
可调用对象，所以离线单测能跑全链路。

检索链路（v0.3.0，顺序不能乱）：

    向量 top-k ┐
               ├→ RRF 融合 → rerank → decay 加权 → 截断 → 输出
    关键词 top-k ┘

RRF：score = Σ 1/(rrf_k + rank)，`rrf_k` 默认 60；两路都命中且名次接近时加
`agreement_bonus`（默认 0.02，按名次差线性衰减）。某一路为空就只用另一路，
不拿空路稀释（空路没有 rank，自然不贡献分数）。

降权：`final = base * decay * (0.5 + 0.5 * importance)`，decay 见 lifecycle.py。
plan（step）过 TTL 直接剔除，不参与融合。

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

from .lifecycle import (
    DEFAULT_ACCESS_CAP,
    DEFAULT_PROTECTED_IMPORTANCE,
    final_score,
    is_expired,
)

DEFAULT_RECALL_K = 30
DEFAULT_RERANK_K = 5
EMBED_TIMEOUT = 15.0  # 秒；embedding 调用超时即降级
RERANK_TIMEOUT = 20.0  # 秒；rerank 调用超时即降级
DEFAULT_MIN_SCORE = 0.2  # 噪声地板默认值（可用配置 min_score 覆盖）
DEFAULT_RRF_K = 60  # RRF 排名衰减参数（论文推荐值）
DEFAULT_AGREEMENT_BONUS = 0.02  # 两路共识加成上限
BM25_K1 = 1.5
BM25_B = 0.75
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


def bm25_scores(rows: list[dict], terms: list[str], *, k1: float = BM25_K1,
                b: float = BM25_B) -> dict[int, float]:
    """BM25 式关键词打分（纯 Python，不引 jieba）。

    中文没有分词，term 直接用查询词本身，在 `主语|谓词|值` 里数出现次数；
    `dl` 是文档字符长度，`avgdl` 是本次候选集平均长度，`idf` 按候选集内文档频率算。
    只给召回到的候选打分（N 是候选集大小），够用来排名，不追求全局 IDF 稳定。
    """
    if not rows or not terms:
        return {}
    docs: dict[int, str] = {}
    for row in rows:
        try:
            doc_id = int(row["id"])
        except (TypeError, ValueError):
            continue
        docs[doc_id] = (
            f"{row.get('subject', '')}|{row.get('predicate', '')}|{row.get('value', '')}"
        ).lower()
    if not docs:
        return {}
    n = len(docs)
    lengths = {doc_id: max(1, len(text)) for doc_id, text in docs.items()}
    avgdl = sum(lengths.values()) / float(n)
    df = {term: sum(1 for text in docs.values() if term in text) for term in terms}
    out: dict[int, float] = {}
    for doc_id, text in docs.items():
        dl = lengths[doc_id]
        score = 0.0
        for term in terms:
            tf = text.count(term)
            if tf <= 0:
                continue
            idf = math.log(1.0 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * (tf * (k1 + 1)) / (tf + k1 * (1.0 - b + b * dl / avgdl))
        out[doc_id] = score
    return out


def rrf_fuse(vec_ids: list[int], kw_ids: list[int], *, k: int = DEFAULT_RRF_K,
             agreement_bonus: float = 0.0) -> dict[int, dict]:
    """Reciprocal Rank Fusion：score(d) = Σ 1/(k + rank_i(d))。

    Args:
        vec_ids: 向量路召回，按相似度降序的 mem id
        kw_ids: 关键词路召回，按 BM25 降序的 mem id
        k: RRF 参数，越大越平滑（默认 60）
        agreement_bonus: 两路都命中时的共识加成上限；名次越接近加得越多

    Returns:
        {mem_id: {"score", "vec_rank", "kw_rank", "best_rank"}}

    某一路为空时，另一路的 rank 照算 —— 空路不贡献分数，也就不会稀释结果。
    """
    vec_list = [int(i) for i in (vec_ids or [])]
    kw_list = [int(i) for i in (kw_ids or [])]
    if not vec_list and not kw_list:
        return {}
    try:
        rrf_k = max(1, int(k))
    except (TypeError, ValueError):
        rrf_k = DEFAULT_RRF_K
    try:
        bonus_cap = max(0.0, float(agreement_bonus or 0.0))
    except (TypeError, ValueError):
        bonus_cap = 0.0

    vec_rank = {doc: rank for rank, doc in enumerate(vec_list)}
    kw_rank = {doc: rank for rank, doc in enumerate(kw_list)}
    span = max(len(vec_list), len(kw_list), 1)

    fused: dict[int, dict] = {}
    for doc in set(vec_rank) | set(kw_rank):
        score = 0.0
        if doc in vec_rank:
            score += 1.0 / (rrf_k + vec_rank[doc] + 1)
        if doc in kw_rank:
            score += 1.0 / (rrf_k + kw_rank[doc] + 1)
        if bonus_cap and doc in vec_rank and doc in kw_rank:
            gap = abs(vec_rank[doc] - kw_rank[doc])
            score += bonus_cap * (1.0 - gap / span)  # 名次越接近越可信
        ranks = [r for r in (vec_rank.get(doc), kw_rank.get(doc)) if r is not None]
        fused[doc] = {
            "score": score,
            "vec_rank": vec_rank.get(doc),
            "kw_rank": kw_rank.get(doc),
            "best_rank": min(ranks) if ranks else 0,
        }
    return fused


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
    score: float = 0.0   # 最终分（decay 加权后）
    base: float = 0.0    # RRF 分（按本次最高分归一到 [0,1]）或 rerank 分
    rrf: float = 0.0     # 原始 RRF 分（展示 / 调试用）
    decay: float = 1.0   # 实际生效的衰减系数
    vec_rank: int | None = None
    kw_rank: int | None = None
    scored: bool = False


@dataclass
class RetrievalResult:
    rows: list[dict] = field(default_factory=list)
    mode: str = "keyword"
    notes: list[str] = field(default_factory=list)
    fusion: str = ""  # 两路召回条数，例如「RRF：向量 5 + 关键词 3 → 6」

    @property
    def ok(self) -> bool:
        return bool(self.rows)


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
        rrf_k: int = DEFAULT_RRF_K,
        agreement_bonus: float = DEFAULT_AGREEMENT_BONUS,
        access_cap: int = DEFAULT_ACCESS_CAP,
        protected_importance: float = DEFAULT_PROTECTED_IMPORTANCE,
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
        try:
            self.rrf_k = max(1, int(rrf_k))
        except (TypeError, ValueError):
            self.rrf_k = DEFAULT_RRF_K
        try:
            self.agreement_bonus = max(0.0, float(agreement_bonus))
        except (TypeError, ValueError):
            self.agreement_bonus = DEFAULT_AGREEMENT_BONUS
        try:
            self.access_cap = max(1, int(access_cap))
        except (TypeError, ValueError):
            self.access_cap = DEFAULT_ACCESS_CAP
        try:
            self.protected_importance = float(protected_importance)
        except (TypeError, ValueError):
            self.protected_importance = DEFAULT_PROTECTED_IMPORTANCE
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

    async def search(self, query: str, *, limit: int | None = None, now=None) -> RetrievalResult:
        """两路召回 → RRF 融合 → rerank → decay 加权 → 截断。

        `now` 只在单测里注入假时钟；日常留空取系统时间。
        """
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

        # 过期剔除：plan（step）过 TTL 就不再参与检索。不物理删，export 仍能看到。
        if pool:
            before = len(pool)
            pool = {
                key: cand for key, cand in pool.items()
                if not is_expired(
                    cand.row, now=now, protected_importance=self.protected_importance
                )
            }
            dropped = before - len(pool)
            if dropped:
                self._note(notes, f"跳过 {dropped} 条过期记忆（plan 过 TTL）")
        if not pool:
            return RetrievalResult([], "keyword", notes)

        # ── 向量打分 ──────────────────────────────────────────
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

        # ── 两路排名 → RRF ────────────────────────────────────
        # 向量路：按余弦降序；噪声地板只对「关键词一个都没中」的候选生效，
        # 避免库里任意一条都被当成「最相关」。
        vec_ranked = [c for c in pool.values() if c.vec is not None]
        vec_ranked.sort(key=lambda c: c.vec, reverse=True)
        vec_ranked = [
            c for c in vec_ranked if c.kw > 0 or (c.vec is not None and c.vec >= self.min_score)
        ][: self.recall_k]
        vec_ids = [int(c.row["id"]) for c in vec_ranked]

        # 关键词路：BM25 式打分降序
        bm25 = bm25_scores([c.row for c in pool.values()], terms)
        kw_ranked = [c for c in pool.values() if bm25.get(int(c.row["id"]), 0.0) > 0.0]
        kw_ranked.sort(key=lambda c: bm25[int(c.row["id"])], reverse=True)
        kw_ranked = kw_ranked[: self.recall_k]
        kw_ids = [int(c.row["id"]) for c in kw_ranked]

        fused = rrf_fuse(
            vec_ids, kw_ids, k=self.rrf_k, agreement_bonus=self.agreement_bonus
        )
        if not fused:
            return RetrievalResult([], "keyword", notes)

        ordered = sorted(
            fused.items(), key=lambda item: (-item[1]["score"], item[1]["best_rank"], item[0])
        )
        best_rrf = ordered[0][1]["score"] or 1.0
        ranked: list[Candidate] = []
        for doc_id, info in ordered[: self.recall_k]:
            cand = pool[doc_id]
            cand.rrf = info["score"]
            cand.base = info["score"] / best_rrf  # 归一到 [0,1]，让 decay 加权有可比性
            cand.vec_rank = info["vec_rank"]
            cand.kw_rank = info["kw_rank"]
            ranked.append(cand)

        # ── 重排（在融合之后） ────────────────────────────────
        rerank_ready = False
        if self.rerank is not None and len(ranked) > 1:
            try:
                docs = [row_text(c.row) for c in ranked]
                raw = await asyncio.wait_for(
                    self.rerank(query, docs, len(docs)), timeout=RERANK_TIMEOUT
                )
                pairs = normalize_rerank_result(raw)
                if pairs:
                    reordered: list[Candidate] = []
                    seen: set[int] = set()
                    for idx, score in sorted(pairs, key=lambda p: p[1], reverse=True):
                        if 0 <= idx < len(ranked) and idx not in seen:
                            cand = ranked[idx]
                            cand.base = float(score)
                            reordered.append(cand)
                            seen.add(idx)
                    ranked = reordered + [c for i, c in enumerate(ranked) if i not in seen]
                    rerank_ready = True
                else:
                    self._note(notes, "rerank 返回空，保持 RRF 排序")
            except Exception as e:  # noqa: BLE001 超时 / 接口挂了都走这里
                self._note(notes, f"rerank 不可用（{type(e).__name__}: {e}）")

        # ── decay 加权 → 截断 ─────────────────────────────────
        for cand in ranked:
            cand.score, cand.decay = final_score(
                cand.base,
                cand.row,
                now=now,
                access_cap=self.access_cap,
                protected_importance=self.protected_importance,
            )
        ranked.sort(
            key=lambda c: (
                c.score,
                str(c.row.get("updated_at") or ""),
                int(c.row.get("id") or 0),
            ),
            reverse=True,
        )

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
            row["decay"] = round(float(cand.decay), 4)
            row["rrf"] = round(float(cand.rrf), 6)
            rows.append(row)

        # 输出前净化：同 (subject, predicate) 只留 updated_at 最新的一条。
        rows = dedupe_rows(rows)[:top]

        # 访问强化：真正返回给用户的这几条才算「被命中」。
        if rows:
            try:
                self.store.reinforce([int(r["id"]) for r in rows], now=now)
            except Exception as e:  # noqa: BLE001 计数失败不影响检索
                self._note(notes, f"访问计数更新失败：{e}")

        fusion = f"RRF：向量 {len(vec_ids)} + 关键词 {len(kw_ids)} → {len(rows)}"
        return RetrievalResult(rows, mode, notes, fusion)
