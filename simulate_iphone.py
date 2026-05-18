import time
import urllib.request
import urllib.parse
import random
import json

SERVER_URL = "https://android-telemetry-server.onrender.com/battery_report"
DEVICE_ID = "iPhone 16 Pro Max"

# Starting location: Near the Freedom Monument in Riga, Latvia
lat = 56.9515 
lon = 24.1134
battery = 98

print(f"[*] Initializing Tracker Simulation...")
print(f"[*] Target Device: {DEVICE_ID}")
print(f"[*] Location: Riga, Latvia")
print(f"[*] C2 Server: {SERVER_URL}\n")

while True:
    # Send as normal Form-encoded data just like the Android app payload would
    data = {
        "device_id": DEVICE_ID,
        "level": battery,
        "lat": lat,
        "lon": lon
    }
    
    encoded_data = urllib.parse.urlencode(data).encode('utf-8')
    
    try:
        req = urllib.request.Request(SERVER_URL, data=encoded_data)
        with urllib.request.urlopen(req, timeout=5) as response:
            resp_body = response.read().decode('utf-8')
            
            # Extract Ping Interval from Server Response if possible (JSON)
            try:
                server_cmd = json.loads(resp_body)
                sleep_interval = server_cmd.get("next_ping_seconds", 3)
            except:
                sleep_interval = 3
                
            print(f"[+] Sent GPS -> Lat: {lat:.6f}, Lon: {lon:.6f} | Batt: {battery}%")
            print(f"    Server C2 Response: {resp_body}")
    except Exception as e:
        print(f"[-] Connection failed: {e}")
        sleep_interval = 3
        
    # Simulate walking physics (~3-6 meters per ping)
    # Drifting generally towards the North-East through Riga Old Town
    lat += random.uniform(-0.0001, 0.0007)
    lon += random.uniform(0.0001, 0.0009)
    
    # Simulate realistic battery drain
    if random.random() < 0.05 and battery > 1:
        battery -= 1
        
    time.sleep(2)  # Changed to 2 seconds for fast visual testing
