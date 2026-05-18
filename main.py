from fastapi import FastAPI, Form, Request, Depends
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import time

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

# Initial placeholder data
latest_stats = {"level": "0", "timestamp": "Waiting for device..."}


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    # Render the template; the page will poll `/stats` for live updates
    return templates.TemplateResponse("index.html", {"request": request})


class BatteryReport(BaseModel):
    level: int


@app.post("/battery_report")
async def receive_battery(level: int = Form(None), payload: BatteryReport | None = None):
    # Accepts either form-encoded `level` or JSON payload {"level": N}
    if payload is not None:
        level_value = int(payload.level)
    elif level is not None:
        level_value = int(level)
    else:
        return JSONResponse({"status": "error", "detail": "no level provided"}, status_code=400)

    latest_stats["level"] = str(max(0, min(100, level_value)))
    latest_stats["timestamp"] = time.strftime("%H:%M:%S")
    return {"status": "success", "level": latest_stats["level"]}


@app.get("/stats")
async def get_stats():
    return latest_stats


@app.get("/health")
async def health():
    # Simple health-check endpoint for Render/Load balancers
    return {"status": "ok"}
