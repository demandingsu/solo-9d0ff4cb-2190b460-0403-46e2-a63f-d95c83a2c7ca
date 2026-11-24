"""核心业务逻辑：预登记、交册核定、缺考名单计算。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .database import read_connection, write_transaction


class ApiError(Exception):
    """业务校验错误，映射为 HTTP 响应。"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


# ---------- 预登记 ----------

def register_session(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = payload["session_id"]
    rooms = payload.get("rooms", [])
    students = payload.get("students", [])

    room_ids = [r["room_id"] for r in rooms]
    if len(set(room_ids)) != len(room_ids):
        raise ApiError(422, "duplicate_room", "同一期内考场 ID 重复")
    student_ids = [s["student_id"] for s in students]
    if len(set(student_ids)) != len(student_ids):
        raise ApiError(422, "duplicate_student", "同一期内学员 ID 重复")

    # 预校验学员的可去考场都在本期考场内
    room_set = set(room_ids)
    for stu in students:
        allowed = stu.get("room_ids", [])
        if not allowed:
            raise ApiError(
                422, "no_allowed_room",
                f"学员 {stu['student_id']} 至少要登记一个可去考场",
            )
        if len(set(allowed)) != len(allowed):
            raise ApiError(
                422, "duplicate_allowed_room",
                f"学员 {stu['student_id']} 的可去考场重复",
            )
        for rid in allowed:
            if rid not in room_set:
                raise ApiError(
                    422, "unknown_room",
                    f"学员 {stu['student_id']} 引用了不存在的考场 {rid}",
                )

    try:
        with write_transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM session WHERE id=?", (session_id,)
            ).fetchone()
            if existing:
                raise ApiError(
                    409, "session_exists", f"期 {session_id} 已登记"
                )

            conn.execute(
                "INSERT INTO session(id, name) VALUES(?, ?)",
                (session_id, payload.get("name") or session_id),
            )
            for room in rooms:
                conn.execute(
                    "INSERT INTO room(session_id, room_id, name)"
                    " VALUES(?, ?, ?)",
                    (session_id, room["room_id"],
                     room.get("name") or room["room_id"]),
                )
            for stu in students:
                conn.execute(
                    "INSERT INTO student(id, session_id, name)"
                    " VALUES(?, ?, ?)",
                    (stu["student_id"], session_id,
                     stu.get("name") or stu["student_id"]),
                )
                for rid in stu["room_ids"]:
                    conn.execute(
                        "INSERT INTO student_room"
                        "(session_id, student_id, room_id) VALUES(?, ?, ?)",
                        (session_id, stu["student_id"], rid),
                    )
    except sqlite3.IntegrityError as exc:
        # 并发注册同期等场景
        raise ApiError(409, "conflict", f"登记冲突：{exc}") from exc

    return {
        "session_id": session_id,
        "rooms": len(rooms),
        "students": len(students),
    }


# ---------- 缺考判定 ----------

def is_absent(conn: sqlite3.Connection, session_id: str, student_id: str) -> bool:
    """可去的考场全部已交册，且各场最新版都没有该学员。"""
    allowed = conn.execute(
        "SELECT room_id FROM student_room"
        " WHERE session_id=? AND student_id=?",
        (session_id, student_id),
    ).fetchall()
    if not allowed:
        return False
    for row in allowed:
        room_id = row["room_id"]
        roster = conn.execute(
            "SELECT 1 FROM roster WHERE session_id=? AND room_id=?",
            (session_id, room_id),
        ).fetchone()
        if roster is None:
            return False  # 还有考场没交册，暂不列为缺考
        present = conn.execute(
            "SELECT 1 FROM roster_entry"
            " WHERE session_id=? AND room_id=? AND student_id=?",
            (session_id, room_id, student_id),
        ).fetchone()
        if present is not None:
            return False  # 任一考场最新版有他，就不是缺考
    return True


# ---------- 交册 ----------

