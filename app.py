"""Smart Attendance System - server.

Run locally:   python app.py
Run on Render: gunicorn app:app
All state lives in MongoDB, so the server can restart or sleep safely.
"""
import csv
import io
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory, session
from pymongo import MongoClient
from pymongo.errors import BulkWriteError, DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------- settings
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = os.environ.get("DB_NAME", "attendance_v2")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")        # hard-coded login
ADMIN_PASS = os.environ.get("ADMIN_PASS", "password")
NUM_SEMESTERS = 7
ONLINE_WINDOW_S = 45        # device counts as online if heard within this time
DISCOVERY_MINUTES = 5       # how long "Add new device" keeps listening
ASSIGN_SECONDS = 120        # how long "Assign card" waits for a tap
IST = timezone(timedelta(hours=5, minutes=30))

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "attendance-secret-change-me")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
db = client[DB_NAME]
devices = db.devices
students = db.students
subjects = db.subjects
sessions = db.sessions
attendance = db.attendance
settings = db.settings


def init_indexes():
    try:
        devices.create_index("device_id", unique=True)
        students.create_index("enrollment", unique=True)
        students.create_index("uid")
        subjects.create_index("id", unique=True)
        sessions.create_index("id", unique=True)
        sessions.create_index([("device_id", 1), ("status", 1)])
        attendance.create_index([("session_id", 1), ("enrollment", 1)], unique=True)
        print("Database ready:", DB_NAME)
    except Exception as e:  # keep the server up even if Atlas is slow
        print("Could not create indexes (check MONGO_URI / network access):", e)


init_indexes()


# ---------------------------------------------------------------- helpers
def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC


def iso(dt):
    return dt.isoformat() + "Z" if dt else None


def to_ist(dt):
    return dt.replace(tzinfo=timezone.utc).astimezone(IST)


def jsonable(x):
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items() if k != "_id"}
    if isinstance(x, list):
        return [jsonable(v) for v in x]
    if isinstance(x, datetime):
        return iso(x)
    return x


def body():
    return request.get_json(force=True, silent=True) or {}


def err(msg, code=400):
    return jsonify(error=msg), code


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def enr_key(e):
    """Sort enrollment numbers ascending, numbers by value (2 before 10), text A-Z."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", str(e))]


def valid_sem(n):
    return n is not None and 1 <= n <= NUM_SEMESTERS


def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not session.get("admin"):
            return err("Please log in", 401)
        return f(*a, **k)
    return wrapper


def is_online(dev):
    ls = dev.get("last_seen")
    return bool(ls) and (now() - ls).total_seconds() <= ONLINE_WINDOW_S


def discovery_active():
    d = settings.find_one({"_id": "discovery"})
    return bool(d) and d["until"] > now()


# ---------------------------------------------------------------- sessions
def close_session(s, end_time):
    """Close a lecture and record everyone in the semester who didn't tap as ABSENT."""
    res = sessions.update_one({"id": s["id"], "status": "active"},
                              {"$set": {"status": "closed", "end": end_time}})
    if res.modified_count == 0:
        return
    present = {a["enrollment"] for a in attendance.find({"session_id": s["id"]}, {"enrollment": 1})}
    docs = []
    for st in students.find({"semester": s["semester"]}):
        if st["enrollment"] not in present:
            docs.append(att_doc(s, st, "ABSENT", None, st.get("uid")))
    if docs:
        try:
            attendance.insert_many(docs, ordered=False)
        except BulkWriteError:
            pass


def close_expired():
    for s in sessions.find({"status": "active", "planned_end": {"$lte": now()}}):
        close_session(s, s["planned_end"])


def att_doc(s, st, status, t, uid):
    return {
        "session_id": s["id"], "enrollment": st["enrollment"], "name": st["name"],
        "semester": s["semester"], "division": st.get("division", ""),
        "subject_id": s["subject_id"], "subject_name": s["subject_name"],
        "device_name": s["device_name"], "date": s["date"], "session_start": s["start"],
        "status": status, "time": t, "uid": uid, "note": "", "corrected": False,
    }


def mark_present(s, st, uid, t):
    existing = attendance.find_one({"session_id": s["id"], "enrollment": st["enrollment"]})
    if existing:
        if existing["status"] == "PRESENT":
            return "already_marked"
        attendance.update_one({"session_id": s["id"], "enrollment": st["enrollment"]},
                              {"$set": {"status": "PRESENT", "time": t, "uid": uid}})
        return "marked"
    try:
        attendance.insert_one(att_doc(s, st, "PRESENT", t, uid))
        return "marked"
    except DuplicateKeyError:
        return "already_marked"


