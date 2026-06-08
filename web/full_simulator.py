import asyncio
import websockets
import json
import uuid
import wave
import io
import time
import random
import urllib.request
import urllib.parse
import threading
from contextlib import suppress

try:
    import readline
except ImportError:
    readline = None

print("="*60)
print("   Android C2 Implant Simulator (WebSockets)")
print("="*60)
import sys
if sys.stdin.isatty():
    choice = input("\nSelect server (1 for Local, 2 for Remote) [Default: 1]: ").strip() or '1'
else:
    choice = '1'

if choice == '2':
    ip_add = input("\nEnter Server Domain or IP [Default: hearts-eliminate-adrian-texts.trycloudflare.com]: ").strip() or "hearts-eliminate-adrian-texts.trycloudflare.com"
    ip_add = ip_add.replace("http://", "").replace("https://", "").split("/")[0]
    base_url = f"wss://{ip_add}/ws"
    upload_url = f"https://{ip_add}/upload_audio"
else:
    base_url = "ws://127.0.0.1:8000/ws"
    upload_url = "http://127.0.0.1:8000/upload_audio"

IMPLANT_KEY = "DeltaForce2027"
DEVICE_ID = "SIMULATED_VICTIM_PHONE"

installed_apps = "com.android.settings,com.whatsapp,com.facebook.katana,com.instagram.android"
location_tracking = True
lat, lon = 56.9496, 24.1052
battery = 100
charging = 1
screen_time_minutes = 0
ping_interval = 5
last_hour_reported = None
last_sent_installed_apps = None
last_sent_battery = None
last_sent_charging = None
last_sent_screen_time = None
print_lock = threading.Lock()
typing = False
current_prompt = ""

def safe_print(*args, **kwargs):
    global typing, current_prompt
    with print_lock:
        kwargs.setdefault('flush', True)
        if typing:
            line_buffer = readline.get_line_buffer() if readline else ""
            sys.stdout.write("\r")
            sys.stdout.write(" " * (len(current_prompt) + len(line_buffer) + 2))
            sys.stdout.write("\r")
            print(*args, **kwargs)
            if current_prompt:
                sys.stdout.write(current_prompt)
            if line_buffer:
                sys.stdout.write(line_buffer)
            sys.stdout.flush()
            return
        print(*args, **kwargs)

async def upload_audio_sim():
    safe_print(f"    [⬆️] UPLOADING AUDIO FILE to {upload_url}...")
    try:
        boundary = uuid.uuid4().hex
        body = []
        body.append(f'--{boundary}\r\nContent-Disposition: form-data; name="implant_key"\r\n\r\n{IMPLANT_KEY}\r\n'.encode('utf-8'))
        body.append(f'--{boundary}\r\nContent-Disposition: form-data; name="device_id"\r\n\r\n{DEVICE_ID}\r\n'.encode('utf-8'))
        
        sample_rate = 8000
        duration = 5
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(1)
            wav.setframerate(sample_rate)
            wav.writeframes(b'\x80' * (sample_rate * duration))
        
        wav_data = buffer.getvalue()
        body.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="recorded_5s.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode('utf-8') + wav_data + b'\r\n')
        body.append(f'--{boundary}--\r\n'.encode('utf-8'))
        
        req_up = urllib.request.Request(upload_url, data=b''.join(body))
        req_up.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
        with suppress(Exception):
            urllib.request.urlopen(req_up, timeout=10)
        safe_print(f"    [✔] UPLOAD COMPLETE!")
    except Exception as e:
        safe_print(f"    [x] UPLOAD FAILED: {e}")

