"""从 LivingMemory 插件搬家：只读打开它的 `livingmemory.db`，切三元组写进本插件的 `mem.db`。

**纯标准库，不 import astrbot**（selftest.py 可以脱离框架直接跑）。

为什么不让用户导文件：两个插件装在同一台 AstrBot 上，数据目录是同一棵树，插件自己找得到
库，就能自己搬。用户要做的只有 `/mem import-lm`。

已 grep 核对的对方事实（lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory）：

    main.py:106                 data_dir = StarTools.get_data_dir("astrbot_plugin_livingmemory")
    storage/atom_store.py:59    CREATE TABLE IF NOT EXISTS memory_atoms(...)

    memory_atoms 列：id, parent_memory_id, atom_type, content, entities(JSON), importance,
    confidence, created_at(REAL epoch), last_accessed_at(REAL epoch), last_reinforced_at,
    event_time, ttl_days, expires_at, status('active'), reinforcement_count,
    decay_type('exponential'), session_id, persona_id, metadata(JSON)

    atom_type 实际取值（core/models/memory_atom.py:12）：episodic / factual / relational /
    preference / planned / unknown（老版本另有 semantic / procedural 之类，一并映射）

三条硬规矩：
  1. **只读**：连接一律 `mode=ro`（WAL 库在只读挂载下打不开时退 `mode=ro&immutable=1`），
     任何情况都不写对方的库。
  2. **列名不硬编**：`PRAGMA table_info` 拿到什么用什么，缺的列按默认值补，绝不整批失败。
  3. **边读边写**：每 500 条一批交给 `Store.import_records`，大库不吃满内存。
"""

from __future__ import annotations

import os
import sqlite3
import time
import urllib.parse
from datetime import datetime, timezone

from .lifecycle import (
    DEFAULT_DECAY_TYPE,
    DEFAULT_IMPORTANCE,
    clamp,
    normalize_mtype,
    ttl_for,
)
from .store import now_iso
from .textnorm import normalize_text

DB_NAME = "livingmemory.db"

# 对方的库固定落点（相对 AstrBot data 目录），按顺序试
RELATIVE_CANDIDATES = (
    "plugin_data/astrbot_plugin_livingmemory/livingmemory.db",
    "plugin_data/livingmemory/livingmemory.db",
    "livingmemory/livingmemory.db",
    "plugins/astrbot_plugin_livingmemory/data/livingmemory.db",
)

# 递归兜底时跳过的目录：这些里面的同名库不是对方的运行时数据
SKIP_DIRS = frozenset({
    "site-packages", "dist-packages", "node_modules", "bower_components",
    "__pycache__", ".git", ".hg", ".svn", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
})

# 表名候选：优先 memory_atoms（当前版本），其次两张老表名
TABLE_CANDIDATES = ("memory_atoms", "memory_documents", "memories")

# memory_atoms 的完整列（只用来算「缺几列」，不参与 SQL 拼装）
ATOM_COLUMNS = (
    "id", "parent_memory_id", "atom_type", "content", "entities", "importance",
    "confidence", "created_at", "last_accessed_at", "last_reinforced_at",
    "event_time", "ttl_days", "expires_at", "status", "reinforcement_count",
    "decay_type", "session_id", "persona_id", "metadata",
)

# 三元组切分用的谓词。长谓词优先（「住在」不会被切成「住」+「在」）。
SPLIT_PREDICATES = (
    "是", "位于", "属于", "叫做", "叫", "住在", "住", "在", "有",
    "喜欢", "用", "负责", "需要", "要", "等于",
)
_PREDICATES_BY_LEN = tuple(sorted(SPLIT_PREDICATES, key=len, reverse=True))

FALLBACK_SUBJECT = "遗留记忆"
FALLBACK_PREDICATE = "内容"

# 切分位置约束：1 < idx ≤ 24（主语至少 2 字、最长 24 字），否则退回整体。
# 单字主语（「我」「他」）留着只会污染检索，宁可不切。
MIN_SUBJECT_LEN = 2
MAX_SUBJECT_LEN = 24

