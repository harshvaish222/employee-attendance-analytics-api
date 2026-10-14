"""Employee Attendance & Analytics API, contract v2.0."""
import os
import re
import calendar
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, StrictInt
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=True)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
MIN_TS, MAX_TS = 100_000_000_000, 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status")
    logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("late_minutes", DESCENDING)], name="ix_attendance_date_status_late")
    logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_status_date_employee")


# ------------------------------ models --------------------------------------
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @field_validator("shift_end")
    @classmethod
    def shifts_differ(cls, value, info):
        if info.data.get("shift_start") == value:
            raise ValueError("shift_start must differ from shift_end")
        return value


def validate_epoch(v):
    if v is None:
        return None
    if type(v) is not int or not MIN_TS <= v <= MAX_TS:
        raise ValueError("must be an integer epoch-millisecond timestamp in the supported range")
    return v


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, v): return validate_epoch(v)


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls, v): return validate_epoch(v)


class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_ts(cls, v): return validate_epoch(v)


# ------------------------------ helpers -------------------------------------
def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def epoch_to_dt(ms):
    validate_epoch(ms)
    return datetime.fromtimestamp(ms / 1000, UTC).replace(microsecond=0)


def dt_to_epoch(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def round_half_up(value, places=2):
    if value is None: return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def parse_date(value):
    try: return date.fromisoformat(value)
    except (ValueError, TypeError): raise HTTPException(422, "date must be YYYY-MM-DD")


def parse_month(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y, m, calendar.monthrange(y, m)[1])
    return start, end


def as_ist(dt):
    if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def attendance_date(punch_dt, emp):
    local = as_ist(punch_dt)
    sh, eh = emp["shift_start"], emp["shift_end"]
    if eh <= sh and local.strftime("%H:%M") < eh:
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def shift_datetime(date_str, hhmm, next_day=False):
    d = date.fromisoformat(date_str) + (timedelta(days=1) if next_day else timedelta())
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST).astimezone(UTC)


def calc_late(punch_in, shift_start, record_date):
    start = shift_datetime(record_date, shift_start)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def calc_work_hours(punch_in, punch_out):
    return round_half_up((punch_out - punch_in).total_seconds() / 3600, 2)


def calc_overtime(punch_out, shift_end, shift_start, record_date):
    end = shift_datetime(record_date, shift_end, next_day=(shift_end <= shift_start))
    mins = int((punch_out - end).total_seconds()) // 60
    return mins if mins >= 30 else 0


def derived(emp, d, pin, pout):
    if pin is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = calc_late(pin, emp["shift_start"], d)
    if pout is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    hours = calc_work_hours(pin, pout)
    return {"late_minutes": late, "work_hours": hours,
            "overtime_minutes": calc_overtime(pout, emp["shift_end"], emp["shift_start"], d),
            "half_day": hours < 4.50}


def clean_employee(doc):
    doc = dict(doc); doc.pop("_id", None)
    doc["created_at"] = dt_to_epoch(doc["created_at"])
    return doc


def clean_log(doc):
    doc = dict(doc); doc.pop("_id", None)
    for k in ("punch_in", "punch_out"):
        doc[k] = dt_to_epoch(doc.get(k))
    doc.setdefault("history", [])
    doc.setdefault("half_day", False)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    for entry in doc["history"]:
        entry["at"] = dt_to_epoch(entry["at"])
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = dt_to_epoch(change[side])
    return doc


