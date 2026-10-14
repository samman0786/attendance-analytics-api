import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient, ASCENDING, DESCENDING

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# --------------------------------------------------------------------------- #
# Startup: Create Indexes
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def configure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING)])

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def truncate_to_seconds(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(microsecond=0)

def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.astimezone(IST).replace(hour=h, minute=m, second=0, microsecond=0)
    diff_minutes = int((punch_in.astimezone(IST) - start).total_seconds() / 60)
    return diff_minutes if diff_minutes > 10 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    hours = (punch_out - punch_in).total_seconds() / 3600.0
    return round(hours + 1e-9, 2)

def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end_dt = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    diff = int((punch_out.astimezone(IST) - end_dt).total_seconds() / 60)
    return diff if diff >= 30 else 0

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class AttendancePatch(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str

# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command('ping')
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    page_size = min(max(1, page_size), 100)
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    
    ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc) if body.punched_at else datetime.now(timezone.utc)
    ts = truncate_to_seconds(ts)
    d = ts.astimezone(IST).date().isoformat()
    
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    
    late_mins = compute_late_minutes(ts, emp.get("shift_start", "09:30")) if body.status in ["PRESENT", "WFH", "ON_DUTY"] else 0
    
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    
    ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc) if body.punched_at else datetime.now(timezone.utc)
    ts = truncate_to_seconds(ts)
    d = ts.astimezone(IST).date().isoformat()
    
    log = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d})
    if not log:
        raise HTTPException(404, "No punch-in record found for today")
    if log.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    
    p_in = log["punch_in"]
    wh = compute_work_hours(p_in, ts)
    ot = compute_overtime(ts, emp.get("shift_end", "18:30"), d)
    half = wh < 4.50
    
    db.attendance_logs.update_one(
        {"_id": log["_id"]},
        {"$set": {
            "punch_out": ts,
            "work_hours": wh,
            "overtime_minutes": ot,
            "half_day": half
        }}
    )
    
    updated = db.attendance_logs.find_one({"_id": log["_id"]})
    updated["id"] = str(updated.pop("_id"))
    return updated

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    page_size = min(max(1, page_size), 100)
    q = {}
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
        
    docs = list(db.attendance_logs.find(q).sort("date", DESCENDING))
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date}")
def patch_attendance(emp_code: str, date: str, body: AttendancePatch):
    log = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    emp = db.employees.find_one({"emp_code": emp_code})
    if not log or not emp:
        raise HTTPException(404, "Record or employee not found")
    
    changes = {}
    old_values = {}
    
    if body.status is not None and body.status != log.get("status"):
        old_values["status"] = log.get("status")
        changes["status"] = body.status
        log["status"] = body.status
        
    if body.punch_in is not None:
        new_pi = truncate_to_seconds(datetime.fromtimestamp(body.punch_in / 1000, tz=timezone.utc))
        if log.get("punch_in") != new_pi:
            old_values["punch_in"] = int(log["punch_in"].timestamp() * 1000) if log.get("punch_in") else None
            changes["punch_in"] = body.punch_in
            log["punch_in"] = new_pi
            log["late_minutes"] = compute_late_minutes(new_pi, emp.get("shift_start", "09:30"))
            
    if body.punch_out is not None:
        new_po = truncate_to_seconds(datetime.fromtimestamp(body.punch_out / 1000, tz=timezone.utc))
        if log.get("punch_out") != new_po:
            old_values["punch_out"] = int(log["punch_out"].timestamp() * 1000) if log.get("punch_out") else None
            changes["punch_out"] = body.punch_out
            log["punch_out"] = new_po
            
    if log.get("punch_in") and log.get("punch_out"):
        wh = compute_work_hours(log["punch_in"], log["punch_out"])
        ot = compute_overtime(log["punch_out"], emp.get("shift_end", "18:30"), date)
        log["work_hours"] = wh
        log["overtime_minutes"] = ot
        log["half_day"] = wh < 4.50
        
    if changes:
        history_entry = {
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            "by": "manager",
            "reason": body.reason,
            "changes": {k: {"old": old_values[k], "new": changes[k]} for k in changes}
        }
        db.attendance_logs.update_one(
            {"_id": log["_id"]},
            {"$set": {
                "status": log["status"],
                "punch_in": log.get("punch_in"),
                "punch_out": log.get("punch_out"),
                "work_hours": log.get("work_hours"),
                "late_minutes": log.get("late_minutes"),
                "overtest_minutes": log.get("overtime_minutes", 0),
                "half_day": log.get("half_day", False)
            },
            "$push": {"history": history_entry}}
        )
        
    updated = db.attendance_logs.find_one({"_id": log["_id"]})
    updated["id"] = str(updated.pop("_id"))
    return updated

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(emp_code: str, month: str = Query(..., regex=r"^\d{4}-\d{2}$")):
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
        {"$group": {
            "_id": "$emp_code",
            "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "present_days": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, 1, 0]}},
            "half_days": {"$sum": {"$cond": ["$half_day", 1, 0]}},
            "absent_days": {"$sum": {"$cond": [{"$eq": ["$status", "ABSENT"]}, 1, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        }}
    ]
    res = list(db.attendance_logs.aggregate(pipeline))
    if not res:
        return {
            "emp_code": emp_code, "month": month, "total_work_hours": 0.0,
            "total_overtime_minutes": 0, "total_late_minutes": 0, "present_days": 0.0,
            "absent_days": 0, "leave_days": 0
        }
    r = res[0]
    r["present_days"] = float(r["present_days"] - 0.5 * r["half_days"])
    r.pop("_id", None)
    r["emp_code"] = emp_code
    r["month"] = month
    r.pop("half_days", None)
    return r

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., regex=r"^\d{4}-\d{2}$"), department: Optional[str] = None):
    match_emp = {"department": department} if department else {}
    pipeline = [
        {"$match": match_emp},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ecode": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$and": [
                        {"$eq": ["$emp_code", "$$ecode"]},
                        {"$regexMatch": {"input": "$date", "regex": f"^{month}"}}
                    ]}
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$addToSet": "$emp_code"},
            "total_work_hours": {"$sum": {"$ifNull": ["$logs.work_hours", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$logs.overtime_minutes", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "records_count": {"$sum": {"$cond": [{"$ifNull": ["$logs.date", False]}, 1, 0]}}
        }},
        {"$project": {
            "department": "$_id",
            "headcount": {"$size": "$headcount"},
            "total_work_hours": 1,
            "total_overtime_minutes": 1,
            "total_late_minutes": 1,
            "average_work_hours": {"$cond": [{"$gt": ["$records_count", 0]}, {"$divide": ["$total_work_hours", "$records_count"]}, 0]}
        }}
    ]
    return list(db.employees.aggregate(pipeline))

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., regex=r"^\d{4}-\d{2}$"), limit: int = 10):
    pipeline = [
        {"$match": {"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$setWindowFields": {
            "sort": {"total_late_minutes": DESCENDING},
            "output": {"rank": {"$denseRank": {}}}
        }},
        {"$limit": limit},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$project": {
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
            "rank": 1,
            "_id": 0
        }}
    ]
    return list(db.attendance_logs.aggregate(pipeline))

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str, date_to: str):
    pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "logs"
        }},
        {"$unwind": "$logs"},
        {"$match": {"logs.date": {"$gte": date_from, "$lte": date_to}}},
        {"$group": {
            "_id": "$logs.date",
            "total_work_hours": {"$sum": {"$ifNull": ["$logs.work_hours", 0]}},
            "present_count": {"$sum": {"$cond": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, 1, 0]}}
        }},
        {"$sort": {"_id": ASCENDING}},
        {"$setWindowFields": {
            "sort": {"_id": ASCENDING},
            "output": {
                "moving_avg_hours": {
                    "$avg": "$total_work_hours",
                    "window": {"range": [-6, "current"], "unit": "day"}
                }
            }
        }},
        {"$project": {
            "date": "$_id",
            "total_work_hours": 1,
            "present_count": 1,
            "moving_avg_hours": {"$round": [{"$ifNull": ["$moving_avg_hours", 0]}, 2]},
            "_id": 0
        }}
    ]
    return list(db.employees.aggregate(pipeline))

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, month: Optional[str] = None, department: Optional[str] = None, emp_code: Optional[str] = None):
    if endpoint == "employee_monthly":
        db_cmd = db.attendance_logs.aggregate([
            {"$match": {"emp_code": emp_code or "", "date": {"$regex": f"^{month or '2025-01'}"}}},
            {"$group": {"_id": "$emp_code", "total": {"$sum": "$work_hours"}}}
        ])
    elif endpoint == "department_summary":
        db_cmd = db.employees.aggregate([
            {"$match": {"department": department} if department else {}},
            {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "l"}}
        ])
    elif endpoint == "late_leaderboard":
        db_cmd = db.attendance_logs.aggregate([
            {"$match": {"date": {"$regex": f"^{month or '2025-01'}"}, "late_minutes": {"$gt": 0}}},
            {"$sort": {"late_minutes": DESCENDING}}
        ])
    else:
        db_cmd = db.attendance_logs.find({"emp_code": emp_code} if emp_code else {})
        
    return db.command("explain", db_cmd.command if hasattr(db_cmd, "command") else {"find": "attendance_logs"}, verbosity="executionStats")