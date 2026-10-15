"""
Employee Attendance & Analytics API
FastAPI + MongoDB backend conforming strictly to openapi.yaml, DATA_MODEL.md, and PROBLEM_STATEMENT.docx.
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# MongoDB connection configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

# Indian Standard Time (UTC+05:30) per rule R1
IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Indexes Setup (Idempotent Startup)
# --------------------------------------------------------------------------- #
def init_indexes():
    """Create all required indexes idempotently at application startup."""
    # 1. Employees indexes
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    # 2. Attendance logs indexes
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)]
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)]
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run at startup
    init_indexes()
    yield
    # Run at shutdown
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helper Functions (Business Rules R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(value: Optional[float], decimals: int) -> Optional[float]:
    """Round a float to given decimals using ROUND_HALF_UP (R4, R8)."""
    if value is None:
        return None
    d = Decimal(str(value))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: Before any calculation, instants are truncated to whole seconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.replace(microsecond=0)


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a UTC BSON datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: Optional[int]) -> Optional[datetime]:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds."""
    if ms is None:
        return None
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def get_attendance_date(
    punch_in_utc: datetime, shift_start: str, shift_end: str
) -> str:
    """
    R1: Attendance date is the IST calendar date of punch_in.
    Overnight shift (shift_end <= shift_start): punch-in earlier than shift_end
    belongs to the shift that started the previous day.
    """
    p_in_ist = truncate_to_seconds(punch_in_utc).astimezone(IST)
    sh_start, sm_start = map(int, shift_start.split(":"))
    sh_end, sm_end = map(int, shift_end.split(":"))

    # Overnight shift check
    if (sh_end, sm_end) <= (sh_start, sm_start):
        if (p_in_ist.hour, p_in_ist.minute, p_in_ist.second) < (sh_end, sm_end, 0):
            # Belongs to previous day's shift
            return (p_in_ist.date() - timedelta(days=1)).isoformat()

    return p_in_ist.date().isoformat()


