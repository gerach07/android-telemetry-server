from fastapi import FastAPI, Form, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import time

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Initial placeholder data
latest_stats = {"level": "0", "timestamp": "Waiting for device..."}

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    # This sends our data variables to the HTML file
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "level": latest_stats["level"], 
        "timestamp": latest_stats["timestamp"]
    })

@app.post("/battery_report")
async def receive_battery(level: str = Form(...)):
    latest_stats["level"] = level
    latest_stats["timestamp"] = time.strftime("%H:%M:%S")
    return {"status": "success"}
