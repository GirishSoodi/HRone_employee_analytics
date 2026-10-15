"""PeoplePulse R1-R10 integration suite. Requires a REAL MongoDB 6.0+.

Run from repository root:
  pip install pytest httpx pymongo
  MONGO_URI=mongodb://localhost:27017 python -m pytest -v test_r1_r10.py

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
from decimal import Decimal, ROUND_HALF_UP

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

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
    uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
    db_name = "peoplepulse_r1r10_test_" + uuid.uuid4().hex[:12]
    admin = MongoClient(uri, serverSelectionTimeoutMS=4000)
    try:
        admin.admin.command("ping")
    except Exception as exc:
        pytest.fail(f"MongoDB not reachable at MONGO_URI: {exc}")
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