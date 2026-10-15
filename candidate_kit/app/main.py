"""
Employee Attendance & Analytics API - Production Ready Implementation
Compliant with OpenAPI 3.0.3 Contract v2.0 and Business Rules R1 - R10.
"""
import calendar
import hashlib
import json
import math
import os
import re
import secrets
from datetime import date as dt_date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional, Union

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# --------------------------------------------------------------------------- #
# MongoDB Connection & Configuration
# --------------------------------------------------------------------------- #
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="Enterprise attendance recording, corrections, and analytics engine.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Lifespan / Startup Indexes
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def create_indexes():
    """
    Creates all required indexes idempotently at application startup.
    Ensures unique constraints and high-performance queries without COLLSCAN.
    """
    try:
        # employees indexes
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="idx_department_emp_code")
        db.employees.create_index([("joined_on", ASCENDING)], name="idx_emp_joined_on")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_dept_joined_on")

        # attendance_logs indexes
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="idx_emp_date_unique")
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_date_desc_emp_asc")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="idx_emp_punch_in_desc")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="idx_emp_date_desc")
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)], name="idx_date_late_emp")
        db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)], name="idx_date_status")

        # users collection index & seed
        db.users.create_index([("email", ASCENDING)], unique=True, name="idx_user_email_unique")
        if db.users.count_documents({}) == 0:
            h, s = hash_password("password123")
            db.users.insert_one({
                "email": "priya@peoplepulse.com",
                "name": "Priya Sharma",
                "phone": "+91 98765 43210",
                "role": "HR Manager",
                "avatar": "https://images.unsplash.com/photo-1573496359142-b8d87734a5a2?w=100&auto=format&fit=crop&q=80",
                "password_hash": h,
                "salt": s,
                "created_at": datetime.now(timezone.utc),
            })
    except Exception as e:
        # Do not crash startup if MongoDB is momentarily unreachable (e.g. before initial ping in tests)
        pass


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """Generates PBKDF2-HMAC-SHA256 password hash and salt."""
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()
    return hashed, salt


def verify_password(password: str, hashed: str, salt: str) -> bool:
    """Verifies candidate password with stored hash and salt."""
    test_hash, _ = hash_password(password, salt)
    return secrets.compare_digest(test_hash, hashed)


# --------------------------------------------------------------------------- #
# Mathematical & Rule Helper Functions (R1 - R8)
# --------------------------------------------------------------------------- #
def _resolve(val: Any, default: Any = None) -> Any:
    """Safely extracts value when function is invoked directly in Python tests without FastAPI DI."""
    if hasattr(val, "default"):
        return val.default if val.default is not ... else default
    return val if val is not None else default


def validate_calendar_date(value: str, field_name: str = "date") -> str:
    """Rejects date strings that match YYYY-MM-DD but are not real calendar dates."""
    try:
        dt_date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} must be a valid date in YYYY-MM-DD format",
        )
    return value


def round_half_up(value: Optional[Union[float, Decimal]], decimals: int = 2) -> Optional[float]:
    """R8: Rounds numerical values half-up using Decimal to avoid float/bankers bias."""
    if value is None:
        return None
    d = Decimal(str(value))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: Instants are truncated to whole seconds before any calculation."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0)


def epoch_ms_to_datetime(ms: int) -> datetime:
    """Converts epoch milliseconds into UTC datetime truncated to seconds."""
    truncated_sec = ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a UTC or naive BSON datetime into epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_time(t_str: str) -> dt_time:
    """Parses HH:MM string into time object."""
    h, m = map(int, t_str.split(":"))
    return dt_time(hour=h, minute=m, second=0)