# ---------------------------------------------------------------- pages / auth
@app.route("/")
def index():
    return send_from_directory(os.path.join(app.root_path, "templates"), "dashboard.html")


@app.route("/health")
def health():
    return "ok"


@app.post("/api/login")
def login():
    b = body()
    if b.get("username") == ADMIN_USER and b.get("password") == ADMIN_PASS:
        session["admin"] = True
        return jsonify(ok=True)
    return err("Wrong username or password", 401)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def me():
    return jsonify(logged_in=bool(session.get("admin")), num_semesters=NUM_SEMESTERS)


# ---------------------------------------------------------------- ESP32 endpoints
@app.post("/api/device/hello")
def device_hello():
    """Heartbeat + first contact. Sent by the ESP32 every ~15 s."""
    close_expired()
    did = str(body().get("device_id", "")).strip()[:64]
    if not did:
        return err("device_id required")
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    dev = devices.find_one({"device_id": did})
    if dev:
        devices.update_one({"device_id": did}, {"$set": {"last_seen": now(), "ip": ip}})
        return jsonify(status=dev["status"])
    if discovery_active():
        try:
            devices.insert_one({"device_id": did, "name": "", "status": "pending",
                                "first_seen": now(), "last_seen": now(), "ip": ip})
        except DuplicateKeyError:
            pass
        return jsonify(status="pending")
    return jsonify(status="unknown")


@app.post("/api/device/tap")
def device_tap():
    close_expired()
    b = body()
    did = str(b.get("device_id", "")).strip()
    uid = str(b.get("uid", "")).upper().strip()
    if not did or not uid:
        return err("device_id and uid required")

    dev = devices.find_one({"device_id": did, "status": "active"})
    if not dev:
        return jsonify(result="unknown_device")
    devices.update_one({"device_id": did}, {"$set": {"last_seen": now()}})

    # tap time: trust the device clock if it looks sane (buffered taps carry old times)
    t = now()
    ts = to_int(b.get("ts"))
    if ts and ts > 1_700_000_000:
        dt = datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
        if now() - timedelta(days=2) <= dt <= now() + timedelta(seconds=60):
            t = dt
    live = abs((now() - t).total_seconds()) < 20

    # 1) is a card assignment waiting for a tap on this device?
    a = settings.find_one({"_id": "assign"})
    if live and a and a.get("state") == "waiting" and a["device_id"] == did and a["until"] > now():
        return handle_assign(a, uid)

    # 2) normal attendance tap
    st = students.find_one({"uid": uid})
    if not st:
        if students.find_one({"card_history.uid": uid}):
            return jsonify(result="card_disabled")
        return jsonify(result="unknown_card")

    s = sessions.find_one(
        {"device_id": did, "start": {"$lte": t}, "planned_end": {"$gte": t},
         "$or": [{"end": None}, {"end": {"$gte": t}}]},
        sort=[("start", -1)])
    if not s:
        return jsonify(result="no_lecture")          # ignored on purpose
    if st["semester"] != s["semester"]:
        return jsonify(result="wrong_semester", name=st["name"])
    return jsonify(result=mark_present(s, st, uid, t), name=st["name"])


def handle_assign(a, uid):
    owner = students.find_one({"uid": uid})
    if owner and owner["enrollment"] != a["enrollment"]:
        msg = f"This card already belongs to {owner['name']} ({owner['enrollment']})."
        settings.update_one({"_id": "assign"}, {"$set": {"state": "error", "message": msg}})
        return jsonify(result="card_in_use")
    st = students.find_one({"enrollment": a["enrollment"]})
    if not st:
        settings.update_one({"_id": "assign"}, {"$set": {"state": "error", "message": "Student not found."}})
        return jsonify(result="student_missing")
    if st.get("uid") != uid:
        upd = {"$set": {"uid": uid}}
        if st.get("uid"):
            upd["$push"] = {"card_history": {"uid": st["uid"], "replaced": now()}}
        students.update_one({"enrollment": st["enrollment"]}, upd)
    settings.update_one({"_id": "assign"}, {"$set": {
        "state": "done", "uid": uid, "message": f"Card assigned to {st['name']}."}})
    return jsonify(result="card_assigned", name=st["name"])