async def manual_screen_time_sender(ws):
    global screen_time_minutes, charging, battery, typing, current_prompt
    while True:
        prompt = "\n[SIM COMMAND] Enter 'screen HH MM', 'charge on|off', 'toggle', 'error <src> <msg>', or press Enter to skip: "
        try:
            typing = True
            current_prompt = prompt
            entry = await asyncio.to_thread(input, prompt)
        except Exception:
            typing = False
            current_prompt = ""
            return
        finally:
            typing = False
            current_prompt = ""
        if not entry.strip():
            continue
        parts = entry.strip().split()
        cmd = parts[0].lower()

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

            payload = {
                "implant_key": IMPLANT_KEY,
                "device_id": DEVICE_ID,
                "battery": battery,
                "charging": charging,
                "lat": lat if location_tracking else 0.0,
                "lon": lon if location_tracking else 0.0,
                "installed_apps": installed_apps
            }
            try:
                await ws.send(json.dumps(payload))
                safe_print(f"    [>] CHARGING STATE SENT: {'ON' if charging == 1 else 'OFF'}")
            except Exception as e:
                safe_print(f"    [!] Failed to send charging state: {e}")
                typing = False
                return
            continue

        if cmd == "toggle":
            charging = 0 if charging == 1 else 1
            payload = {
                "implant_key": IMPLANT_KEY,
                "device_id": DEVICE_ID,
                "battery": battery,
                "charging": charging,
                "lat": lat if location_tracking else 0.0,
                "lon": lon if location_tracking else 0.0,
                "installed_apps": installed_apps
            }
            try:
                await ws.send(json.dumps(payload))
                safe_print(f"    [>] CHARGING STATE TOGGLED: {'ON' if charging == 1 else 'OFF'}")
            except Exception as e:
                safe_print(f"    [!] Failed to send charging toggle: {e}")
                typing = False
                return
            continue

        if cmd == "screen":
            if len(parts) < 3:
                safe_print("    [!] Usage: screen HH MM")
                continue
            try:
                hh = max(0, int(parts[1]))
                mm = max(0, int(parts[2]))
                screen_time_minutes = hh * 60 + mm
            except ValueError:
                safe_print("    [!] Invalid values, use numeric hours and minutes")
                continue

            payload = {
                "implant_key": IMPLANT_KEY,
                "device_id": DEVICE_ID,
                "battery": battery,
                "charging": charging,
                "lat": lat if location_tracking else 0.0,
                "lon": lon if location_tracking else 0.0,
                "installed_apps": installed_apps,
                "screen_time_minutes": screen_time_minutes
            }
            try:
                await ws.send(json.dumps(payload))
                hh = screen_time_minutes // 60
                mm = screen_time_minutes % 60
                safe_print(f"    [>] MANUAL SCREEN TIME SENT: {hh}h {mm}m")
            except Exception as e:
                safe_print(f"    [!] Failed to send manual screen time: {e}")
                typing = False
                return
            continue

        if cmd == "error":
            if len(parts) < 3:
                safe_print("    [!] Usage: error <source> <message...>")
                continue
            source = parts[1]
            msg = " ".join(parts[2:])
            payload = {
                "implant_key": IMPLANT_KEY,
                "device_id": DEVICE_ID,
                "error_source": source,
                "error_msg": msg
            }
            try:
                await ws.send(json.dumps(payload))
                safe_print(f"    [>] ERROR REPORT SENT: {source} -> {msg}")
            except Exception as e:
                safe_print(f"    [!] Failed to send error report: {e}")
                typing = False
                return
            continue

        safe_print("    [!] Unknown command. Use 'screen HH MM', 'charge on|off', 'error <src> <msg>', or 'toggle charge'.")

