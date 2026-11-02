"""WebUI 数据层离线自测（不依赖 astrbot、不起 HTTP）。

跑法：PYTHONDONTWRITEBYTECODE=1 python3 test_webapi.py
"""
import sys

try:
    from core import webapi
except ImportError:  # 平铺加载
    import webapi  # type: ignore

ROWS = [
    {"id": 1, "subject": "点点", "predicate": "主力开发机", "value": "freestyle-vm",
     "mtype": "fact", "importance": 0.9, "scope": "global", "updated_at": "2026-11-02T10:00:00"},
    {"id": 2, "subject": "点点", "predicate": "待办", "value": "写 README",
     "mtype": "plan", "importance": 0.4, "scope": "global", "updated_at": "2026-11-02T11:00:00"},
    {"id": 3, "subject": "neko", "predicate": "机器", "value": "friend-box",
     "mtype": "event", "importance": 0.6, "scope": "global", "updated_at": "2026-11-02T09:00:00"},
]
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS ", name)
    else:
        FAIL += 1
        print("FAIL ", name)


# 列表：分页
d = webapi.list_payload(ROWS, limit=2, offset=0)
check("分页：total=3, 返回 2 条", d["total"] == 3 and len(d["items"]) == 2)
check("分页：按 updated_at 倒序", [i["id"] for i in d["items"]] == [2, 1])

# 列表：关键词
d = webapi.list_payload(ROWS, q="friend-box")
check("搜索：命中 1 条且是 neko 那条", d["total"] == 1 and d["items"][0]["id"] == 3)

# 列表：类型筛选
d = webapi.list_payload(ROWS, mtype="plan")
check("类型筛选：只剩 plan", d["total"] == 1 and d["items"][0]["mtype"] == "plan")

# 列表：越界参数被夹住
d = webapi.list_payload(ROWS, limit=9999, offset=-5)
check("越界参数：limit 夹到 200、offset 归零", d["limit"] == 200 and d["offset"] == 0)

# 统计
s = webapi.stats_payload(ROWS, extra={"embed": True, "scope": "global"})
check("统计：total=3", s["total"] == 3)
check("统计：类型分布 fact=1/plan=1/event=1", s["by_type"]["fact"] == 1 and s["by_type"]["plan"] == 1 and s["by_type"]["event"] == 1)
check("统计：高重要度 1 条", s["high_importance"] == 1)
check("统计：透传 embed 标记", s["embed"] is True)

# 关系图
g = webapi.graph_payload(ROWS)
hubs = [n for n in g["nodes"] if n["kind"] == "subject"]
check("关系图：2 个主语簇", len(hubs) == 2)
check("关系图：边数 = 记忆条数", len(g["edges"]) == 3)
check("关系图：未截断", g["truncated"] == 0)

g2 = webapi.graph_payload(ROWS, limit=2)
check("关系图：limit=2 时截断并回报", g2["truncated"] >= 1 and len(g2["nodes"]) <= 2)

# 错误体
e = webapi.error_payload("缺少 id")
check("错误体：ok=False 且带 message", e["ok"] is False and e["message"] == "缺少 id")

print(f"\n{PASS}/{PASS + FAIL} 通过")
sys.exit(0 if FAIL == 0 else 1)
