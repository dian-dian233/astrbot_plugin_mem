"""记忆生命周期：类型 / TTL / 衰减 / 访问强化。**纯标准库，不 import astrbot**。

四种类型（`mtype`）与默认 TTL（天）/ 衰减方式：

    fact        3650  exp      稳定事实（服务器路径、端口、长期偏好对象）
    preference   365  exp      偏好（口味、风格、说话习惯）
    event         30  exp      发生过的事（一次发布、一次故障）
    plan           2  step     待办 / 计划：过期即不参与检索（v0.2 是直接拒写）

衰减公式（`decay_factor`）：

    exp     exp(-ln2 * days / (ttl/2))        ← 半衰期 = ttl/2
    linear  max(0, 1 - days/ttl)
    step    1.0 if days <= ttl else 0.05

访问强化（`effective_decay`）：被检索命中过的记忆衰减更慢，
`d_eff = d + (1 - d) * min(access_count, access_cap) / access_cap`。
`importance >= protected_importance`（默认 0.9）的记忆完全不衰减。

最终排序分（`final_score`）：

    final = base * d_eff * (0.5 + 0.5 * importance)

`base` 是 RRF 融合分（有 rerank 时是 rerank 分）。

思路来源：lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory
`core/models/memory_atom.py:35 compute_decay_score` 与 `:110 compute_ttl`
（只借公式形状，不引入它的 pydantic / sqlalchemy / networkx 依赖）。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

MTYPES = ("fact", "preference", "event", "plan")
DECAY_TYPES = ("linear", "step", "exp")

DEFAULT_MTYPE = "fact"
DEFAULT_IMPORTANCE = 0.5
DEFAULT_PROTECTED_IMPORTANCE = 0.9
DEFAULT_ACCESS_CAP = 10
DEFAULT_ACCESS_HALVE = 0.5

# 默认 TTL（天），对应配置 ttl_fact / ttl_preference / ttl_event / ttl_plan
DEFAULT_TTL_DAYS = {
    "fact": 3650.0,
    "preference": 365.0,
    "event": 30.0,
    "plan": 2.0,
}

# 默认衰减方式：只有 plan 用 step（过期即失效），其余指数衰减。
DEFAULT_DECAY_TYPE = {
    "fact": "exp",
    "preference": "exp",
    "event": "exp",
    "plan": "step",
}

# 中英文别名（用户手写 / 旧数据里可能出现）
_MTYPE_ALIASES = {
    "factual": "fact", "事实": "fact", "长期": "fact",
    "pref": "preference", "偏好": "preference", "喜好": "preference",
    "event": "event", "事件": "event", "episodic": "event",
    "plan": "plan", "计划": "plan", "待办": "plan", "todo": "plan", "planned": "plan",
}

# 识别「待办 / 计划」的线索词：出现在谓词或内容里就判 plan（可用 --type 覆盖）。
# 刻意收窄：只收明确指向「未来要做的事」的词，避免把「服务器需要重启」这类
# 陈述句误判成待办。
PLAN_HINTS = (
    "待办", "todo", "计划", "提醒", "截止", "deadline", "记得", "别忘",
    "明天", "后天", "下周", "下个月", "月底", "年底",
)


def clamp(value, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


def normalize_mtype(value, default: str = DEFAULT_MTYPE) -> str:
    """任意写法 → 四种类型之一；认不出来就回落到 default。"""
    raw = str(value or "").strip().lower()
    if raw in MTYPES:
        return raw
    if raw in _MTYPE_ALIASES:
        return _MTYPE_ALIASES[raw]
    fallback = str(default or DEFAULT_MTYPE).strip().lower()
    if fallback in MTYPES:
        return fallback
    return _MTYPE_ALIASES.get(fallback, DEFAULT_MTYPE)


def infer_mtype(subject: str = "", predicate: str = "", value: str = "",
                default: str = DEFAULT_MTYPE) -> str:
    """按线索词判断类型：命中 PLAN_HINTS 判 plan，否则用 default。"""
    blob = f"{predicate or ''} {value or ''}".lower()
    if any(hint in blob for hint in PLAN_HINTS):
        return "plan"
    return normalize_mtype(default)


def ttl_for(mtype, overrides: dict | None = None) -> float:
    """该类型的 TTL（天）。overrides 形如 {"fact": 3650, "plan": 2}（来自配置）。"""
    name = normalize_mtype(mtype)
    if overrides:
        raw = overrides.get(name)
        try:
            if raw is not None:
                return max(0.01, float(raw))
        except (TypeError, ValueError):
            pass
    return DEFAULT_TTL_DAYS[name]


def decay_type_for(mtype) -> str:
    return DEFAULT_DECAY_TYPE[normalize_mtype(mtype)]


def parse_ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def age_days(created_at, now: datetime | None = None) -> float:
    """从 created_at 到 now 的天数（负数按 0 算，解析失败按 0 算）。"""
    created = parse_ts(created_at)
    if created is None:
        return 0.0
    ref = now or datetime.now(timezone.utc).astimezone()
    try:
        if created.tzinfo is None and ref.tzinfo is not None:
            created = created.replace(tzinfo=ref.tzinfo)
        elif created.tzinfo is not None and ref.tzinfo is None:
            ref = ref.replace(tzinfo=created.tzinfo)
        delta = (ref - created).total_seconds() / 86400.0
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(0.0, delta)


def decay_factor(decay_type, ttl_days, days_since) -> float:
    """衰减系数 ∈ [0, 1]。ttl_days <= 0 时按 1 天算，避免除零。"""
    effective_ttl = max(1.0, float(ttl_days or 0))
    days = max(0.0, float(days_since or 0))
    name = str(getattr(decay_type, "value", decay_type) or "exp").strip().lower()

    if name == "linear":
        return max(0.0, 1.0 - days / effective_ttl)
    if name == "step":
        return 1.0 if days <= effective_ttl else 0.05

    half_life = effective_ttl / 2.0
    return math.exp(-math.log(2) * days / max(0.5, half_life))


def effective_decay(decay, access_count, access_cap=DEFAULT_ACCESS_CAP) -> float:
    """访问强化：被命中次数越多，衰减越慢；到 access_cap 封顶后不再变慢。"""
    base = clamp(decay)
    try:
        cap = int(access_cap)
    except (TypeError, ValueError):
        cap = DEFAULT_ACCESS_CAP
    if cap <= 0:
        return base
    try:
        count = max(0, int(access_count))
    except (TypeError, ValueError):
        count = 0
    boost = min(count, cap) / float(cap)
    return base + (1.0 - base) * boost


def is_protected(importance, protected_importance=DEFAULT_PROTECTED_IMPORTANCE) -> bool:
    return clamp(importance, 0.0, 1.0) >= clamp(protected_importance)


def is_expired(row: dict, *, now: datetime | None = None,
               protected_importance=DEFAULT_PROTECTED_IMPORTANCE) -> bool:
    """是否已过期。只有 step（plan）过 TTL 会被剔除 —— 见 README「记忆生命周期」。

    exp / linear 只降权不剔除：fact 的 3650 天是衰减尺度，不是「到期销毁」。
    """
    if is_protected(row.get("importance", DEFAULT_IMPORTANCE), protected_importance):
        return False
    decay_type = str(row.get("decay_type") or "exp").lower()
    if decay_type != "step":
        return False
    try:
        ttl = float(row.get("ttl_days") or 0)
    except (TypeError, ValueError):
        return False
    if ttl <= 0:
        return False
    return age_days(row.get("created_at"), now) > ttl


def final_score(base: float, row: dict, *, now: datetime | None = None,
                access_cap=DEFAULT_ACCESS_CAP,
                protected_importance=DEFAULT_PROTECTED_IMPORTANCE) -> tuple[float, float]:
    """返回 (最终分数, 实际生效的衰减系数)。"""
    importance = clamp(row.get("importance", DEFAULT_IMPORTANCE))
    if is_protected(importance, protected_importance):
        decay = 1.0
    else:
        raw = decay_factor(
            row.get("decay_type"),
            row.get("ttl_days"),
            age_days(row.get("created_at"), now),
        )
        decay = effective_decay(raw, row.get("access_count") or 0, access_cap)
    try:
        base_value = float(base)
    except (TypeError, ValueError):
        base_value = 0.0
    return base_value * decay * (0.5 + 0.5 * importance), decay


__all__ = [
    "MTYPES",
    "DECAY_TYPES",
    "DEFAULT_MTYPE",
    "DEFAULT_IMPORTANCE",
    "DEFAULT_PROTECTED_IMPORTANCE",
    "DEFAULT_ACCESS_CAP",
    "DEFAULT_ACCESS_HALVE",
    "DEFAULT_TTL_DAYS",
    "DEFAULT_DECAY_TYPE",
    "PLAN_HINTS",
    "clamp",
    "normalize_mtype",
    "infer_mtype",
    "ttl_for",
    "decay_type_for",
    "parse_ts",
    "age_days",
    "decay_factor",
    "effective_decay",
    "is_protected",
    "is_expired",
    "final_score",
]
