"""SQLite 连接与表结构。

设计要点：
- 所有写操作在单连接上以 BEGIN IMMEDIATE 事务串行执行，保证并行交册时
  每批的“签到册、缺考名单、回执”一起生效，不会出现半批状态。
- 考场、签到册等业务数据均按期隔离，主键带 session_id，不同期可复用
  相同的考场编号。
- absent_student 是按最新版签到册维护的物化结果，查缺考名单直接读表。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("ABSENCE_DB_PATH", "/data/absence.db")

# 进程内写锁：配合 BEGIN IMMEDIATE，把交册批次完全串行化。
_write_lock = threading.Lock()

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 期
CREATE TABLE IF NOT EXISTS session (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 考场（按期，不同期可复用同一考场编号）
CREATE TABLE IF NOT EXISTS room (
    session_id  TEXT NOT NULL REFERENCES session(id),
    room_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    PRIMARY KEY (session_id, room_id)
);

-- 学员（按期登记）
CREATE TABLE IF NOT EXISTS student (
    id          TEXT NOT NULL,
    session_id  TEXT NOT NULL REFERENCES session(id),
    name        TEXT NOT NULL,
    PRIMARY KEY (session_id, id)
);

-- 学员可去的考场（多对多，预先登记）
CREATE TABLE IF NOT EXISTS student_room (
    session_id  TEXT NOT NULL,
    student_id  TEXT NOT NULL,
    room_id     TEXT NOT NULL,
    PRIMARY KEY (session_id, student_id, room_id),
    FOREIGN KEY (session_id, student_id)
        REFERENCES student(session_id, id),
    FOREIGN KEY (session_id, room_id)
        REFERENCES room(session_id, room_id)
);

-- 每个考场已接收的签到册版本（仅留最新版）
CREATE TABLE IF NOT EXISTS roster (
    session_id    TEXT NOT NULL,
    room_id       TEXT NOT NULL,
    version       INTEGER NOT NULL,
    batch_id      TEXT NOT NULL,
    submitted_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (session_id, room_id),
    FOREIGN KEY (session_id, room_id)
        REFERENCES room(session_id, room_id)
);

-- 最新版签到册中的在场学员
CREATE TABLE IF NOT EXISTS roster_entry (
    session_id  TEXT NOT NULL,
    room_id     TEXT NOT NULL,
    student_id  TEXT NOT NULL,
    PRIMARY KEY (session_id, room_id, student_id),
    FOREIGN KEY (session_id, room_id)
        REFERENCES room(session_id, room_id)
);

-- 交册批次及回执（回执原样落库，同批号重交可原样取回）
CREATE TABLE IF NOT EXISTS batch (
    batch_id        TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    payload_hash    TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    receipt_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 物化缺考名单：该学员可去的考场全部已交册，且最新版均无此人
CREATE TABLE IF NOT EXISTS absent_student (
    session_id  TEXT NOT NULL,
    student_id  TEXT NOT NULL,
    PRIMARY KEY (session_id, student_id)
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def write_transaction():
    """串行化的写事务：取进程锁后立即 BEGIN IMMEDIATE 取库级写锁。"""
    with _write_lock:
        conn = get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


@contextmanager
def read_connection():
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()