def compute_late_minutes(
    punch_in_utc: datetime, shift_start: str, attendance_date_str: str
) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly > 10:00 after shift_start;
    then late_minutes = floor elapsed minutes since shift_start (not grace period end).
    """
    p_in_ist = truncate_to_seconds(punch_in_utc).astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    att_date = date.fromisoformat(attendance_date_str)
    shift_start_dt = datetime(
        att_date.year, att_date.month, att_date.day, sh, sm, 0, tzinfo=IST
    )

    elapsed_seconds = (p_in_ist - shift_start_dt).total_seconds()
    # Strictly more than 10 minutes (600 seconds)
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    p_in = truncate_to_seconds(punch_in_utc)
    p_out = truncate_to_seconds(punch_out_utc)
    diff_sec = (p_out - p_in).total_seconds()
    return round_half_up(diff_sec / 3600.0, 2)


def compute_overtime_minutes(
    punch_out_utc: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date_str: str,
) -> int:
    """
    R3: Whole minutes (floor) between shift_end and punch_out, counted only when >= 30;
    otherwise 0. For overnight shifts, shift_end is on the following calendar day.
    """
    p_out_ist = truncate_to_seconds(punch_out_utc).astimezone(IST)
    sh_start, sm_start = map(int, shift_start.split(":"))
    sh_end, sm_end = map(int, shift_end.split(":"))
    att_date = date.fromisoformat(attendance_date_str)

    # If overnight, shift end is next calendar day
    if (sh_end, sm_end) <= (sh_start, sm_start):
        shift_end_date = att_date + timedelta(days=1)
    else:
        shift_end_date = att_date

    shift_end_dt = datetime(
        shift_end_date.year,
        shift_end_date.month,
        shift_end_date.day,
        sh_end,
        sm_end,
        0,
        tzinfo=IST,
    )
    elapsed_sec = (p_out_ist - shift_end_dt).total_seconds()
    if elapsed_sec <= 0:
        return 0
    ot = int(elapsed_sec // 60)
    return ot if ot >= 30 else 0


def count_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7: Working days are Mon-Fri in the month. If joined mid-month,
    count Mon-Fri on or after joined_on.
    """
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, num_days)

    joined_date = date.fromisoformat(joined_on_str)
    if joined_date > month_end:
        return 0

    curr = max(month_start, joined_date)
    working_days = 0
    while curr <= month_end:
        if curr.weekday() < 5:  # Monday to Friday
            working_days += 1
        curr += timedelta(days=1)
    return working_days


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB attendance document into openapi.yaml AttendanceRecord schema."""
    history = []
    for h in doc.get("history", []):
        formatted_changes = {}
        for k, v in h.get("changes", {}).items():
            from_val = v.get("from")
            to_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                from_val = to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val
                to_val = to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val
            formatted_changes[k] = {"from": from_val, "to": to_val}

        history.append(
            {
                "at": to_epoch_ms(h["at"]),
                "by": h["by"],
                "reason": h["reason"],
                "changes": formatted_changes,
            }
        )

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB employee document into openapi.yaml Employee schema."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        None, ge=100000000000, le=4102444800000
    )
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        None, ge=100000000000, le=4102444800000
    )


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(
        None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"
    )
    punch_in: Optional[int] = Field(
        None, ge=100000000000, le=4102444800000
    )
    punch_out: Optional[int] = Field(
        None, ge=100000000000, le=4102444800000
    )


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database connection failure",
        )


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee. Enforces unique emp_code via database index."""
    doc = body.model_dump()
    doc["created_at"] = truncate_to_seconds(datetime.now(timezone.utc))

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail=f"Employee code '{body.emp_code}' already exists",
        )

    return format_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post(
    "/attendance/punch-in",
    status_code=201,
    tags=["attendance"],
)
def punch_in(body: PunchInRequest):
    """Record a punch-in. Computes attendance date and late minutes. Race-safe."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=404,
            detail=f"Employee '{body.emp_code}' not found",
        )

    # Determine punch-in instant truncated to whole seconds
    if body.punched_at is not None:
        p_in_dt = from_epoch_ms(body.punched_at)
    else:
        p_in_dt = truncate_to_seconds(datetime.now(timezone.utc))

    att_date = get_attendance_date(p_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(p_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": p_in_dt,
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
        raise HTTPException(
            status_code=409,
            detail=f"Already punched in for date '{att_date}'",
        )

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """Record a punch-out. Closes most recent open record, computes R3-R5."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=404,
            detail=f"Employee '{body.emp_code}' not found",
        )

    if body.punched_at is not None:
        p_out_dt = from_epoch_ms(body.punched_at)
    else:
        p_out_dt = truncate_to_seconds(datetime.now(timezone.utc))

    # Find employee's most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": p_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(
            status_code=404,
            detail="No punch-in record found for this punch-out",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Record is already punched out",
        )

    # Validate timing rules
    p_in_dt = record["punch_in"]
    if p_out_dt <= p_in_dt:
        raise HTTPException(
            status_code=422,
            detail="punch_out must be after punch_in",
        )

    if (p_out_dt - p_in_dt).total_seconds() > 86400:
        raise HTTPException(
            status_code=422,
            detail="punch_out cannot be more than 24 hours after punch_in",
        )

    work_hours = compute_work_hours(p_in_dt, p_out_dt)
    overtime_minutes = compute_overtime_minutes(
        p_out_dt, emp["shift_start"], emp["shift_end"], record["date"]
    )
    half_day = work_hours < 4.50

    # Atomic update to guarantee single punch-out under concurrency
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": p_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Record was punched out concurrently",
        )

    return format_attendance_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date descending, then emp_code ascending."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must not be greater than date_to",
        )

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
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """Regularize (manually correct) one day's record; append to history audit trail."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=404,
            detail=f"Employee '{emp_code}' not found",
        )

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(
            status_code=404,
            detail=f"Attendance record for date '{date}' not found",
        )

    # Validate setting ABSENT / LEAVE
    if body.status in ("ABSENT", "LEAVE") and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(
            status_code=422,
            detail="Cannot supply punch times when status is ABSENT or LEAVE",
        )

    # Determine prospective fields
    new_status = body.status if body.status is not None else record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = (
            from_epoch_ms(body.punch_in)
            if body.punch_in is not None
            else record.get("punch_in")
        )
        new_punch_out = (
            from_epoch_ms(body.punch_out)
            if body.punch_out is not None
            else record.get("punch_out")
        )

    # Presence status requires punch_in
    if new_status in ("PRESENT", "WFH", "ON_DUTY") and not new_punch_in:
        raise HTTPException(
            status_code=422,
            detail="Presence status requires a punch_in",
        )

    # punch_in must stay on record's attendance date
    if new_punch_in:
        calculated_date = get_attendance_date(
            new_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if calculated_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in belongs to attendance date '{calculated_date}', not '{date}'",
            )

    # punch_out timing check
    if new_punch_out:
        if not new_punch_in:
            raise HTTPException(
                status_code=422,
                detail="punch_out requires punch_in",
            )
        diff_sec = (new_punch_out - new_punch_in).total_seconds()
        if diff_sec <= 0:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in",
            )
        if diff_sec > 86400:
            raise HTTPException(
                status_code=422,
                detail="punch_out cannot be more than 24 hours after punch_in",
            )

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(
            new_punch_in, emp["shift_start"], date
        )
        if new_punch_out:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime_minutes(
                new_punch_out, emp["shift_start"], emp["shift_end"], date
            )
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Check which fields actually changed
    old_fields = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    new_fields = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    changes_for_history = {}
    for key in (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ):
        if old_fields[key] != new_fields[key]:
            changes_for_history[key] = {
                "from": old_fields[key],
                "to": new_fields[key],
            }

    if not changes_for_history:
        raise HTTPException(
            status_code=422,
            detail="Request resulted in no changes to the record",
        )

    history_entry = {
        "at": truncate_to_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_for_history,
    }

    # Optimistic concurrency check: match current history length to prevent lost updates
    existing_history = record.get("history", [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            "history": {"$size": len(existing_history)},
        },
        {
            "$set": new_fields,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Record was modified concurrently",
        )

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (Pipelines)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee backed by MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=404,
            detail=f"Employee '{emp_code}' not found",
        )

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    working_days = count_working_days(month, emp["joined_on"])

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "day_of_week": {
                    "$isoDayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                },
                "is_present": {
                    "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                },
                "is_leave": {"$eq": ["$status", "LEAVE"]},
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
                                    "$is_present",
                                    {"$lte": ["$day_of_week", 5]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": ["$is_leave", 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        agg = res[0]
        present_days = float(agg["present_days"])
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late_minutes = int(agg["total_late_minutes"])
        total_overtime_minutes = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up(present_days / working_days * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    """Build the aggregation pipeline for department summary."""
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
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
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "emp_code": 1,
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "day_of_week": {
                                "$isoDayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            },
                            "is_present": {
                                "$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]
                            },
                            "is_leave": {"$eq": ["$status", "LEAVE"]},
                            "is_on_duty": {"$eq": ["$status", "ON_DUTY"]},
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
                                            "$$l.is_present",
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
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": "$$l.late_minutes",
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": "$$l.is_leave",
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": "$$l.is_on_duty",
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            "$$l.is_present",
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "pl",
                            "in": "$$pl.work_hours",
                        }
                    }
                },
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    """Department summary for a month cross-collection aggregation."""
    pipeline = build_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)

    items = []
    for r in cursor:
        items.append(
            {
                "department": r["department"],
                "headcount": int(r["headcount"]),
                "present_days": float(r["present_days"]),
                "avg_work_hours": round_half_up(r.get("avg_work_hours"), 2),
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )

    return {"month": month, "items": items}


def build_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
):
    """Build the aggregation pipeline for late leaderboard with standard competition ranking."""
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
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

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
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
        ]
    )

    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """Late-comers leaderboard for a month with standard competition ranking."""
    pipeline = build_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(
    department: str, from_date: str, to_date: str
):
    """Build the aggregation pipeline for department daily trend with gap-filling and 7-day MA."""
    from_dt = datetime.fromisoformat(from_date + "T00:00:00Z")
    to_dt = datetime.fromisoformat(to_date + "T00:00:00Z")
    next_day_dt = to_dt + timedelta(days=1)

    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": "$department",
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
                "dept_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$dept_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 1,
                "employees": 1,
                "logs": 1,
                "date_val": {"$literal": from_dt},
            }
        },
        {
            "$densify": {
                "field": "date_val",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, next_day_dt],
                },
            }
        },
        {
            "$fill": {
                "output": {
                    "employees": {"method": "locf"},
                    "logs": {"method": "locf"},
                }
            }
        },
        {
            "$project": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_val",
                    }
                },
                "day_of_week": {"$isoDayOfWeek": "$date_val"},
                "employees": 1,
                "logs": 1,
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$lte": ["$day_of_week", 5]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
                "day_logs": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {"$eq": ["$$l.date", "$date"]},
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$day_logs",
                                    "as": "l",
                                    "cond": {
                                        "$in": [
                                            "$$l.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                }
                            },
                            "as": "pl",
                            "in": {
                                "$cond": [
                                    {"$ifNull": ["$$pl.half_day", False]},
                                    0.5,
                                    1.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "l",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$l.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        {"$round": ["$moving_avg_7d", 4]},
                    ]
                },
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend for a department with gap filling and 7-day moving average."""
    # Check if department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(
            status_code=404,
            detail=f"Department '{department}' not found",
        )

    f_date = date.fromisoformat(from_date)
    t_date = date.fromisoformat(to_date)

    if t_date < f_date:
        raise HTTPException(
            status_code=422,
            detail="to date must not be earlier than from date",
        )

    if (t_date - f_date).days > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    items = list(db.employees.aggregate(pipeline))

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(
        ...,
        pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$",
    ),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Return raw MongoDB explain output with executionStats for an endpoint's main query."""
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from must not be greater than date_to",
            )
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
            q["status"] = status

        skip = (page - 1) * page_size
        explain_doc = (
            db.attendance_logs.find(q)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip(skip)
            .limit(page_size)
            .explain()
        )
        collection_name = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required for employee_monthly",
            )
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_date, "$lte": end_date},
                }
            }
        ]
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        collection_name = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for department_summary",
            )
        pipeline = build_department_summary_pipeline(month, department)
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        collection_name = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for late_leaderboard",
            )
        pipeline = build_leaderboard_pipeline(month, limit, department)
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        collection_name = "attendance_logs"

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required for department_trend",
            )
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        collection_name = "employees"

    # Convert BSON explain to JSON serializable dict
    json_safe_explain = json_util.loads(json_util.dumps(explain_doc))

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": json_safe_explain,
    }
