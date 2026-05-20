with open('./main.py', 'r') as f:
    content = f.read()

if 'def get_db():' not in content:
    content = content.replace("DB_FILE = 'telemetry.db'", "DB_FILE = 'telemetry.db'\n\ndef get_db():\n    return sqlite3.connect(DB_FILE, check_same_thread=False)\n")

with open('./main.py', 'w') as f:
    f.write(content)