# ---------------------------------------------------------------- devices (dashboard)
@app.get("/api/devices")
@login_required
def list_devices():
    close_expired()
    active, pending = [], []
    for d in devices.find().sort("name", 1):
        item = {"device_id": d["device_id"], "name": d.get("name", ""), "status": d["status"],
                "last_seen": iso(d.get("last_seen")), "online": is_online(d), "ip": d.get("ip")}
        if d["status"] == "active":
            active.append(item)
        elif is_online(d):
            pending.append(item)
    disc = settings.find_one({"_id": "discovery"})
    return jsonify(devices=active, pending=pending,
                   discovering=bool(disc) and disc["until"] > now(),
                   discovery_until=iso(disc["until"]) if disc else None)


@app.post("/api/devices/discover")
@login_required
def start_discovery():
    until = now() + timedelta(minutes=DISCOVERY_MINUTES)
    settings.replace_one({"_id": "discovery"}, {"_id": "discovery", "until": until}, upsert=True)
    devices.delete_many({"status": "pending"})
    return jsonify(ok=True, until=iso(until))


@app.post("/api/devices/stop-discovery")
@login_required
def stop_discovery():
    settings.replace_one({"_id": "discovery"}, {"_id": "discovery", "until": now()}, upsert=True)
    devices.delete_many({"status": "pending"})
    return jsonify(ok=True)


@app.post("/api/devices/accept")
@login_required
def accept_device():
    b = body()
    name = (b.get("name") or "").strip()
    if not name:
        return err("Give the device a name, for example Room 101")
    r = devices.update_one({"device_id": b.get("device_id"), "status": "pending"},
                           {"$set": {"status": "active", "name": name}})
    return jsonify(ok=True) if r.matched_count else err("Device not found - it may have timed out", 404)


@app.put("/api/devices/<device_id>")
@login_required
def rename_device(device_id):
    name = (body().get("name") or "").strip()
    if not name:
        return err("Name required")
    devices.update_one({"device_id": device_id}, {"$set": {"name": name}})
    return jsonify(ok=True)


@app.delete("/api/devices/<device_id>")
@login_required
def remove_device(device_id):
    if sessions.find_one({"device_id": device_id, "status": "active"}):
        return err("Close the open attendance on this device first")
    devices.delete_one({"device_id": device_id})
    return jsonify(ok=True)


# ---------------------------------------------------------------- subjects
@app.get("/api/subjects")
@login_required
def list_subjects():
    q = {}
    sem = to_int(request.args.get("semester"))
    if sem:
        q["semester"] = sem
    if not request.args.get("include_hidden"):
        q["hidden"] = {"$ne": True}
    return jsonify(jsonable(list(subjects.find(q).sort([("semester", 1), ("name", 1)]))))


