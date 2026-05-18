from fastapi import FastAPI, Form
import time

app = FastAPI()

# Global variable to store the latest data for testing
latest_stats = {"level": "Unknown", "timestamp": None}

@app.get("/")
def read_root():
    return {"status": "online", "last_update": latest_stats}

@app.post("/battery_report")
async def receive_battery(level: str = Form(...)):
    # This matches your C++ daemon's "level=X" format
    latest_stats["level"] = level
    latest_stats["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
    
    print(f"Received battery update: {level}%")
    return {"status": "success", "received": level}