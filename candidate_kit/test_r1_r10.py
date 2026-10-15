"""PeoplePulse R1-R10 integration suite. Requires a REAL MongoDB 6.0+.

Run from repository root (Windows):
  pip install pytest httpx pymongo python-dotenv
  python -m pytest -v test_r1_r10.py

MongoDB credentials are loaded from the repository-root .env file.
Required .env variable: MONGO_URI=mongodb+srv://...
This suite has no localhost fallback.

SAFETY: Uses a random isolated database, and deletes ONLY that database at exit.
The suite imports app.main and uses FastAPI TestClient (no uvicorn needed).
"""
import calendar
import importlib
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from pymongo import MongoClient

# Load credentials from the .env file beside this test file, before reading env vars.
ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env", override=False)

IST = timezone(timedelta(hours=5, minutes=30))
MONTH = "2026-07"


def ms(day, clock, seconds=0, millis=0):
    h, m = map(int, clock.split(":"))
    return int(datetime.fromisoformat(day).replace(hour=h, minute=m, second=seconds,
               microsecond=millis * 1000, tzinfo=IST).timestamp() * 1000)


def record(emp, day, status="PRESENT", hours=8.0, late=0, overtime=0, half=False):
    return {"emp_code": emp, "date": day, "status": status,
            "punch_in": None, "punch_out": None, "work_hours": hours,
            "late_minutes": late, "overtime_minutes": overtime,
            "half_day": half, "history": []}


def employee(code, dept="Engineering", joined="2026-07-01", start="09:30", end="18:30"):
    return {"emp_code": code, "name": code, "email": f"{code.lower()}@test.invalid",
            "department": dept, "joined_on": joined, "shift_start": start,
            "shift_end": end, "created_at": datetime.now(timezone.utc)}


@pytest.fixture(scope="session")
def ctx():
    uri = os.environ.get("MONGO_URI", "").strip()
    if not uri:
        pytest.fail(
            "MONGO_URI is missing. Add your MongoDB Atlas connection string "
            "to the .env file in the repository root."
        )
    if not uri.startswith(("mongodb://", "mongodb+srv://")):
        pytest.fail("MONGO_URI must start with mongodb:// or mongodb+srv://.")
    if uri.startswith(("mongodb://localhost", "mongodb://127.0.0.1")):
        pytest.fail("Local MongoDB URI detected. Configure MongoDB Atlas in .env.")

    db_name = "peoplepulse_r1r10_test_" + uuid.uuid4().hex[:12]
    admin = MongoClient(uri, serverSelectionTimeoutMS=10000)
    try:
        admin.admin.command("ping")
    except Exception as exc:
        admin.close()
        pytest.fail(
            "MongoDB Atlas is not reachable. Check MONGO_URI in .env, Atlas "
            "Network Access, database-user credentials, and DNS/network access. "
            f"Details: {exc}"
        )
    # Never point this suite at a production DB; it creates a new, uniquely named DB.
    os.environ["MONGO_URI"] = uri
    os.environ["MONGO_DB"] = db_name
    if "app.main" in sys.modules:
        pytest.fail("app.main already imported before test DB configuration")
    module = importlib.import_module("app.main")
    module.create_indexes()
    client = TestClient(module.app)
    try:
        yield client, admin[db_name]
    finally:
        client.close()
        module.client.close()
        admin.drop_database(db_name)
        admin.close()


@pytest.fixture(autouse=True)
def reset(ctx):
    _, db = ctx
    db.attendance_logs.delete_many({})
    db.employees.delete_many({})
    yield


def call(ctx, method, url, status=200, **kw):
    res = ctx[0].request(method, url, **kw)
    assert res.status_code == status, f"{method} {url}: expected {status}, got {res.status_code}: {res.text[:700]}"
    return res.json()


def seed(ctx, *employees):
    ctx[1].employees.insert_many(list(employees))