def _canonical_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def submit_rosters(payload: dict[str, Any]) -> dict[str, Any]:
    batch_id = payload["batch_id"]
    session_id = payload["session_id"]
    rosters = payload["rosters"]

    if not rosters:
        raise ApiError(422, "empty_batch", "批次至少要包含一场签到册")
    batch_room_ids = [r["room_id"] for r in rosters]
    if len(set(batch_room_ids)) != len(batch_room_ids):
        raise ApiError(
            422, "duplicate_room_in_batch", "同批中同一考场出现多次"
        )

    payload_hash = _canonical_hash(payload)

    with write_transaction() as conn:
        # 0) 期必须存在
        session = conn.execute(
            "SELECT 1 FROM session WHERE id=?", (session_id,)
        ).fetchone()
        if session is None:
            raise ApiError(
                404, "session_not_found", f"期 {session_id} 不存在"
            )

        # 1) 幂等：同批号原样再交 -> 取回原回执；换内容 -> 拒收
        prior = conn.execute(
            "SELECT payload_hash, receipt_json FROM batch WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if prior is not None:
            if prior["payload_hash"] != payload_hash:
                raise ApiError(
                    409, "batch_id_conflict",
                    f"批号 {batch_id} 已用于不同内容的交册，整批拒收",
                )
            return json.loads(prior["receipt_json"])

        # 2) 整批校验，任一不通过则不写任何数据
        valid_rooms = {
            row["room_id"]
            for row in conn.execute(
                "SELECT room_id FROM room WHERE session_id=?", (session_id,)
            ).fetchall()
        }
        for roster in rosters:
            room_id = roster["room_id"]
            if room_id not in valid_rooms:
                raise ApiError(
                    422, "unknown_room",
                    f"考场 {room_id} 不属于期 {session_id}，整批拒收",
                )
            old = conn.execute(
                "SELECT version FROM roster"
                " WHERE session_id=? AND room_id=?",
                (session_id, room_id),
            ).fetchone()
            old_version = old["version"] if old else 0
            if roster["version"] <= old_version:
                raise ApiError(
                    422, "version_not_higher",
                    f"考场 {room_id} 版号 {roster['version']} 未高于已交版号 "
                    f"{old_version}，整批拒收",
                )
            entries = roster.get("student_ids", [])
            if len(set(entries)) != len(entries):
                raise ApiError(
                    422, "duplicate_student_in_roster",
                    f"考场 {room_id} 签到册内学员重复，整批拒收",
                )
            for sid in entries:
                allowed = conn.execute(
                    "SELECT 1 FROM student_room"
                    " WHERE session_id=? AND student_id=? AND room_id=?",
                    (session_id, sid, room_id),
                ).fetchone()
                if allowed is None:
                    raise ApiError(
                        422, "student_not_allowed",
                        f"学员 {sid} 不能去考场 {room_id}，整批拒收",
                    )

        # 3) 本批影响面：所有可去本批任一考场的学员（只重核这些人）
        placeholders = ",".join("?" for _ in batch_room_ids)
        affected_rows = conn.execute(
            f"SELECT DISTINCT student_id FROM student_room"
            f" WHERE session_id=? AND room_id IN ({placeholders})",
            [session_id, *batch_room_ids],
        ).fetchall()
        affected = sorted(r["student_id"] for r in affected_rows)

        before_absent = {
            sid for sid in affected
            if conn.execute(
                "SELECT 1 FROM absent_student"
                " WHERE session_id=? AND student_id=?",
                (session_id, sid),
            ).fetchone() is not None
        }

        # 4) 签到册生效：覆盖为最新版（空册同样落库，视为已交）
        for roster in rosters:
            room_id = roster["room_id"]
            conn.execute(
                "DELETE FROM roster_entry"
                " WHERE session_id=? AND room_id=?",
                (session_id, room_id),
            )
            conn.execute(
                "INSERT INTO roster(session_id, room_id, version, batch_id)"
                " VALUES(?, ?, ?, ?)"
                " ON CONFLICT(session_id, room_id) DO UPDATE SET"
                " version=excluded.version, batch_id=excluded.batch_id",
                (session_id, room_id, roster["version"], batch_id),
            )
            for sid in roster.get("student_ids", []):
                conn.execute(
                    "INSERT INTO roster_entry"
                    "(session_id, room_id, student_id) VALUES(?, ?, ?)",
                    (session_id, room_id, sid),
                )

        # 5) 重核影响面，更新物化缺考名单，得到净增/净减
        after_absent: set[str] = set()
        for sid in affected:
            if is_absent(conn, session_id, sid):
                after_absent.add(sid)
                if sid not in before_absent:
                    conn.execute(
                        "INSERT INTO absent_student(session_id, student_id)"
                        " VALUES(?, ?)",
                        (session_id, sid),
                    )
            elif sid in before_absent:
                conn.execute(
                    "DELETE FROM absent_student"
                    " WHERE session_id=? AND student_id=?",
                    (session_id, sid),
                )

        added = sorted(after_absent - before_absent)
        removed = sorted(before_absent - after_absent)

        receipt = {
            "batch_id": batch_id,
            "session_id": session_id,
            "status": "accepted",
            "rooms_accepted": [
                {"room_id": r["room_id"], "version": r["version"],
                 "present_count": len(r.get("student_ids", []))}
                for r in rosters
            ],
            "absence_before": sorted(before_absent),
            "absence_after": sorted(after_absent),
            "added_absent": added,
            "removed_absent": removed,
        }

        # 6) 回执与签到册同一事务落库
        conn.execute(
            "INSERT INTO batch(batch_id, session_id, payload_hash,"
            " request_json, receipt_json) VALUES(?, ?, ?, ?, ?)",
            (batch_id, session_id, payload_hash,
             json.dumps(payload, ensure_ascii=False, sort_keys=True),
             json.dumps(receipt, ensure_ascii=False)),
        )
        return receipt


# ---------- 查询 ----------

def get_absent_list(session_id: str) -> dict[str, Any]:
    with read_connection() as conn:
        session = conn.execute(
            "SELECT id, name FROM session WHERE id=?", (session_id,)
        ).fetchone()
        if session is None:
            raise ApiError(
                404, "session_not_found", f"期 {session_id} 不存在"
            )
        rows = conn.execute(
            "SELECT s.id AS student_id, s.name"
            " FROM absent_student a JOIN student s"
            " ON s.session_id=a.session_id AND s.id=a.student_id"
            " WHERE a.session_id=? ORDER BY s.id",
            (session_id,),
        ).fetchall()
        return {
            "session_id": session_id,
            "absent_students": [
                {"student_id": r["student_id"], "name": r["name"]}
                for r in rows
            ],
        }


def get_receipt(batch_id: str) -> dict[str, Any]:
    with read_connection() as conn:
        row = conn.execute(
            "SELECT receipt_json FROM batch WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise ApiError(
                404, "receipt_not_found", f"批号 {batch_id} 不存在"
            )
        return json.loads(row["receipt_json"])


def get_room_status(session_id: str) -> dict[str, Any]:
    with read_connection() as conn:
        session = conn.execute(
            "SELECT 1 FROM session WHERE id=?", (session_id,)
        ).fetchone()
        if session is None:
            raise ApiError(
                404, "session_not_found", f"期 {session_id} 不存在"
            )
        rows = conn.execute(
            "SELECT rm.room_id AS room_id, rm.name, ro.version, ro.batch_id,"
            " (SELECT COUNT(*) FROM roster_entry e"
            "  WHERE e.session_id=rm.session_id AND e.room_id=rm.room_id)"
            " AS present_count"
            " FROM room rm LEFT JOIN roster ro"
            " ON ro.session_id=rm.session_id AND ro.room_id=rm.room_id"
            " WHERE rm.session_id=? ORDER BY rm.room_id",
            (session_id,),
        ).fetchall()
        return {
            "session_id": session_id,
            "rooms": [
                {
                    "room_id": row["room_id"],
                    "name": row["name"],
                    "submitted": row["version"] is not None,
                    "version": row["version"],
                    "batch_id": row["batch_id"],
                    "present_count": row["present_count"],
                }
                for row in rows
            ],
        }
