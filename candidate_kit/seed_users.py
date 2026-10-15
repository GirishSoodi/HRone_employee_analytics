"""
Seed HR user credentials into MongoDB Atlas.
Run: python seed_users.py
"""
import hashlib, os, secrets, ssl, sys
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB  = os.getenv("MONGO_DB", "attendance_db").strip()

if not MONGO_URI:
    sys.exit("ERROR: MONGO_URI not set in .env")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

# Verify connection
client.admin.command("ping")
print("MongoDB Atlas connection OK")

def hash_password(password, salt=None):
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
    return hashed, salt

SEED_USERS = [
    {
        "email":    "priya@peoplepulse.com",
        "name":     "Priya Sharma",
        "phone":    "+91 98765 43210",
        "role":     "HR Manager",
        "password": "password123",
        "avatar":   "https://images.unsplash.com/photo-1573496359142-b8d87734a5a2?w=100&auto=format&fit=crop&q=80",
    },
    {
        "email":    "admin@peoplepulse.com",
        "name":     "Admin User",
        "phone":    "+91 99999 00000",
        "role":     "Central Admin",
        "password": "admin123",
        "avatar":   "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=100&auto=format&fit=crop&q=80",
    },
]

try:
    db.users.create_index([("email", 1)], unique=True, name="idx_user_email_unique")
except Exception:
    pass  # index already exists

for u in SEED_USERS:
    if db.users.find_one({"email": u["email"]}):
        print(f"  [SKIP] Already exists: {u['email']}")
        continue
    h, s = hash_password(u["password"])
    db.users.insert_one({
        "email":         u["email"],
        "name":          u["name"],
        "phone":         u["phone"],
        "role":          u["role"],
        "avatar":        u["avatar"],
        "password_hash": h,
        "salt":          s,
    })
    print(f"  [OK]   Seeded: {u['email']}  /  password: {u['password']}")

print(f"\nTotal users in DB: {db.users.count_documents({})}")
for user in db.users.find({}, {"email": 1, "name": 1, "role": 1, "_id": 0}):
    print(f"  - {user['name']} <{user['email']}> [{user['role']}]")