# R1 — IST attendance date, overnight shifts, truncation of milliseconds.
def test_r1_overnight_attendance_date_and_truncation(ctx):
    seed(ctx, employee("EMP1001", start="22:00", end="06:00"))
    inp = ms("2026-07-08", "05:30", millis=999)
    r = call(ctx, "POST", "/attendance/punch-in", 201,
             json={"emp_code": "EMP1001", "punched_at": inp})
    assert r["date"] == "2026-07-07"
    assert r["punch_in"] == inp - 999
    out = call(ctx, "POST", "/attendance/punch-out", 200,
               json={"emp_code": "EMP1001", "punched_at": ms("2026-07-08", "06:30", millis=999)})
    assert out["date"] == "2026-07-07"
    assert out["punch_out"] == ms("2026-07-08", "06:30")
    assert out["overtime_minutes"] == 30


# R2 — grace is strictly >10:00; count minutes from shift start, floor.
@pytest.mark.parametrize("clock,seconds,expected", [
    ("09:40", 0, 0), ("09:40", 1, 10), ("10:15", 59, 45),
])
def test_r2_late_grace(ctx, clock, seconds, expected):
    seed(ctx, employee("EMP1002"))
    r = call(ctx, "POST", "/attendance/punch-in", 201,
             json={"emp_code": "EMP1002", "punched_at": ms("2026-07-06", clock, seconds)})
    assert r["late_minutes"] == expected


# R3 — threshold at 30 minutes, whole-minute floor.
@pytest.mark.parametrize("out_time,seconds,expected", [
    ("18:59", 59, 0), ("19:00", 0, 30), ("19:00", 59, 30),
])
def test_r3_overtime(ctx, out_time, seconds, expected):
    seed(ctx, employee("EMP1003"))
    call(ctx, "POST", "/attendance/punch-in", 201,
         json={"emp_code": "EMP1003", "punched_at": ms("2026-07-06", "09:30")})
    r = call(ctx, "POST", "/attendance/punch-out", 200,
             json={"emp_code": "EMP1003", "punched_at": ms("2026-07-06", out_time, seconds)})
    assert r["overtime_minutes"] == expected


# R4 — null before punch-out; half-up rounding of elapsed seconds.
def test_r4_work_hours_rounding(ctx):
    seed(ctx, employee("EMP1004"))
    r = call(ctx, "POST", "/attendance/punch-in", 201,
             json={"emp_code": "EMP1004", "punched_at": ms("2026-07-06", "09:30")})
    assert r["work_hours"] is None
    # 4h 30m 18s = 4.505 hours => 4.51 half-up.
    r = call(ctx, "POST", "/attendance/punch-out", 200,
             json={"emp_code": "EMP1004", "punched_at": ms("2026-07-06", "14:00", 18)})
    assert r["work_hours"] == 4.51


# R5 — rounded work_hours <4.50 => half day.
@pytest.mark.parametrize("out_clock,seconds,half", [
    ("13:59", 0, True), ("14:00", 0, False),
])
def test_r5_half_day(ctx, out_clock, seconds, half):
    seed(ctx, employee("EMP1005"))
    call(ctx, "POST", "/attendance/punch-in", 201,
         json={"emp_code": "EMP1005", "punched_at": ms("2026-07-06", "09:30")})
    r = call(ctx, "POST", "/attendance/punch-out", 200,
             json={"emp_code": "EMP1005", "punched_at": ms("2026-07-06", out_clock, seconds)})
    assert r["half_day"] is half


# R6 — three presence statuses count; ABSENT/LEAVE do not.
def test_r6_presence_statuses(ctx):
    seed(ctx, employee("EMP1006"))
    docs = [record("EMP1006", f"2026-07-{d:02d}", s) for d, s in [
        (6, "PRESENT"), (7, "WFH"), (8, "ON_DUTY"), (9, "ABSENT"), (10, "LEAVE")]]
    ctx[1].attendance_logs.insert_many(docs)
    r = call(ctx, "GET", "/analytics/employees/EMP1006/monthly?month=2026-07")
    assert r["present_days"] == 3
    assert r["leave_days"] == 1


