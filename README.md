# Android Telemetry Server

A tiny FastAPI app that accepts battery telemetry and displays a small dashboard.

Quick start

1. Create a virtual environment and install deps:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Run the app with Uvicorn (development):

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

3. Open http://localhost:8000 in your browser. The page polls `/stats` every 5s.

Test POST example (JSON):

```bash
curl -X POST -H "Content-Type: application/json" -d '{"level": 76}' http://localhost:8000/battery_report
```

Or use the small form on the dashboard to send test data.

## Deploying to Render

You can deploy this repository to Render as a Web Service. The included `render.yaml` will configure a Python web service that installs the requirements and runs the app with `gunicorn` and the `uvicorn` worker.

Recommended start command (used in `render.yaml`):

```
gunicorn -k uvicorn.workers.UvicornWorker main:app --bind 0.0.0.0:$PORT --workers 2
```

If you prefer the simpler (less production-ready) command you can use:

```
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Render settings:
- Build Command: `pip install -r requirements.txt` (handled by `render.yaml`)
- Start Command: see above (handled by `render.yaml`)
- Health check / Ping path: `/stats`

After pushing to your Git repo, connect it in Render and the `render.yaml` will be used to deploy automatically.
