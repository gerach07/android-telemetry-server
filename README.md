# CloverProject Web C2

This repository contains the Android implant telemetry backend and a web UI for the simulated command-and-control server.

## Updated structure
- `web/` contains the current web application and server files.
- `web/templates/` contains the HTML UI templates.
- `web/static/` contains static assets used by the UI.
- `web/start_commands.txt` contains the recommended local startup commands.

## Run the server
From the repository root:

```bash
cd web
pkill -f 'uvicorn main:app' || true
nohup uvicorn main:app --host 0.0.0.0 --port 8000 > server.log 2>&1 &
```

Then open the UI at:

```text
http://127.0.0.1:8000
```

## Web UI stop button
A new "Stop Server" button is available in the Danger Zone section of the web UI. It sends a request to the server and exits the process after a short delay.

## Authentication
- Default admin username: `admin`
- Default admin password: `1234`

For production, set environment variables:

```bash
export ADMIN_USERNAME=admin
export ADMIN_HASH=<sha256-hash-of-your-password>
export IMPLANT_KEY=<your-implant-key>
```

## Notes
- The server now uses `web/main.py` and serves files from `web/templates` and `web/static`.
- The `web/start_commands.txt` file has been updated to point to the `web/` application folder.
- If you want to stop the server from the browser, use the Stop Server button after logging in.