# 对方的 atom_type → 本插件 mtype
ATOM_TYPE_TO_MTYPE = {
    "episodic": "event",
    "semantic": "fact",
    "factual": "fact",
    "relational": "fact",
    "procedural": "preference",
    "preference": "preference",
    "planned": "plan",
    "unknown": "fact",
}

# 对方的 decay_type → 本插件 decay_type
DECAY_MAP = {
    "exponential": "exp",
    "exp": "exp",
    "linear": "linear",
    "step": "step",
}

DEFAULT_CONFIDENCE = 0.8
DEFAULT_BATCH = 500

# 小于这个数（1973-03）的数字不当 epoch 看，免得把「2026」这种年份当 1970 年
_EPOCH_FLOOR = 100_000_000
# 大于这个数（2100-01-01）当脏数据
_EPOCH_CEIL = 4_102_444_800


class LivingMemoryError(Exception):
    """找不到库 / 打不开库 / 表结构认不出来。上层转成一句人话，不抛 traceback。"""


# ── 时间转换 ───────────────────────────────────────────────────────────


def _local_tz():
    return datetime.now().astimezone().tzinfo or timezone.utc


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_local_tz())
    return moment.astimezone().isoformat(timespec="seconds")


def _iso_from_epoch(value: float, default: str = "") -> str:
    if not (0 <= value <= _EPOCH_CEIL):
        return default
    try:
        return _iso(datetime.fromtimestamp(value, tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        return default


def to_iso(value, *, default: str = "") -> str:
    """对方的 created_at（REAL epoch 浮点）/ ISO 字符串 → 本地时区 ISO 字符串。

    三种都吃：`1700000000.0`、`"1700000000.0"`、`"2026-10-27T06:25:00+00:00"`。
    认不出来回 `default`（调用方决定补 now 还是留空），**绝不抛异常**。
    """
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return _iso_from_epoch(float(value), default)
    text = str(value).strip()
    if not text:
        return default
    number = None
    if set(text) <= set("0123456789.-+eE"):
        try:
            number = float(text)
        except ValueError:
            number = None
    if number is not None and abs(number) >= _EPOCH_FLOOR:
        return _iso_from_epoch(number, default)
    try:
        return _iso(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return default


# ── 三元组切分 ─────────────────────────────────────────────────────────


def split_content(content: str) -> tuple[str, str, str, bool]:
    """一句自然语言 → (主语, 谓词, 值, 是否切成功)。

    规则钉死（同一句话每次切出来必须一样，不然每次搬家结果都不同）：
    从左到右扫每个位置 idx，该位置上按「长谓词优先」匹配 `SPLIT_PREDICATES`；
    满足 `MIN_SUBJECT_LEN <= idx <= MAX_SUBJECT_LEN`（即 1 < idx ≤ 24）且两段都非空就切。
    一个位置都不满足 → `("遗留记忆", "内容", 原文, False)`。
    """
    text = normalize_text(content)
    if not text:
        return FALLBACK_SUBJECT, FALLBACK_PREDICATE, "", False
    upper = min(MAX_SUBJECT_LEN, len(text) - 1)
    for idx in range(MIN_SUBJECT_LEN, upper + 1):
        for predicate in _PREDICATES_BY_LEN:
            if not text.startswith(predicate, idx):
                continue
            subject = text[:idx].strip()
            value = text[idx + len(predicate):].strip()
            if subject and value:
                return subject, predicate, value, True
    return FALLBACK_SUBJECT, FALLBACK_PREDICATE, text, False


# ── 行映射 ─────────────────────────────────────────────────────────────


def _cell(row, columns, name: str, default=None):
    """取一列：列不存在 / 值为 NULL 都给 default（这就是「缺列不整批失败」的实现）。"""
    if name not in columns:
        return default
    value = row.get(name, default)
    return default if value is None else value


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def map_atom(row, columns, *, scope: str = "global") -> dict:
    """`memory_atoms` 的一行 → 本插件 mem 表的一行。

    返回里 `skip` 非空表示这行不该导入：`inactive`（status 不是 active）/ `empty`（内容空）。
    另外带 `splitted`（是否切出了三元组）、`dropped_session` / `dropped_persona`
    （对方的会话 / 人格字段，本插件用 scope 分池，这两个字段丢弃但要计数上报）。
    """
    content = normalize_text(_cell(row, columns, "content", ""))
    if not content:
        return {"skip": "empty"}

    status = str(_cell(row, columns, "status", "active") or "active").strip().lower()
    if status and status != "active":
        return {"skip": "inactive"}

    subject, predicate, value, splitted = split_content(content)

    atom_type = str(_cell(row, columns, "atom_type", "") or "").strip().lower()
    mtype = ATOM_TYPE_TO_MTYPE.get(atom_type) or normalize_mtype(atom_type or "fact")

    importance = clamp(_as_float(_cell(row, columns, "importance", DEFAULT_IMPORTANCE),
                                 DEFAULT_IMPORTANCE))
    confidence = clamp(_as_float(_cell(row, columns, "confidence", DEFAULT_CONFIDENCE),
                                 DEFAULT_CONFIDENCE))

    created_at = to_iso(_cell(row, columns, "created_at", None))
    if not created_at:
        created_at = to_iso(_cell(row, columns, "event_time", None))
    if not created_at:
        created_at = now_iso()
    last_accessed = to_iso(_cell(row, columns, "last_accessed_at", None)) or created_at

    ttl_days = _as_float(_cell(row, columns, "ttl_days", None), -1.0)
    if ttl_days <= 0:  # 缺列 / NULL / 脏值 → 按本插件该类型的默认 TTL
        ttl_days = ttl_for(mtype)

    decay_raw = str(_cell(row, columns, "decay_type", "") or "").strip().lower()
    decay_type = DECAY_MAP.get(decay_raw) or DEFAULT_DECAY_TYPE[mtype]

    access_count = max(0, int(_as_float(_cell(row, columns, "reinforcement_count", 0), 0)))

    return {
        "scope": scope,
        "subject": subject,
        "predicate": predicate,
        "value": value,
        "confidence": confidence,
        "source": "livingmemory",
        "created_at": created_at,
        "updated_at": created_at,
        "valid_from": created_at,
        "mtype": mtype,
        "importance": importance,
        "ttl_days": ttl_days,
        "decay_type": decay_type,
        "last_accessed_at": last_accessed,
        "access_count": access_count,
        "splitted": splitted,
        "dropped_session": bool(str(_cell(row, columns, "session_id", "") or "").strip()),
        "dropped_persona": bool(str(_cell(row, columns, "persona_id", "") or "").strip()),
        "skip": "",
    }


# ── 只读打开 / 表结构探测 ───────────────────────────────────────────────


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def open_ro(path: str) -> sqlite3.Connection:
    """只读打开对方的库。**永不**拿到可写句柄（mode=ro）。

    WAL 模式的库如果放在只读挂载点、或 -shm 建不出来，`mode=ro` 会连打开都失败；
    这时退 `immutable=1`（把主库文件当不可变快照读）。两种情况都不会写对方的库。
    """
    absolute = os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))
    if not os.path.isfile(absolute):
        raise LivingMemoryError(f"库文件不存在：{absolute}")
    quoted = urllib.parse.quote(absolute, safe="/")
    conn = None
    try:
        conn = sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=5.0)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()  # 真正触发打开
    except sqlite3.Error as first_error:
        if conn is not None:
            conn.close()
        try:
            conn = sqlite3.connect(f"file:{quoted}?mode=ro&immutable=1", uri=True, timeout=5.0)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        except sqlite3.Error:
            if conn is not None:
                conn.close()
            raise LivingMemoryError(f"打不开 {absolute}（只读）：{first_error}") from first_error
    conn.row_factory = sqlite3.Row
    return conn


