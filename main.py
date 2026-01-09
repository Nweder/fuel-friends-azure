import os
import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Header, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

PRICE_PER_LITER = 10.0

# Local: ./fuel.db
# Azure persistent: /home/fuel.db
DB_PATH = os.getenv("DB_PATH", "./fuel.db")

APP_PASSWORD = os.getenv("APP_PASSWORD")  # set in Azure Configuration (no default)
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]

app = FastAPI(title="Fuel Friends")

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# ---------- AUTH ----------
def require_password(x_app_password: str | None = Header(default=None, alias="X-App-Password")):
    if not APP_PASSWORD:
        raise HTTPException(status_code=500, detail="APP_PASSWORD is not configured in Azure")
    if x_app_password != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.post("/api/login")
async def login(req: Request):
    body = await req.json()
    password = (body.get("password") or "").strip()

    if not APP_PASSWORD:
        raise HTTPException(status_code=500, detail="APP_PASSWORD is not configured in Azure")

    if password != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Fel lösenord")

    return {"ok": True}

# ---------- DB ----------
def connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = connect()
    cur = conn.cursor()

    # Create table (new installs)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            total_liters REAL NOT NULL DEFAULT 0,
            paid_sek REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    # Upgrade existing DB (older installs without paid_sek)
    try:
        cur.execute("ALTER TABLE friends ADD COLUMN paid_sek REAL NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # already exists

    conn.commit()
    conn.close()

@app.on_event("startup")
def startup():
    init_db()

def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()

def calc_total_sek(total_liters: float) -> float:
    return round(float(total_liters) * PRICE_PER_LITER, 2)

def clean_name(name: str) -> str:
    return name.strip()

# ---------- MODELS ----------
class FriendCreate(BaseModel):
    name: str = Field(min_length=2)

class FriendUpdate(BaseModel):
    name: str = Field(min_length=2)

class AddLitersBody(BaseModel):
    liters: float = Field(gt=0)

class PayBody(BaseModel):
    amount: float = Field(gt=0)

# ---------- API (protected) ----------
@app.get("/api/friends", dependencies=[Depends(require_password)])
def list_friends():
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id, name, total_liters, paid_sek, created_at FROM friends ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()

    out = []
    for r in rows:
        liters = float(r["total_liters"])
        total_sek = calc_total_sek(liters)
        paid = float(r["paid_sek"])
        remaining = round(total_sek - paid, 2)

        out.append({
            "id": int(r["id"]),
            "name": r["name"],
            "totalLiters": liters,
            "totalSek": total_sek,
            "paidSek": round(paid, 2),
            "remainingSek": remaining
        })
    return out

@app.post("/api/friends", status_code=201, dependencies=[Depends(require_password)])
def create_friend(body: FriendCreate):
    name = clean_name(body.name)
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Name must be at least 2 characters.")

    conn = connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO friends (name, total_liters, paid_sek, created_at) VALUES (?, ?, ?, ?)",
        (name, 0.0, 0.0, now_utc_iso())
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()

    return {"id": new_id, "name": name, "totalLiters": 0.0, "totalSek": 0.0, "paidSek": 0.0, "remainingSek": 0.0}

@app.put("/api/friends/{id}", dependencies=[Depends(require_password)])
def rename_friend(id: int, body: FriendUpdate):
    name = clean_name(body.name)
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Name must be at least 2 characters.")

    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id, total_liters, paid_sek FROM friends WHERE id = ?", (id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Friend not found.")

    cur.execute("UPDATE friends SET name = ? WHERE id = ?", (name, id))
    conn.commit()
    conn.close()

    liters = float(row["total_liters"])
    total_sek = calc_total_sek(liters)
    paid = float(row["paid_sek"])
    remaining = round(total_sek - paid, 2)

    return {"id": id, "name": name, "totalLiters": liters, "totalSek": total_sek, "paidSek": round(paid, 2), "remainingSek": remaining}

@app.delete("/api/friends/{id}", status_code=204, dependencies=[Depends(require_password)])
def delete_friend(id: int):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id FROM friends WHERE id = ?", (id,))
    if not cur.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Friend not found.")

    cur.execute("DELETE FROM friends WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return

@app.post("/api/friends/{id}/add-liters", dependencies=[Depends(require_password)])
def add_liters(id: int, body: AddLitersBody):
    liters_to_add = float(body.liters)

    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id, name, total_liters, paid_sek FROM friends WHERE id = ?", (id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Friend not found.")

    new_total_liters = float(row["total_liters"]) + liters_to_add
    cur.execute("UPDATE friends SET total_liters = ? WHERE id = ?", (new_total_liters, id))
    conn.commit()
    conn.close()

    total_sek = calc_total_sek(new_total_liters)
    paid = float(row["paid_sek"])
    remaining = round(total_sek - paid, 2)

    return {"id": id, "name": row["name"], "totalLiters": new_total_liters, "totalSek": total_sek, "paidSek": round(paid, 2), "remainingSek": remaining}

@app.post("/api/friends/{id}/pay", dependencies=[Depends(require_password)])
def pay_friend(id: int, body: PayBody):
    amount = float(body.amount)

    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id, name, total_liters, paid_sek FROM friends WHERE id = ?", (id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Friend not found.")

    liters = float(row["total_liters"])
    total_sek = calc_total_sek(liters)

    new_paid = float(row["paid_sek"]) + amount
    if new_paid > total_sek:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Betalt kan inte bli mer än totalsumman ({total_sek} kr).")

    cur.execute("UPDATE friends SET paid_sek = ? WHERE id = ?", (new_paid, id))
    conn.commit()
    conn.close()

    remaining = round(total_sek - new_paid, 2)
    return {"id": id, "name": row["name"], "totalLiters": liters, "totalSek": total_sek, "paidSek": round(new_paid, 2), "remainingSek": remaining}

@app.post("/api/friends/{id}/reset", dependencies=[Depends(require_password)])
def reset_friend(id: int):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM friends WHERE id = ?", (id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Friend not found.")

    cur.execute("UPDATE friends SET total_liters = 0, paid_sek = 0 WHERE id = ?", (id,))
    conn.commit()
    conn.close()

    return {"id": id, "name": row["name"], "totalLiters": 0.0, "totalSek": 0.0, "paidSek": 0.0, "remainingSek": 0.0}

@app.post("/api/reset-all", dependencies=[Depends(require_password)])
def reset_all():
    conn = connect()
    cur = conn.cursor()
    cur.execute("UPDATE friends SET total_liters = 0, paid_sek = 0")
    conn.commit()
    conn.close()
    return {"ok": True}

# Frontend (static/)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
