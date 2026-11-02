"""记忆库存储层：sqlite + 事实三元组 + 同槽覆盖 + 向量缓存。

设计：纯标准库，**不 import astrbot**，可以脱离框架单独跑（selftest.py 就是这么测的）。

表结构（mem 表对齐 dd-home/mem/mem.py）：

    mem(id, scope, subject, predicate, value, confidence, source,
        created_at, updated_at, valid_from, valid_to, superseded_by)
    vec(mem_id, model, dim, vec BLOB, text_hash)

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
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

DEFAULT_DB_NAME = "mem.db"
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

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
  superseded_by INTEGER
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
"""


class StoreError(Exception):
    """记忆库打不开 / 读不了 / 写不进。上层统一回「记忆库读不了」，不抛 traceback。"""


def now_iso() -> str:
    """本地时区、精确到秒的 ISO 时间串（和 dd-home/mem/mem.py 一致）。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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


def _newer(left: str, right: str) -> bool:
    """left 是否比 right 新（ISO 串解析失败就退回字符串比较）。"""
    a, b = _parse_ts(left), _parse_ts(right)
    if a is not None and b is not None:
        return a > b
    return str(left or "") > str(right or "")


class Store:
    """记忆库。所有方法同步、线程安全（每次现开连接）。"""

    def __init__(self, path: str):
        self.path = path
        try:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with self._conn() as conn:
                conn.executescript(SCHEMA)
        except (sqlite3.Error, OSError) as e:
            raise StoreError(f"打开 {path} 失败：{e}") from e

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
    ) -> dict:
        """写一条事实。同槽旧记录自动作废（填 valid_to + superseded_by）。"""
        ts = now_iso()
        with self._conn() as conn:
            old = conn.execute(
                "SELECT id FROM mem WHERE scope=? AND subject=? AND predicate=? AND valid_to IS NULL",
                (scope, subject, predicate),
            ).fetchall()
            cur = conn.execute(
                "INSERT INTO mem(scope,subject,predicate,value,confidence,source,"
                "created_at,updated_at,valid_from) VALUES(?,?,?,?,?,?,?,?,?)",
                (scope, subject, predicate, value, confidence, source, ts, ts, ts),
            )
            new_id = int(cur.lastrowid)
            superseded = [int(r["id"]) for r in old]
            for old_id in superseded:
                conn.execute(
                    "UPDATE mem SET valid_to=?, superseded_by=?, updated_at=? WHERE id=?",
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
        return {
            "scope": scope,
            "subject": subject,
            "predicate": predicate,
            "value": value,
            "confidence": confidence,
            "source": str(raw.get("source") or source),
            "created_at": created_at,
            "updated_at": updated_at,
            "valid_from": str(raw.get("valid_from") or updated_at),
            "valid_to": raw.get("valid_to"),
        }

    @_db_guard
    def _merge_one(self, rec: dict) -> str:
        with self._conn() as conn:
            if rec.get("valid_to"):  # 历史记录：直接补进去，不参与覆盖
                conn.execute(
                    "INSERT INTO mem(scope,subject,predicate,value,confidence,source,"
                    "created_at,updated_at,valid_from,valid_to) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        rec["scope"], rec["subject"], rec["predicate"], rec["value"],
                        rec["confidence"], rec["source"], rec["created_at"],
                        rec["updated_at"], rec["valid_from"], rec["valid_to"],
                    ),
                )
                return "added"

            row = conn.execute(
                "SELECT * FROM mem WHERE scope=? AND subject=? AND predicate=? AND valid_to IS NULL",
                (rec["scope"], rec["subject"], rec["predicate"]),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO mem(scope,subject,predicate,value,confidence,source,"
                    "created_at,updated_at,valid_from) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        rec["scope"], rec["subject"], rec["predicate"], rec["value"],
                        rec["confidence"], rec["source"], rec["created_at"],
                        rec["updated_at"], rec["valid_from"],
                    ),
                )
                return "added"

            old = _row_to_dict(row)
            if old["value"] == rec["value"]:
                return "skipped"
            if not _newer(rec["updated_at"], old["updated_at"]):
                return "skipped"

            cur = conn.execute(
                "INSERT INTO mem(scope,subject,predicate,value,confidence,source,"
                "created_at,updated_at,valid_from) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    rec["scope"], rec["subject"], rec["predicate"], rec["value"],
                    rec["confidence"], rec["source"], rec["created_at"],
                    rec["updated_at"], rec["valid_from"],
                ),
            )
            conn.execute(
                "UPDATE mem SET valid_to=?, superseded_by=?, updated_at=? WHERE id=?",
                (rec["updated_at"], int(cur.lastrowid), rec["updated_at"], old["id"]),
            )
            return "updated"
