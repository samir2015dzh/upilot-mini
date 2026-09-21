"""SQLite 状态：文件队列、SHA1 缓存、上传断点、cid 缓存。

设计对应 UPilot 的 upload_files 表：
  - sha1 一旦算完就落库 → 重试/重启都不再读盘
  - upload_id / upload_callback / upload_oss_url / upload_partsize 落库 → 分块断点续传
  - status 状态机：待处理 / 待秒传 / 秒传中 / 待重试 / 待上传 / 上传中 / 已上传 / 错误
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  sha1 TEXT,
  status TEXT NOT NULL DEFAULT '待处理',
  stable_count INTEGER NOT NULL DEFAULT 0,
  retry_count INTEGER NOT NULL DEFAULT 0,
  first_failed_at REAL,
  next_retry_at REAL,
  upload_id TEXT, upload_callback TEXT, upload_oss_url TEXT, upload_partsize INTEGER,
  target_dir TEXT, pid INTEGER, file_id TEXT,
  progress REAL DEFAULT 0, speed REAL DEFAULT 0,
  message TEXT,
  upload_mode TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status ON files(status, next_retry_at);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def _ddl_columns() -> list[tuple[str, str]]:
    """从 DDL 里解析出 files 表的 (列名, 定义串)，供 _migrate 对齐老库。"""
    body = DDL.split("CREATE TABLE IF NOT EXISTS files(", 1)[1].split(");", 1)[0]
    out: list[tuple[str, str]] = []
    for chunk in body.split(","):
        decl = chunk.strip()
        if not decl or decl.upper().startswith(("PRIMARY KEY", "CONSTRAINT", "UNIQUE", "FOREIGN")):
            continue
        out.append((decl.split()[0], decl))
    return out


class Store:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(DDL)
            self._migrate()
            self.db.commit()

    def _migrate(self) -> None:
        """老库补列：按 DDL 全量对齐，以后新增字段不用再手工登记一条 ALTER。

        原来只硬编码补 upload_mode 一列 —— 下次再往 DDL 加列、忘了同步这里，
        老库就会缺列，而 store.set 写该列时 sqlite 直接报 "no such column"，
        那条文件的上传状态就再也写不进去（报错被 spawn 的 try 兜住，只在日志里
        留一行，界面上看不到，很难查）。
        """
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(files)").fetchall()}
        for name, decl in _ddl_columns():
            if name in have or "PRIMARY KEY" in decl.upper():
                continue
            # sqlite 的 ALTER TABLE 不允许新增「NOT NULL 且无默认值」的列。
            # 这类列只可能是最初建表就有的，老库不可能缺，跳过即可。
            up = decl.upper()
            if "NOT NULL" in up and "DEFAULT" not in up:
                continue
            self.db.execute(f"ALTER TABLE files ADD COLUMN {decl}")

    # ---------- 队列 ----------

    def seen(self, path: str, size: int, mtime: float) -> dict | None:
        """同 path+size+mtime 已存在则返回记录（= 上游 check_duplicate，不重复入队）。"""
        with self.lock:
            cur = self.db.execute(
                "SELECT * FROM files WHERE path=? AND size=? AND mtime=?", (path, size, mtime)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def add(self, path: str, size: int, mtime: float) -> dict:
        now = time.time()
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO files(path,size,mtime,status,created_at,updated_at)"
                " VALUES(?,?,?,'待处理',?,?)",
                (path, size, mtime, now, now),
            )
            self.db.commit()
        return self.get(path)

    def get(self, path: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
            return dict(row) if row else None

    def set(self, path: str, **kv) -> None:
        if not kv:
            return
        kv["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in kv)
        with self.lock:
            self.db.execute(f"UPDATE files SET {cols} WHERE path=?", (*kv.values(), path))
            self.db.commit()

    def by_status(self, status: str, limit: int = 20, due: bool = False) -> list[dict]:
        sql = "SELECT * FROM files WHERE status=?"
        args: list = [status]
        if due:
            sql += " AND (next_retry_at IS NULL OR next_retry_at<=?)"
            args.append(time.time())
        sql += " ORDER BY created_at ASC, rowid ASC LIMIT ?"
        args.append(limit)
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def counts(self) -> dict:
        with self.lock:
            rows = self.db.execute("SELECT status, COUNT(*) c FROM files GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}

    def recent(self, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT path,status,progress,message,updated_at,target_dir FROM files"
                " ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def active(self, limit: int = 50) -> list[dict]:
        """正在处理的文件（含等待重试的），用于界面实时区。"""
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM files WHERE status IN ('处理中','秒传中','待重试','上传中')"
                " ORDER BY CASE status WHEN '秒传中' THEN 0 WHEN '上传中' THEN 1"
                " WHEN '处理中' THEN 2 ELSE 3 END, updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        with self.lock:
            rows = self.db.execute(
                "SELECT status, COUNT(*) c, COALESCE(SUM(size),0) b FROM files GROUP BY status"
            ).fetchall()
            total = self.db.execute(
                "SELECT COUNT(*) c, COALESCE(SUM(size),0) b FROM files"
            ).fetchone()
            rapid = self.db.execute(
                "SELECT COUNT(*) c, COALESCE(SUM(size),0) b FROM files"
                " WHERE upload_mode='rapid'"
            ).fetchone()
            normal = self.db.execute(
                "SELECT COUNT(*) c, COALESCE(SUM(size),0) b FROM files"
                " WHERE upload_mode='normal'"
            ).fetchone()
            retried = self.db.execute(
                "SELECT COALESCE(SUM(retry_count),0) r FROM files"
            ).fetchone()
        return {
            "total": {"count": total["c"], "bytes": total["b"]},
            "rapid": {"count": rapid["c"], "bytes": rapid["b"]},
            "normal": {"count": normal["c"], "bytes": normal["b"]},
            "retry_total": retried["r"],
            "by_status": {r["status"]: {"count": r["c"], "bytes": r["b"]} for r in rows},
        }

    def requeue(self, statuses=("错误",)) -> int:
        """把指定状态的行重置为待处理（用于界面的“重试失败项”）。"""
        marks = ",".join("?" for _ in statuses)
        with self.lock:
            cur = self.db.execute(
                f"UPDATE files SET status='待处理', retry_count=0, first_failed_at=NULL,"
                f" next_retry_at=NULL, message='手动重新入队', updated_at=? WHERE status IN ({marks})",
                (time.time(), *statuses),
            )
            self.db.commit()
            return cur.rowcount

    def retry_now(self) -> int:
        """把所有“待重试”提前到期（界面上的“立即重试秒传”）。"""
        with self.lock:
            cur = self.db.execute(
                "UPDATE files SET next_retry_at=0, updated_at=? WHERE status='待重试'",
                (time.time(),),
            )
            self.db.commit()
            return cur.rowcount

    # ---------- cid 缓存 ----------

    def cid_get(self, cloud_path: str) -> int | None:
        with self.lock:
            row = self.db.execute("SELECT v FROM meta WHERE k=?", (f"cid:{cloud_path}",)).fetchone()
        return int(row["v"]) if row else None

    def cid_set(self, cloud_path: str, cid: int) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (f"cid:{cloud_path}", str(cid))
            )
            self.db.commit()

    def cid_del(self, cloud_path: str) -> None:
        """删掉某个云端路径的 cid 缓存。

        云端目录被删/被移走、115 回 10014 时必须清掉这条 —— 只清内存那份的话，
        下一次 ensure_pid 会立刻从 SQLite 读回同一个旧 cid，等于没清。
        """
        with self.lock:
            self.db.execute("DELETE FROM meta WHERE k=?", (f"cid:{cloud_path}",))
            self.db.commit()

    # ---------- 任务明细（界面用） ----------

    def list_tasks(self, status: str | None = None, q: str = "", limit: int = 50,
                   offset: int = 0) -> tuple[list[dict], int]:
        """按状态/关键词翻页查文件明细，返回 (行, 总数)。"""
        where: list[str] = []
        args: list = []
        if status and status not in ("全部", "all"):
            if status == "进行中":
                where.append("status IN ('处理中','秒传中','上传中','待重试')")
            elif status == "待上传":
                where.append("status IN ('待处理','待秒传','待上传')")
            elif status == "已完成":
                where.append("status='已上传'")
            else:
                where.append("status=?")
                args.append(status)
        if q:
            where.append("(path LIKE ? OR target_dir LIKE ? OR message LIKE ?)")
            like = f"%{q}%"
            args.extend([like, like, like])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self.lock:
            total = self.db.execute(
                f"SELECT COUNT(*) c FROM files{clause}", args).fetchone()["c"]
            rows = self.db.execute(
                f"SELECT * FROM files{clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (*args, int(limit), int(offset))).fetchall()
        return [dict(r) for r in rows], int(total)

    def forget(self, path: str) -> int:
        """删掉记录：文件本体不动，下次扫描会当作新文件重新入队。"""
        with self.lock:
            cur = self.db.execute("DELETE FROM files WHERE path=?", (path,))
            self.db.commit()
            return cur.rowcount

    def reset_one(self, path: str) -> int:
        """单个文件重新排队（保留已算好的 sha1，省一次读盘）。"""
        with self.lock:
            cur = self.db.execute(
                "UPDATE files SET status='待处理', retry_count=0, first_failed_at=NULL,"
                " next_retry_at=NULL, stable_count=999, message='界面重新入队', updated_at=?"
                " WHERE path=?", (time.time(), path))
            self.db.commit()
            return cur.rowcount

    def mark_ready(self, path: str) -> int:
        """跳过稳定等待，把文件直接置为可处理（界面上的“立即上传”）。"""
        with self.lock:
            cur = self.db.execute(
                "UPDATE files SET stable_count=999, message='界面触发立即上传', updated_at=?"
                " WHERE path=? AND status IN ('待处理','待秒传','待上传','错误','待重试')",
                (time.time(), path))
            self.db.commit()
            return cur.rowcount
