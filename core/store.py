"""记忆库存储层：sqlite + 事实三元组 + 同槽覆盖 + 向量缓存。

设计：纯标准库，**不 import astrbot**，可以脱离框架单独跑（selftest.py 就是这么测的）。

表结构（mem 表对齐 dd-home/mem/mem.py，v0.3.0 加生命周期列）：

    mem(id, scope, subject, predicate, value, confidence, source,
        created_at, updated_at, valid_from, valid_to, superseded_by,
        mtype, importance, ttl_days, decay_type, last_accessed_at, access_count,
        origin)
    vec(mem_id, model, dim, vec BLOB, text_hash)
    meta(key, value)          -- 衰减回合上次执行时间等

`origin`（v0.8.0）存 `unified_msg_origin`，形如 `diandian:GroupMessage:738458661`，
用来回答「这条记忆是哪个会话写的」。没有来源时是空串（不能是 NULL：老库 ALTER 时
`NOT NULL DEFAULT ''` 才会给已有行填默认值）。

v0.3.0 的六个生命周期列**全部有默认值**，老库启动时由 `_ensure_columns()` 逐列
`ALTER TABLE ... ADD COLUMN` 补上：v0.2 及更早的数据会拿到 mtype=fact、
ttl_days=3650、importance=0.5、access_count=0，行为等价于「不过期」。

同槽覆盖：同一 (scope, subject, predicate) 只保留一条 `valid_to IS NULL` 的记录。
写新值时先插新行，再把旧行填上 `valid_to` + `superseded_by`（指向新 id），
检索只读有效记录。历史不物理删除。

vec 表在规格的四列之外多一列 `text_hash`：规格要求「按 sha256(subject|predicate|value)
缓存，内容不变不重复调 embedding」，落库时得有个字段能校验内容有没有变，
否则模型换了 / 内容改了都没法判断缓存是否还有效。

线程模型：每次操作现开 sqlite 连接（`timeout=5`），不跨线程共享连接 —— 插件 handler
可能在不同线程里跑，共享连接必踩 `SQLite objects created in a thread can only be used
in that same thread`。
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

from .lifecycle import (
    DECAY_TYPES,
    DEFAULT_ACCESS_HALVE,
    DEFAULT_DECAY_TYPE,
    DEFAULT_IMPORTANCE,
    DEFAULT_PROTECTED_IMPORTANCE,
    DEFAULT_TTL_DAYS,
    clamp,
    normalize_mtype,
)

DEFAULT_DB_NAME = "mem.db"
# 会话来源（unified_msg_origin）最长保留多少字符：umo 一般是 `平台:消息类型:会话号`，
# 200 足够；给个上限是防止脏数据把 db 撑大 / 前端下拉框被一条超长值拖坏。
MAX_ORIGIN_LEN = 200
# 本文件在 <插件根>/core/ 下，往上退一层才是插件目录；
# 这个值用于「db_path 不许落在插件代码目录」的拦截和 data 目录反推，退错一层会静默失效。
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCHEMA = """
CREATE TABLE IF NOT EXISTS mem(
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL,
  subject TEXT NOT NULL,
  predicate TEXT NOT NULL,
  value TEXT NOT NULL,
  confidence REAL DEFAULT 0.8,
  source TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  superseded_by INTEGER,
  mtype TEXT NOT NULL DEFAULT 'fact',
  importance REAL NOT NULL DEFAULT 0.5,
  ttl_days REAL NOT NULL DEFAULT 3650,
  decay_type TEXT NOT NULL DEFAULT 'exp',
  last_accessed_at TEXT,
  access_count INTEGER NOT NULL DEFAULT 0,
  origin TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_mem_slot ON mem(scope, subject, predicate, valid_to);
CREATE INDEX IF NOT EXISTS ix_mem_subject ON mem(subject);

CREATE TABLE IF NOT EXISTS vec(
  mem_id INTEGER NOT NULL,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vec BLOB NOT NULL,
  text_hash TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(mem_id, model)
);

CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# v0.3.0 生命周期列 + v0.8.0 会话来源列：老库缺哪列补哪列（ALTER TABLE 带 DEFAULT，已有行自动填默认值）
SCHEMA_COLUMNS = {
    "mtype": "TEXT NOT NULL DEFAULT 'fact'",
    "importance": "REAL NOT NULL DEFAULT 0.5",
    "ttl_days": "REAL NOT NULL DEFAULT 3650",
    "decay_type": "TEXT NOT NULL DEFAULT 'exp'",
    "last_accessed_at": "TEXT",
    "access_count": "INTEGER NOT NULL DEFAULT 0",
    "origin": "TEXT NOT NULL DEFAULT ''",
}


def _ensure_columns(conn) -> list[str]:
    """给老库补列，返回本次新增的列名（新库返回空列表）。

    `PRAGMA table_info` 每次现查，不记版本号：跑几次都幂等，缺哪列补哪列。
    """
    have = {row["name"] for row in conn.execute("PRAGMA table_info(mem)")}
    added: list[str] = []
    for name, ddl in SCHEMA_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE mem ADD COLUMN {name} {ddl}")
            added.append(name)
    return added


class StoreError(Exception):
    """记忆库打不开 / 读不了 / 写不进。上层统一回「记忆库读不了」，不抛 traceback。"""


def now_iso(when: datetime | None = None) -> str:
    """本地时区、精确到秒的 ISO 时间串（和 dd-home/mem/mem.py 一致）。

    `when` 只在测试 / 回填时注入假时钟；日常调用留空。
    """
    moment = when or datetime.now(timezone.utc).astimezone()
    return moment.isoformat(timespec="seconds")


def normalize_origin(value) -> str:
    """统一 `unified_msg_origin`：去首尾空白、截到 `MAX_ORIGIN_LEN`。

    None / 数字 / 任意对象都安全转成字符串；拿不到来源时就是空串，**不伪造**
    （WebUI 写入没有 event，origin 必须是空的）。
    """
    if value is None:
        return ""
    return str(value).strip()[:MAX_ORIGIN_LEN]


# ── 路径解析 ───────────────────────────────────────────────────────────


def _warn(warn, message: str) -> None:
    if callable(warn):
        with contextlib.suppress(Exception):
            warn(message)


def _inside(path: str, parent: str) -> bool:
    try:
        p = os.path.realpath(path)
        q = os.path.realpath(parent)
        return p == q or os.path.commonpath([p, q]) == q
    except (ValueError, OSError):
        return False


def _data_candidates() -> list[tuple[str, bool]]:
    """候选 AstrBot data 目录，返回 (路径, 允许新建)。

    允许新建=False 的「猜出来的」路径只在已经存在时才用 —— 不为了跑个单测
    就在别人机器上凭空造 ~/AstrBot/data。
    """
    out: list[tuple[str, bool]] = []
    for env in ("MEM_DATA_DIR", "ASTRBOT_DATA_DIR"):
        value = os.environ.get(env, "").strip()
        if value:
            out.append((value, True))
    try:  # AstrBot 官方工具函数；没有 astrbot（跑单测）时一定失败
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        out.append((str(get_astrbot_data_path()), True))
    except Exception:  # noqa: BLE001
        pass
    # 插件装在 <root>/data/plugins/<插件名> 时反推 <root>/data
    parent = os.path.dirname(PLUGIN_DIR)
    grand = os.path.dirname(parent)
    if os.path.basename(parent) == "plugins" and os.path.basename(grand) == "data":
        out.append((grand, True))
    out.append((os.path.join(os.getcwd(), "data"), True))
    out.append((os.path.join(os.path.expanduser("~"), "AstrBot", "data"), False))
    out.append((os.path.join(os.path.expanduser("~"), ".astrbot", "data"), False))
    return out


def resolve_db_path(configured: str = "", *, plugin_dir: str = PLUGIN_DIR, warn=None) -> str:
    """解析记忆库文件路径。

    - 配置了 `db_path` 就用它（目录会自动建；如果指向目录则补 mem.db）；
    - 留空则在 AstrBot data 目录下用 `mem.db`；
    - **绝不落在插件代码目录**（命中插件目录会跳过并 warning）。
    """
    raw = str(configured or "").strip()
    if raw:
        path = os.path.realpath(os.path.abspath(os.path.expanduser(raw)))
        if _inside(path, plugin_dir):
            _warn(
                warn,
                f"[mem] db_path={path} 在插件代码目录里，拒绝使用（升级插件会丢数据），改用默认 data 目录",
            )
        else:
            if os.path.isdir(path):
                path = os.path.join(path, DEFAULT_DB_NAME)
            parent = os.path.dirname(path)
            try:
                if parent:
                    os.makedirs(parent, exist_ok=True)
                return path
            except OSError as e:
                _warn(warn, f"[mem] db_path={path} 建目录失败（{e}），改用默认 data 目录")

    for cand, allow_create in _data_candidates():
        path = os.path.realpath(os.path.abspath(os.path.expanduser(cand)))
        if _inside(path, plugin_dir):
            continue
        if allow_create:
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                continue
        elif not os.path.isdir(path):
            continue
        if not os.access(path, os.W_OK):
            continue
        return os.path.join(path, DEFAULT_DB_NAME)

    fallback_dir = os.path.join(tempfile.gettempdir(), "astrbot_plugin_mem")
    os.makedirs(fallback_dir, exist_ok=True)
    _warn(warn, f"[mem] 没找到可写的 AstrBot data 目录，记忆库先放临时目录：{fallback_dir}")
    return os.path.join(fallback_dir, DEFAULT_DB_NAME)


def astrbot_data_dirs() -> list[str]:
    """AstrBot data 目录候选（**只返回真实存在的**，按优先级去重）。

    给「从 LivingMemory 搬家」找对方的库用：读别人的库不需要能写，所以不建目录。
    """
    out: list[str] = []
    seen: set[str] = set()
    for cand, _allow_create in _data_candidates():
        path = os.path.realpath(os.path.abspath(os.path.expanduser(cand)))
        if path in seen:
            continue
        seen.add(path)
        if os.path.isdir(path):
            out.append(path)
    return out


# ── 工具 ───────────────────────────────────────────────────────────────


def _db_guard(fn):
    """把 sqlite 异常统一收成 StoreError（上层只认这一种）。"""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except StoreError:
            raise
        except (sqlite3.Error, OSError) as e:
            raise StoreError(f"{type(e).__name__}: {e}") from e

    return wrapper


def _like_pattern(term: str) -> str:
    """LIKE 参数：转义 % _ \\，避免用户输入 `%` 把整库匹配出来。"""
    escaped = str(term).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()} if row is not None else {}  # sqlite3.Row 迭代给的是值，不是键


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _aware(moment: datetime) -> datetime:
    """补时区：naive 当本地时间。

    外部导入的时间串（比如从别的插件搬来的 ISO）可能没有偏移量，直接和 `now_iso()`
    的 aware 串比 `datetime` 会抛 `TypeError: can't compare offset-naive and
    offset-aware datetimes`；先补齐再比。
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=datetime.now().astimezone().tzinfo or timezone.utc)
    return moment


def _newer(left: str, right: str) -> bool:
    """left 是否比 right 新（ISO 串解析失败就退回字符串比较）。"""
    a, b = _parse_ts(left), _parse_ts(right)
    if a is not None and b is not None:
        return _aware(a) > _aware(b)
    return str(left or "") > str(right or "")


def content_hash(subject: str, predicate: str, value: str) -> str:
    """同 subject + predicate + value → 同一个哈希（跨批次去重用）。

    和 `retrieve.value_hash` 的区别：那个只哈希 value（给 embedding 缓存做键），
    这个哈希整个三元组（判断「这条事实库里已经有了」）。
    """
    raw = f"{subject}\x1f{predicate}\x1f{value}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Store:
    """记忆库。所有方法同步、线程安全（每次现开连接）。"""

    def __init__(self, path: str, *, warn=None):
        self.path = path
        self.migrated_columns: list[str] = []
        try:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with self._conn() as conn:
                conn.executescript(SCHEMA)
                self.migrated_columns = _ensure_columns(conn)
        except (sqlite3.Error, OSError) as e:
            raise StoreError(f"打开 {path} 失败：{e}") from e
        if self.migrated_columns:
            _warn(
                warn,
                f"[mem] 老库迁移：mem 表补上 {', '.join(self.migrated_columns)} "
                "（缺省值：mtype=fact / ttl=3650 天 / origin 空，历史数据不会过期）",
            )

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── 写 ────────────────────────────────────────────────────────────

    @_db_guard
    def add(
        self,
        scope: str,
        subject: str,
        predicate: str,
        value: str,
        *,
        source: str = "chat",
        confidence: float = 0.8,
        supersede_ids: list[int] | None = None,
        mtype: str | None = None,
        importance: float | None = None,
        ttl_days: float | None = None,
        decay_type: str | None = None,
        created_at: str | None = None,
        origin: str = "",
    ) -> dict:
        """写一条事实。同槽旧记录自动作废（填 valid_to + superseded_by）。

        supersede_ids：上层（写入相似去重）判定「同一件事」的其它记录，一并作废。
        和同槽覆盖走同一条落库路径，保证 superseded_by 都指向本次新 id。

        mtype / importance / ttl_days / decay_type 留空就按类型默认值补：
        plan 2 天 step、event 30 天 exp、preference 365 天 exp、fact 3650 天 exp。

        origin：写入方会话的 `unified_msg_origin`；WebUI / 导入没有 event，留空。
        """
        name = normalize_mtype(mtype)
        origin_value = normalize_origin(origin)
        try:
            importance_value = clamp(
                DEFAULT_IMPORTANCE if importance is None else importance
            )
        except (TypeError, ValueError):
            importance_value = DEFAULT_IMPORTANCE
        try:
            ttl = float(DEFAULT_TTL_DAYS[name] if ttl_days is None else ttl_days)
        except (TypeError, ValueError):
            ttl = float(DEFAULT_TTL_DAYS[name])
        decay = str(decay_type or DEFAULT_DECAY_TYPE[name]).strip().lower()
        if decay not in DECAY_TYPES:
            decay = DEFAULT_DECAY_TYPE[name]

        ts = now_iso() if created_at is None else str(created_at)
        with self._conn() as conn:
            old = conn.execute(
                "SELECT id FROM mem WHERE scope=? AND subject=? AND predicate=? AND valid_to IS NULL",
                (scope, subject, predicate),
            ).fetchall()
            cur = conn.execute(
                "INSERT INTO mem(scope,origin,subject,predicate,value,confidence,source,"
                "created_at,updated_at,valid_from,mtype,importance,ttl_days,decay_type,"
                "last_accessed_at,access_count) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (
                    scope, origin_value, subject, predicate, value, confidence, source,
                    ts, ts, ts, name, importance_value, ttl, decay, ts,
                ),
            )
            new_id = int(cur.lastrowid)
            superseded = [int(r["id"]) for r in old]
            for extra in supersede_ids or []:
                try:
                    extra_id = int(extra)
                except (TypeError, ValueError):
                    continue
                if extra_id != new_id and extra_id not in superseded:
                    superseded.append(extra_id)
            for old_id in superseded:
                conn.execute(
                    "UPDATE mem SET valid_to=?, superseded_by=?, updated_at=? "
                    "WHERE id=? AND valid_to IS NULL",
                    (ts, new_id, ts, old_id),
                )
            row = conn.execute("SELECT * FROM mem WHERE id=?", (new_id,)).fetchone()
        return {"id": new_id, "superseded": superseded, "row": _row_to_dict(row)}

    @_db_guard
    def forget(self, keyword: str, *, scope: str | None = None) -> list[dict]:
        """按关键词作废有效记录（填 valid_to，不物理删）。返回被作废的行。"""
        ts = now_iso()
        like = _like_pattern(keyword)
        sql = (
            "SELECT * FROM mem WHERE valid_to IS NULL AND "
            "(subject LIKE ? ESCAPE '\\' OR predicate LIKE ? ESCAPE '\\' "
            "OR value LIKE ? ESCAPE '\\')"
        )
        params: list = [like, like, like]
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE mem SET valid_to=?, updated_at=? WHERE id=? AND valid_to IS NULL",
                    (ts, ts, r["id"]),
                )
        return [_row_to_dict(r) for r in rows]

    @_db_guard
    def forget_ids(self, mem_ids: list[int]) -> list[dict]:
        """按 id 作废有效记录（填 valid_to，不物理删）。返回被作废的行。

        给 WebUI 表格的「作废」按钮用：`forget()` 按关键词匹配，命中面太宽，
        点一行可能带走一串；这里只认 id。语义与 `forget()` 一致——只填
        `valid_to` / `updated_at`，不填 `superseded_by`（不是被新值取代）。
        """
        ts = now_iso()
        wanted: list[int] = []
        for item in mem_ids or []:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value > 0:
                wanted.append(value)
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM mem WHERE valid_to IS NULL AND id IN ({marks})", wanted
            ).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE mem SET valid_to=?, updated_at=? WHERE id=? AND valid_to IS NULL",
                    (ts, ts, r["id"]),
                )
        return [_row_to_dict(r) for r in rows]

    # ── 生命周期：访问强化 / 衰减回合 / 合并 ──────────────────────────

    @_db_guard
    def reinforce(self, ids: list[int], *, now: datetime | None = None) -> int:
        """检索命中后加固：access_count += 1、last_accessed_at = now。

        只加固有效记录（valid_to IS NULL）。返回实际加固条数。
        """
        clean: list[int] = []
        for item in ids or []:
            try:
                clean.append(int(item))
            except (TypeError, ValueError):
                continue
        if not clean:
            return 0
        ts = now_iso(now)
        marks = ",".join("?" * len(clean))
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE mem SET access_count = access_count + 1, last_accessed_at=? "
                f"WHERE id IN ({marks}) AND valid_to IS NULL",
                [ts, *clean],
            )
            return int(cur.rowcount or 0)

    @_db_guard
    def apply_decay_pass(
        self,
        *,
        access_halve: float = DEFAULT_ACCESS_HALVE,
        protected_importance: float = DEFAULT_PROTECTED_IMPORTANCE,
        now: datetime | None = None,
        force: bool = False,
        min_interval: float = 3600.0,
    ) -> dict:
        """衰减回合：所有非保护记录的 access_count 按 access_halve 回落。

        防止「十年前的热点」靠历史访问次数永远霸榜。默认每小时最多跑一次
        （时间戳存 meta 表），`force=True` 用于单测。
        """
        moment = now or datetime.now(timezone.utc).astimezone()
        stamp = moment.timestamp()
        try:
            halve = clamp(access_halve, 0.0, 1.0)
        except (TypeError, ValueError):
            halve = DEFAULT_ACCESS_HALVE
        with self._conn() as conn:
            last_row = conn.execute(
                "SELECT value FROM meta WHERE key='decay_last_run'"
            ).fetchone()
            try:
                last = float(last_row["value"]) if last_row else 0.0
            except (TypeError, ValueError):
                last = 0.0
            if not force and last and stamp - last < float(min_interval):
                return {"skipped": True, "scanned": 0, "updated": 0, "last_run": last}
            rows = conn.execute(
                "SELECT id, importance, access_count FROM mem WHERE valid_to IS NULL"
            ).fetchall()
            updated = 0
            for row in rows:
                try:
                    if float(row["importance"] or 0) >= float(protected_importance):
                        continue  # 保护记录不衰减
                except (TypeError, ValueError):
                    pass
                count = int(row["access_count"] or 0)
                if count <= 0:
                    continue
                new_count = max(0, int(count * halve))
                if new_count != count:
                    conn.execute(
                        "UPDATE mem SET access_count=? WHERE id=?", (new_count, int(row["id"]))
                    )
                    updated += 1
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('decay_last_run', ?)",
                (str(stamp),),
            )
        return {"skipped": False, "scanned": len(rows), "updated": updated, "last_run": stamp}

    @_db_guard
    def merge_rows(
        self,
        keep_id: int,
        drop_ids: list[int],
        *,
        importance: float | None = None,
        access_count: int | None = None,
        now: datetime | None = None,
    ) -> dict:
        """consolidate 落库：保留行更新 importance / access_count，其余行填 valid_to。"""
        ts = now_iso(now)
        keep = int(keep_id)
        drop = []
        for item in drop_ids or []:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if value != keep:
                drop.append(value)
        with self._conn() as conn:
            sets = ["updated_at=?"]
            params: list = [ts]
            if importance is not None:
                sets.append("importance=?")
                params.append(clamp(importance))
            if access_count is not None:
                sets.append("access_count=?")
                params.append(max(0, int(access_count)))
            conn.execute(f"UPDATE mem SET {', '.join(sets)} WHERE id=?", [*params, keep])
            for drop_id in drop:
                conn.execute(
                    "UPDATE mem SET valid_to=?, superseded_by=?, updated_at=? "
                    "WHERE id=? AND valid_to IS NULL",
                    (ts, keep, ts, drop_id),
                )
        return {"keep": keep, "dropped": drop, "at": ts}

    @_db_guard
    def type_counts(self) -> dict:
        """有效记录按 mtype 计数（stats 用）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT mtype, COUNT(*) n FROM mem WHERE valid_to IS NULL GROUP BY mtype"
            ).fetchall()
        return {str(r["mtype"] or "fact"): int(r["n"]) for r in rows}

    @_db_guard
    def origin_counts(self) -> list[dict]:
        """有效记忆按 origin 聚合：会话 ID → 条数 → 最近更新时间。

        按条数降序；空 origin 也会成一组（origin="" ），由调用方显示成「未记录来源」。
        `last_updated` 只在这一组里取（不是全库），前端下拉和 `/mem sessions` 共用。
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT origin, COUNT(*) n, MAX(updated_at) last_updated FROM mem "
                "WHERE valid_to IS NULL GROUP BY origin ORDER BY n DESC, last_updated DESC"
            ).fetchall()
        return [
            {
                "origin": normalize_origin(r["origin"]),
                "count": int(r["n"]),
                "last_updated": str(r["last_updated"] or ""),
            }
            for r in rows
        ]

    @_db_guard
    def meta_get(self, key: str, default: str = "") -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    @_db_guard
    def meta_set(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (str(key), str(value))
            )

    # ── 读 ────────────────────────────────────────────────────────────

    @_db_guard
    def get(self, mem_id: int) -> dict:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM mem WHERE id=?", (mem_id,)).fetchone()
        return _row_to_dict(row)

    @_db_guard
    def active_rows(self, *, scope: str | None = None, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM mem WHERE valid_to IS NULL"
        params: list = []
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        sql += " ORDER BY updated_at DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    @_db_guard
    def active_rows_by_subject(self, scope: str, subject: str) -> list[dict]:
        """同 scope + 同主语的全部有效记录（写入相似去重的候选池）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM mem WHERE scope=? AND subject=? AND valid_to IS NULL "
                "ORDER BY updated_at DESC, id DESC",
                (scope, subject),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    @_db_guard
    def dedupe_slots(self, *, dry_run: bool = False) -> dict:
        """全库扫同槽重复：`(scope, subject, predicate)` 每组只留 `updated_at` 最新的一条。

        dry_run=True 只统计不改库。返回里的 `mergeable_groups` 就是「可合并 M 组」。
        """
        ts = now_iso()
        with self._conn() as conn:
            rows = [
                _row_to_dict(r)
                for r in conn.execute(
                    "SELECT * FROM mem WHERE valid_to IS NULL "
                    "ORDER BY scope, subject, predicate, updated_at, id"
                ).fetchall()
            ]
            groups: dict[tuple[str, str, str], list[dict]] = {}
            for row in rows:
                groups.setdefault((row["scope"], row["subject"], row["predicate"]), []).append(row)

            keep: dict[tuple[str, str, str], dict] = {}
            drop: list[dict] = []
            merged_groups: dict[str, int] = {}
            for key, items in groups.items():
                best = items[0]
                for cand in items[1:]:
                    newer = _newer(cand["updated_at"], best["updated_at"])
                    tie = (
                        str(cand["updated_at"]) == str(best["updated_at"])
                        and int(cand["id"]) > int(best["id"])
                    )
                    if newer or tie:
                        best = cand
                keep[key] = best
                if len(items) > 1:
                    merged_groups[f"{key[0]}|{key[1]}|{key[2]}"] = int(best["id"])
                    drop.extend(r for r in items if int(r["id"]) != int(best["id"]))

            if drop and not dry_run:
                for row in drop:
                    slot = (row["scope"], row["subject"], row["predicate"])
                    conn.execute(
                        "UPDATE mem SET valid_to=?, superseded_by=?, updated_at=? "
                        "WHERE id=? AND valid_to IS NULL",
                        (ts, int(keep[slot]["id"]), ts, int(row["id"])),
                    )

        return {
            "scanned": len(rows),
            "mergeable_groups": len(merged_groups),
            "mergeable_rows": len(drop),
            "superseded": [int(r["id"]) for r in drop],
            "kept": merged_groups,
            "dry_run": bool(dry_run),
            "applied": bool(drop) and not dry_run,
        }

    @_db_guard
    def keyword_search(self, terms: list[str], *, limit: int = 30, scope: str | None = None) -> list[dict]:
        """多关键词 LIKE 召回，Python 侧按命中数排序。空 terms 返回空。"""
        terms = [t for t in (terms or []) if t]
        if not terms:
            return []
        where = " OR ".join(
            ["(subject LIKE ? ESCAPE '\\' OR predicate LIKE ? ESCAPE '\\' OR value LIKE ? ESCAPE '\\')"]
            * len(terms)
        )
        sql = f"SELECT * FROM mem WHERE valid_to IS NULL AND ({where})"
        params: list = []
        for t in terms:
            params += [_like_pattern(t)] * 3
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        sql += " ORDER BY updated_at DESC LIMIT 500"
        with self._conn() as conn:
            rows = [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]

        def hits(row: dict) -> int:
            blob = f"{row['subject']}|{row['predicate']}|{row['value']}"
            return sum(1 for t in terms if t in blob)

        rows.sort(key=lambda r: (hits(r), str(r.get("updated_at") or "")), reverse=True)
        return rows[: int(limit)]

    @_db_guard
    def stats(self, *, model: str | None = None) -> dict:
        with self._conn() as conn:
            active = conn.execute("SELECT COUNT(*) FROM mem WHERE valid_to IS NULL").fetchone()[0]
            history = conn.execute("SELECT COUNT(*) FROM mem WHERE valid_to IS NOT NULL").fetchone()[0]
            scopes = [
                (r["scope"], r["n"])
                for r in conn.execute(
                    "SELECT scope, COUNT(*) n FROM mem WHERE valid_to IS NULL GROUP BY scope ORDER BY n DESC"
                ).fetchall()
            ]
            last = conn.execute("SELECT MAX(updated_at) FROM mem").fetchone()[0]
            if model:
                vec_rows = conn.execute("SELECT COUNT(*) FROM vec WHERE model=?", (model,)).fetchone()[0]
            else:
                vec_rows = conn.execute("SELECT COUNT(*) FROM vec").fetchone()[0]
        return {
            "active": int(active),
            "history": int(history),
            "scopes": scopes,
            "types": self.type_counts(),
            "last_updated": last or "",
            "vec_rows": int(vec_rows),
            "path": self.path,
        }

    # ── 向量缓存 ──────────────────────────────────────────────────────

    @_db_guard
    def vec_get(self, mem_id: int, model: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT dim, vec, text_hash FROM vec WHERE mem_id=? AND model=?",
                (int(mem_id), model),
            ).fetchone()
        return _row_to_dict(row) if row else None

    @_db_guard
    def vec_put(self, mem_id: int, model: str, dim: int, blob: bytes, text_hash: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO vec(mem_id, model, dim, vec, text_hash) VALUES(?,?,?,?,?)",
                (int(mem_id), model, int(dim), blob, text_hash),
            )

    @_db_guard
    def vec_count(self, model: str | None = None) -> int:
        with self._conn() as conn:
            if model:
                return int(conn.execute("SELECT COUNT(*) FROM vec WHERE model=?", (model,)).fetchone()[0])
            return int(conn.execute("SELECT COUNT(*) FROM vec").fetchone()[0])

    # ── 导出 / 导入 ───────────────────────────────────────────────────

    @_db_guard
    def export_json(self, path: str) -> dict:
        with self._conn() as conn:
            rows = [_row_to_dict(r) for r in conn.execute("SELECT * FROM mem ORDER BY id").fetchall()]
        payload = {"format": "astrbot_plugin_mem", "version": 1, "exported_at": now_iso(), "items": rows}
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return {"count": len(rows), "path": path}

    def import_json(self, path: str, *, source: str = "import") -> dict:
        """导入并合并：同槽按 `updated_at` 新者胜；重复内容跳过。"""
        try:
            with open(os.path.expanduser(path), "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as e:
            raise StoreError(f"读不了 {path}：{e}") from e

        items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise StoreError("文件格式不对：要 {\"items\": [...]} 或 JSON 数组")

        added = updated = skipped = invalid = 0
        errors: list[str] = []
        for raw in items:
            try:
                rec = self._normalize_import_item(raw, source)
            except ValueError as e:
                invalid += 1
                if len(errors) < 3:
                    errors.append(str(e))
                continue
            result = self._merge_one(rec)
            if result == "added":
                added += 1
            elif result == "updated":
                updated += 1
            else:
                skipped += 1
        return {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "invalid": invalid,
            "errors": errors,
        }

    @staticmethod
    def _normalize_import_item(raw, source: str) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("不是对象")
        subject = str(raw.get("subject") or "").strip()
        predicate = str(raw.get("predicate") or "").strip()
        value = str(raw.get("value") or "").strip()
        if not (subject and predicate and value):
            raise ValueError("subject/predicate/value 缺一不可")
        scope = str(raw.get("scope") or "global").strip() or "global"
        updated_at = str(raw.get("updated_at") or raw.get("valid_from") or "").strip() or now_iso()
        created_at = str(raw.get("created_at") or updated_at).strip()
        try:
            confidence = float(raw.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        mtype = normalize_mtype(raw.get("mtype"))
        try:
            importance = clamp(raw.get("importance", DEFAULT_IMPORTANCE))
        except (TypeError, ValueError):
            importance = DEFAULT_IMPORTANCE
        try:
            ttl_days = float(raw.get("ttl_days", DEFAULT_TTL_DAYS[mtype]))
        except (TypeError, ValueError):
            ttl_days = float(DEFAULT_TTL_DAYS[mtype])
        decay_type = str(raw.get("decay_type") or DEFAULT_DECAY_TYPE[mtype]).strip().lower()
        if decay_type not in DECAY_TYPES:
            decay_type = DEFAULT_DECAY_TYPE[mtype]
        try:
            access_count = max(0, int(raw.get("access_count", 0)))
        except (TypeError, ValueError):
            access_count = 0
        return {
            "scope": scope,
            # v0.8.0：导入不认文件里的 origin —— `/mem import` 没有会话上下文，
            # 不能把别人的来源硬塞进来；真覆盖时由 `_merge_one` 继承旧行的 origin。
            "origin": "",
            "subject": subject,
            "predicate": predicate,
            "value": value,
            "confidence": confidence,
            "source": str(raw.get("source") or source),
            "created_at": created_at,
            "updated_at": updated_at,
            "valid_from": str(raw.get("valid_from") or updated_at),
            "valid_to": raw.get("valid_to"),
            "mtype": mtype,
            "importance": importance,
            "ttl_days": ttl_days,
            "decay_type": decay_type,
            "last_accessed_at": raw.get("last_accessed_at"),
            "access_count": access_count,
        }

    @_db_guard
    def _merge_one(self, rec: dict) -> str:
        columns = (
            "scope", "origin", "subject", "predicate", "value", "confidence", "source",
            "created_at", "updated_at", "valid_from", "mtype", "importance",
            "ttl_days", "decay_type", "last_accessed_at", "access_count",
        )
        base_values = [rec[name] for name in columns]

        def insert(conn, valid_to=None, superseded_by=None, origin=None) -> int:
            values = list(base_values)
            if origin is not None:
                values[columns.index("origin")] = normalize_origin(origin)
            all_columns = list(columns) + ["valid_to", "superseded_by"]
            cols = ",".join(all_columns)
            marks = ",".join("?" * len(all_columns))
            cur = conn.execute(
                f"INSERT INTO mem({cols}) VALUES({marks})",
                [*values, valid_to, superseded_by],
            )
            return int(cur.lastrowid)

        with self._conn() as conn:
            if rec.get("valid_to"):  # 历史记录：直接补进去，不参与覆盖
                insert(conn, rec["valid_to"], rec.get("superseded_by"))
                return "added"

            row = conn.execute(
                "SELECT * FROM mem WHERE scope=? AND subject=? AND predicate=? AND valid_to IS NULL",
                (rec["scope"], rec["subject"], rec["predicate"]),
            ).fetchone()
            if row is None:
                insert(conn)
                return "added"

            old = _row_to_dict(row)
            if old["value"] == rec["value"]:
                return "skipped"
            if not _newer(rec["updated_at"], old["updated_at"]):
                return "skipped"

            # 同槽覆盖：新值沿用旧行的 origin。导入本身没有会话上下文（rec["origin"]
            # 恒为 ""），不能把已有归属抹掉；旧行作废后仍保留它自己的 origin 供审计。
            new_id = insert(conn, origin=rec.get("origin") or old.get("origin") or "")
            conn.execute(
                "UPDATE mem SET valid_to=?, superseded_by=?, updated_at=? WHERE id=?",
                (rec["updated_at"], new_id, rec["updated_at"], old["id"]),
            )
            return "updated"

    # ── 批量导入（LivingMemory 搬家） ──────────────────────────────────

    @_db_guard
    def import_records(self, records, *, dry_run: bool = False, state: dict | None = None) -> dict:
        """一批记录一次连接写完：内容重复跳过，同槽新者胜。

        和 `add()` / `import_json()` 的区别：
        - 一次连接写一整批（搬几千条时不要一条一个连接）；
        - **不碰 embedding**（一条一个 HTTP 请求会把搬家拖成几分钟）；
        - 去重靠 `content_hash(subject, predicate, value)`，跟库里已有的有效行比；
        - `dry_run=True` 走完全相同的写入路径再 `rollback()`，所以 `--dry-run` 报出来的
          数字就是真跑的数字，不是另写一套估算逻辑。

        `state` 是**跨批次**的去重状态（`{"contents": set, "slots": dict, "loaded": bool}`）：
        搬家是分批写的，而 dry-run 每批都会回滚 —— 不把状态传出去的话，第二批就看不到
        第一批写过的内容，报出来的「重复」会比真跑少。传同一个 `state` 进来即可对齐。

        返回：{added, updated, duplicate, stale, invalid, outcomes}，
        `outcomes` 逐条对应输入顺序（added / updated / duplicate / stale / invalid），
        调用方拿它统计「切出三元组 / 退回整体」各多少条。
        """
        added = updated = duplicate = stale = invalid = 0
        outcomes: list[str] = []
        columns = (
            "scope", "origin", "subject", "predicate", "value", "confidence", "source",
            "created_at", "updated_at", "valid_from", "mtype", "importance",
            "ttl_days", "decay_type", "last_accessed_at", "access_count",
        )
        with self._conn() as conn:
            if state is None:
                state = {}
            if not state.get("loaded"):
                rows = conn.execute(
                    "SELECT id, scope, subject, predicate, value, updated_at, origin "
                    "FROM mem WHERE valid_to IS NULL"
                ).fetchall()
                state["contents"] = {
                    content_hash(r["subject"], r["predicate"], r["value"]) for r in rows
                }
                state["slots"] = {
                    (r["scope"], r["subject"], r["predicate"]): {
                        "id": int(r["id"]),
                        "updated_at": r["updated_at"] or "",
                        "origin": normalize_origin(r["origin"]),
                    }
                    for r in rows
                }
                state["loaded"] = True
            contents = state["contents"]
            slots = state["slots"]
            for raw in records:
                try:
                    rec = self._normalize_import_item(raw, "livingmemory")
                except ValueError:
                    invalid += 1
                    outcomes.append("invalid")
                    continue
                key = content_hash(rec["subject"], rec["predicate"], rec["value"])
                if key in contents:
                    duplicate += 1
                    outcomes.append("duplicate")
                    continue
                slot = (rec["scope"], rec["subject"], rec["predicate"])
                old = slots.get(slot)
                if old is not None and not _newer(rec["updated_at"], old["updated_at"]):
                    stale += 1
                    outcomes.append("stale")
                    continue
                values = []
                for name in columns:
                    if name == "origin":
                        # 同槽覆盖沿用旧行 origin；全新行没有来源（空串）
                        values.append((old or {}).get("origin") or rec["origin"])
                    else:
                        values.append(rec[name])
                cur = conn.execute(
                    f"INSERT INTO mem({','.join(columns)}) "
                    f"VALUES({','.join('?' * len(columns))})",
                    values,
                )
                new_id = int(cur.lastrowid)
                if old is not None:
                    conn.execute(
                        "UPDATE mem SET valid_to=?, superseded_by=?, updated_at=? "
                        "WHERE id=? AND valid_to IS NULL",
                        (rec["updated_at"], new_id, rec["updated_at"], old["id"]),
                    )
                    updated += 1
                    outcomes.append("updated")
                else:
                    added += 1
                    outcomes.append("added")
                contents.add(key)
                slots[slot] = {
                    "id": new_id,
                    "updated_at": rec["updated_at"],
                    "origin": ((old or {}).get("origin") or rec["origin"]),
                }
            if dry_run:
                conn.rollback()
        return {
            "added": added,
            "updated": updated,
            "duplicate": duplicate,
            "stale": stale,
            "invalid": invalid,
            "outcomes": outcomes,
        }