@app.post("/api/subjects")
@login_required
def add_subject():
    b = body()
    name = (b.get("name") or "").strip()
    sem = to_int(b.get("semester"))
    if not name or not valid_sem(sem):
        return err(f"Subject name and a semester (1-{NUM_SEMESTERS}) are required")
    dup = subjects.find_one({"semester": sem, "hidden": {"$ne": True},
                             "name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}})
    if dup:
        return err("This subject already exists in that semester")
    doc = {"id": uuid.uuid4().hex[:10], "semester": sem, "name": name, "hidden": False, "created": now()}
    subjects.insert_one(doc)
    return jsonify(jsonable(doc))


@app.put("/api/subjects/<sid>")
@login_required
def rename_subject(sid):
    name = (body().get("name") or "").strip()
    if not name:
        return err("Name required")
    subjects.update_one({"id": sid}, {"$set": {"name": name}})
    sessions.update_many({"subject_id": sid}, {"$set": {"subject_name": name}})
    attendance.update_many({"subject_id": sid}, {"$set": {"subject_name": name}})
    return jsonify(ok=True)


@app.delete("/api/subjects/<sid>")
@login_required
def hide_subject(sid):
    """Hide instead of delete so old reports keep working."""
    subjects.update_one({"id": sid}, {"$set": {"hidden": True}})
    return jsonify(ok=True)


# ---------------------------------------------------------------- students
@app.get("/api/students")
@login_required
def list_students():
    q = {}
    sem = to_int(request.args.get("semester"))
    if sem:
        q["semester"] = sem
    out = []
    for s in sorted(students.find(q), key=lambda r: enr_key(r["enrollment"])):
        out.append({"enrollment": s["enrollment"], "name": s["name"], "semester": s["semester"],
                    "division": s.get("division", ""), "uid": s.get("uid"), "has_card": bool(s.get("uid"))})
    return jsonify(out)


@app.post("/api/students")
@login_required
def add_student():
    b = body()
    name = (b.get("name") or "").strip()
    enr = (b.get("enrollment") or "").strip()
    sem = to_int(b.get("semester"))
    if not name or not enr or not valid_sem(sem):
        return err(f"Name, enrollment number and semester (1-{NUM_SEMESTERS}) are required")
    if "/" in enr or "\\" in enr:
        return err("Enrollment number cannot contain / or \\")
    try:
        students.insert_one({"enrollment": enr, "name": name, "semester": sem,
                             "division": (b.get("division") or "").strip(), "card_history": [],
                             "created": now()})
    except DuplicateKeyError:
        return err("A student with this enrollment number already exists")
    return jsonify(ok=True)


@app.put("/api/students/<enr>")
@login_required
def edit_student(enr):
    b = body()
    upd = {}
    if (b.get("name") or "").strip():
        upd["name"] = b["name"].strip()
    if "division" in b:
        upd["division"] = (b.get("division") or "").strip()
    if b.get("semester") is not None:
        sem = to_int(b["semester"])
        if not valid_sem(sem):
            return err("Invalid semester")
        upd["semester"] = sem
    if not upd:
        return err("Nothing to change")
    if not students.update_one({"enrollment": enr}, {"$set": upd}).matched_count:
        return err("Student not found", 404)
    if "name" in upd:
        attendance.update_many({"enrollment": enr}, {"$set": {"name": upd["name"]}})
    return jsonify(ok=True)


@app.delete("/api/students/<enr>")
@login_required
def delete_student(enr):
    students.delete_one({"enrollment": enr})      # past attendance records stay
    return jsonify(ok=True)


# ---------------------------------------------------------------- card assignment
@app.post("/api/students/<enr>/assign")
@login_required
def assign_start(enr):
    st = students.find_one({"enrollment": enr})
    if not st:
        return err("Student not found", 404)
    dev = devices.find_one({"device_id": body().get("device_id"), "status": "active"})
    if not dev:
        return err("Choose a registered device")
    if not is_online(dev):
        return err(f"{dev['name']} is offline")
    settings.replace_one({"_id": "assign"}, {
        "_id": "assign", "enrollment": enr, "student_name": st["name"], "device_id": dev["device_id"],
        "device_name": dev["name"], "state": "waiting", "message": "", "until": now() + timedelta(seconds=ASSIGN_SECONDS),
    }, upsert=True)
    return jsonify(ok=True, seconds=ASSIGN_SECONDS)


@app.get("/api/assign/status")
@login_required
def assign_status():
    a = settings.find_one({"_id": "assign"})
    if not a:
        return jsonify(state="idle")
    if a["state"] == "waiting" and a["until"] <= now():
        return jsonify(state="timeout", message="No card was tapped in time.")
    return jsonify(state=a["state"], message=a.get("message", ""), student=a.get("student_name"),
                   device=a.get("device_name"), enrollment=a.get("enrollment"))


@app.post("/api/assign/cancel")
@login_required
def assign_cancel():
    settings.delete_one({"_id": "assign"})
    return jsonify(ok=True)


# ---------------------------------------------------------------- lectures
def session_view(s):
    recs = list(attendance.find({"session_id": s["id"]}))
    present = sorted([r for r in recs if r["status"] == "PRESENT"], key=lambda r: enr_key(r["enrollment"]))
    present_ids = {r["enrollment"] for r in present}
    roster = sorted(students.find({"semester": s["semester"]}), key=lambda r: enr_key(r["enrollment"]))
    waiting = [{"name": r["name"], "enrollment": r["enrollment"]} for r in roster if r["enrollment"] not in present_ids]
    return {
        "id": s["id"], "device_name": s["device_name"], "subject_name": s["subject_name"],
        "semester": s["semester"], "start": iso(s["start"]), "planned_end": iso(s["planned_end"]),
        "present": [{"name": r["name"], "enrollment": r["enrollment"], "time": iso(r.get("time"))} for r in present],
        "waiting": waiting, "roster_size": len(roster),
    }


@app.post("/api/sessions/start")
@login_required
def start_session():
    close_expired()
    b = body()
    dev = devices.find_one({"device_id": b.get("device_id"), "status": "active"})
    if not dev:
        return err("Choose a registered device")
    if not is_online(dev):
        return err(f"{dev['name']} is offline. Check its power and WiFi, then try again.")
    if sessions.find_one({"device_id": dev["device_id"], "status": "active"}):
        return err(f"{dev['name']} already has attendance open. Close it first.")
    sub = subjects.find_one({"id": b.get("subject_id"), "hidden": {"$ne": True}})
    if not sub:
        return err("Choose a subject")
    minutes = max(5, min(480, to_int(b.get("minutes")) or 20))
    start = now()
    s = {"id": uuid.uuid4().hex[:12], "device_id": dev["device_id"], "device_name": dev["name"],
         "semester": sub["semester"], "subject_id": sub["id"], "subject_name": sub["name"],
         "start": start, "planned_end": start + timedelta(minutes=minutes), "end": None,
         "status": "active", "date": to_ist(start).strftime("%Y-%m-%d")}
    sessions.insert_one(s)
    return jsonify(ok=True, id=s["id"])


@app.get("/api/sessions/active")
@login_required
def active_sessions():
    close_expired()
    return jsonify([session_view(s) for s in sessions.find({"status": "active"}).sort("start", 1)])


@app.post("/api/sessions/<sid>/end")
@login_required
def end_session(sid):
    s = sessions.find_one({"id": sid, "status": "active"})
    if not s:
        return err("Attendance not found or already closed", 404)
    close_session(s, now())
    return jsonify(ok=True)


# ---------------------------------------------------------------- reports
def date_query(q):
    df, dt = request.args.get("date_from"), request.args.get("date_to")
    if df or dt:
        q["date"] = {}
        if df:
            q["date"]["$gte"] = df
        if dt:
            q["date"]["$lte"] = dt


@app.get("/api/reports/sessions")
@login_required
def report_sessions():
    close_expired()
    q = {"status": "closed"}
    if to_int(request.args.get("semester")):
        q["semester"] = to_int(request.args["semester"])
    if request.args.get("subject_id"):
        q["subject_id"] = request.args["subject_id"]
    date_query(q)
    sess = list(sessions.find(q).sort("start", -1).limit(300))
    counts = {}
    if sess:
        for g in attendance.aggregate([
                {"$match": {"session_id": {"$in": [s["id"] for s in sess]}}},
                {"$group": {"_id": {"s": "$session_id", "st": "$status"}, "n": {"$sum": 1}}}]):
            counts.setdefault(g["_id"]["s"], {})[g["_id"]["st"]] = g["n"]
    out = []
    for s in sess:
        c = counts.get(s["id"], {})
        p, a = c.get("PRESENT", 0), c.get("ABSENT", 0)
        out.append({"id": s["id"], "date": s["date"], "start": iso(s["start"]), "end": iso(s["end"]),
                    "subject_name": s["subject_name"], "semester": s["semester"],
                    "device_name": s["device_name"], "present": p, "total": p + a,
                    "percent": round(100 * p / (p + a)) if p + a else 0})
    return jsonify(out)


@app.get("/api/reports/session/<sid>")
@login_required
def report_session(sid):
    s = sessions.find_one({"id": sid})
    if not s:
        return err("Lecture not found", 404)
    recs = sorted(attendance.find({"session_id": sid}), key=lambda r: enr_key(r["enrollment"]))
    return jsonify(session=jsonable({k: s[k] for k in ("id", "subject_name", "semester", "device_name", "date", "start", "end", "status")}),
                   records=jsonable([{k: r.get(k) for k in ("enrollment", "name", "division", "status", "time", "note", "corrected")} for r in recs]))


@app.put("/api/attendance")
@login_required
def correct_attendance():
    b = body()
    status = str(b.get("status", "")).upper()
    if status not in ("PRESENT", "ABSENT"):
        return err("Status must be PRESENT or ABSENT")
    r = attendance.update_one({"session_id": b.get("session_id"), "enrollment": b.get("enrollment")},
                              {"$set": {"status": status, "note": (b.get("note") or "").strip(),
                                        "corrected": True, "corrected_at": now()}})
    return jsonify(ok=True) if r.matched_count else err("Record not found", 404)


def subject_matrix(subject_id):
    q = {"subject_id": subject_id, "status": "closed"}
    date_query(q)
    sess = list(sessions.find(q).sort("start", 1))
    ids = [s["id"] for s in sess]
    rows = {}
    for r in attendance.find({"session_id": {"$in": ids}}):
        row = rows.setdefault(r["enrollment"], {"enrollment": r["enrollment"], "name": r["name"],
                                                "division": r.get("division", ""), "marks": {}})
        row["marks"][r["session_id"]] = "P" if r["status"] == "PRESENT" else "A"
    out = []
    for row in sorted(rows.values(), key=lambda x: enr_key(x["enrollment"])):
        marks = [row["marks"].get(i, "-") for i in ids]
        p = marks.count("P")
        total = p + marks.count("A")
        out.append({"enrollment": row["enrollment"], "name": row["name"], "division": row["division"],
                    "marks": marks, "present": p, "total": total,
                    "percent": round(100 * p / total) if total else 0})
    cols = [{"id": s["id"], "label": to_ist(s["start"]).strftime("%d-%m %H:%M")} for s in sess]
    return cols, out


@app.get("/api/reports/subject")
@login_required
def report_subject():
    cols, rows = subject_matrix(request.args.get("subject_id", ""))
    return jsonify(columns=cols, rows=rows)


def student_records(enr, direction):
    """A student's records from closed lectures, filtered by ?subject_id= and ?date_from= / ?date_to=."""
    closed = [s["id"] for s in sessions.find({"status": "closed"}, {"id": 1})]
    q = {"enrollment": enr, "session_id": {"$in": closed}}
    if request.args.get("subject_id"):
        q["subject_id"] = request.args["subject_id"]
    date_query(q)
    return list(attendance.find(q).sort("session_start", direction))


@app.get("/api/reports/student/<enr>")
@login_required
def report_student(enr):
    recs = student_records(enr, -1)
    by_sub = {}
    for r in recs:
        b = by_sub.setdefault(r["subject_id"], {"subject_name": r["subject_name"], "semester": r["semester"], "present": 0, "total": 0})
        b["total"] += 1
        b["present"] += r["status"] == "PRESENT"
    summary = [dict(v, percent=round(100 * v["present"] / v["total"]) if v["total"] else 0) for v in by_sub.values()]
    st = students.find_one({"enrollment": enr}) or {}
    return jsonify(student={"enrollment": enr, "name": st.get("name") or (recs[0]["name"] if recs else enr)},
                   summary=sorted(summary, key=lambda x: (x["semester"], x["subject_name"])),
                   records=[{"date": r["date"], "subject_name": r["subject_name"], "status": r["status"],
                             "time": iso(r.get("time")), "note": r.get("note", "")} for r in recs])


# ---------------------------------------------------------------- CSV export
def csv_response(rows, filename):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/export/subject.csv")
@login_required
def export_subject():
    sid = request.args.get("subject_id", "")
    cols, rows = subject_matrix(sid)
    sub = subjects.find_one({"id": sid}) or {}
    out = [["Enrollment", "Name", "Division"] + [c["label"] for c in cols] + ["Present", "Total", "Percent"]]
    for r in rows:
        out.append([r["enrollment"], r["name"], r["division"]] + r["marks"] + [r["present"], r["total"], f"{r['percent']}%"])
    return csv_response(out, f"subject_{re.sub(r'[^A-Za-z0-9]+', '_', sub.get('name', 'report'))}.csv")


@app.get("/api/export/session/<sid>.csv")
@login_required
def export_session(sid):
    s = sessions.find_one({"id": sid})
    if not s:
        return err("Lecture not found", 404)
    out = [["Enrollment", "Name", "Division", "Status", "Tap time (IST)", "Note"]]
    for r in sorted(attendance.find({"session_id": sid}), key=lambda r: enr_key(r["enrollment"])):
        tm = to_ist(r["time"]).strftime("%H:%M:%S") if r.get("time") else ""
        out.append([r["enrollment"], r["name"], r.get("division", ""), r["status"], tm, r.get("note", "")])
    return csv_response(out, f"lecture_{s['date']}_{re.sub(r'[^A-Za-z0-9]+', '_', s['subject_name'])}.csv")


@app.get("/api/export/student/<enr>.csv")
@login_required
def export_student(enr):
    out = [["Date", "Subject", "Status", "Note"]]
    for r in student_records(enr, 1):
        out.append([r["date"], r["subject_name"], r["status"], r.get("note", "")])
    return csv_response(out, f"student_{re.sub(r'[^A-Za-z0-9]+', '_', enr)}.csv")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
