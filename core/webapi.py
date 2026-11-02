"""WebUI 管理页的纯标准库逻辑层（不 import astrbot，能单独跑单测）。

main.py 里的 Web API handler 只做三件事：拿参数 → 调这里 → 调 `core/store.py`
现成方法；形状校验、过滤、分页、关系图布局全在这一层，便于离线断言。

约定：
    - 任何参数问题抛 `WebApiError`，handler 统一转 400 + 中文提示；
    - 单条记录字段缺失不抛异常，`shape_item` 一律给默认值，避免一条脏数据把整页拖垮。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

#: 关系图最多画多少个节点（前端同步用这个数做提示）
GRAPH_MAX_NODES = 300

#: 列表默认 / 最大每页条数
DEFAULT_LIMIT = 20
MAX_LIMIT = 200

#: 合法记忆类型
MEM_TYPES = ("fact", "preference", "event", "plan")

#: 悬停 / 表格里 value 的展示长度
VALUE_PREVIEW = 60


class WebApiError(Exception):
    """参数不合法：handler 捕获后返回 400。message 直接给用户看。"""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.message = str(message)
        self.status_code = int(status_code)


# ── 参数解析 ──────────────────────────────────────────────────────────


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):  # query 里 ?q=a&q=b 会给 list
        value = value[0] if value else ""
    return str(value).strip()


def parse_int(raw: Any, *, default: int, low: int, high: int, label: str) -> int:
    """query 里的整数参数。空值走默认，非法抛 400。"""
    text = _text(raw)
    if not text:
        return default
    try:
        number = int(text)
    except (TypeError, ValueError):
        raise WebApiError(f"{label} 必须是整数，收到「{text}」") from None
    if number < low or number > high:
        raise WebApiError(f"{label} 要在 {low}~{high} 之间，收到 {number}")
    return number


def parse_list_query(query: Mapping[str, Any] | None) -> dict:
    """`GET list?q=&type=&limit=&offset=` → 已校验的参数字典。"""
    query = query or {}
    q = _text(query.get("q"))
    if len(q) > 200:
        raise WebApiError(f"搜索词太长了（上限 200 字），收到 {len(q)} 字")
    mtype = _text(query.get("type")).lower()
    if mtype and mtype not in MEM_TYPES:
        raise WebApiError(
            f"type 只能是 {' / '.join(MEM_TYPES)}，收到「{mtype}」"
        )
    return {
        "q": q,
        "type": mtype,
        "limit": parse_int(
            query.get("limit"), default=DEFAULT_LIMIT, low=1, high=MAX_LIMIT, label="limit"
        ),
        "offset": parse_int(
            query.get("offset"), default=0, low=0, high=10_000_000, label="offset"
        ),
    }


def parse_bool(raw: Any, *, default: bool = False, label: str = "参数") -> bool:
    """JSON body 里的布尔开关：接受 true/false、"1"/"0"、"true"/"false"、on/off。"""
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = _text(raw).lower()
    if text in ("1", "true", "yes", "on", "是"):
        return True
    if text in ("0", "false", "no", "off", "否", ""):
        return False
    raise WebApiError(f"{label} 要是 true / false，收到「{text}」")


def parse_add_payload(payload: Mapping[str, Any] | None) -> dict:
    """`POST add` 的 body 校验。语义校验（长度 / 叙事体）交给插件 `_validate`。"""
    if not isinstance(payload, Mapping):
        raise WebApiError("请求体要是 JSON 对象，例如 {\"subject\":\"点点\",\"predicate\":\"喜欢\",\"value\":\"美式\"}")
    subject = _text(payload.get("subject"))
    predicate = _text(payload.get("predicate"))
    value = _text(payload.get("value"))
    missing = [
        name
        for name, val in (("subject", subject), ("predicate", predicate), ("value", value))
        if not val
    ]
    if missing:
        raise WebApiError(f"缺少必填字段：{'、'.join(missing)}")

    mtype = _text(payload.get("mtype")).lower()
    if mtype and mtype not in MEM_TYPES:
        raise WebApiError(f"mtype 只能是 {' / '.join(MEM_TYPES)}，收到「{mtype}」")

    importance_raw = payload.get("importance")
    importance = None
    if importance_raw is not None and _text(importance_raw) != "":
        try:
            importance = float(importance_raw)
        except (TypeError, ValueError):
            raise WebApiError(f"importance 要是 0~1 的小数，收到「{importance_raw}」") from None
        if not 0.0 <= importance <= 1.0:
            raise WebApiError(f"importance 要在 0~1 之间，收到 {importance}")

    return {
        "subject": subject,
        "predicate": predicate,
        "value": value,
        "scope": _text(payload.get("scope")),
        "mtype": mtype or None,
        "importance": importance,
    }


def parse_id_payload(payload: Mapping[str, Any] | None) -> int:
    """`POST forget {id}` 的 body 校验。"""
    if not isinstance(payload, Mapping):
        raise WebApiError("请求体要是 JSON 对象，例如 {\"id\": 12}")
    raw = payload.get("id")
    text = _text(raw)
    if not text:
        raise WebApiError("缺少 id（要作废的记忆编号）")
    try:
        mem_id = int(text)
    except (TypeError, ValueError):
        raise WebApiError(f"id 必须是整数，收到「{text}」") from None
    if mem_id <= 0:
        raise WebApiError(f"id 必须是正整数，收到 {mem_id}")
    return mem_id


# ── 记录整形 / 过滤 / 分页 ────────────────────────────────────────────


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def shape_item(row: Mapping[str, Any]) -> dict:
    """DB 行 → 前端表格行。缺字段给默认值，永不抛。"""
    row = row or {}
    value = str(row.get("value") or "")
    return {
        "id": int(row.get("id") or 0),
        "subject": str(row.get("subject") or ""),
        "predicate": str(row.get("predicate") or ""),
        "value": value,
        "value_preview": value[:VALUE_PREVIEW],
        "mtype": str(row.get("mtype") or "fact"),
        "importance": round(_as_float(row.get("importance")), 3),
        "scope": str(row.get("scope") or ""),
        "source": str(row.get("source") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "created_at": str(row.get("created_at") or ""),
        "valid_to": str(row.get("valid_to") or ""),
        "active": not str(row.get("valid_to") or ""),
    }


def matches(row: Mapping[str, Any], q: str, mtype: str) -> bool:
    """搜索词命中 subject / predicate / value 任一；mtype 精确匹配。"""
    if mtype and str(row.get("mtype") or "") != mtype:
        return False
    if not q:
        return True
    needle = q.lower()
    for field in ("subject", "predicate", "value"):
        if needle in str(row.get(field) or "").lower():
            return True
    return False


def filter_rows(rows: Iterable[Mapping[str, Any]], q: str, mtype: str) -> list[dict]:
    return [dict(row) for row in rows if matches(row, q, mtype)]


def paginate(rows: list[dict], limit: int, offset: int) -> tuple[list[dict], int]:
    """返回 (当前页行, 总数)。offset 越界给空页，不报错。"""
    total = len(rows)
    return rows[offset : offset + limit], total


def build_list(rows: Iterable[Mapping[str, Any]], query: Mapping[str, Any] | None) -> dict:
    """`GET list` 的完整实现：校验 → 过滤 → 分页 → 整形。"""
    params = parse_list_query(query)
    matched = filter_rows(rows, params["q"], params["type"])
    page, total = paginate(matched, params["limit"], params["offset"])
    return {
        "total": total,
        "items": [shape_item(row) for row in page],
        "limit": params["limit"],
        "offset": params["offset"],
        "q": params["q"],
        "type": params["type"],
    }


# ── 统计 ──────────────────────────────────────────────────────────────


def build_stats(store_stats: Mapping[str, Any], provider: Mapping[str, Any] | None = None) -> dict:
    """`store.stats()` + provider 状态 → 前端四个卡要的字段。"""
    store_stats = store_stats or {}
    active = int(store_stats.get("active") or 0)
    history = int(store_stats.get("history") or 0)
    provider = provider or {}
    return {
        "total": active + history,
        "active": active,
        "history": history,
        "types": dict(store_stats.get("types") or {}),
        "scopes": list(store_stats.get("scopes") or []),
        "last_updated": str(store_stats.get("last_updated") or ""),
        "vec_rows": int(store_stats.get("vec_rows") or 0),
        "path": str(store_stats.get("path") or ""),
        "provider": {
            "ok": bool(provider.get("ok")),
            "id": str(provider.get("id") or ""),
            "model": str(provider.get("model") or ""),
        },
    }


# ── 关系图 ────────────────────────────────────────────────────────────


def build_graph(rows: Iterable[Mapping[str, Any]], max_nodes: int = GRAPH_MAX_NODES) -> dict:
    """同 subject 的记录连成簇（星形：第一个节点当枢纽）。

    节点按 importance 降序、id 升序取前 `max_nodes` 个；超了置 `truncated=True`，
    前端据此提示「只画了前 N 个」。边只连已画出的节点，避免悬空引用。
    """
    shaped = [shape_item(row) for row in rows]
    total = len(shaped)
    ordered = sorted(shaped, key=lambda item: (-item["importance"], item["id"]))
    kept = ordered[: max(0, int(max_nodes))]
    kept_ids = {item["id"] for item in kept}

    edges: list[dict] = []
    seen: set[tuple[int, int]] = set()
    hubs: dict[str, dict] = {}
    # 先按 id 升序找枢纽，保证同一份数据每次画出来的边一样（可复现）
    for node in sorted(kept, key=lambda item: item["id"]):
        subject = node["subject"] or f"#{node['id']}"
        hub = hubs.get(subject)
        if hub is None:
            hubs[subject] = node
            continue
        key = (hub["id"], node["id"])
        if key in seen or hub["id"] not in kept_ids:
            continue
        seen.add(key)
        edges.append({"source": hub["id"], "target": node["id"]})

    nodes = [
        {
            "id": node["id"],
            "label": f"{node['subject']} · {node['predicate']}".strip(" ·"),
            "mtype": node["mtype"],
            "importance": node["importance"],
            "value": node["value_preview"],
        }
        for node in sorted(kept, key=lambda item: item["id"])
    ]
    return {
        "nodes": nodes,
        "edges": edges,
        "total_nodes": total,
        "shown_nodes": len(nodes),
        "truncated": total > len(nodes),
        "max_nodes": int(max_nodes),
    }


# ── dedupe / consolidate 结果整形 ─────────────────────────────────────


def summarize_dedupe(report: Mapping[str, Any] | None) -> dict:
    """`store.dedupe_slots()` 的结果 → 一行回显 + 结构化字段。"""
    report = report or {}
    groups = int(report.get("mergeable_groups") or 0)
    rows = int(report.get("mergeable_rows") or 0)
    dry_run = bool(report.get("dry_run"))
    scanned = int(report.get("scanned") or 0)
    if groups:
        summary = f"扫描 {scanned} 条，{groups} 组同槽重复（涉及 {rows} 条）"
        summary += "；dry-run 没改库" if dry_run else "，已全部作废"
    else:
        summary = f"扫描 {scanned} 条，没有同槽重复" + ("（dry-run）" if dry_run else "")
    return {
        "summary": summary,
        "scanned": scanned,
        "groups": groups,
        "rows": rows,
        "dry_run": dry_run,
        "applied": bool(report.get("applied")),
        "superseded": list(report.get("superseded") or []),
    }


def summarize_consolidate(
    plans: list[Mapping[str, Any]] | None,
    *,
    scanned: int,
    dry_run: bool,
    threshold: float = 0.0,
) -> dict:
    """consolidate 的 plans（keeper + drop）→ 一行回显 + 结构化字段。

    plans 由 main.py 的 `_consolidation_plans` 产出（复用指令同一条聚类逻辑）。
    """
    plans = list(plans or [])
    drops = sum(len(plan.get("drop") or []) for plan in plans)
    if plans:
        summary = f"扫描 {scanned} 条，可合并 {len(plans)} 组（涉及 {drops} 条重复）"
        summary += "；dry-run 没改库" if dry_run else "，已作废重复行"
    elif threshold <= 0:
        summary = f"扫描 {scanned} 条，可合并 0 组（dedupe_threshold=0，只合并一字不差的值）"
    else:
        summary = f"扫描 {scanned} 条，没有可合并的记录"
    groups = [
        {
            "keep": int((plan.get("keeper") or {}).get("id") or 0),
            "drop": [int((row or {}).get("id") or 0) for row in (plan.get("drop") or [])],
            "subject": str((plan.get("keeper") or {}).get("subject") or ""),
        }
        for plan in plans
    ]
    return {
        "summary": summary,
        "scanned": int(scanned),
        "groups": groups,
        "groups_count": len(groups),
        "rows": drops,
        "dry_run": bool(dry_run),
        "applied": bool(drops) and not dry_run,
        "threshold": float(threshold or 0.0),
    }


__all__ = [
    "GRAPH_MAX_NODES",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MEM_TYPES",
    "WebApiError",
    "parse_int",
    "parse_list_query",
    "parse_bool",
    "parse_add_payload",
    "parse_id_payload",
    "shape_item",
    "matches",
    "filter_rows",
    "paginate",
    "build_list",
    "build_stats",
    "build_graph",
    "summarize_dedupe",
    "summarize_consolidate",
]