def working_days_expr(start_str, end_str):
    """MongoDB expression counting weekdays in inclusive ISO date strings."""
    start = {"$dateFromString": {"dateString": start_str}}
    end_exclusive = {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": end_str}},
                                   "unit": "day", "amount": 1}}
    return {"$size": {"$filter": {
        "input": {"$map": {"input": {"$range": [0, {"$dateDiff": {"startDate": start, "endDate": end_exclusive, "unit": "day"}}]},
                            "as": "n", "in": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$n"}}}},
        "as": "d", "cond": {"$and": [{"$gte": [{"$dayOfWeek": "$$d"}, 2]}, {"$lte": [{"$dayOfWeek": "$$d"}, 6]}]}}}}


def log_date_filter(start, end):
    return {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}


def explain_aggregate(collection, pipeline):
    return db.command({"explain": {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}},
                       "verbosity": "executionStats"})

@app.get("/", include_in_schema=False)
def home():
    """Provide a simple landing response for the API root URL."""
    return {
        "message": "Employee Attendance & Analytics API is running.",
        "docs": "/docs",
        "health": "/health",
    }

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = now_utc()
    try: employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return clean_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    items = [clean_employee(x) for x in employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "unknown employee")
    ts = epoch_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp)
    fields = {"emp_code": body.emp_code, "date": d, "status": body.status,
              "punch_in": ts, "punch_out": None, "work_hours": None,
              **derived(emp, d, ts, None), "history": []}
    # Unique compound index is the concurrency control; check-then-insert alone is unsafe.
    try: logs.insert_one(fields)
    except DuplicateKeyError: raise HTTPException(409, "already punched in for this date")
    return clean_log(fields)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "unknown employee")
    ts = epoch_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not rec: raise HTTPException(404, "no punch-in found")
    pin = rec.get("punch_in")
    if rec.get("punch_out") is not None: raise HTTPException(409, "record already punched out")
    if ts <= pin or (ts-pin).total_seconds() > 86400: raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    values = derived(emp, rec["date"], pin, ts)
    updated = logs.find_one_and_update({"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **values}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "record already punched out")
    return clean_log(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[date] = None,
                    date_to: Optional[date] = None, status: Optional[str] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if status is not None and status not in STATUSES: raise HTTPException(422, "invalid status")
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
    q = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from.isoformat()
        if date_to: q["date"]["$lte"] = date_to.isoformat()
    if status: q["status"] = status
    total = logs.count_documents(q)
    docs = logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items": [clean_log(x) for x in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: date, body: RegularizeIn):
    d = date.isoformat()
    emp = employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "unknown employee")
    rec = logs.find_one({"emp_code": emp_code, "date": d})
    if not rec: raise HTTPException(404, "attendance record not found")
    old = {"status": rec.get("status"), "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
           "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes", 0),
           "overtime_minutes": rec.get("overtime_minutes", 0), "half_day": rec.get("half_day", False)}
    new_status = body.status if body.status is not None else rec["status"]
    pin = epoch_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
    pout = epoch_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None: raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        pin = pout = None
    else:
        if pin is None: raise HTTPException(422, "presence status requires punch_in")
        if attendance_date(pin, emp) != d: raise HTTPException(422, "punch_in must stay on the attendance date")
        if pout is not None and (pout <= pin or (pout-pin).total_seconds() > 86400):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    values = {"status": new_status, "punch_in": pin, "punch_out": pout, **derived(emp, d, pin, pout)}
    changes = {}
    for key, value in values.items():
        previous = old.get(key)
        if previous != value:
            changes[key] = {"from": previous, "to": value}
    if not changes: raise HTTPException(422, "request does not change the record")
    hist = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-swap protects against concurrent edits losing history.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "history": rec.get("history", [])} if "history" in rec else {"_id": rec["_id"], "history": {"$exists": False}},
        {"$set": values, "$push": {"history": hist}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "record changed concurrently; retry")
    return clean_log(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    start, end = parse_month(month)
    # Employee-first lookup preserves employees with no attendance records.
    pipeline = [
      {"$match": {"emp_code": emp_code}},
      {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
        "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]},
          {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}}], "as": "records"}},
      {"$project": {"_id": 0, "emp_code": 1, "working_days": {"$cond": [{"$gt": ["$joined_on", end.isoformat()]}, 0,
          working_days_expr({"$max": ["$joined_on", start.isoformat()]}, end.isoformat())]},
        "records": 1, "joined_on": 1}},
      {"$project": {"emp_code": 1, "working_days": 1, "records": 1,
        "present_days": {"$sum": {"$map": {"input": "$records", "as": "r", "in": {"$cond": [
          {"$and": [{"$in": ["$$r.status", list(PRESENCE)]},
                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$$r.date", "1970-01-01"]}}}}, 2]},
                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$$r.date", "1970-01-01"]}}}}, 6]}]},
          {"$cond": [{"$ifNull": ["$$r.half_day", False]}, 0.5, 1]}, 0]}}}},
        "leave_days": {"$size": {"$filter": {"input": "$records", "as": "r", "cond": {"$eq": ["$$r.status", "LEAVE"]}}}},
        "late_count": {"$size": {"$filter": {"input": "$records", "as": "r", "cond": {"$gt": [{"$ifNull": ["$$r.late_minutes", 0]}, 0]}}}},
        "total_late_minutes": {"$sum": {"$map": {"input": "$records", "as": "r", "in": {"$ifNull": ["$$r.late_minutes", 0]}}}},
        "total_overtime_minutes": {"$sum": {"$map": {"input": "$records", "as": "r", "in": {"$ifNull": ["$$r.overtime_minutes", 0]}}}}}},
      {"$project": {"emp_code": 1, "month": {"$literal": month}, "working_days": 1, "present_days": 1, "leave_days": 1,
        "late_count": 1, "total_late_minutes": 1, "total_overtime_minutes": 1,
        "attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]},
          {"$divide": [{"$floor": [{"$add": [{"$multiply": [{"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]}, 100]}, 0.5]}]}, 100]}, None]}}}
    ]
    result = list(employees.aggregate(pipeline))
    if not result: raise HTTPException(404, "unknown employee")
    return result[0]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                       department: Optional[str] = None):
    start, end = parse_month(month)
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None: match["department"] = department
    pipeline = [
      {"$match": match},
      {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
        "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]},
          {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}}], "as": "records"}},
      {"$project": {"department": 1, "joined_on": 1, "records": 1,
        "working_days": working_days_expr({"$max": ["$joined_on", start.isoformat()]}, end.isoformat())}},
      {"$unwind": {"path": "$records", "preserveNullAndEmptyArrays": True}},
      {"$group": {"_id": "$department",
        "headcount": {"$addToSet": "$emp_code"},
        "present_days": {"$sum": {"$cond": [{"$and": [
          {"$in": ["$records.status", list(PRESENCE)]},
          {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$records.date", "1970-01-01"]}}}}, 2]},
          {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$records.date", "1970-01-01"]}}}}, 6]}]},
          {"$cond": [{"$ifNull": ["$records.half_day", False]}, 0.5, 1]}, 0]}},
        "hours": {"$push": {"$cond": [{"$and": [{"$in": ["$records.status", list(PRESENCE)]},
          {"$ne": ["$records.work_hours", None]}]}, "$records.work_hours", None]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$records.late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$records.late_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$records.status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$records.status", "ON_DUTY"]}, 1, 0]}}}},
      {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"},
        "present_days": 1, "avg_work_hours": {"$let": {"vars": {"valid": {"$filter": {"input": "$hours", "as": "h", "cond": {"$ne": ["$$h", None]}}}},
          "in": {"$cond": [{"$gt": [{"$size": "$$valid"}, 0]}, {"$divide": [{"$floor": [{"$add": [{"$multiply": [{"$avg": "$$valid"}, 100]}, 0.5]}]}, 100]}, None]}}},
        "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
      {"$match": {"headcount": {"$gt": 0}}}, {"$sort": {"department": 1}}
    ]
    # Month is required in the response and aggregation output is kept database-backed.
    items = list(employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = parse_month(month)
    pipeline = [
      {"$match": log_date_filter(start, end)},
      {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                  "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
      {"$match": {"total_late_minutes": {"$gt": 0}}},
      {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
      {"$unwind": "$employee"},
    ]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
      {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
      {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
      {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
         "output": {"rank": {"$rank": {}}}}},
      {"$match": {"rank": {"$lte": limit}}},
      {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}},
      {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ]
    return {"month": month, "items": list(logs.aggregate(pipeline))}



@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: date = Query(..., alias="from"),
                     to_date: date = Query(..., alias="to")):
    start, end = from_date, to_date
    if end < start: raise HTTPException(422, "to must be on or after from")
    days = (end - start).days + 1
    if days > 92: raise HTTPException(422, "date range cannot exceed 92 days")
    if not employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "unknown department")
    # Gap-fill entirely in MongoDB: $documents creates the calendar, lookups use indexed collections.
    pipeline = [
      {"$documents": [{"seed": 1}]},
      {"$project": {"dates": {"$map": {"input": {"$range": [0, days]},
        "as": "i", "in": {"$dateToString": {"format": "%Y-%m-%d",
          "date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start.isoformat()}},
            "unit": "day", "amount": "$$i"}}}}}}}},
      {"$unwind": "$dates"},
      {"$set": {"date": "$dates", "date_value": {"$dateFromString": {"dateString": "$dates"}}}},
      {"$lookup": {"from": "employees", "let": {"day": "$date"},
        "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$department", department]},
          {"$lte": ["$joined_on", "$$day"]}]}}}, {"$project": {"_id": 0, "emp_code": 1}},],
        "as": "eligible_employees"}},
      {"$set": {"headcount": {"$size": "$eligible_employees"}}},
      {"$lookup": {"from": "attendance_logs", "let": {"day": "$date",
                    "codes": "$eligible_employees.emp_code"},
        "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]},
          {"$in": ["$emp_code", "$$codes"]}]}}},
          {"$group": {"_id": None,
            "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]},
              {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}],
        "as": "day_logs"}},
      {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$day_logs.present_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$day_logs.late_count", 0]}, 0]},
                "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$date_value"}, 2]},
                                            {"$lte": [{"$dayOfWeek": "$date_value"}, 6]}]}}},
      {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
        {"$divide": ["$present_count", "$headcount"]}, None]}}},
      {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
        "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}}}},
      {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
        "present_count": 1, "late_count": 1,
        "attendance_rate": {"$cond": [{"$ne": ["$attendance_rate", None]},
          {"$floor": [{"$add": [{"$multiply": ["$attendance_rate", 10000]}, 0.5]}]}, None]},
        "moving_avg_7d": {"$cond": [{"$ne": ["$moving_avg_7d", None]},
          {"$floor": [{"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}]}, None]}}},
      {"$set": {"attendance_rate": {"$cond": [{"$ne": ["$attendance_rate", None]},
        {"$divide": ["$attendance_rate", 10000]}, None]},
        "moving_avg_7d": {"$cond": [{"$ne": ["$moving_avg_7d", None]},
        {"$divide": ["$moving_avg_7d", 10000]}, None]}}},
      {"$sort": {"date": 1}}
    ]
    items = list(db.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[str] = None,
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if status:
            if status not in STATUSES: raise HTTPException(422, "invalid status")
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from.isoformat()
            if date_to: q["date"]["$lte"] = date_to.isoformat()
        # Use the same filter, sort and pagination as GET /attendance.
        # PyMongo's Cursor.explain() does not accept a verbosity argument, so
        # issue the MongoDB explain command directly to request executionStats.
        find_command = {
            "find": logs.name,
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        return db.command("explain", find_command, verbosity="executionStats")
    
    if endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        start, end = parse_month(month)
        pipeline = [{"$match": {"emp_code": emp_code}},
          {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]},
              {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}}], "as": "records"}}]
        return explain_aggregate(employees, pipeline)
    if endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        start, end = parse_month(month)
        match = {"joined_on": {"$lte": end.isoformat()}}
        if department is not None: match["department"] = department
        pipeline = [{"$match": match}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
          "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]},
            {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}}], "as": "records"}}]
        return explain_aggregate(employees, pipeline)
    if endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        start, end = parse_month(month)
        pipeline = [{"$match": log_date_filter(start, end)}, {"$group": {"_id": "$emp_code",
          "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}}]
        return explain_aggregate(logs, pipeline)
    if endpoint == "department_trend":
        if not department or from_date is None or to_date is None:
            raise HTTPException(422, "department, from and to are required")
        if to_date < from_date or (to_date-from_date).days >= 92:
            raise HTTPException(422, "invalid trend date range")
        # Same range filter used by the trend's attendance lookup.
        pipeline = [{"$match": {"date": {"$gte": from_date.isoformat(), "$lte": to_date.isoformat()}}},
                    {"$sort": {"date": 1, "emp_code": 1}}]
        return explain_aggregate(logs, pipeline)
    raise HTTPException(422, "unknown endpoint")
