"""SQLite 持久层：任务与逐行状态，重启后可继续查询进度。

设计要点：
- “保存已开始”（SUBMITTING）与“已提交未确认”（UNCONFIRMED）都持久化：
  点击保存前后进程被杀死，重入时也绝不可能把该行当作未提交而重填。
- (门户, 文件指纹) 唯一约束实现同一门户相同文件的重复/并发请求接续同一任务。
- 单连接 + 线程锁 + WAL，配合 asyncio 事件循环串行访问，安全且简单。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .states import (
    ROW_PENDING, ROW_SUBMITTED_UNCONFIRMED, ROW_SUBMITTING,
    TASK_QUEUED, TASK_RUNNING,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    portal_url      TEXT NOT NULL,
    file_sha256     TEXT NOT NULL,
    filename        TEXT NOT NULL,
    status          TEXT NOT NULL,
    note            TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    UNIQUE(portal_url, file_sha256)
);

CREATE TABLE IF NOT EXISTS task_rows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    row_no          INTEGER NOT NULL,          -- CSV 数据行序号（从 1 起）
    hole_no         TEXT NOT NULL,
    top_depth       REAL NOT NULL,
    bottom_depth    REAL NOT NULL,
    lithology       TEXT NOT NULL,
    status          TEXT NOT NULL,
    detail          TEXT,
    record_url      TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL,
    UNIQUE(task_id, row_no)
);

CREATE INDEX IF NOT EXISTS idx_rows_task ON task_rows(task_id, row_no);
"""


class DB:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- tasks ----------
    def create_or_get_task(
        self, task_id: str, portal_url: str, file_sha256: str,
        filename: str, rows: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        """返回 (task, created)。重复文件返回既有任务，不改动其进度。"""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM tasks WHERE portal_url=? AND file_sha256=?",
                (portal_url, file_sha256),
            )
            existing = cur.fetchone()
            if existing is not None:
                return dict(existing), False

            self._conn.execute(
                "INSERT INTO tasks(id, portal_url, file_sha256, filename, status,"
                " note, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (task_id, portal_url, file_sha256, filename, TASK_QUEUED,
                 None, now, now),
            )
            for r in rows:
                self._conn.execute(
                    "INSERT INTO task_rows(task_id, row_no, hole_no, top_depth,"
                    " bottom_depth, lithology, status, detail, attempts, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (task_id, r["row_no"], r["hole_no"], r["top_depth"],
                     r["bottom_depth"], r["lithology"], ROW_PENDING, None, 0, now),
                )
            self._conn.commit()
            task = self._conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(task), True

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def set_task_status(self, task_id: str, status: str, note: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status=?, note=?, updated_at=? WHERE id=?",
                (status, note, time.time(), task_id),
            )
            self._conn.commit()

    # ---------- rows ----------
    def list_rows(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM task_rows WHERE task_id=? ORDER BY row_no", (task_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_row(self, task_id: str, row_no: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM task_rows WHERE task_id=? AND row_no=?",
                (task_id, row_no),
            ).fetchone()
            return dict(row) if row else None

    def update_row(
        self, task_id: str, row_no: int, status: str,
        detail: str | None = None, record_url: str | None = None,
        inc_attempt: bool = False,
    ) -> None:
        with self._lock:
            sets = ["status=?", "detail=?", "updated_at=?"]
            params: list[Any] = [status, detail, time.time()]
            if record_url is not None:
                sets.append("record_url=?")
                params.append(record_url)
            if inc_attempt:
                sets.append("attempts=attempts+1")
            params.extend([task_id, row_no])
            self._conn.execute(
                f"UPDATE task_rows SET {', '.join(sets)} WHERE task_id=? AND row_no=?",
                params,
            )
            self._conn.commit()

    def mark_submitting(self, task_id: str, row_no: int,
                        detail: str = "保存动作已开始，等待保存确认") -> None:
        """点击保存前落库：此后任何超时/崩溃都按“无法排除已提交”处理。

        只允许从 PENDING/ERROR（从未保存过）进入 SUBMITTING，
        终态/未确认/冲突行调用均为无操作，杜绝任何路径上的重复填报。
        """
        with self._lock:
            self._conn.execute(
                "UPDATE task_rows SET status=?, detail=?, updated_at=? "
                "WHERE task_id=? AND row_no=? AND status IN (?, ?)",
                (ROW_SUBMITTING, detail, time.time(),
                 task_id, row_no, ROW_PENDING, "ERROR"),
            )
            self._conn.commit()

    def row_counts(self, task_id: str) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) c FROM task_rows WHERE task_id=? GROUP BY status",
                (task_id,),
            ).fetchall()
            return {r["status"]: r["c"] for r in rows}

    def recover_stale_running(self) -> int:
        """崩溃恢复：上次进程残留的 RUNNING 任务回到 QUEUED。

        同时把停在 SUBMITTING（保存已开始但未见结果）的行升级为
        UNCONFIRMED：重启后无法排除门户已落库，只能续查、绝不重提。
        其他行状态（PENDING/UNCONFIRMED/CONFLICT 等）原样保留。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET status=?, note=?, updated_at=? WHERE status=?",
                (TASK_QUEUED, "检测到上次运行中断，已重新排队（行进度保留）",
                 time.time(), TASK_RUNNING),
            )
            # 即使任务并非 RUNNING（例如中断发生在两轮处理之间），
            # 残留的 SUBMITTING 行也必须收敛为只查不填。
            self._conn.execute(
                "UPDATE task_rows SET status=?, detail=?, updated_at=? "
                "WHERE status=?",
                (ROW_SUBMITTED_UNCONFIRMED,
                 "进程中断时保存已开始且未见确认，重启后按未确认处理（只查不填）",
                 time.time(), ROW_SUBMITTING),
            )
            self._conn.commit()
            return cur.rowcount