# R7 — Mon-Fri after joined_on, weekend presence excluded but late/OT included.
def test_r7_midmonth_join_and_weekend(ctx):
    seed(ctx, employee("EMP1007", joined="2026-07-15"))
    ctx[1].attendance_logs.insert_many([
        record("EMP1007", "2026-07-15", half=True),
        record("EMP1007", "2026-07-18", late=12, overtime=45),  # Saturday
        record("EMP1007", "2026-07-20"),
    ])
    r = call(ctx, "GET", "/analytics/employees/EMP1007/monthly?month=2026-07")
    expected_days = sum(date(2026, 7, d).weekday() < 5 for d in range(15, 32))
    assert r["working_days"] == expected_days
    assert r["present_days"] == 1.5
    assert r["total_late_minutes"] == 12
    assert r["total_overtime_minutes"] == 45
    assert r["attendance_pct"] == float(Decimal(150 / expected_days).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# R8 — reported rates round to 4 places half-up; numbers 2 places.
def test_r8_four_decimal_rate(ctx):
    seed(ctx, employee("EMP1008", dept="R8Dept"),
         employee("EMP1009", dept="R8Dept"), employee("EMP1010", dept="R8Dept"))
    ctx[1].attendance_logs.insert_one(record("EMP1008", "2026-07-06"))
    r = call(ctx, "GET", "/analytics/departments/R8Dept/trend?from=2026-07-06&to=2026-07-06")
    assert len(r["items"]) == 1
    assert r["items"][0]["attendance_rate"] == 0.3333


# R9 — headcount includes employees with no logs; excludes post-period joiners.
def test_r9_headcount_without_logs(ctx):
    seed(ctx, employee("EMP1011", dept="R9Dept"),
         employee("EMP1012", dept="R9Dept"),
         employee("EMP1013", dept="R9Dept", joined="2026-08-01"))
    ctx[1].attendance_logs.insert_one(record("EMP1011", "2026-07-06"))
    r = call(ctx, "GET", "/analytics/departments/summary?month=2026-07")
    row = next(i for i in r["items"] if i["department"] == "R9Dept")
    assert row["headcount"] == 2


# R10 — 1-based pagination, defaults/max, filtered totals, sort.
def test_r10_employee_pagination(ctx):
    seed(ctx, *(employee(f"EMP{2000+i}", dept="R10Dept") for i in range(25)),
         employee("EMP3000", dept="OtherDept"))
    first = call(ctx, "GET", "/employees?department=R10Dept")
    second = call(ctx, "GET", "/employees?department=R10Dept&page=2")
    assert first["total"] == second["total"] == 25
    assert first["page"] == 1 and first["page_size"] == 20
    assert len(first["items"]) == 20 and len(second["items"]) == 5
    codes = [i["emp_code"] for i in first["items"] + second["items"]]
    assert codes == sorted(codes) and len(set(codes)) == 25
    call(ctx, "GET", "/employees?page=0", 422)
    call(ctx, "GET", "/employees?page_size=101", 422)


def test_r10_attendance_filter_total_and_sort(ctx):
    seed(ctx, employee("EMP1020"), employee("EMP1021"))
    ctx[1].attendance_logs.insert_many([
        record("EMP1021", "2026-07-06"),
        record("EMP1020", "2026-07-06"),
        record("EMP1020", "2026-07-07", status="LEAVE"),
    ])
    r = call(ctx, "GET", "/attendance?status=PRESENT&page_size=1")
    assert r["total"] == 2 and len(r["items"]) == 1
    r = call(ctx, "GET", "/attendance?status=PRESENT&page_size=10")
    assert [x["emp_code"] for x in r["items"]] == ["EMP1020", "EMP1021"]
    call(ctx, "GET", "/attendance?date_from=2026-07-10&date_to=2026-07-01", 422)


# Contract and concurrency checks beyond R1-R10.
def test_invalid_employee_and_duplicate(ctx):
    data = employee("EMP1030")
    data.pop("created_at")
    call(ctx, "POST", "/employees", 201, json=data)
    call(ctx, "POST", "/employees", 409, json=data)
    call(ctx, "POST", "/employees", 422, json={**data, "emp_code": "INVALID"})
    call(ctx, "POST", "/employees", 422, json={**data, "emp_code": "EMP1031", "shift_end": "09:30"})
    call(ctx, "POST", "/attendance/punch-in", 404,
         json={"emp_code": "EMP999999", "punched_at": ms("2026-07-06", "09:30")})


def test_concurrent_punch_ins(ctx):
    seed(ctx, employee("EMP1040"))
    payload = {"emp_code": "EMP1040", "punched_at": ms("2026-07-06", "09:30")}
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: ctx[0].post("/attendance/punch-in", json=payload).status_code, range(8)))
    assert sorted(codes) == [201] + [409] * 7, codes
    assert ctx[1].attendance_logs.count_documents({"emp_code": "EMP1040"}) == 1


