import sqlite3
import time
import secrets
import hashlib
import math
from fastapi import FastAPI, Form, Request, Depends, HTTPException, status, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_FILE = "telemetry.db"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD_HASH = hashlib.sha256("SuperSecret2027".encode()).hexdigest()

def haversine(lat1, lon1, lat2, lon2):
    # Radius of earth in kilometers
    R = 6371.0 
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = math.sin(dLat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dLon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c # Distance in km

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            level INTEGER,
            lat REAL,
            lon REAL,
            timestamp TEXT,
            ping_interval INTEGER DEFAULT 60
        )
    ''')
    try:
        c.execute("ALTER TABLE devices ADD COLUMN ping_interval INTEGER DEFAULT 60")
    except:
        pass # Column might already exist
        
    try:
        c.execute("ALTER TABLE devices ADD COLUMN notif_state INTEGER DEFAULT 0")
        c.execute("ALTER TABLE devices ADD COLUMN notif_text TEXT DEFAULT ''")
    except:
        pass

    c.execute('''
        CREATE TABLE IF NOT EXISTS location_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            lat REAL,
            lon REAL,
            timestamp TEXT,
            unix_time REAL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            created_at REAL
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def verify_session(request: Request):
    token = request.cookies.get("session_token")
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM sessions WHERE token = ?", (token,))
    session = c.fetchone()
    conn.close()
    if not session:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    html_content = """
    <!DOCTYPE html>
    <html><head><title>Admin Login</title><script src="https://cdn.tailwindcss.com"></script></head>
    <body class="bg-gray-900 flex items-center justify-center h-screen">
        <div class="bg-gray-800 p-8 rounded-2xl shadow-2xl border border-gray-700 w-96">
            <h1 class="text-2xl text-white font-bold mb-6 text-center">Secure Telemetry Access</h1>
            <form action="/api/login" method="post" class="flex flex-col gap-4">
                <input type="text" name="username" placeholder="Username" class="px-4 py-2 rounded bg-gray-700 text-white border border-gray-600 focus:outline-none focus:border-blue-500">
                <input type="password" name="password" placeholder="Password" class="px-4 py-2 rounded bg-gray-700 text-white border border-gray-600 focus:outline-none focus:border-blue-500">
                <button type="submit" class="bg-blue-600 text-white font-bold py-2 rounded hover:bg-blue-500 transition">Authenticate</button>
            </form>
        </div>
    </body></html>
    """
    return HTMLResponse(content=html_content)

@app.post("/api/login")
async def process_login(response: Response, username: str = Form(...), password: str = Form(...)):
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    if secrets.compare_digest(username, ADMIN_USERNAME) and secrets.compare_digest(pwd_hash, ADMIN_PASSWORD_HASH):
        token = secrets.token_urlsafe(64)
        conn = get_db()
        c = conn.cursor()
        
        # OPTIMIZATION: Prune expired sessions (older than 24 hours) to prevent database bloat
        c.execute("DELETE FROM sessions WHERE created_at < ?", (time.time() - 86400,))
        
        c.execute("INSERT INTO sessions (token, created_at) VALUES (?, ?)", (token, time.time()))
        conn.commit()
        conn.close()
        response = RedirectResponse(url="/", status_code=302)
        # OPTIMIZATION: Added secure=True for HTTPS environments in production
        response.set_cookie(key="session_token", value=token, httponly=True, samesite="strict", secure=True, max_age=86400)
        return response
    return HTMLResponse("<body style='background:#111;color:red;text-align:center;padding:50px;font-family:sans-serif;'><h1>Invalid Credentials</h1><a href='/login'>Try Again</a></body>", status_code=401)

@app.get("/logout")
async def logout(response: Response, request: Request):
    token = request.cookies.get("session_token")
    if token:
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()
    response = RedirectResponse(url="/login")
    response.delete_cookie("session_token")
    return response

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    try:
        verify_session(request)
    except HTTPException:
        return RedirectResponse(url="/login")
    return FileResponse("templates/index.html", media_type="text/html")

class TelemetryReport(BaseModel):
    device_id: str
    level: int = None
    lat: float = None
    lon: float = None

