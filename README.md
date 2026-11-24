# 补考缺考核定 API

培训组用于核定补考缺考名单的 FastAPI 服务：按期在 SQLite 中预先登记学员
及其可去的一个或多个考场，各考场交签到册后，系统自动核定缺考名单并出回执。

## 业务规则

1. **预登记**：每期登记考场、学员以及每名学员可去的一个或多个考场。
2. **改派**：按期号和学员号重设某名学员当前可去的一个或多个考场（至少一场）。
   改派只重核该学员本人；移出原场后，该学员在原场**现行**签到册中的在册记录
   仍然算到考。
3. **按批交册**：同一期的多个考场可在一批里一并交册，每场交一份完整签到册；
   空册（`student_ids` 为空）也算已交。该场换册时允许保留上一版已有的签到
   （即使该学员已改派离开本场）；但某学员从上一版删去后，未重新恢复该场
   资格就再次补入，则整批拒收。
4. **缺考判定**：学员**现行可去**的考场**全部已交册**，且**本期任何一场现行
   签到册都没有他**，才列为缺考；只要任一可去考场还没交册，就暂不列缺考。
5. **影响面**：每批只重核“可去本批任一考场”或“已在本批考场上一版签到册
   在册”的学员；即使交册并行，回执仍按整批生效前后列出缺考名单的
   **净增（added_absent）和净减（removed_absent）**。改派只重核该学员一人。
6. **原子生效**：签到册、可去考场变更、物化缺考名单、回执在同一个数据库
   事务内提交；改派与交册并行时按各自实际生效的先后串行处理，失败回滚、
   保留原状态。
7. **整批拒收（任一条件触发，全批不写入）**：
   - 任一册版号未高于该场已交版号；
   - 册内列了不能去该场且未在上一版签到册中的学员；
   - 同批号曾用于不同内容（`batch_id_conflict`）。
8. **幂等与回执**：同一批号、原样内容再交，直接取回原回执，不产生新版本；
   历史回执始终可按批号原样查回。

## 启动（Docker Compose）

```bash
docker compose up --build
# 服务与交互文档：
#   API 文档（可直接在页面上交册、查名单）: http://localhost:8000/docs
#   OpenAPI:                              http://localhost:8000/openapi.json
#   健康检查:                             http://localhost:8000/health
```

SQLite 数据文件位于容器内 `/data/absence.db`，通过命名卷 `absence-data` 持久化。
可用环境变量 `ABSENCE_DB_PATH` 覆盖数据库路径。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admin/sessions` | 按期预登记考场与学员（含可去考场） |
| PUT  | `/sessions/{session_id}/students/{student_id}/rooms` | 按期号、学员号重设可去考场（改派） |
| POST | `/rosters/batches` | 一批一场或多场交册，返回收据（含净增/净减） |
| GET  | `/rosters/batches/{batch_id}` | 按批号取回原回执 |
| GET  | `/sessions/{session_id}/absences` | 查本期缺考名单 |
| GET  | `/sessions/{session_id}/rooms` | 查各场交册状态（版号、在册人数） |

### 示例：预登记

```bash
curl -X POST http://localhost:8000/admin/sessions -H 'Content-Type: application/json' -d '{
  "session_id": "2026Q4-makeup",
  "rooms": [{"room_id": "A"}, {"room_id": "B"}],
  "students": [
    {"student_id": "s1", "room_ids": ["A", "B"]},
    {"student_id": "s2", "room_ids": ["A"]}
  ]
}'
```

### 示例：改派

```bash
# 把 s2 的可去考场改为 B（至少一场；改派只重核 s2 的缺考状态）
curl -X PUT http://localhost:8000/sessions/2026Q4-makeup/students/s2/rooms \
  -H 'Content-Type: application/json' -d '{"room_ids": ["B"]}'
```

回执字段：

```json
{
  "session_id": "2026Q4-makeup",
  "student_id": "s2",
  "room_ids": ["B"],
  "absent_before": false,
  "absent_after": false
}
```

### 示例：一批交两场签到册（含空册）

```bash
curl -X POST http://localhost:8000/rosters/batches -H 'Content-Type: application/json' -d '{
  "batch_id": "B-001",
  "session_id": "2026Q4-makeup",
  "rosters": [
    {"room_id": "A", "version": 1, "student_ids": ["s1", "s2"]},
    {"room_id": "B", "version": 1, "student_ids": []}
  ]
}'
```

回执字段：

```json
{
  "batch_id": "B-001",
  "status": "accepted",
  "rooms_accepted": [{"room_id": "A", "version": 1, "present_count": 2}],
  "absence_before": [],
  "absence_after": ["s1"],
  "added_absent": ["s1"],
  "removed_absent": []
}
```

> `absence_before/absence_after` 只统计本批影响面（可去本批考场或在本批考场
> 上一版签到册在册的学员）。全量缺考名单以 `GET /sessions/{id}/absences` 为准。

### 错误响应

```json
{"error": "version_not_higher", "message": "考场 A 版号 1 未高于已交版号 1，整批拒收"}
```

错误码：`session_exists`、`session_not_found`、`student_not_found`、
`unknown_room`、`no_allowed_room`、`duplicate_allowed_room`、
`version_not_higher`、`student_not_allowed`、`duplicate_room_in_batch`、
`duplicate_student_in_roster`、`batch_id_conflict`、`receipt_not_found`、
`validation_error`。

## 本地非 Docker 运行与实测

```bash
pip install -r requirements.txt
ABSENCE_DB_PATH=/tmp/absence.db uvicorn app.main:app --host 127.0.0.1 --port 8000
python3 -I scripts/e2e_demo.py        # 端到端实测（含空册、跨场更正、改派、拒收、幂等）
```

## 目录结构

```
app/
  database.py   SQLite 连接、表结构、串行化写事务
  schemas.py    Pydantic 请求/响应模型（驱动 /docs）
  service.py    预登记、改派、交册核定、缺考计算、查询
  main.py       FastAPI 路由与异常处理
scripts/
  e2e_demo.py   端到端实测脚本（仅标准库）
Dockerfile
docker-compose.yml
```
