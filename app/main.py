"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Lifespan: Idempotent Index Initialization
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create all required indexes idempotently at startup
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_employees_emp_code_unique")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_employees_dept_joined")
        db.employees.create_index([("joined_on", ASCENDING)], name="idx_employees_joined_on")

        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="idx_attendance_emp_date_unique")
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_attendance_date_desc_emp_asc")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_out", ASCENDING), ("punch_in", DESCENDING)], name="idx_attendance_punch_lookup")
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)], name="idx_attendance_date_late")
        db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)], name="idx_attendance_date_status")
    except Exception:
        # If DB is not reachable at startup, continue so health check reports status
        pass
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Enums and Schemas
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$", json_schema_extra={"example": "EMP0001"})
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: List[Employee]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Any]]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: Status
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    work_hours: Optional[float] = None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntry] = Field(default_factory=list)


class AttendancePage(BaseModel):
    items: List[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class Trend(BaseModel):
    department: str
    items: List[TrendItem]


# --------------------------------------------------------------------------- #
# Domain Helpers (Rules R1 - R8)
# --------------------------------------------------------------------------- #
def truncate_to_seconds_ms(epoch_ms: int) -> int:
    """R1: Instants are truncated to whole seconds before any calculation."""
    return (epoch_ms // 1000) * 1000


def ms_to_utc_dt(epoch_ms: int) -> datetime:
    """Converts epoch milliseconds to timezone-aware UTC datetime."""
    truncated = truncate_to_seconds_ms(epoch_ms)
    return datetime.fromtimestamp(truncated / 1000.0, tz=timezone.utc)


def normalize_utc_dt(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalizes any datetime from MongoDB or input to timezone-aware UTC datetime."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a BSON or Python datetime to epoch milliseconds."""
    if dt is None:
        return None
    norm = normalize_utc_dt(dt)
    return int(norm.timestamp() * 1000)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Overnight shifts (shift_end <= shift_start): a punch-in earlier than shift_end
    belongs to the shift that started the previous day.
    """
    punch_in_utc = normalize_utc_dt(punch_in_utc)
    punch_in_ist = punch_in_utc.astimezone(IST)
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))

    is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)
    if is_overnight:
        if (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second) < (eh_h, eh_m, 0):
            prev_day = punch_in_ist.date() - timedelta(days=1)
            return prev_day.isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch_in is strictly more than 10:00
    after shift_start; then late_minutes = whole minutes (floor) since shift_start,
    not since the grace period ended.
    """
    punch_in_utc = normalize_utc_dt(punch_in_utc)
    punch_in_ist = punch_in_utc.astimezone(IST)
    d = date.fromisoformat(attendance_date_str)
    sh_h, sh_m = map(int, shift_start.split(":"))
    start_dt = datetime(d.year, d.month, d.day, sh_h, sh_m, 0, tzinfo=IST)

    diff_sec = (punch_in_ist - start_dt).total_seconds()
    if diff_sec > 600:  # strictly more than 10:00 (600 seconds)
        return int(diff_sec // 60)
    return 0


def compute_overtime(punch_out_utc: datetime, shift_end: str, attendance_date_str: str, shift_start: str) -> int:
    """
    R3: Overtime. Whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    punch_out_utc = normalize_utc_dt(punch_out_utc)
    punch_out_ist = punch_out_utc.astimezone(IST)
    d = date.fromisoformat(attendance_date_str)
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))

    if (eh_h, eh_m) <= (sh_h, sh_m):
        end_date = d + timedelta(days=1)
    else:
        end_date = d

    end_dt = datetime(end_date.year, end_date.month, end_date.day, eh_h, eh_m, 0, tzinfo=IST)
    diff_sec = (punch_out_ist - end_dt).total_seconds()
    diff_min = int(diff_sec // 60)
    return diff_min if diff_min >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    punch_in_utc = normalize_utc_dt(punch_in_utc)
    punch_out_utc = normalize_utc_dt(punch_out_utc)
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    hours_dec = Decimal(str(diff_sec)) / Decimal("3600")
    rounded = hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: After punch-out, half_day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def round_half_up_2(val: Optional[Union[float, Decimal]]) -> Optional[float]:
    """R8: 2 decimals half-up for counts/averages/percentages."""
    if val is None:
        return None
    return float(Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_half_up_4(val: Optional[Union[float, Decimal]]) -> Optional[float]:
    """R8: 4 decimals half-up for rates."""
    if val is None:
        return None
    return float(Decimal(str(val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def count_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7: Mon-Fri working days in calendar month, starting from joined_on for mid-month joiners.
    """
    y, m = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(y, m)
    month_start = date(y, m, 1)
    month_end = date(y, m, last_day)

    joined_date = date.fromisoformat(joined_on_str)
    start_date = max(month_start, joined_date)
    if start_date > month_end:
        return 0

    count = 0
    curr = start_date
    while curr <= month_end:
        if curr.weekday() < 5:  # Mon-Fri
            count += 1
        curr += timedelta(days=1)
    return count


def doc_to_attendance_record(doc: Dict[str, Any]) -> AttendanceRecord:
    """Transforms a MongoDB attendance document into the OpenAPI AttendanceRecord response schema."""
    history_entries = []
    for h in doc.get("history", []):
        raw_changes = h.get("changes", {})
        formatted_changes = {}
        for k, v in raw_changes.items():
            from_v = v.get("from")
            to_v = v.get("to")
            if isinstance(from_v, datetime):
                from_v = dt_to_epoch_ms(from_v)
            if isinstance(to_v, datetime):
                to_v = dt_to_epoch_ms(to_v)
            formatted_changes[k] = {"from": from_v, "to": to_v}

        history_entries.append(
            HistoryEntry(
                at=dt_to_epoch_ms(h.get("at")),
                by=h.get("by", ""),
                reason=h.get("reason", ""),
                changes=formatted_changes,
            )
        )

    return AttendanceRecord(
        emp_code=doc["emp_code"],
        date=doc["date"],
        status=Status(doc["status"]),
        punch_in=dt_to_epoch_ms(doc.get("punch_in")),
        punch_out=dt_to_epoch_ms(doc.get("punch_out")),
        work_hours=doc.get("work_hours"),
        late_minutes=doc.get("late_minutes", 0),
        overtime_minutes=doc.get("overtime_minutes", 0),
        half_day=bool(doc.get("half_day", False)),
        history=history_entries,
    )


def doc_to_employee(doc: Dict[str, Any]) -> Employee:
    """Transforms a MongoDB employee document into the OpenAPI Employee schema."""
    return Employee(
        emp_code=doc["emp_code"],
        name=doc["name"],
        email=doc["email"],
        department=doc["department"],
        shift_start=doc["shift_start"],
        shift_end=doc["shift_end"],
        joined_on=doc["joined_on"],
        created_at=dt_to_epoch_ms(doc.get("created_at")),
    )


# --------------------------------------------------------------------------- #
# Endpoints: System & Employees
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"], summary="Readiness probe")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, response_model=Employee, tags=["employees"])
def create_employee(body: EmployeeCreate):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(status_code=409, detail="emp_code already exists")

    created_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = created_at_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return doc_to_employee(doc)


@app.get("/employees", response_model=EmployeePage, tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match."),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)

    items = [doc_to_employee(doc) for doc in cursor]
    return EmployeePage(items=items, total=total, page=page, page_size=page_size)


# --------------------------------------------------------------------------- #
# Endpoints: Attendance Operations
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        p_ms = truncate_to_seconds_ms(body.punched_at)
    else:
        now_dt = datetime.now(timezone.utc)
        p_ms = truncate_to_seconds_ms(int(now_dt.timestamp() * 1000))

    punch_in_dt = ms_to_utc_dt(p_ms)
    attendance_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_dt, emp["shift_start"], attendance_date)

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": attendance_date}):
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return doc_to_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200, response_model=AttendanceRecord, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        p_ms = truncate_to_seconds_ms(body.punched_at)
    else:
        now_dt = datetime.now(timezone.utc)
        p_ms = truncate_to_seconds_ms(int(now_dt.timestamp() * 1000))

    punch_out_dt = ms_to_utc_dt(p_ms)

    # Find the most recent record whose punch_in is at or before punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found for this employee")

    # If already punched out
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_dt = normalize_utc_dt(record["punch_in"])
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")

    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at must be within 24 hours of punch_in")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_minutes = compute_overtime(punch_out_dt, emp["shift_end"], record["date"], emp["shift_start"])
    half_day = compute_half_day(work_hours)

    # Race-safe atomic update
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Record was punched out concurrently")

    return doc_to_attendance_record(updated_doc)


@app.get("/attendance", response_model=AttendancePage, tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Status] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status.value

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)

    items = [doc_to_attendance_record(doc) for doc in cursor]
    return AttendancePage(items=items, total=total, page=page, page_size=page_size)


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord, tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    existing = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not existing:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    target_status = body.status.value if body.status is not None else existing["status"]

    if target_status in (Status.ABSENT.value, Status.LEAVE.value):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Setting status ABSENT or LEAVE clears both punch times; cannot supply punch_in or punch_out",
            )
        new_punch_in_dt = None
        new_punch_out_dt = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in_ms = truncate_to_seconds_ms(body.punch_in)
            new_punch_in_dt = ms_to_utc_dt(new_punch_in_ms)
        else:
            new_punch_in_dt = normalize_utc_dt(existing.get("punch_in"))

        if new_punch_in_dt is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires a punch_in",
            )

        # Validate that punch_in stays on the record's attendance date under R1
        calc_date = get_attendance_date(new_punch_in_dt, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the record's attendance date",
            )

        if body.punch_out is not None:
            new_punch_out_ms = truncate_to_seconds_ms(body.punch_out)
            new_punch_out_dt = ms_to_utc_dt(new_punch_out_ms)
        else:
            new_punch_out_dt = normalize_utc_dt(existing.get("punch_out"))

        if new_punch_out_dt is not None:
            if new_punch_out_dt <= new_punch_in_dt:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be strictly after punch_in",
                )
            if (new_punch_out_dt - new_punch_in_dt).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be within 24 hours of punch_in",
                )

        # Recompute derived fields
        new_late_minutes = compute_late_minutes(new_punch_in_dt, emp["shift_start"], date)
        if new_punch_out_dt is not None:
            new_work_hours = compute_work_hours(new_punch_in_dt, new_punch_out_dt)
            new_overtime_minutes = compute_overtime(new_punch_out_dt, emp["shift_end"], date, emp["shift_start"])
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Check which fields actually changed
    changes_db: Dict[str, Dict[str, Any]] = {}

    old_status = existing.get("status")
    if old_status != target_status:
        changes_db["status"] = {"from": old_status, "to": target_status}

    old_punch_in_dt = normalize_utc_dt(existing.get("punch_in"))
    if old_punch_in_dt != new_punch_in_dt:
        changes_db["punch_in"] = {"from": old_punch_in_dt, "to": new_punch_in_dt}

    old_punch_out_dt = normalize_utc_dt(existing.get("punch_out"))
    if old_punch_out_dt != new_punch_out_dt:
        changes_db["punch_out"] = {"from": old_punch_out_dt, "to": new_punch_out_dt}

    old_work_hours = existing.get("work_hours")
    if old_work_hours != new_work_hours:
        changes_db["work_hours"] = {"from": old_work_hours, "to": new_work_hours}

    old_late_minutes = existing.get("late_minutes", 0)
    if old_late_minutes != new_late_minutes:
        changes_db["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}

    old_overtime_minutes = existing.get("overtime_minutes", 0)
    if old_overtime_minutes != new_overtime_minutes:
        changes_db["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}

    old_half_day = bool(existing.get("half_day", False))
    if old_half_day != new_half_day:
        changes_db["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes_db:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    # Safe atomic update: check that history length hasn't changed to prevent losing entries
    existing_history = existing.get("history", [])
    history_len = len(existing_history)

    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": existing["_id"],
            f"history.{history_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in_dt,
                "punch_out": new_punch_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Concurrent edit conflict on attendance record")

    return doc_to_attendance_record(updated_doc)


# --------------------------------------------------------------------------- #
# Endpoints: MongoDB Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly, tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = count_working_days(month, emp["joined_on"])

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$regex": f"^{month}-"},
            }
        },
        {
            "$addFields": {
                "day_of_week": {
                    "$isoDayOfWeek": {
                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": ["$day_of_week", 5]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg_result = list(db.attendance_logs.aggregate(pipeline))
    if agg_result:
        res = agg_result[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late_minutes = int(res.get("total_late_minutes", 0))
        total_overtime_minutes = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        pct_raw = (present_days / working_days) * 100.0
        attendance_pct = round_half_up_2(pct_raw)
    else:
        attendance_pct = None

    return EmployeeMonthly(
        emp_code=emp_code,
        month=month,
        working_days=working_days,
        present_days=round_half_up_2(present_days) or 0.0,
        leave_days=leave_days,
        late_count=late_count,
        total_late_minutes=total_late_minutes,
        total_overtime_minutes=total_overtime_minutes,
        attendance_pct=attendance_pct,
    )


@app.get("/analytics/departments/summary", response_model=DepartmentSummary, tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    y, m = map(int, month.split("-"))
    _, last_day_num = calendar.monthrange(y, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day_num:02d}"

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"dept_codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$dept_codes"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "day_of_week": {
                                "$isoDayOfWeek": {
                                    "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                                }
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$lte": ["$$l.day_of_week", 5]},
                                        ]
                                    },
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "work_hours_list": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$logs.late_minutes",
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {"$avg": "$work_hours_list.work_hours"},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]

    raw_items = list(db.employees.aggregate(pipeline))
    items = []
    for raw in raw_items:
        avg_wh = raw.get("avg_work_hours")
        items.append(
            DepartmentSummaryItem(
                department=raw["department"],
                headcount=raw["headcount"],
                present_days=round_half_up_2(raw.get("present_days", 0.0)) or 0.0,
                avg_work_hours=round_half_up_2(avg_wh) if avg_wh is not None else None,
                late_count=raw.get("late_count", 0),
                total_late_minutes=raw.get("total_late_minutes", 0),
                leave_count=raw.get("leave_count", 0),
                on_duty_count=raw.get("on_duty_count", 0),
            )
        )

    return DepartmentSummary(month=month, items=items)


@app.get("/analytics/leaderboard/late", response_model=Leaderboard, tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    y, m = map(int, month.split("-"))
    _, last_day_num = calendar.monthrange(y, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day_num:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    # Window rank stage for competition ranking (1, 2, 2, 4)
    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": DESCENDING,
                "_id": ASCENDING,
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    raw_items = list(db.attendance_logs.aggregate(pipeline))
    items = [LeaderboardItem(**r) for r in raw_items]
    return Leaderboard(month=month, items=items)


@app.get("/analytics/departments/{department}/trend", response_model=Trend, tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date_str: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date_str: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    # Verify department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(from_date_str)
    d_to = date.fromisoformat(to_date_str)

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")

    diff_days = (d_to - d_from).days + 1
    if diff_days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # Get department employee codes
    dept_employees = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    emp_codes = [e["emp_code"] for e in dept_employees]

    # Pre-aggregate department attendance records by date inside MongoDB
    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date_str, "$lte": to_date_str},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]

    day_logs = {doc["_id"]: doc for doc in db.attendance_logs.aggregate(pipeline)}

    items: List[TrendItem] = []
    # Build exact daily series for all days in range [from, to]
    curr = d_from
    while curr <= d_to:
        d_str = curr.isoformat()
        is_working = curr.weekday() < 5  # Mon-Fri

        # Headcount: department employees with joined_on <= that day
        headcount = sum(1 for e in dept_employees if e.get("joined_on", "9999-99-99") <= d_str)

        log = day_logs.get(d_str)
        if log:
            present_cnt = float(log.get("present_count", 0.0))
            late_cnt = int(log.get("late_count", 0))
        else:
            present_cnt = 0.0
            late_cnt = 0

        if is_working and headcount > 0:
            rate_raw = present_cnt / headcount
            att_rate = round_half_up_4(rate_raw)
        else:
            att_rate = None

        items.append(
            TrendItem(
                date=d_str,
                is_working_day=is_working,
                headcount=headcount,
                present_count=round_half_up_2(present_cnt) or 0.0,
                late_count=late_cnt,
                attendance_rate=att_rate,
                moving_avg_7d=None,  # will compute over preceding window
            )
        )
        curr += timedelta(days=1)

    # 7-day moving average calculation:
    # Mean of non-null attendance_rate among current row and up to 6 preceding rows inside the requested range
    for i in range(len(items)):
        start_idx = max(0, i - 6)
        window = [items[j].attendance_rate for j in range(start_idx, i + 1) if items[j].attendance_rate is not None]
        if window:
            avg_val = sum(window) / len(window)
            items[i].moving_avg_7d = round_half_up_4(avg_val)
        else:
            items[i].moving_avg_7d = None

    return Trend(department=department, items=items)


# --------------------------------------------------------------------------- #
# Endpoint: Admin Explain
# --------------------------------------------------------------------------- #
class ExplainOutput(BaseModel):
    endpoint: str
    collection: str
    explain: Dict[str, Any]


def clean_bson_types(val: Any) -> Any:
    """Recursively converts BSON datetimes and non-serializable objects to JSON primitives."""
    if isinstance(val, dict):
        return {k: clean_bson_types(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [clean_bson_types(v) for v in val]
    elif isinstance(val, datetime):
        return dt_to_epoch_ms(val)
    elif isinstance(val, Decimal):
        return float(val)
    return val


@app.get("/admin/explain/{endpoint}", response_model=ExplainOutput, tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Status] = Query(None),
    from_date_param: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date_param: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: Optional[int] = Query(1, ge=1),
    page_size: Optional[int] = Query(20, ge=1, le=100),
):
    try:
        if endpoint == "attendance_list":
            if date_from and date_to and date_from > date_to:
                raise HTTPException(status_code=422, detail="date_from > date_to")
            q: Dict[str, Any] = {}
            if emp_code:
                q["emp_code"] = emp_code
            if date_from or date_to:
                q["date"] = {}
                if date_from:
                    q["date"]["$gte"] = date_from
                if date_to:
                    q["date"]["$lte"] = date_to
            if status:
                q["status"] = status.value

            skip_val = ((page or 1) - 1) * (page_size or 20)
            limit_val = page_size or 20
            find_cmd = {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip_val,
                "limit": limit_val,
            }
            explain_res = db.command("explain", find_cmd, verbosity="executionStats")
            return ExplainOutput(endpoint=endpoint, collection="attendance_logs", explain=clean_bson_types(explain_res))

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(status_code=422, detail="emp_code and month are required")
            pipeline = [
                {
                    "$match": {
                        "emp_code": emp_code,
                        "date": {"$regex": f"^{month}-"},
                    }
                },
                {
                    "$addFields": {
                        "day_of_week": {
                            "$isoDayOfWeek": {
                                "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                            }
                        }
                    }
                },
                {
                    "$group": {
                        "_id": None,
                        "present_days": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$lte": ["$day_of_week", 5]},
                                        ]
                                    },
                                    {"$cond": ["$half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            }
                        },
                        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                        "total_late_minutes": {"$sum": "$late_minutes"},
                        "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                    }
                },
            ]
            explain_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
            return ExplainOutput(endpoint=endpoint, collection="attendance_logs", explain=clean_bson_types(explain_res))

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(status_code=422, detail="month is required")
            y, m = map(int, month.split("-"))
            _, last_day_num = calendar.monthrange(y, m)
            month_start = f"{month}-01"
            month_end = f"{month}-{last_day_num:02d}"

            emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
            if department:
                emp_match["department"] = department

            pipeline = [
                {"$match": emp_match},
                {
                    "$group": {
                        "_id": "$department",
                        "headcount": {"$sum": 1},
                        "emp_codes": {"$push": "$emp_code"},
                    }
                },
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "let": {"dept_codes": "$emp_codes"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {"$in": ["$emp_code", "$$dept_codes"]},
                                            {"$gte": ["$date", month_start]},
                                            {"$lte": ["$date", month_end]},
                                        ]
                                    }
                                }
                            }
                        ],
                        "as": "logs",
                    }
                },
                {"$sort": {"_id": 1}},
            ]
            explain_res = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
            return ExplainOutput(endpoint=endpoint, collection="employees", explain=clean_bson_types(explain_res))

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(status_code=422, detail="month is required")
            y, m = map(int, month.split("-"))
            _, last_day_num = calendar.monthrange(y, m)
            month_start = f"{month}-01"
            month_end = f"{month}-{last_day_num:02d}"

            pipeline = [
                {
                    "$match": {
                        "date": {"$gte": month_start, "$lte": month_end},
                        "late_minutes": {"$gt": 0},
                    }
                },
                {
                    "$group": {
                        "_id": "$emp_code",
                        "total_late_minutes": {"$sum": "$late_minutes"},
                        "late_count": {"$sum": 1},
                    }
                },
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "_id",
                        "foreignField": "emp_code",
                        "as": "emp",
                    }
                },
                {"$unwind": "$emp"},
            ]
            if department is not None:
                pipeline.append({"$match": {"emp.department": department}})

            pipeline.extend([
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {"$match": {"rank": {"$lte": limit or 10}}},
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
            ])
            explain_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
            return ExplainOutput(endpoint=endpoint, collection="attendance_logs", explain=clean_bson_types(explain_res))

        elif endpoint == "department_trend":
            if not department or not from_date_param or not to_date_param:
                raise HTTPException(status_code=422, detail="department, from, and to are required")
            if to_date_param < from_date_param:
                raise HTTPException(status_code=422, detail="to cannot be earlier than from")

            # Look up employee codes for the department
            dept_employees = list(db.employees.find({"department": department}, {"emp_code": 1}))
            emp_codes = [e["emp_code"] for e in dept_employees]

            pipeline = [
                {
                    "$match": {
                        "emp_code": {"$in": emp_codes},
                        "date": {"$gte": from_date_param, "$lte": to_date_param},
                    }
                },
                {
                    "$group": {
                        "_id": "$date",
                        "present_count": {"$sum": 1},
                        "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    }
                },
                {"$sort": {"_id": 1}},
            ]
            explain_res = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
            return ExplainOutput(endpoint=endpoint, collection="attendance_logs", explain=clean_bson_types(explain_res))

        else:
            raise HTTPException(status_code=422, detail="Unknown endpoint")

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