@app.post("/battery_report")
async def receive_battery(
    device_id: str = Form(None), 
    level: int = Form(None), 
    lat: float = Form(None), 
    lon: float = Form(None), 
    payload: TelemetryReport = None
):
    if payload is not None:
        did = payload.device_id
        lvl = payload.level
        lat_val = payload.lat
        lon_val = payload.lon
    else:
        did = device_id
        lvl = level
        lat_val = lat
        lon_val = lon

    if not did:
        did = "unknown_device"

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM devices WHERE device_id = ?", (did,))
    existing = c.fetchone()
    
    new_lvl = int(lvl) if lvl is not None else (existing['level'] if existing and existing['level'] is not None else 0)
    new_lvl = max(0, min(100, new_lvl))
    new_lat = lat_val if lat_val is not None else (existing['lat'] if existing else None)
    new_lon = lon_val if lon_val is not None else (existing['lon'] if existing else None)
    new_ts = time.strftime("%H:%M:%S")
    unix_now = time.time()
    ping_interval = existing['ping_interval'] if existing else 60

    c.execute('''
        INSERT OR REPLACE INTO devices (device_id, level, lat, lon, timestamp, ping_interval)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (did, new_lvl, new_lat, new_lon, new_ts, ping_interval))
    
    if lat_val is not None and lon_val is not None:
        if not existing or existing['lat'] != lat_val or existing['lon'] != lon_val:
            c.execute('''
                INSERT INTO location_history (device_id, lat, lon, timestamp, unix_time)
                VALUES (?, ?, ?, ?, ?)
            ''', (did, lat_val, lon_val, new_ts, unix_now))
    
    conn.commit()
    conn.close()
    
    # Retrieve current notification states
    notif_state = existing['notif_state'] if existing and 'notif_state' in existing.keys() else 0
    notif_text = existing['notif_text'] if existing and 'notif_text' in existing.keys() else ""
    
    # Return the ping interval and Notification command so the Android daemon knows what to do
    return {
        "status": "success", 
        "device_id": did, 
        "next_ping_seconds": ping_interval,
        "notif_state": notif_state,
        "notif_text": notif_text
    }

@app.post("/set_interval")
async def set_interval(device_id: str = Form(...), interval: int = Form(...), verified: bool = Depends(verify_session)):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE devices SET ping_interval = ? WHERE device_id = ?", (interval, device_id))
    conn.commit()
    conn.close()
    return {"status": "updated", "interval": interval}

@app.post("/set_notification")
async def set_notification(device_id: str = Form(...), state: int = Form(...), text: str = Form(""), verified: bool = Depends(verify_session)):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE devices SET notif_state = ?, notif_text = ? WHERE device_id = ?", (state, text, device_id))
    conn.commit()
    conn.close()
    return {"status": "updated", "state": state, "text": text}

@app.get("/devices")
async def get_devices(verified: bool = Depends(verify_session)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT device_id FROM devices")
    devices = [row['device_id'] for row in c.fetchall()]
    conn.close()
    return {"devices": devices}

@app.get("/stats")
async def get_stats(device_id: str = None, verified: bool = Depends(verify_session)):
    conn = get_db()
    c = conn.cursor()
    
    if device_id:
        c.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,))
        row = c.fetchone()
    else: 
        c.execute("SELECT * FROM devices LIMIT 1")
        row = c.fetchone()
        
    speed_kmh = 0
    activity_state = "Stationary"
    
    if row and row['device_id']:
        did = row['device_id']
        c.execute("SELECT lat, lon, unix_time FROM location_history WHERE device_id = ? ORDER BY unix_time DESC LIMIT 2", (did,))
        last_two = c.fetchall()
        
        if len(last_two) == 2:
            loc1, loc2 = last_two[0], last_two[1]
            dist_km = haversine(loc1['lat'], loc1['lon'], loc2['lat'], loc2['lon'])
            time_diff_hours = (loc1['unix_time'] - loc2['unix_time']) / 3600.0
            if time_diff_hours > 0:
                speed_kmh = dist_km / time_diff_hours
                
            if speed_kmh > 20:
                activity_state = f"Driving ({int(speed_kmh)} km/h)"
            elif speed_kmh > 3:
                activity_state = f"Walking ({int(speed_kmh)} km/h)"
            else:
                activity_state = "Stationary"
                
    conn.close()
    
    if row:
        result = dict(row)
        result["activity"] = activity_state
        return result
    return {"level": "0", "timestamp": "Waiting for devices..."}

@app.get("/history_view", response_class=HTMLResponse)
async def history_view(request: Request):
    try:
        verify_session(request)
    except HTTPException:
        return RedirectResponse(url="/login")
    return FileResponse("templates/history.html", media_type="text/html")

@app.get("/history_detailed")
async def get_history_detailed(device_id: str, verified: bool = Depends(verify_session)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT lat, lon, timestamp FROM location_history WHERE device_id = ? ORDER BY unix_time DESC", (device_id,))
    records = [{"lat": row['lat'], "lon": row['lon'], "time": row['timestamp']} for row in c.fetchall()]
    conn.close()
    return {"history": records}

@app.get("/history")
async def get_history(device_id: str, verified: bool = Depends(verify_session)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT lat, lon FROM location_history WHERE device_id = ? ORDER BY unix_time ASC", (device_id,))
    coords = [[row['lat'], row['lon']] for row in c.fetchall()]
    conn.close()
    return {"path": coords}

@app.get("/health")
async def health():
    return {"status": "ok"}