def compute_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date in IST.
    Overnight shifts (shift_end <= shift_start): punch-in IST earlier than shift_end
    belongs to the shift that started the previous day.
    """
    dt_ist = punch_in_dt.astimezone(IST)
    h_s, m_s = map(int, shift_start.split(":"))
    h_e, m_e = map(int, shift_end.split(":"))
    is_overnight = (h_e, m_e) <= (h_s, m_s)
    
    if is_overnight and dt_ist.time() < dt_time(h_e, m_e):
        att_date = dt_ist.date() - timedelta(days=1)
    else:
        att_date = dt_ist.date()
    return att_date.isoformat()


def compute_shift_instants(attendance_date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Returns shift_start and shift_end UTC datetimes for the given attendance date."""
    base_date = dt_date.fromisoformat(attendance_date_str)
    h_s, m_s = map(int, shift_start.split(":"))
    h_e, m_e = map(int, shift_end.split(":"))
    is_overnight = (h_e, m_e) <= (h_s, m_s)

    start_ist = datetime.combine(base_date, dt_time(h_s, m_s), tzinfo=IST)
    if is_overnight:
        end_ist = datetime.combine(base_date + timedelta(days=1), dt_time(h_e, m_e), tzinfo=IST)
    else:
        end_ist = datetime.combine(base_date, dt_time(h_e, m_e), tzinfo=IST)

    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)


