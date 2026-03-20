from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import requests
from cachetools import TTLCache
import time
import threading
from collections import deque
import sqlite3
import json
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

cache = TTLCache(maxsize=10, ttl=5)
GOLD_API = "https://api.gold-api.com/price/XAU"
alert_history = deque(maxlen=200)

# ================= DATABASE =================

DB_PATH = "aurum.db"

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trackers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            price REAL NOT NULL,
            cooldown_secs INTEGER DEFAULT 30,
            label TEXT DEFAULT '',
            last_triggered REAL DEFAULT 0
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id TEXT PRIMARY KEY,
            type TEXT,
            tracker_price REAL,
            current_price REAL,
            label TEXT,
            timestamp REAL,
            timestamp_readable TEXT
        )
    """)
    con.commit()
    con.close()

def db_get_trackers():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT id, type, price, cooldown_secs, label, last_triggered FROM trackers").fetchall()
    con.close()
    return [{"_id": r[0], "type": r[1], "price": r[2], "cooldown_secs": r[3], "label": r[4], "_last_triggered": r[5]} for r in rows]

def db_add_tracker(t):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT INTO trackers (type, price, cooldown_secs, label) VALUES (?,?,?,?)",
                (t.type, t.price, t.cooldown_secs, t.label))
    con.commit()
    con.close()

def db_remove_tracker(idx):
    trackers = db_get_trackers()
    if 0 <= idx < len(trackers):
        row_id = trackers[idx]["_id"]
        con = sqlite3.connect(DB_PATH)
        con.execute("DELETE FROM trackers WHERE id=?", (row_id,))
        con.commit()
        con.close()

def db_update_last_triggered(row_id, ts):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE trackers SET last_triggered=? WHERE id=?", (ts, row_id))
    con.commit()
    con.close()

def db_add_history(event):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT OR IGNORE INTO history (id, type, tracker_price, current_price, label, timestamp, timestamp_readable) VALUES (?,?,?,?,?,?,?)",
        (event["id"], event["type"], event["tracker_price"], event["current_price"],
         event["label"], event["timestamp"], event["timestamp_readable"])
    )
    con.commit()
    con.close()

def db_get_history():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT id, type, tracker_price, current_price, label, timestamp, timestamp_readable FROM history ORDER BY timestamp DESC LIMIT 200").fetchall()
    con.close()
    return [{"id": r[0], "type": r[1], "tracker_price": r[2], "current_price": r[3],
             "label": r[4], "timestamp": r[5], "timestamp_readable": r[6]} for r in rows]

def db_clear_history():
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM history")
    con.commit()
    con.close()

def _clean(trackers):
    return [{"type": t["type"], "price": t["price"], "cooldown_secs": t["cooldown_secs"], "label": t["label"]} for t in trackers]


# ================= MODELS =================

class Tracker(BaseModel):
    type: str
    price: float
    cooldown_secs: int = 30
    label: str = ""


# ================= SERVE FRONTEND =================

@app.get("/")
def serve_frontend():
    return FileResponse("index.html")


# ================= PRICE =================

def fetch_gold_price():
    if "XAUUSD" in cache:
        return cache["XAUUSD"]
    try:
        res = requests.get(GOLD_API, timeout=5).json()
        data = {
            "price": res.get("price"),
            "name": res.get("name", "Gold"),
            "symbol": res.get("symbol", "XAU"),
            "updatedAt": res.get("updatedAt"),
            "updatedAtReadable": res.get("updatedAtReadable"),
            "timestamp": int(time.time()),
        }
        cache["XAUUSD"] = data
        return data
    except Exception as e:
        print(f"Price fetch error: {e}")
        return {"price": None}

@app.get("/xauusd")
def get_price():
    return fetch_gold_price()


# ================= TRACKERS =================

@app.post("/add-tracker")
def add_tracker(tracker: Tracker):
    db_add_tracker(tracker)
    return {"status": "added", "trackers": _clean(db_get_trackers())}

@app.get("/trackers")
def get_trackers():
    return _clean(db_get_trackers())

@app.delete("/remove-tracker/{index}")
def remove_tracker(index: int):
    db_remove_tracker(index)
    return {"trackers": _clean(db_get_trackers())}

@app.patch("/update-tracker/{index}")
def update_tracker(index: int, tracker: Tracker):
    trackers = db_get_trackers()
    if 0 <= index < len(trackers):
        row_id = trackers[index]["_id"]
        con = sqlite3.connect(DB_PATH)
        con.execute("UPDATE trackers SET type=?, price=?, cooldown_secs=?, label=? WHERE id=?",
                    (tracker.type, tracker.price, tracker.cooldown_secs, tracker.label, row_id))
        con.commit()
        con.close()
    return {"trackers": _clean(db_get_trackers())}


# ================= ALERT HISTORY =================

@app.get("/alert-history")
def get_alert_history():
    return db_get_history()

@app.delete("/alert-history")
def clear_alert_history():
    db_clear_history()
    return {"status": "cleared"}


# ================= ALERT ENGINE =================

def check_trackers():
    while True:
        try:
            data = fetch_gold_price()
            price = data.get("price")

            if price:
                now = time.time()
                trackers = db_get_trackers()

                for t in trackers:
                    triggered = (
                        (t["type"] == "BUY"  and price <= t["price"]) or
                        (t["type"] == "SELL" and price >= t["price"])
                    )
                    if triggered:
                        cooldown = t.get("cooldown_secs", 30)
                        last = t.get("_last_triggered", 0)
                        if now - last >= cooldown:
                            db_update_last_triggered(t["_id"], now)
                            event = {
                                "id": f"{t['_id']}-{int(now*1000)}",
                                "type": t["type"],
                                "tracker_price": t["price"],
                                "current_price": price,
                                "label": t.get("label", ""),
                                "timestamp": now,
                                "timestamp_readable": time.strftime("%H:%M:%S", time.localtime(now)),
                            }
                            db_add_history(event)
                            arrow = "🟢" if t["type"] == "BUY" else "🔴"
                            print(f"{arrow} {t['type']} ALERT: current={price:.2f} trigger={t['price']:.2f}")
        except Exception as e:
            print(f"Alert engine error: {e}")

        time.sleep(5)


# ================= SELF PING (keeps Render free tier awake) =================

def self_ping():
    import os
    # Render sets RENDER_EXTERNAL_URL automatically — falls back to localhost for local dev
    base = os.environ.get('RENDER_EXTERNAL_URL', 'http://localhost:10000')
    url = base.rstrip('/') + '/ping'
    while True:
        time.sleep(600)  # every 10 minutes
        try:
            requests.get(url, timeout=10)
            print(f'Self-ping OK → {url}')
        except Exception as e:
            print(f'Self-ping failed: {e}')

@app.get('/ping')
def ping():
    return {'status': 'alive', 'timestamp': int(time.time())}


# ================= STARTUP =================

init_db()
threading.Thread(target=check_trackers, daemon=True).start()
threading.Thread(target=self_ping, daemon=True).start()