def test_concurrent_punch_outs(ctx):
    seed(ctx, employee("EMP1041"))
    call(ctx, "POST", "/attendance/punch-in", 201,
         json={"emp_code": "EMP1041", "punched_at": ms("2026-07-06", "09:30")})
    payload = {"emp_code": "EMP1041", "punched_at": ms("2026-07-06", "18:30")}
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda _: ctx[0].post("/attendance/punch-out", json=payload).status_code, range(2)))
    # Contract demands exactly one 200 and one 409 (not 404).
    assert sorted(codes) == [200, 409], codes


def test_leaderboard_competition_rank_and_ties(ctx):
    seed(ctx, *(employee(f"EMP{1050+i}", dept="RankDept") for i in range(4)))
    ctx[1].attendance_logs.insert_many([
        record("EMP1050", "2026-07-06", late=40),
        record("EMP1051", "2026-07-06", late=30),
        record("EMP1052", "2026-07-06", late=30),
        record("EMP1053", "2026-07-06", late=20),
    ])
    r = call(ctx, "GET", "/analytics/leaderboard/late?month=2026-07&department=RankDept&limit=2")
    assert [(x["emp_code"], x["rank"]) for x in r["items"]] == [
        ("EMP1050", 1), ("EMP1051", 2), ("EMP1052", 2)]


def test_trend_gap_filling_and_weekend(ctx):
    seed(ctx, employee("EMP1060", dept="TrendDept"))
    ctx[1].attendance_logs.insert_one(record("EMP1060", "2026-07-03"))
    r = call(ctx, "GET", "/analytics/departments/TrendDept/trend?from=2026-07-03&to=2026-07-07")
    items = r["items"]
    assert [x["date"] for x in items] == [f"2026-07-{d:02d}" for d in range(3, 8)]
    assert [x["is_working_day"] for x in items] == [True, False, False, True, True]
    assert items[0]["attendance_rate"] == 1.0
    assert items[1]["attendance_rate"] is None
    assert items[3]["attendance_rate"] == 0.0


def test_regularization_audit_history(ctx):
    seed(ctx, employee("EMP1070"))
    call(ctx, "POST", "/attendance/punch-in", 201,
         json={"emp_code": "EMP1070", "punched_at": ms("2026-07-06", "09:30")})
    r = call(ctx, "PATCH", "/attendance/EMP1070/2026-07-06", 200,
             json={"reason": "Corrected shift record", "regularized_by": "Test HR", "status": "LEAVE"})
    assert r["status"] == "LEAVE" and r["punch_in"] is None
    assert len(r["history"]) == 1
    assert r["history"][0]["changes"]["status"] == {"from": "PRESENT", "to": "LEAVE"}
    call(ctx, "PATCH", "/attendance/EMP1070/2026-07-06", 422,
         json={"reason": "Nothing to change", "regularized_by": "Test HR", "status": "LEAVE"})


def test_timestamp_type_validation(ctx):
    seed(ctx, employee("EMP1080"))
    for invalid in ["1783348200000", 1783348200, -1, True]:
        call(ctx, "POST", "/attendance/punch-in", 422,
             json={"emp_code": "EMP1080", "punched_at": invalid})


def test_explain_endpoint_exists(ctx):
    r = call(ctx, "GET", "/admin/explain/attendance_list", 200)
    assert isinstance(r, dict)
    # No IXSCAN assertion here: empty test DB may legitimately use a collection scan.
