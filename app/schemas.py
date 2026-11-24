"""请求/响应模型（同时驱动 /docs 的交互文档）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class RoomIn(BaseModel):
    room_id: str = Field(..., description="考场 ID，同一期内唯一")
    name: str | None = Field(None, description="考场名称")


class StudentIn(BaseModel):
    student_id: str = Field(..., description="学员 ID，同一期内唯一")
    name: str | None = Field(None, description="学员姓名")
    room_ids: list[str] = Field(
        ..., min_length=1, description="该学员可去的一个或多个考场"
    )


class RegisterRequest(BaseModel):
    session_id: str = Field(..., description="期 ID")
    name: str | None = Field(None, description="期名称")
    rooms: list[RoomIn] = Field(..., min_length=1, description="本期全部考场")
    students: list[StudentIn] = Field(..., min_length=1, description="本期学员及可去考场")


class RosterIn(BaseModel):
    room_id: str = Field(..., description="考场 ID")
    version: int = Field(..., gt=0, description="签到册版号，必须高于该场已交版号")
    student_ids: list[str] = Field(
        default_factory=list,
        description="签到在册学员 ID 列表；空列表即空册，空册也算已交",
    )


class SubmitRostersRequest(BaseModel):
    batch_id: str = Field(..., description="交册批号，原样重交可取回原回执")
    session_id: str = Field(..., description="期 ID")
    rosters: list[RosterIn] = Field(
        ..., min_length=1, description="本批一并交册的一个或多个考场签到册"
    )


class AcceptedRoom(BaseModel):
    room_id: str
    version: int
    present_count: int


class Receipt(BaseModel):
    batch_id: str
    session_id: str
    status: str
    rooms_accepted: list[AcceptedRoom]
    absence_before: list[str] = Field(..., description="本批生效前，受影响学员中的缺考者")
    absence_after: list[str] = Field(..., description="本批生效后，受影响学员中的缺考者")
    added_absent: list[str] = Field(..., description="缺考名单净增")
    removed_absent: list[str] = Field(..., description="缺考名单净减")


class AbsentStudent(BaseModel):
    student_id: str
    name: str


class AbsentListResponse(BaseModel):
    session_id: str
    absent_students: list[AbsentStudent]


class RoomStatus(BaseModel):
    room_id: str
    name: str
    submitted: bool
    version: int | None
    batch_id: str | None
    present_count: int


class RoomStatusResponse(BaseModel):
    session_id: str
    rooms: list[RoomStatus]
