"""Wipe ALL attendance data (devices, students, subjects, lectures). Use before your real demo."""
import os
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
db = MongoClient(os.environ.get("MONGO_URI", "mongodb://localhost:27017/"))[os.environ.get("DB_NAME", "attendance_v2")]
if input("Type YES to delete everything in this database: ").strip() == "YES":
    for c in ("devices", "students", "subjects", "sessions", "attendance", "settings"):
        db[c].delete_many({})
    print("All data cleared.")
else:
    print("Cancelled. Nothing was deleted.")