def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    """
    R2: Grace period 10 minutes (600 seconds).
    Late ONLY if punch_in is strictly more than 10:00 (> 600s) after shift_start.
    Then late_minutes = whole minutes (floor) since shift_start.
    """
    punch_sec = punch_in.replace(microsecond=0)
    start_sec = shift_start_dt.replace(microsecond=0)
    diff = (punch_sec - start_sec).total_seconds()
    if diff > 600:
        return int(math.floor(diff / 60))
    return 0


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    """
    R3: Overtime = whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    """
    diff = (punch_out - shift_end_dt).total_seconds()
    if diff >= 1800:  # 30 * 60
        return int(math.floor(diff / 60))
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    sec = (punch_out - punch_in).total_seconds()
    raw = Decimal(str(sec)) / Decimal("3600")
    return float(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_doc(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Converts internal MongoDB doc to compliant API response dict."""
    if not doc:
        return None
    d = dict(doc)
    d.pop("_id", None)
    d.pop("id", None)

    for field in ("created_at", "punch_in", "punch_out"):
        if field in d:
            d[field] = datetime_to_epoch_ms(d[field])

    if "history" in d and isinstance(d["history"], list):
        serialized_hist = []
        for h in d["history"]:
            h_entry = dict(h)
            if "at" in h_entry:
                h_entry["at"] = datetime_to_epoch_ms(h_entry["at"])
            if "changes" in h_entry and isinstance(h_entry["changes"], dict):
                ser_changes = {}
                for k, v in h_entry["changes"].items():
                    c_from = v.get("from")
                    c_to = v.get("to")
                    if k in ("punch_in", "punch_out"):
                        ser_changes[k] = {
                            "from": datetime_to_epoch_ms(c_from) if isinstance(c_from, datetime) else c_from,
                            "to": datetime_to_epoch_ms(c_to) if isinstance(c_to, datetime) else c_to,
                        }
                    else:
                        ser_changes[k] = {"from": c_from, "to": c_to}
                h_entry["changes"] = ser_changes
            serialized_hist.append(h_entry)
        d["history"] = serialized_hist
    elif "history" not in d:
        d["history"] = []

    if "half_day" not in d:
        d["half_day"] = False
    if "late_minutes" not in d:
        d["late_minutes"] = 0
    if "overtime_minutes" not in d:
        d["overtime_minutes"] = 0

    return d


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            dt_date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date in YYYY-MM-DD format")
        return value

    @field_validator("shift_end")
    @classmethod
    def validate_shifts(cls, v, info):
        start = info.data.get("shift_start", "09:30")
        if v == start:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, strict=True, ge=100000000000, le=4102444800000)


class SignupIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(..., min_length=6)
    phone: Optional[str] = "+91 98765 43210"


class LoginIn(BaseModel):
    email: str
    password: str


class ProfileUpdateIn(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    avatar: Optional[str] = None
    role: Optional[str] = None
    password: Optional[str] = None


# --------------------------------------------------------------------------- #
# Endpoints: Authentication & User Profile
# --------------------------------------------------------------------------- #
@app.post("/auth/signup", status_code=status.HTTP_201_CREATED, tags=["auth"])
def auth_signup(body: SignupIn):
    """Registers a new HR Manager and stores credentials in MongoDB."""
    email_clean = body.email.lower()
    if db.users.find_one({"email": email_clean}):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    h, s = hash_password(body.password)
    doc = {
        "email": email_clean,
        "name": body.name,
        "phone": body.phone or "+91 98765 43210",
        "role": "HR Manager",
        "avatar": f"https://api.dicebear.com/7.x/initials/svg?seed={body.name}",
        "password_hash": h,
        "salt": s,
        "created_at": datetime.now(timezone.utc),
    }
    db.users.insert_one(doc)
    doc.pop("_id", None)
    doc.pop("password_hash", None)
    doc.pop("salt", None)
    doc["created_at"] = int(doc["created_at"].timestamp() * 1000)
    return doc


@app.post("/auth/login", tags=["auth"])
def auth_login(body: LoginIn):
    """Authenticates HR credentials against MongoDB."""
    user = db.users.find_one({"email": body.email.lower()})
    if not user or not verify_password(body.password, user.get("password_hash", ""), user.get("salt", "")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    user.pop("_id", None)
    user.pop("password_hash", None)
    user.pop("salt", None)
    if isinstance(user.get("created_at"), datetime):
        user["created_at"] = int(user["created_at"].timestamp() * 1000)
    return user


@app.get("/auth/me", tags=["auth"])
def auth_me(email: Optional[str] = Query(None)):
    """Retrieves current authenticated profile."""
    target_email = email.lower() if email else "priya@peoplepulse.com"
    user = db.users.find_one({"email": target_email})
    if not user:
        user = db.users.find_one({})
    if not user:
        return {
            "email": "priya@peoplepulse.com",
            "name": "Priya Sharma",
            "phone": "+91 98765 43210",
            "role": "HR Manager",
            "avatar": "https://images.unsplash.com/photo-1573496359142-b8d87734a5a2?w=100&auto=format&fit=crop&q=80",
        }
    user.pop("_id", None)
    user.pop("password_hash", None)
    user.pop("salt", None)
    if isinstance(user.get("created_at"), datetime):
        user["created_at"] = int(user["created_at"].timestamp() * 1000)
    return user


@app.patch("/auth/profile", tags=["auth"])
def auth_update_profile(body: ProfileUpdateIn, email: Optional[str] = Query(None)):
    """Updates HR profile: name, phone, image, and credentials."""
    target_email = (email or (body.email if body.email else "priya@peoplepulse.com")).lower()
    user = db.users.find_one({"email": target_email}) or db.users.find_one({})
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User profile not found")
    updates: Dict[str, Any] = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.phone is not None:
        updates["phone"] = body.phone
    if body.avatar is not None:
        updates["avatar"] = body.avatar
    if body.role is not None:
        updates["role"] = body.role
    if body.email is not None and body.email.lower() != user.get("email"):
        updates["email"] = body.email.lower()
    if body.password:
        h, s = hash_password(body.password)
        updates["password_hash"] = h
        updates["salt"] = s
    if updates:
        db.users.update_one({"_id": user["_id"]}, {"$set": updates})
    updated = db.users.find_one({"_id": user["_id"]})
    updated.pop("_id", None)
    updated.pop("password_hash", None)
    updated.pop("salt", None)
    if isinstance(updated.get("created_at"), datetime):
        updated["created_at"] = int(updated["created_at"].timestamp() * 1000)
    return updated


# --------------------------------------------------------------------------- #
# Endpoints: System & Employees
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database unavailable: {e}")


@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeIn):
    """
    Creates an employee. Unique emp_code is enforced by the database unique index.
    Returns 409 on duplicate, 422 on validation error.
    """
    now = truncate_to_seconds(datetime.now(timezone.utc))
    doc = body.model_dump()
    doc["created_at"] = now

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="emp_code already exists")

    return serialize_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """Lists employees sorted by emp_code ascending. Total reflects filters."""
    page = int(_resolve(page, 1))
    page_size = int(_resolve(page_size, 20))
    department = _resolve(department, None)

    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_doc(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Endpoints: Attendance Operations
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInIn):
    """
    Records punch-in. Race-safe via compound unique index on (emp_code, date).
    Returns 404 for unknown employee, 409 if already punched in for that date.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown employee")

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_in_dt = truncate_to_seconds(datetime.now(timezone.utc))

    att_date = compute_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    shift_start_dt, _ = compute_shift_instants(att_date, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, shift_start_dt)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already punched in for this date")

    return serialize_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutIn):
    """
    Closes the employee's most recent record whose punch_in <= punched_at.
    Handles overnight shifts across midnight.
    Returns 404 if no record, 409 if already punched out (atomic race-safe), 422 if invalid timestamp.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown employee")

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_out_dt = truncate_to_seconds(datetime.now(timezone.utc))

    # Find the latest open record whose punch_in <= punch_out_dt
    candidate = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_dt},
            "punch_out": None,
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not candidate:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No open punch-in found")

    punch_in_dt = candidate["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be strictly after punch_in")

    if (punch_out_dt - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out cannot be more than 24 hours after punch_in")

    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    half = compute_half_day(work_hrs)
    _, shift_end_dt = compute_shift_instants(candidate["date"], emp["shift_start"], emp["shift_end"])
    ot_min = compute_overtime(punch_out_dt, shift_end_dt)

    # Atomic CAS update ensures simultaneous punch-outs yield exactly one 200 and one 409
    res = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": half,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not res:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record is already punched out")

    return serialize_doc(res)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    High performance on 100k+ records backed by compound index.
    """
    page = int(_resolve(page, 1))
    page_size = int(_resolve(page_size, 20))
    emp_code = _resolve(emp_code, None)
    date_from = _resolve(date_from, None)
    date_to = _resolve(date_to, None)
    status = _resolve(status, None)

    if date_from:
        validate_calendar_date(date_from, "date_from")
    if date_to:
        validate_calendar_date(date_to, "date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_q = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q
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

    items = [serialize_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    body: RegularizeIn,
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Regularize one day's record: recomputes derived fields, appends to history.
    Race-safe with optimistic concurrency control.
    """
    validate_calendar_date(date, "date")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown employee")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")

    new_status = body.status if body.status is not None else rec["status"]

    # Rule: setting ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out with it is 422
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Cannot supply punch times when status is ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            final_punch_in = epoch_ms_to_datetime(body.punch_in)
        else:
            final_punch_in = rec.get("punch_in")
            if final_punch_in is not None and final_punch_in.tzinfo is None:
                final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)

        if final_punch_in is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Presence status requires a punch_in")

        # R1: punch_in must stay on the record's attendance date
        computed_date = compute_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_in does not belong to record's attendance date")

        if body.punch_out is not None:
            final_punch_out = epoch_ms_to_datetime(body.punch_out)
        else:
            final_punch_out = rec.get("punch_out")
            if final_punch_out is not None and final_punch_out.tzinfo is None:
                final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be strictly after punch_in")
            if (final_punch_out - final_punch_in).total_seconds() > 86400:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_late = 0
        new_work_hours = None
        new_overtime = 0
        new_half_day = False
    else:
        shift_start_dt, shift_end_dt = compute_shift_instants(date, emp["shift_start"], emp["shift_end"])
        new_late = compute_late_minutes(final_punch_in, shift_start_dt)
        if final_punch_out is not None:
            new_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            new_overtime = compute_overtime(final_punch_out, shift_end_dt)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime = 0
            new_half_day = False

    # Compute changes
    old_punch_in = rec.get("punch_in")
    old_punch_out = rec.get("punch_out")
    if old_punch_in and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    if old_punch_out and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    changes: Dict[str, Any] = {}
    if rec["status"] != new_status:
        changes["status"] = {"from": rec["status"], "to": new_status}
    if old_punch_in != final_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": final_punch_in}
    if old_punch_out != final_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": final_punch_out}
    if rec.get("work_hours") != new_work_hours:
        changes["work_hours"] = {"from": rec.get("work_hours"), "to": new_work_hours}
    if rec.get("late_minutes", 0) != new_late:
        changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": new_late}
    if rec.get("overtime_minutes", 0) != new_overtime:
        changes["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": new_overtime}
    if rec.get("half_day", False) != new_half_day:
        changes["half_day"] = {"from": rec.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Request resulted in no field changes")

    now = truncate_to_seconds(datetime.now(timezone.utc))
    history_entry = {
        "at": now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency check: match current history length
    current_history = rec.get("history", [])
    update_result = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(current_history)]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late,
                "overtime_minutes": new_overtime,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not update_result:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Concurrent regularization conflict")

    return serialize_doc(update_result)


# --------------------------------------------------------------------------- #
# Endpoints: Analytics (Pure MongoDB Aggregations)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str = Path(...), month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """Monthly summary for one employee calculated entirely in MongoDB."""
    emp_code = _resolve(emp_code)
    month = _resolve(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown employee")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    first_day = f"{month}-01"
    last_day = f"{month}-{num_days:02d}"

    # Calculate working_days (Mon-Fri) per R7
    joined_on = emp["joined_on"]
    working_days = 0
    if joined_on <= last_day:
        start_date = max(dt_date.fromisoformat(first_day), dt_date.fromisoformat(joined_on))
        end_date = dt_date.fromisoformat(last_day)
        cur = start_date
        while cur <= end_date:
            if cur.weekday() < 5:  # Mon=0, Fri=4
                working_days += 1
            cur += timedelta(days=1)

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": first_day, "$lte": last_day},
        }},
        {"$project": {
            "date": 1,
            "status": 1,
            "work_hours": 1,
            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
            "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
            "half_day": {"$ifNull": ["$half_day", False]},
            "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
        }},
        {"$project": {
            "is_mon_fri": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "is_leave": {"$eq": ["$status", "LEAVE"]},
            "half_day": 1,
            "late_minutes": 1,
            "overtime_minutes": 1,
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": ["$is_mon_fri", "$is_present"]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0,
                    ]
                }
            },
            "leave_days": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        agg = res[0]
        present_days = float(agg.get("present_days", 0.0))
        leave_days = int(agg.get("leave_days", 0))
        late_count = int(agg.get("late_count", 0))
        total_late = int(agg.get("total_late_minutes", 0))
        total_ot = int(agg.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary across employees and attendance_logs collections.
    Headcount includes employees with zero logs (R9). Average work hours is over records.
    """
    month = _resolve(month)
    department = _resolve(department)
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    first_day = f"{month}-01"
    last_day = f"{month}-{num_days:02d}"

    emp_match: Dict[str, Any] = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$emp"]},
                            {"$gte": ["$date", first_day]},
                            {"$lte": ["$date", last_day]},
                        ]
                    }
                }}
            ],
            "as": "logs",
        }},
        {"$unwind": {
            "path": "$logs",
            "preserveNullAndEmptyArrays": True,
        }},
        {"$project": {
            "department": 1,
            "emp_code": 1,
            "status": "$logs.status",
            "date": "$logs.date",
            "work_hours": "$logs.work_hours",
            "late_minutes": {"$ifNull": ["$logs.late_minutes", 0]},
            "half_day": {"$ifNull": ["$logs.half_day", False]},
            "dayOfWeek": {
                "$cond": [
                    {"$ifNull": ["$logs.date", False]},
                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                    None,
                ]
            },
        }},
        {"$group": {
            "_id": "$department",
            "all_emps": {"$addToSet": "$emp_code"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            ]
                        },
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0,
                    ]
                }
            },
            "valid_work_hours": {
                "$push": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$work_hours", None]},
                            ]
                        },
                        "$work_hours",
                        "$$REMOVE",
                    ]
                }
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {"$sum": "$late_minutes"},
            "leave_count": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "on_duty_count": {
                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
            },
        }},
        {"$project": {
            "department": "$_id",
            "headcount": {"$size": "$all_emps"},
            "present_days": "$present_days",
            "avg_work_hours": {"$avg": "$valid_work_hours"},
            "late_count": "$late_count",
            "total_late_minutes": "$total_late_minutes",
            "leave_count": "$leave_count",
            "on_duty_count": "$on_duty_count",
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": ASCENDING}},
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        avg_wh = doc.get("avg_work_hours")
        items.append({
            "department": doc["department"],
            "headcount": doc["headcount"],
            "present_days": float(doc["present_days"]),
            "avg_work_hours": round_half_up(avg_wh, 2) if avg_wh is not None else None,
            "late_count": int(doc["late_count"]),
            "total_late_minutes": int(doc["total_late_minutes"]),
            "leave_count": int(doc["leave_count"]),
            "on_duty_count": int(doc["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Ranks employees by total late_minutes in the month (only total > 0).
    Uses standard competition ranking (1, 2, 2, 4) with ties returned when rank <= limit.
    """
    month = _resolve(month)
    limit = int(_resolve(limit, 10))
    department = _resolve(department)
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    first_day = f"{month}-01"
    last_day = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {"$match": {
            "date": {"$gte": first_day, "$lte": last_day},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$project": {
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": DESCENDING},
            "output": {
                "rank": {"$rank": {}}
            },
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ])

    items = []
    for doc in db.attendance_logs.aggregate(pipeline):
        items.append({
            "rank": int(doc["rank"]),
            "emp_code": doc["emp_code"],
            "name": doc["name"],
            "department": doc["department"],
            "total_late_minutes": int(doc["total_late_minutes"]),
            "late_count": int(doc["late_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department with gap filling and 7-day moving average.
    Computed entirely via MongoDB pipeline using $densify and $setWindowFields.
    """
    department = _resolve(department)
    from_date = _resolve(from_date)
    to_date = _resolve(to_date)
    validate_calendar_date(from_date, "from")
    validate_calendar_date(to_date, "to")
    dept_check = db.employees.find_one({"department": department})
    if not dept_check:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown department")

    d_from = dt_date.fromisoformat(from_date)
    d_to = dt_date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'to' date must be >= 'from' date")

    diff_days = (d_to - d_from).days + 1
    if diff_days > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range cannot exceed 92 days")

    # Pipeline densifies across all calendar days, joins headcount and logs
    # Using an aggregation pipeline on attendance_logs with $densify
    # Ensure boundary days are seeded into pipeline for $densify
    pipeline: List[Dict[str, Any]] = [
        {"$match": {
            "date": {"$gte": from_date, "$lte": to_date},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                        0.0,
                    ]
                }
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            },
        }},
        {"$project": {
            "date_dt": {"$dateFromString": {"dateString": "$_id"}},
            "present_count": 1,
            "late_count": 1,
        }},
        # Union boundary docs so $densify always has from and to anchors
        {"$unionWith": {
            "coll": "employees",
            "pipeline": [
                {"$limit": 1},
                {"$project": {
                    "_id": 0,
                    "date_dt": {"$dateFromString": {"dateString": from_date}},
                    "present_count": {"$literal": 0.0},
                    "late_count": {"$literal": 0},
                }},
                {"$unionWith": {
                    "coll": "employees",
                    "pipeline": [
                        {"$limit": 1},
                        {"$project": {
                            "_id": 0,
                            "date_dt": {"$dateFromString": {"dateString": to_date}},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }}
                    ]
                }}
            ]
        }},
        {"$group": {
            "_id": "$date_dt",
            "present_count": {"$sum": "$present_count"},
            "late_count": {"$sum": "$late_count"},
        }},
        {"$project": {
            "date_dt": "$_id",
            "present_count": 1,
            "late_count": 1,
        }},
        {"$densify": {
            "field": "date_dt",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [
                    datetime.combine(d_from, dt_time(0, 0), tzinfo=timezone.utc),
                    datetime.combine(d_to + timedelta(days=1), dt_time(0, 0), tzinfo=timezone.utc),
                ]
            }
        }},
        {"$project": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
            "dayOfWeek": {"$dayOfWeek": "$date_dt"},
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"curDate": "$date"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$department", department]},
                            {"$lte": ["$joined_on", "$$curDate"]},
                        ]
                    }
                }},
                {"$count": "count"},
            ],
            "as": "hc",
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
            "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {
                        "$and": [
                            "$is_working_day",
                            {"$gt": ["$headcount", 0]}
                        ]
                    },
                    {"$divide": ["$present_count", "$headcount"]},
                    None,
                ]
            }
        }},
        {"$sort": {"date": ASCENDING}},
        {"$setWindowFields": {
            "sortBy": {"date": ASCENDING},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {
                        "documents": [-6, 0]
                    }
                }
            }
        }},
    ]

    items = []
    for doc in db.attendance_logs.aggregate(pipeline):
        att_rate = doc.get("attendance_rate")
        m_avg = doc.get("moving_avg_7d")
        items.append({
            "date": doc["date"],
            "is_working_day": bool(doc["is_working_day"]),
            "headcount": int(doc["headcount"]),
            "present_count": float(doc["present_count"]),
            "late_count": int(doc["late_count"]),
            "attendance_rate": round_half_up(att_rate, 4) if att_rate is not None else None,
            "moving_avg_7d": round_half_up(m_avg, 4) if m_avg is not None else None,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Endpoints: Admin Explain (Verifies Index Scan)
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"] = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status_param: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    Runs MongoDB's explain with executionStats on the endpoint's exact query or pipeline.
    Validates that query execution uses IXSCAN without COLLSCAN.
    """
    endpoint = _resolve(endpoint)
    emp_code = _resolve(emp_code)
    month = _resolve(month)
    department = _resolve(department)
    limit = int(_resolve(limit, 10))
    date_from = _resolve(date_from)
    date_to = _resolve(date_to)
    status_param = _resolve(status_param)
    from_date = _resolve(from_date)
    to_date = _resolve(to_date)
    page = int(_resolve(page, 1))
    page_size = int(_resolve(page_size, 20))

    for value, field_name in (
        (date_from, "date_from"),
        (date_to, "date_to"),
        (from_date, "from"),
        (to_date, "to"),
    ):
        if value:
            validate_calendar_date(value, field_name)

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from > date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            date_q = {}
            if date_from:
                date_q["$gte"] = date_from
            if date_to:
                date_q["$lte"] = date_to
            q["date"] = date_q
        if status_param:
            q["status"] = status_param

        skip = (page - 1) * page_size
        plan = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": DESCENDING, "emp_code": ASCENDING},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(plan))}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        first_day = f"{month}-01"
        last_day = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day, "$lte": last_day},
            }},
            {"$project": {
                "date": 1,
                "status": 1,
                "work_hours": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }},
            {"$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
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
            }},
        ]
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(plan))}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        first_day = f"{month}-01"
        last_day = f"{month}-{num_days:02d}"

        emp_match: Dict[str, Any] = {"joined_on": {"$lte": last_day}}
        if department:
            emp_match["department"] = department

        pipeline = [
            {"$match": emp_match},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {"$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$emp_code", "$$emp"]},
                                {"$gte": ["$date", first_day]},
                                {"$lte": ["$date", last_day]},
                            ]
                        }
                    }}
                ],
                "as": "logs",
            }},
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {"$project": {
                "department": 1,
                "emp_code": 1,
                "status": "$logs.status",
                "date": "$logs.date",
                "work_hours": "$logs.work_hours",
                "late_minutes": {"$ifNull": ["$logs.late_minutes", 0]},
                "half_day": {"$ifNull": ["$logs.half_day", False]},
                "dayOfWeek": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None,
                    ]
                },
            }},
            {"$group": {
                "_id": "$department",
                "all_emps": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": {"$cond": [
                    {"$and": [
                        {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    ]},
                    {"$cond": ["$half_day", 0.5, 1.0]},
                    0.0,
                ]}},
                "valid_work_hours": {"$push": {"$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": ["$work_hours", None]},
                    ]},
                    "$work_hours",
                    "$$REMOVE",
                ]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }},
            {"$project": {
                "department": "$_id",
                "headcount": {"$size": "$all_emps"},
                "present_days": "$present_days",
                "avg_work_hours": {"$avg": "$valid_work_hours"},
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }},
            {"$match": {"headcount": {"$gt": 0}}},
            {"$sort": {"department": ASCENDING}},
        ]
        plan = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": json.loads(json_util.dumps(plan))}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        first_day = f"{month}-01"
        last_day = f"{month}-{num_days:02d}"

        pipeline: List[Dict[str, Any]] = [
            {"$match": {"date": {"$gte": first_day, "$lte": last_day}, "late_minutes": {"$gt": 0}}},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }},
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline.extend([
            {"$project": {
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }},
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {"rank": {"$rank": {}}},
            }},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        ])
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(plan))}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        d_from = dt_date.fromisoformat(from_date)
        d_to = dt_date.fromisoformat(to_date)
        if d_to < d_from:
            raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        pipeline: List[Dict[str, Any]] = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$group": {
                "_id": "$date",
                "present_count": {"$sum": {"$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]},
                    0.0,
                ]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }},
            {"$project": {"date_dt": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
            {"$unionWith": {"coll": "employees", "pipeline": [
                {"$limit": 1},
                {"$project": {"_id": 0, "date_dt": {"$dateFromString": {"dateString": from_date}}, "present_count": {"$literal": 0.0}, "late_count": {"$literal": 0}}},
                {"$unionWith": {"coll": "employees", "pipeline": [
                    {"$limit": 1},
                    {"$project": {"_id": 0, "date_dt": {"$dateFromString": {"dateString": to_date}}, "present_count": {"$literal": 0.0}, "late_count": {"$literal": 0}}},
                ]}},
            ]}},
            {"$group": {"_id": "$date_dt", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
            {"$project": {"date_dt": "$_id", "present_count": 1, "late_count": 1}},
            {"$densify": {"field": "date_dt", "range": {"step": 1, "unit": "day", "bounds": [
                datetime.combine(d_from, dt_time(0, 0), tzinfo=timezone.utc),
                datetime.combine(d_to + timedelta(days=1), dt_time(0, 0), tzinfo=timezone.utc),
            ]}}},
            {"$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "dayOfWeek": {"$dayOfWeek": "$date_dt"},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }},
            {"$lookup": {"from": "employees", "let": {"curDate": "$date"}, "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$department", department]},
                    {"$lte": ["$joined_on", "$$curDate"]},
                ]}}},
                {"$count": "count"},
            ], "as": "hc"}},
            {"$addFields": {"headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]}, "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]}}},
            {"$addFields": {"attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]}}},
            {"$sort": {"date": ASCENDING}},
            {"$setWindowFields": {"sortBy": {"date": ASCENDING}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        ]
        plan = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(plan))}


# --------------------------------------------------------------------------- #
# Frontend Static Files Mount
# --------------------------------------------------------------------------- #
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    @app.get("/dashboard", include_in_schema=False)
    def serve_dashboard():
        """Serves the rich interactive frontend dashboard."""
        index_file = os.path.join(static_dir, "index.html")
        if os.path.exists(index_file):
            return FileResponse(index_file)
        return {"message": "Frontend static files directory mounted."}
