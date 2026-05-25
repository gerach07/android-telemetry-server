import requests
import time
import random
import threading
import struct

SERVER_URL = "http://localhost:8000"
IMPLANT_KEY = "DeltaForce2027"
DEVICE_ID = "fake_android_99"

# Starting coordinates (e.g., San Francisco)
lat = 37.7749
lon = -122.4194
battery = 100
is_recording = False
ping_interval = 5

def command_polling_thread():
    global ping_interval, is_recording
    last_notif = ""
    while True:
        try:
            res = requests.get(f"{SERVER_URL}/check_commands?device_id={DEVICE_ID}")
            if res.status_code == 200:
                data = res.json()
                if data:
                    ping_interval = data.get("ping_interval", ping_interval)
                    
                    # Prevent spamming the same notification repeatedly in the console
                    current_notif = data.get("notif_text", "")
                    if data.get("notif_state") == 1 and current_notif != last_notif:
                        print(f"\n⚠️ INSTANT NOTIFICATION: '{current_notif}'")
                        last_notif = current_notif
                        
                    if data.get("play_audio") == 1:
                        print("🔊 BLASTING LOUD AUDIO... (Playing siren sound on device)")
                        
                    if data.get("record_audio") == 1 and not is_recording:
                        threading.Thread(target=simulate_audio_recording, args=(DEVICE_ID, IMPLANT_KEY), daemon=True).start()
        except:
            pass
        time.sleep(2) # Fast lazy fetch without destroying CPU

def simulate_audio_recording(device_id, implant_key):
    global is_recording
    is_recording = True
    print("🎙️ AUDIO RECORDING STARTED IN BACKGROUND (30s)...")
    time.sleep(30)
    
    sample_rate = 8000
    duration = 30
    num_samples = sample_rate * duration
    
    audio_data = bytearray(random.randint(0, 255) for _ in range(num_samples))
    
    channels = 1
    bits_per_sample = 8
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)
    
    wav_header = struct.pack('<4sI4s4sIHHIIHH4sI',
        b'RIFF',
        36 + num_samples,
        b'WAVE',
        b'fmt ',
        16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample,
        b'data',
        num_samples
    )
    
    full_wav = wav_header + audio_data
    
    requests.post(f"{SERVER_URL}/upload_audio", 
        data={"implant_key": implant_key, "device_id": device_id},
        files={"file": ("simulated_audio.wav", full_wav, "audio/wav")}
    )
    print("✅ Uploaded 30s white noise audio payload (cleared Armed badge)")
    is_recording = False

print(f"📱 Starting simulated Android device: {DEVICE_ID}")
print(f"📡 Connecting to {SERVER_URL}...\n")

# Start the fast command polling thread
threading.Thread(target=command_polling_thread, daemon=True).start()

while True:
    try:
        # Simulate slight movement
        lat += random.uniform(-0.0005, 0.0005)
        lon += random.uniform(-0.0005, 0.0005)
        
        # Simulate battery drain
        if random.random() > 0.8:
            battery = max(1, battery - 1)

        payload = {
            "implant_key": IMPLANT_KEY,
            "device_id": DEVICE_ID,
            "level": battery,
            "lat": lat,
            "lon": lon
        }

        response = requests.post(f"{SERVER_URL}/battery_report", data=payload)
        
        if response.status_code == 200:
            print(f"✅ Sent telemetry (Bat: {battery}%, Lat: {lat:.4f}, Lon: {lon:.4f})")
            print(f"💤 Sleeping for {ping_interval} seconds...\n")
            time.sleep(ping_interval)
            
        else:
            print(f"❌ Server rejected payload. Status: {response.status_code}")
            time.sleep(5)
            
    except requests.exceptions.ConnectionError:
        print("🔌 Cannot connect to server. Is it running? Retrying in 5s...")
        time.sleep(5)
    except Exception as e:
        print(f"⚠️ Error: {e}")
        time.sleep(5)