def pick_table(conn) -> tuple[str, set[str]]:
    """找表 + 拿真实列名。返回 (表名, 列名集合)。"""
    names = [str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    order = [name for name in TABLE_CANDIDATES if name in names]
    order += [
        name for name in names
        if name not in TABLE_CANDIDATES
        and not name.startswith("sqlite_")
        and "memor" in name.lower()
    ]
    for table in order:
        columns = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({_quote_ident(table)})")}
        if "content" in columns:
            return table, columns
    raise LivingMemoryError(
        "对方的库里没找到 memory_atoms / memory_documents / memories（含 content 列的表）："
        + (", ".join(names) or "一个表都没有")
    )


def iter_atoms(conn, table: str, columns, *, batch: int = DEFAULT_BATCH, limit: int = 0):
    """按 id 顺序逐批读，边读边交出去（不一次性 fetchall）。"""
    order = "id" if "id" in columns else "rowid"
    cursor = conn.execute(f"SELECT * FROM {_quote_ident(table)} ORDER BY {order}")
    read = 0
    while True:
        rows = cursor.fetchmany(batch)
        if not rows:
            return
        for row in rows:
            if limit and read >= limit:
                return
            read += 1
            yield dict(row)


# ── 路径探测 ───────────────────────────────────────────────────────────


def candidate_paths(data_dirs) -> list[str]:
    """AstrBot data 目录 → 对方的库可能在哪（固定候选，按顺序）。"""
    out: list[str] = []
    for base in data_dirs:
        for rel in RELATIVE_CANDIDATES:
            path = os.path.join(base, rel)
            if path not in out:
                out.append(path)
    return out


def find_recursive(base: str, *, max_depth: int = 4) -> str:
    """在 base 下限制深度找文件名恰为 livingmemory.db 的文件。找不到回空串。"""
    base = os.path.abspath(base)
    if not os.path.isdir(base):
        return ""
    for root, dirs, files in os.walk(base):
        rel = os.path.relpath(root, base)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= max_depth:
            dirs[:] = []  # 到底了，不再往下钻（文件在当前层的 depth 就是 depth+1）
        dirs[:] = sorted(
            d for d in dirs
            if d.lower() not in SKIP_DIRS and not d.startswith(".")
        )
        if DB_NAME in files:
            return os.path.join(root, DB_NAME)
    return ""


def probe(data_dirs, *, configured: str = "", max_depth: int = 4) -> dict:
    """找库：配置 → 固定候选 → 递归兜底。返回 {path, source, tried}，找不到 path 为空串。"""
    tried: list[str] = []
    configured = str(configured or "").strip()
    if configured:
        path = os.path.realpath(os.path.abspath(os.path.expanduser(configured)))
        if os.path.isdir(path):
            path = os.path.join(path, DB_NAME)
        tried.append(path)
        if os.path.isfile(path):
            return {"path": path, "source": "配置 lm_db_path", "tried": tried}

    for path in candidate_paths(data_dirs):
        if path in tried:
            continue
        tried.append(path)
        if os.path.isfile(path):
            return {"path": path, "source": "AstrBot data 目录", "tried": tried}

    for base in data_dirs:
        found = find_recursive(base, max_depth=max_depth)
        if found:
            tried.append(found)
            return {"path": found, "source": f"递归搜索（≤{max_depth} 层）", "tried": tried}
        tried.append(f"{base}（递归 {max_depth} 层内没有 {DB_NAME}）")

    return {"path": "", "source": "", "tried": tried}


# ── 导入主流程 ─────────────────────────────────────────────────────────


def _new_stats(path: str, *, dry_run: bool, limit: int) -> dict:
    return {
        "path": path,
        "dry_run": dry_run,
        "limit": limit,
        "table": "",
        "missing_columns": 0,
        "read": 0,
        "added": 0,
        "updated": 0,
        "splitted": 0,
        "fallback": 0,
        "skipped_duplicate": 0,
        "skipped_inactive": 0,
        "skipped_empty": 0,
        "skipped_stale": 0,
        "dropped_session": 0,
        "dropped_persona": 0,
        "elapsed": 0.0,
    }


def _flush(store, records: list[dict], stats: dict, *, dry_run: bool, state: dict) -> None:
    result = store.import_records(records, dry_run=dry_run, state=state)
    stats["added"] += result["added"]
    stats["updated"] += result["updated"]
    stats["skipped_duplicate"] += result["duplicate"]
    stats["skipped_stale"] += result["stale"]
    for record, outcome in zip(records, result["outcomes"]):
        if outcome in ("added", "updated"):
            stats["splitted" if record.get("splitted") else "fallback"] += 1


def run_import(store, *, db_path: str, scope: str = "global", dry_run: bool = False,
               limit: int = 0, batch: int = DEFAULT_BATCH) -> dict:
    """读对方的库、映射、分批写进 store。返回统计（全部是整数，方便断言）。"""
    started = time.monotonic()
    stats = _new_stats(db_path, dry_run=dry_run, limit=limit)
    conn = open_ro(db_path)
    try:
        table, columns = pick_table(conn)
        stats["table"] = table
        if table == "memory_atoms":
            stats["missing_columns"] = len([c for c in ATOM_COLUMNS if c not in columns])
        # 跨批次的去重状态：dry-run 每批回滚，状态不传出去的话第二批就看不到第一批
        state: dict = {}
        buffer: list[dict] = []
        for row in iter_atoms(conn, table, columns, batch=batch, limit=limit):
            stats["read"] += 1
            mapped = map_atom(row, columns, scope=scope)
            reason = mapped.get("skip")
            if reason == "inactive":
                stats["skipped_inactive"] += 1
                continue
            if reason == "empty":
                stats["skipped_empty"] += 1
                continue
            if mapped["dropped_session"]:
                stats["dropped_session"] += 1
            if mapped["dropped_persona"]:
                stats["dropped_persona"] += 1
            buffer.append(mapped)
            if len(buffer) >= batch:
                _flush(store, buffer, stats, dry_run=dry_run, state=state)
                buffer = []
        if buffer:
            _flush(store, buffer, stats, dry_run=dry_run, state=state)
    finally:
        conn.close()
    stats["elapsed"] = time.monotonic() - started
    return stats


# ── 输出（一条消息，别刷屏） ───────────────────────────────────────────


def format_report(stats: dict) -> str:
    imported = stats["added"] + stats["updated"]
    lines = [f"LivingMemory 搬家：{stats['path']}"]
    table_line = f"表：{stats['table']}"
    if stats["missing_columns"]:
        table_line += f"（缺 {stats['missing_columns']} 列，按默认值补）"
    lines.append(table_line)
    lines.append(
        f"读入 {stats['read']} 条 → 导入 {imported} 条"
        f"（切出三元组 {stats['splitted']} / 退回整体 {stats['fallback']}）"
    )
    skip = f"跳过：已存在重复 {stats['skipped_duplicate']}、非活跃 {stats['skipped_inactive']}"
    if stats["skipped_stale"]:
        skip += f"、旧于现有 {stats['skipped_stale']}"
    if stats["skipped_empty"]:
        skip += f"、空内容 {stats['skipped_empty']}"
    lines.append(skip)
    lines.append(f"用时 {stats['elapsed']:.1f}s")
    dropped = []
    if stats["dropped_session"]:
        dropped.append(f"session_id {stats['dropped_session']} 条")
    if stats["dropped_persona"]:
        dropped.append(f"persona_id {stats['dropped_persona']} 条")
    if dropped:
        lines.append("丢弃：" + "、".join(dropped) + "（对方按会话/人格分池，本插件用 scope）")
    if stats["updated"]:
        lines.append(f"其中覆盖同槽旧值 {stats['updated']} 条")
    if stats["limit"]:
        lines.append(f"按 --limit {stats['limit']} 只读了前 {stats['limit']} 条")
    if stats["dry_run"]:
        lines.append("--dry-run：只统计，一个字没写")
    return "\n".join(lines)


def format_not_found(tried) -> str:
    lines = [
        "没找到 LivingMemory 的库，可手动指定 /mem import-lm --path <livingmemory.db>",
    ]
    shown = list(tried or [])
    if not shown:
        lines.append("（连 AstrBot data 目录都没探到，直接用 --path 指到 livingmemory.db）")
        return "\n".join(lines)
    lines.append("找过这些位置：")
    for path in shown[:12]:
        lines.append(f"  · {path}")
    if len(shown) > 12:
        lines.append(f"  · …还有 {len(shown) - 12} 个")
    return "\n".join(lines)
