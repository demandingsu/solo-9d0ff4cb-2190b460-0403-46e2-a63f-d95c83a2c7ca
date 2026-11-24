"""端到端实测脚本（仅用标准库，可 `python3 -I` 运行）。

覆盖：
1. 预登记（学员可去多个考场）
2. 并行交册（并发 POST，服务端串行核定）
3. 补交空册：空册也算已交，触发缺考净增
4. 同批跨场更正：一批同时覆盖多场，缺考净增/净减同时发生
5. 拒收：版号未高于已交版号、列了不能去该场的学员、同批号换内容
6. 幂等：同批号原样再交取回原回执
7. 缺考名单查询、交册状态查询
8. 改派：移出原场后原场现行签到仍算到考；换册保留上一版签到
9. 改派后删去、未恢复资格补入整批拒收，恢复资格后可补入
10. 旧回执可查、原样重交返回当时回执；改派与交册并行
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
SID = os.environ.get("SESSION_ID", f"e2e-{int(time.time())}")

passed = 0
failed = 0


def call(method: str, path: str, body: dict | None = None,
         expect_error: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            obj = json.loads(resp.read().decode())
            status = resp.status
    except urllib.error.HTTPError as e:
        obj = json.loads(e.read().decode())
        status = e.code
    if expect_error is not None:
        assert status >= 400, f"期望失败却成功: {method} {path} -> {obj}"
        assert obj["error"] == expect_error, (
            f"期望错误 {expect_error}，实际 {obj}")
        ok(f"拒收符合预期: {expect_error}")
        return None
    assert status < 400, f"请求失败: {method} {path} -> {status} {obj}"
    return obj


def ok(msg: str) -> None:
    global passed
    passed += 1
    print(f"  ✓ {msg}")


def check(cond: bool, msg: str) -> None:
    global failed
    assert cond, msg
    ok(msg)


def main() -> None:
    print(f"== 目标 API: {BASE}，期: {SID} ==")

    # ---------- 1. 预登记 ----------
    # 考场 A、B；学员 s1~s4
    # s1 可去 A/B；s2 可去 A；s3 可去 A/B；s4 只可去 B
    call("POST", "/admin/sessions", {
        "session_id": SID,
        "name": "补考第一期",
        "rooms": [{"room_id": "A", "name": "第一考场"},
                 {"room_id": "B", "name": "第二考场"}],
        "students": [
            {"student_id": "s1", "name": "张三", "room_ids": ["A", "B"]},
            {"student_id": "s2", "name": "李四", "room_ids": ["A"]},
            {"student_id": "s3", "name": "王五", "room_ids": ["A", "B"]},
            {"student_id": "s4", "name": "赵六", "room_ids": ["B"]},
        ],
    })
    ok("预登记成功：2 个考场、4 名学员（含多考场学员）")

    # ---------- 2. 并行交册 ----------
    # A 册（v1）：s1、s2 在场；B 册（v1）：s3 在场
    def submit(body):
        return call("POST", "/rosters/batches", body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(submit, {
            "batch_id": "B-A1", "session_id": SID,
            "rosters": [{"room_id": "A", "version": 1,
                         "student_ids": ["s1", "s2"]}]})
        f2 = pool.submit(submit, {
            "batch_id": "B-B1", "session_id": SID,
            "rosters": [{"room_id": "B", "version": 1,
                         "student_ids": ["s3"]}]})
        r_a, r_b = f1.result(), f2.result()
    check(r_a["status"] == "accepted" and r_b["status"] == "accepted",
          "并行交册两批均被接收（服务端串行核定）")
    # 两场都已交，开始判定：
    # s1 在 A 最新版 -> 不缺考；s2 在 A -> 不缺考；
    # s3 在 B -> 不缺考；s4 只可去 B，B 最新版无 s4 -> 缺考
    absent = call("GET", f"/sessions/{SID}/absences")["absent_students"]
    check([x["student_id"] for x in absent] == ["s4"],
          "两场交册后缺考名单仅 s4")

    # ---------- 3. 补交空册 ----------
    # B 场补交 v2 空册：空册也算已交，B 最新版变为无人在场
    r = call("POST", "/rosters/batches", {
        "batch_id": "B-B2-empty", "session_id": SID,
        "rosters": [{"room_id": "B", "version": 2, "student_ids": []}]})
    check(r["rooms_accepted"][0]["present_count"] == 0,
          "空册被接收，present_count=0")
    # s1 仍在 A 最新版在册 -> 不缺考；s3 在 A 无记录、B 空册 -> 新缺考；
    # s4 只可去 B，B 空册 -> 维持缺考
    check(r["added_absent"] == ["s3"] and r["removed_absent"] == [],
          f"补交空册回执：净增 {r['added_absent']}（仅 s3），无净减")
    absent = call("GET", f"/sessions/{SID}/absences")["absent_students"]
    check(sorted(x["student_id"] for x in absent) == ["s3", "s4"],
          "空册生效后缺考名单为 s3、s4（s1/s2 仍在 A 在场）")
    rooms = call("GET", f"/sessions/{SID}/rooms")["rooms"]
    b_room = next(x for x in rooms if x["room_id"] == "B")
    check(b_room["submitted"] is True and b_room["version"] == 2
          and b_room["present_count"] == 0,
          "交册状态显示 B 场 v2 已交、在册 0 人（空册算已交）")

    # ---------- 4. 同批跨场更正 ----------
    # 更正前：A v1=[s1,s2]，B v2=空册；缺考 s3、s4
    # 一批同时交 A v2（去掉 s2）与 B v3（补登 s1、s3、s4）：
    # s2：只能去 A，A v2 已无 s2 -> 新缺考（净增）
    # s3：B v3 补登 -> 不再缺考（净减）
    # s4：B v3 补登 -> 不再缺考（净减）
    # s1：A v2 仍在册 -> 维持不缺考
    r = call("POST", "/rosters/batches", {
        "batch_id": "B-FIX", "session_id": SID,
        "rosters": [
            {"room_id": "A", "version": 2, "student_ids": ["s1"]},
            {"room_id": "B", "version": 3,
             "student_ids": ["s1", "s3", "s4"]},
        ]})
    check(len(r["rooms_accepted"]) == 2, "同批两场签到册一并接收")
    check(r["added_absent"] == ["s2"],
          f"跨场更正净增缺考: {r['added_absent']}（s2）")
    check(set(r["removed_absent"]) == {"s3", "s4"},
          f"跨场更正净减缺考: {r['removed_absent']}（s3、s4）")
    check(r["absence_after"] == ["s2"],
          f"回执 absence_after={r['absence_after']}")
    absent = call("GET", f"/sessions/{SID}/absences")["absent_students"]
    check([x["student_id"] for x in absent] == ["s2"],
          "更正后缺考名单仅 s2（名单与签到册同事务生效）")

    # ---------- 5. 拒收：版号未高于已交版号 ----------
    call("POST", "/rosters/batches", {
        "batch_id": "B-OLD", "session_id": SID,
        "rosters": [{"room_id": "A", "version": 2, "student_ids": ["s2"]}]},
        expect_error="version_not_higher")
    rooms = call("GET", f"/sessions/{SID}/rooms")["rooms"]
    a_room = next(x for x in rooms if x["room_id"] == "A")
    check(a_room["batch_id"] == "B-FIX", "被拒批次未改动任何已交数据")

    # ---------- 6. 拒收：列了不能去该场的学员 ----------
    call("POST", "/rosters/batches", {
        "batch_id": "B-BAD", "session_id": SID,
        "rosters": [{"room_id": "B", "version": 4,
                     "student_ids": ["s2"]}]},  # s2 只能去 A
        expect_error="student_not_allowed")
    rooms = call("GET", f"/sessions/{SID}/rooms")["rooms"]
    b_room = next(x for x in rooms if x["room_id"] == "B")
    check(b_room["version"] == 3, "非法学员导致整批拒收，B 场仍停留在 v3")

    # ---------- 7. 同批号：原样再交取回回执，换内容拒收 ----------
    original = call("GET", f"/rosters/batches/B-FIX")
    resent = call("POST", "/rosters/batches", {
        "batch_id": "B-FIX", "session_id": SID,
        "rosters": [
            {"room_id": "A", "version": 2, "student_ids": ["s1"]},
            {"room_id": "B", "version": 3,
             "student_ids": ["s1", "s3", "s4"]},
        ]})
    check(resent == original, "同批号原样再交，取回与原回执完全一致")
    call("POST", "/rosters/batches", {
        "batch_id": "B-FIX", "session_id": SID,
        "rosters": [{"room_id": "A", "version": 9, "student_ids": []}]},
        expect_error="batch_id_conflict")
    ok("同批号换内容被拒收")

    # ---------- 8. 改派 ----------
    SID2 = SID + "-reassign"
    call("POST", "/admin/sessions", {
        "session_id": SID2,
        "name": "改派测试期",
        "rooms": [{"room_id": "A"}, {"room_id": "B"}, {"room_id": "C"}],
        "students": [
            {"student_id": "u1", "room_ids": ["A", "B"]},
            {"student_id": "u2", "room_ids": ["A"]},
            {"student_id": "u3", "room_ids": ["B"]},
            {"student_id": "u4", "room_ids": ["A"]},
        ],
    })
    ok("改派测试期预登记成功")
    call("POST", "/rosters/batches", {
        "batch_id": "RA-1", "session_id": SID2,
        "rosters": [
            {"room_id": "A", "version": 1, "student_ids": ["u1", "u2"]},
            {"room_id": "B", "version": 1, "student_ids": ["u1", "u3"]},
        ]})
    ok("两场先行交册")

    # u1 由 A/B 改派为仅 B：原 A 场现行签到仍算到考
    r = call("PUT", f"/sessions/{SID2}/students/u1/rooms",
             {"room_ids": ["B"]})
    check(r["room_ids"] == ["B"] and r["absent_before"] is False
          and r["absent_after"] is False,
          "改派 u1 至 B，仍因原 A 场现行签到在册而不缺考")

    # 换册保留上一版已有签到：u1 已不能去 A，但上一版 A 册有 u1
    r = call("POST", "/rosters/batches", {
        "batch_id": "RA-2", "session_id": SID2,
        "rosters": [{"room_id": "A", "version": 2,
                     "student_ids": ["u1", "u2"]}]})
    check(r["status"] == "accepted",
          "换册保留上一版已有的 u1 签到（虽已改派离开 A）")
    r = call("POST", "/rosters/batches", {
        "batch_id": "RA-3", "session_id": SID2,
        "rosters": [{"room_id": "A", "version": 3,
                     "student_ids": ["u2"]}]})
    check(r["removed_absent"] == [] and r["added_absent"] == [],
          "A 场删去 u1 后，u1 仍在 B 场现行签到中，不算缺考")
    check("u1" not in [x["student_id"] for x in
                       call("GET", f"/sessions/{SID2}/absences")
                       ["absent_students"]],
          "u1 不在缺考名单")

    # u2 由 A 改派为 B：A 现行签到仍算到考
    r = call("PUT", f"/sessions/{SID2}/students/u2/rooms",
             {"room_ids": ["B"]})
    check(r["absent_after"] is False,
          "改派 u2 至 B，原 A 场现行签到仍算到考")
    r = call("POST", "/rosters/batches", {
        "batch_id": "RA-4", "session_id": SID2,
        "rosters": [{"room_id": "A", "version": 4, "student_ids": []}]})
    check(r["added_absent"] == ["u2"],
          "A 场空册删去 u2 后列为缺考（交册重核到原场在册的改派学员）")

    # 删去后未恢复资格就补入 -> 整批拒收
    call("POST", "/rosters/batches", {
        "batch_id": "RA-BAD", "session_id": SID2,
        "rosters": [{"room_id": "A", "version": 5,
                     "student_ids": ["u2"]}]},
        expect_error="student_not_allowed")
    rooms = call("GET", f"/sessions/{SID2}/rooms")["rooms"]
    a_room = next(x for x in rooms if x["room_id"] == "A")
    check(a_room["version"] == 4 and a_room["present_count"] == 0,
          "未恢复 A 场资格补入 u2 整批拒收，A 场仍停留 v4 空册")
    check("u2" in [x["student_id"] for x in
                   call("GET", f"/sessions/{SID2}/absences")
                   ["absent_students"]],
          "被拒批次未写入，u2 仍为缺考")
    call("GET", "/rosters/batches/RA-BAD", expect_error="receipt_not_found")
    ok("被拒批次未落回执")

    # 同批多场：一场合法、一场非法补入 -> 整批原子拒收
    call("POST", "/rosters/batches", {
        "batch_id": "RA-BAD-MULTI", "session_id": SID2,
        "rosters": [
            {"room_id": "B", "version": 3, "student_ids": ["u3"]},
            {"room_id": "A", "version": 6, "student_ids": ["u3"]},
        ]}, expect_error="student_not_allowed")
    rooms = call("GET", f"/sessions/{SID2}/rooms")["rooms"]
    b_room = next(x for x in rooms if x["room_id"] == "B")
    check(b_room["version"] == 1,
          "同批另一场合法签到也未生效（整批不写入），B 仍停留 v1")

    # 改派到尚未交册的 C：暂不列缺考；C 交册后才列为缺考
    r = call("PUT", f"/sessions/{SID2}/students/u4/rooms",
             {"room_ids": ["C"]})
    check(r["absent_before"] is True and r["absent_after"] is False,
          "u4 改派到未交册的 C：暂不列缺考")
    r = call("POST", "/rosters/batches", {
        "batch_id": "RA-C1", "session_id": SID2,
        "rosters": [{"room_id": "C", "version": 1, "student_ids": []}]})
    check(r["added_absent"] == ["u4"],
          "C 场交空册后 u4 列为缺考（重核回到可去本场的学员）")

    # 恢复资格后补入 -> 接收，缺考净减
    r = call("PUT", f"/sessions/{SID2}/students/u2/rooms",
             {"room_ids": ["A"]})
    check(r["absent_before"] is True and r["absent_after"] is True,
          "恢复 u2 的 A 场资格：A 场已交空册，仍暂列缺考")
    r = call("POST", "/rosters/batches", {
        "batch_id": "RA-5", "session_id": SID2,
        "rosters": [{"room_id": "A", "version": 5,
                     "student_ids": ["u2"]}]})
    check(r["removed_absent"] == ["u2"],
          "恢复资格后补入 u2 被接收，缺考净减")

    # 改派校验失败保留原状态
    call("PUT", f"/sessions/{SID2}/students/u3/rooms",
         {"room_ids": ["A", "Z"]}, expect_error="unknown_room")
    call("PUT", f"/sessions/{SID2}/students/nobody/rooms",
         {"room_ids": ["A"]}, expect_error="student_not_found")
    call("PUT", f"/sessions/{SID2}/students/u3/rooms",
         {"room_ids": []}, expect_error="validation_error")
    call("POST", "/rosters/batches", {
        "batch_id": "RA-BAD2", "session_id": SID2,
        "rosters": [{"room_id": "A", "version": 6,
                     "student_ids": ["u3"]}]},
        expect_error="student_not_allowed")
    ok("改派失败（未知考场/未知学员/空列表）保留 u3 原有 B 场资格")

    # ---------- 9. 旧回执可查、原样重交 ----------
    original = call("GET", "/rosters/batches/B-FIX")
    resent = call("POST", "/rosters/batches", {
        "batch_id": "B-FIX", "session_id": SID,
        "rosters": [
            {"room_id": "A", "version": 2, "student_ids": ["s1"]},
            {"room_id": "B", "version": 3,
             "student_ids": ["s1", "s3", "s4"]},
        ]})
    check(resent == original,
          "旧批号回执继续可查，原样重交返回当时的回执")
    check(call("GET", "/rosters/batches/B-FIX")["status"] == "accepted",
          "改派后历史交册回执仍原样可查")

    # ---------- 10. 改派与交册并行 ----------
    def reassign_u3():
        return call("PUT", f"/sessions/{SID2}/students/u3/rooms",
                    {"room_ids": ["A"]})

    def submit_b_empty():
        return call("POST", "/rosters/batches", {
            "batch_id": "RA-6", "session_id": SID2,
            "rosters": [{"room_id": "B", "version": 2,
                         "student_ids": []}]})

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(reassign_u3)
        f2 = pool.submit(submit_b_empty)
        r_reassign, r_submit = f1.result(), f2.result()
    check(r_reassign["room_ids"] == ["A"]
          and r_submit["status"] == "accepted",
          "改派与交册并行均被接收（按各自生效先后串行处理）")
    absent = [x["student_id"] for x in
              call("GET", f"/sessions/{SID2}/absences")["absent_students"]]
    check("u3" in absent,
          "并行后 u3 已离开 B（B 空册）且 A 最新版无他，列为缺考")

    print(f"\n全部通过：{passed} 项断言，失败：{failed} 项")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\n断言失败: {e}", file=sys.stderr)
        sys.exit(1)