async def run_simulator():
    global lat, lon, battery, charging, location_tracking, screen_time_minutes, last_hour_reported, last_sent_installed_apps, last_sent_battery, last_sent_charging, last_sent_screen_time
    while True:
        try:
            async with websockets.connect(base_url) as ws:
                safe_print(f"[+] Connected to WebSocket WS: {base_url}")
                safe_print(f"[i] INSTALLED APPS AVAILABLE: {installed_apps}")
                
                # Reset tracking variables to force a full payload on new connection
                last_sent_installed_apps = None
                last_sent_battery = None
                last_sent_charging = None
                last_sent_screen_time = None
                
                # Receiver task
                async def receiver():
                    global location_tracking, ping_interval
                    async for msg in ws:
                        safe_print(f"[<] WS COMMAND RECEIVED: {msg}")
                        try:
                            cmd = json.loads(msg)
                            task = cmd.get("task")
                            if task == "check_location_state":
                                safe_print("    [!] CHECK LOCATION STATE REQUEST RECEIVED")
                            elif task == "update_blocked_apps":
                                safe_print(f"    [!] UPDATING BLOCKED APPS LIST: {cmd.get('apps')}")
                            elif task == "refresh_installed_apps":
                                safe_print("    [!] INSTALLED APPS REFRESH REQUEST RECEIVED")
                                await ws.send(json.dumps({
                                    "implant_key": IMPLANT_KEY,
                                    "device_id": DEVICE_ID,
                                    "battery": battery,
                                    "charging": charging,
                                    "lat": lat if location_tracking else 0.0,
                                    "lon": lon if location_tracking else 0.0,
                                    "installed_apps": installed_apps
                                }))
                                safe_print(f"    [>] REFRESHED APP LIST SENT: {installed_apps}")
                            elif task == "set_location":
                                location_tracking = (cmd.get("track", 1) == 1)
                                safe_print(f"    [!] LOCATION TRACKING SET TO: {location_tracking}")
                            elif task == "set_interval":
                                ping_interval = int(cmd.get('interval', ping_interval))
                                safe_print(f"    [!] CONFIG UPDATE RECEIVED: interval={ping_interval}")
                            elif task == "system_alert":
                                safe_print(f"    [!] SYSTEM ALERT RECEIVED: state={cmd.get('state')} text={cmd.get('text')}")
                            elif task == "audio_blast":
                                safe_print(f"    [!] AUDIO BLAST RECEIVED: play={cmd.get('play')}")
                            elif task == "mic_record":
                                safe_print("    [🎤] MIC RECORD REQUEST RECEIVED")
                                asyncio.create_task(upload_audio_sim())
                            elif task == "power_cmd":
                                safe_print(f"    [!] POWER COMMAND RECEIVED: action={cmd.get('action')}")
                            elif task == "factory_reset":
                                safe_print("    [!] FACTORY RESET REQUEST RECEIVED")
                            elif task == "shell":
                                cmd_text = cmd.get('command') or '<no command provided>'
                                safe_print(f"    [💻] SHELL COMMAND REQUESTED: {cmd_text}")
                            else:
                                safe_print(f"    [!] UNKNOWN SERVER TASK: {task}")
                        except Exception as e:
                            safe_print(f"    [!] ERROR PROCESSING WS COMMAND: {e}")
                
                recv_task = asyncio.create_task(receiver())
                manual_screen_task = asyncio.create_task(manual_screen_time_sender(ws))
                try:
                    # Sender loop
                    while True:
                        if location_tracking:
                            lat += random.uniform(-0.0002, 0.0005)
                            lon += random.uniform(0.0001, 0.0008)

                        if charging == 1:
                            if battery < 100 and random.random() < 0.8:
                                battery = min(100, battery + 1)
                        else:
                            if random.random() < 0.1 and battery > 1:
                                battery -= 1

                        now = time.localtime()
                        current_hour = f"{now.tm_year}-{now.tm_mon}-{now.tm_mday}-{now.tm_hour}"
                        payload = {
                            "implant_key": IMPLANT_KEY,
                            "device_id": DEVICE_ID,
                            "lat": lat if location_tracking else 0.0,
                            "lon": lon if location_tracking else 0.0
                        }

                        if last_sent_installed_apps is None or installed_apps != last_sent_installed_apps:
                            payload["installed_apps"] = installed_apps
                            last_sent_installed_apps = installed_apps

                        if last_sent_charging is None or charging != last_sent_charging:
                            payload["charging"] = charging
                            last_sent_charging = charging

                        if battery != last_sent_battery:
                            payload["battery"] = battery
                            last_sent_battery = battery

                        if now.tm_min == 0 and last_hour_reported != current_hour:
                            last_hour_reported = current_hour
                            if screen_time_minutes == 0:
                                screen_time_minutes += random.randint(10, 30)
                            payload["screen_time_minutes"] = screen_time_minutes
                            payload["event"] = "hourly_screen_time_update"
                            last_sent_screen_time = screen_time_minutes

                        payload_json = json.dumps(payload)
                        safe_print(f"[>] SENDING: {payload_json}")
                        await ws.send(payload_json)
                        if payload.get("event") == "hourly_screen_time_update":
                            safe_print(f"[>] HOURLY SCREEN TIME SENT | Total: {screen_time_minutes} minutes | Charging: {charging}")
                        else:
                            safe_print(f"[>] TICK SENT | Batt: {battery}% | Charging: {charging} | GPS Tracking: {location_tracking} | Screen Time: {screen_time_minutes}m")

                        await asyncio.sleep(ping_interval)
                finally:
                    recv_task.cancel()
                    manual_screen_task.cancel()
        except Exception as e:
            safe_print(f"[-] Connection dropped ({e}), retrying in 5s...")
            await asyncio.sleep(5)

if __name__ == '__main__':
    asyncio.run(run_simulator())
