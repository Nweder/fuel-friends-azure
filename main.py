import os
import sqlite3
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

PRICE_PER_LITER = 10.0

# Local: ./fuel.db
# Azure: set DB_PATH=/home/fuel.db  (persistent)
DB_PATH = os.getenv("DB_PATH", "./fuel.db")

# When frontend is served by same host, CORS not needed.
# Keep it safe: allow the same origin by default.
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

# Serve frontend from / (same URL as API)
app.mount("/", StaticFiles(directory="static", html=True), name="static")


def connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            total_liters REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
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

class FriendCreate(BaseModel):
    name: str = Field(min_length=2)

class FriendUpdate(BaseModel):
    name: str = Field(min_length=2)

class AddLitersBody(BaseModel):
    liters: float = Field(gt=0)

@app.get("/api/friends")
def list_friends():
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id, name, total_liters, created_at FROM friends ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()

    out = []
    for r in rows:
        liters = float(r["total_liters"])
        out.append({
            "id": int(r["id"]),
            "name": r["name"],
            "totalLiters": liters,
            "totalSek": calc_total_sek(liters),
        })
    return out

@app.post("/api/friends", status_code=201)
def create_friend(body: FriendCreate):
    name = clean_name(body.name)
    if len(name) < 2:
        raise HTTPException(400, detail="Name must be at least 2 characters.")

    conn = connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO friends (name, total_liters, created_at) VALUES (?, ?, ?)",
        (name, 0.0, now_utc_iso())
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()

    return {"id": new_id, "name": name, "totalLiters": 0.0, "totalSek": 0.0}

@app.put("/api/friends/{id}")
def rename_friend(id: int, body: FriendUpdate):
    name = clean_name(body.name)
    if len(name) < 2:
        raise HTTPException(400, detail="Name must be at least 2 characters.")

    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id, total_liters FROM friends WHERE id = ?", (id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, detail="Friend not found.")

    cur.execute("UPDATE friends SET name = ? WHERE id = ?", (name, id))
    conn.commit()
    liters = float(row["total_liters"])
    conn.close()

    return {"id": id, "name": name, "totalLiters": liters, "totalSek": calc_total_sek(liters)}

@app.delete("/api/friends/{id}", status_code=204)
def delete_friend(id: int):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id FROM friends WHERE id = ?", (id,))
    if not cur.fetchone():
        conn.close()
        raise HTTPException(404, detail="Friend not found.")

    cur.execute("DELETE FROM friends WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return

@app.post("/api/friends/{id}/add-liters")
def add_liters(id: int, body: AddLitersBody):
    liters_to_add = float(body.liters)
    if liters_to_add <= 0:
        raise HTTPException(400, detail="Liters must be > 0.")

    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id, name, total_liters FROM friends WHERE id = ?", (id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, detail="Friend not found.")

    new_total = float(row["total_liters"]) + liters_to_add
    cur.execute("UPDATE friends SET total_liters = ? WHERE id = ?", (new_total, id))
    conn.commit()
    conn.close()

    return {"id": id, "name": row["name"], "totalLiters": new_total, "totalSek": calc_total_sek(new_total)}

@app.post("/api/friends/{id}/reset")
def reset_friend(id: int):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM friends WHERE id = ?", (id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, detail="Friend not found.")

    cur.execute("UPDATE friends SET total_liters = 0 WHERE id = ?", (id,))
    conn.commit()
    conn.close()

    return {"id": id, "name": row["name"], "totalLiters": 0.0, "totalSek": 0.0}

@app.post("/api/reset-all")
def reset_all():
    conn = connect()
    cur = conn.cursor()
    cur.execute("UPDATE friends SET total_liters = 0")
    conn.commit()
    conn.close()
    return {"ok": True}
