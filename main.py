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

# set in Azure Configuration (no default recommended in prod)
APP_PASSWORD = os.getenv("APP_PASSWORD")

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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            total_liters REAL NOT NULL DEFAULT 0,
            paid_sek REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    # Upgrade old DBs (if paid_sek missing)
    try:
        cur.execute("ALTER TABLE friends ADD COLUMN paid_sek REAL NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # Transaktionslogg
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            friend_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (friend_id) REFERENCES friends(id) ON DELETE CASCADE
        )
    """)

    conn.commit()
    conn.close()

@app.on_event("startup")
def startup():
    init_db()

def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()

def calc_total_sek(liters: float) -> float:
    return round(float(liters) * PRICE_PER_LITER, 2)

def clean_name(name: str) -> str:
    return name.strip()

def round2(x: float) -> float:
    return round(float(x), 2)

def log_transaction(conn, friend_id: int, trans_type: str, amount: float, description: str):
    """Logga en transaktion till databasen"""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transactions (friend_id, type, amount, description, created_at) VALUES (?, ?, ?, ?, ?)",
        (friend_id, trans_type, amount, description, now_utc_iso())
    )
    conn.commit()

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
        liters = float(r["total_liters"])  # KVAR liters
        total_sek = calc_total_sek(liters)  # KVAR summa
        paid = float(r["paid_sek"])

        out.append({
            "id": int(r["id"]),
            "name": r["name"],
            "totalLiters": round2(liters),
            "totalSek": round2(total_sek),
            "paidSek": round2(paid),
            "remainingSek": round2(total_sek),  # samma som totalSek eftersom liters = kvar
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
    
    # Logga skapande
    log_transaction(conn, new_id, "created", 0.0, f"Skapade kontakt: {name}")
    
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

    return {
        "id": id,
        "name": name,
        "totalLiters": round2(liters),
        "totalSek": round2(total_sek),
        "paidSek": round2(paid),
        "remainingSek": round2(total_sek),
    }

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

    new_liters = float(row["total_liters"]) + liters_to_add
    cur.execute("UPDATE friends SET total_liters = ? WHERE id = ?", (new_liters, id))
    
    # Logga transaktion
    log_transaction(conn, id, "add_liters", liters_to_add, f"Lade till {round2(liters_to_add)} L")
    
    conn.commit()
    conn.close()

    total_sek = calc_total_sek(new_liters)
    paid = float(row["paid_sek"])

    return {
        "id": id,
        "name": row["name"],
        "totalLiters": round2(new_liters),
        "totalSek": round2(total_sek),
        "paidSek": round2(paid),
        "remainingSek": round2(total_sek),
    }

# ✅ NY LOGIK: betalt kan ge överskott (positiv balans)
@app.post("/api/friends/{id}/pay", dependencies=[Depends(require_password)])
def pay_friend(id: int, body: PayBody):
    amount = float(body.amount)
    liters_to_subtract = amount / PRICE_PER_LITER  # 100kr => 10L

    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id, name, total_liters, paid_sek FROM friends WHERE id = ?", (id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Friend not found.")

    current_liters = float(row["total_liters"])
    current_paid = float(row["paid_sek"])

    # Tillåt överbetalning - liters kan bli negativa (= överskott)
    new_liters = current_liters - liters_to_subtract
    new_paid = current_paid + amount

    cur.execute(
        "UPDATE friends SET total_liters = ?, paid_sek = ? WHERE id = ?",
        (new_liters, new_paid, id)
    )
    
    # Logga betalning
    log_transaction(conn, id, "payment", amount, f"Betalade {round2(amount)} kr")
    
    conn.commit()
    conn.close()

    total_sek = calc_total_sek(new_liters)  # Negativa liter = negativt saldo = överskott

    return {
        "id": id,
        "name": row["name"],
        "totalLiters": round2(new_liters),
        "totalSek": round2(total_sek),
        "paidSek": round2(new_paid),
        "remainingSek": round2(total_sek),
    }

@app.post("/api/friends/{id}/reset", dependencies=[Depends(require_password)])
def reset_friend(id: int):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM friends WHERE id = ?", (id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Friend not found.")

    # reset både liters och betalt
    cur.execute("UPDATE friends SET total_liters = 0, paid_sek = 0 WHERE id = ?", (id,))
    
    # Logga nollställning
    log_transaction(conn, id, "reset", 0.0, "Nollställde kontot")
    
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

@app.get("/api/friends/{id}/transactions", dependencies=[Depends(require_password)])
def get_transactions(id: int):
    conn = connect()
    cur = conn.cursor()
    
    # Kontrollera att personen finns
    cur.execute("SELECT id FROM friends WHERE id = ?", (id,))
    if not cur.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Friend not found.")
    
    # Hämta transaktioner
    cur.execute("""
        SELECT id, type, amount, description, created_at 
        FROM transactions 
        WHERE friend_id = ? 
        ORDER BY created_at DESC
        LIMIT 50
    """, (id,))
    rows = cur.fetchall()
    conn.close()
    
    transactions = []
    for r in rows:
        transactions.append({
            "id": int(r["id"]),
            "type": r["type"],
            "amount": round2(r["amount"]),
            "description": r["description"],
            "createdAt": r["created_at"]
        })
    
    return transactions

# Frontend (static/)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
