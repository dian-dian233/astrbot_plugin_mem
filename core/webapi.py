"""WebUI 用的数据整理函数（纯函数，不依赖 astrbot，便于离线自测）。

只做「把 store 里的行整理成前端要的结构」，不碰业务语义。
"""

import json
from collections import defaultdict

MTYPES = ("fact", "preference", "event", "plan")
MAX_GRAPH_NODES = 300
PREVIEW_LEN = 80


def _ts(row):
    return str(row.get("updated_at") or row.get("created_at") or "")


def _clip(text, n=PREVIEW_LEN):
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def list_payload(rows, *, q="", mtype="", limit=20, offset=0):
    """筛选 + 分页。rows 应已经是有效记录。"""
    keyword = (q or "").strip().lower()
    want = (mtype or "").strip().lower()

    def hit(row):
        if want and want != "all" and str(row.get("mtype", "")).lower() != want:
            return False
        if not keyword:
            return True
        blob = " ".join(
            str(row.get(k, "")) for k in ("subject", "predicate", "value", "mtype")
        ).lower()
        return keyword in blob

    filtered = [r for r in rows if hit(r)]
    filtered.sort(key=_ts, reverse=True)
    limit = max(1, min(int(limit or 20), 200))
    offset = max(0, int(offset or 0))
    page = filtered[offset : offset + limit]
    items = [
        {
            "id": r.get("id"),
            "subject": r.get("subject", ""),
            "predicate": r.get("predicate", ""),
            "value": r.get("value", ""),
            "mtype": r.get("mtype", "fact"),
            "importance": round(float(r.get("importance") or 0), 2),
            "scope": r.get("scope", "global"),
            "updated_at": _ts(r),
            "access_count": int(r.get("access_count") or 0),
        }
        for r in page
    ]
    return {
        "total": len(filtered),
        "limit": limit,
        "offset": offset,
        "items": items,
    }


def stats_payload(rows, *, extra=None):
    """总量、各类型分布、字段健康度。"""
    by_type = {t: 0 for t in MTYPES}
    for r in rows:
        t = str(r.get("mtype") or "fact").lower()
        by_type[t] = by_type.get(t, 0) + 1
    importances = [float(r.get("importance") or 0) for r in rows]
    payload = {
        "total": len(rows),
        "by_type": by_type,
        "avg_importance": round(sum(importances) / len(importances), 2) if importances else 0,
        "high_importance": sum(1 for x in importances if x >= 0.9),
        "with_vectors": sum(1 for r in rows if r.get("has_vec")),
    }
    if extra:
        payload.update(extra)
    return payload


def graph_payload(rows, *, limit=MAX_GRAPH_NODES):
    """按 subject 聚簇：每个 subject 一个中心节点，挂它下面的事实。"""
    groups = defaultdict(list)
    for r in rows:
        groups[str(r.get("subject") or "?")].append(r)

    truncated = 0
    nodes, edges = [], []
    used = 0
    for idx, (subject, items) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1]))):
        if used >= limit:
            truncated += sum(len(v) for k, v in list(groups.items())[idx:])
            break
        hub = f"s{idx}"
        nodes.append(
            {
                "id": hub,
                "label": subject,
                "kind": "subject",
                "mtype": "hub",
                "importance": 0,
                "value": f"{len(items)} 条",
            }
        )
        used += 1
        for row in items:
            if used >= limit:
                truncated += 1
                continue
            nid = f"m{row.get('id')}"
            nodes.append(
                {
                    "id": nid,
                    "label": _clip(row.get("predicate"), 12),
                    "kind": "memory",
                    "mtype": str(row.get("mtype") or "fact"),
                    "importance": round(float(row.get("importance") or 0), 2),
                    "value": _clip(row.get("value")),
                }
            )
            edges.append({"source": hub, "target": nid})
            used += 1
    return {"nodes": nodes, "edges": edges, "truncated": truncated, "groups": len(groups)}


def error_payload(message, code="bad_request"):
    return {"ok": False, "code": code, "message": str(message)}


def ok_payload(**data):
    out = {"ok": True}
    out.update(data)
    return out


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False)
