import re

# ============================
# Update index.html
# ============================
with open("/home/adrians/tmp_repo/templates/index.html", "r") as f:
    index_html = f.read()

# Add Delete Button next to Logout
delete_btn_html = """                <!-- Status Badge -->
                <div class="flex items-center bg-gray-50 px-4 py-2 rounded-xl border border-gray-200">
                    <span id="dot" class="h-2 w-2 bg-gray-300 rounded-full mr-2 shadow-sm"></span>
                    <span id="status-text" class="text-xs font-bold tracking-wide text-gray-500 uppercase">Offline</span>
                </div>
                
                <button type="button" id="delete-device-btn" class="text-[11px] font-bold bg-gray-800 text-white px-3 py-2 rounded-xl hover:bg-gray-700 transition shadow-sm uppercase tracking-wide">Delete Target</button>"""

index_html = index_html.replace(
'''                <!-- Status Badge -->
                <div class="flex items-center bg-gray-50 px-4 py-2 rounded-xl border border-gray-200">
                    <span id="dot" class="h-2 w-2 bg-gray-300 rounded-full mr-2 shadow-sm"></span>
                    <span id="status-text" class="text-xs font-bold tracking-wide text-gray-500 uppercase">Offline</span>
                </div>''', delete_btn_html)

# Add is_online check
old_online = """                    statusDot.className = "h-2 w-2 bg-green-500 rounded-full mr-2 shadow-[0_0_8px_rgba(34,197,94,0.8)] animate-pulse";
                    statusText.textContent = 'Online';
                    statusText.className = "text-xs font-bold tracking-wide text-green-600 uppercase";"""

new_online = """                    if (data.is_online) {
                        statusDot.className = "h-2 w-2 bg-green-500 rounded-full mr-2 shadow-[0_0_8px_rgba(34,197,94,0.8)] animate-pulse";
                        statusText.textContent = 'Online';
                        statusText.className = "text-xs font-bold tracking-wide text-green-600 uppercase";
                    } else {
                        statusDot.className = "h-2 w-2 bg-gray-400 rounded-full mr-2 shadow-sm";
                        statusText.textContent = 'Offline';
                        statusText.className = "text-xs font-bold tracking-wide text-gray-600 uppercase";
                    }"""
index_html = index_html.replace(old_online, new_online)

# Add Delete Device Logic
delete_js = """
        document.getElementById('play-audio-btn').addEventListener('click', () => setAudioCmd(1));
        document.getElementById('stop-audio-btn').addEventListener('click', () => setAudioCmd(0));

        document.getElementById('delete-device-btn').addEventListener('click', async () => {
            const activeDevice = selectEl.value;
            if(!activeDevice) return alert("Select a target to delete first.");
            if(!confirm(`WARNING: Erase ${activeDevice} and its location history completely?`)) return;
            
            try {
                const fd = new FormData();
                fd.append('device_id', activeDevice);
                const res = await fetch('/delete_device', { method: 'POST', body: fd });
                if(res.ok) {
                    alert('Target device and history wiped successfully.');
                    window.location.reload();
                }
            } catch(e) { console.error(e); }
        });"""

index_html = index_html.replace("""        document.getElementById('play-audio-btn').addEventListener('click', () => setAudioCmd(1));
        document.getElementById('stop-audio-btn').addEventListener('click', () => setAudioCmd(0));""", delete_js)

with open("/home/adrians/tmp_repo/templates/index.html", "w") as f:
    f.write(index_html)


# ============================
# Update history.html
# ============================
with open("/home/adrians/tmp_repo/templates/history.html", "r") as f:
    history_html = f.read()

# I will append simple flatpickr configuration to history.html
history_ui_old = '''        <!-- Top Navigation -->
        <nav class="mb-8 flex flex-col md:flex-row justify-between items-center bg-white p-4 rounded-2xl shadow-sm border border-gray-200">
            <div class="flex items-center gap-4 mb-4 md:mb-0">
                <a href="/" class="bg-gray-100 hover:bg-gray-200 text-gray-600 p-2 rounded-lg transition">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18"></path></svg>
                </a>
                <div>
                    <h1 class="text-xl font-bold tracking-tight text-gray-900">Location History</h1>
                    <p class="text-xs text-gray-500 font-medium">GPS Tracking Logs</p>
                </div>
            </div>
            
            <div class="flex items-center gap-4">
                <div class="flex items-center gap-2 bg-gray-50 px-3 py-2 rounded-xl border border-gray-200">
                    <label class="text-xs font-bold text-gray-500 uppercase">Target:</label>
                    <select id="device-select" class="bg-transparent font-semibold text-gray-800 text-sm focus:outline-none cursor-pointer">
                        <option value="">Awaiting connection...</option>
                    </select>
                </div>
            </div>
        </nav>'''

history_ui_new = '''        <!-- Top Navigation -->
        <nav class="mb-8 flex flex-col md:flex-row justify-between items-center bg-white p-4 rounded-2xl shadow-sm border border-gray-200">
            <div class="flex items-center gap-4 mb-4 md:mb-0">
                <a href="/" class="bg-gray-100 hover:bg-gray-200 text-gray-600 p-2 rounded-lg transition">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18"></path></svg>
                </a>
                <div>
                    <h1 class="text-xl font-bold tracking-tight text-gray-900">Location History</h1>
                    <p class="text-xs text-gray-500 font-medium">GPS Tracking Logs</p>
                </div>
            </div>
            
            <div class="flex items-center gap-4">
                <div class="flex items-center gap-2 bg-gray-50 px-3 py-2 rounded-xl border border-gray-200">
                    <label class="text-xs font-bold text-gray-500 uppercase">Target:</label>
                    <select id="device-select" class="bg-transparent font-semibold text-gray-800 text-sm focus:outline-none cursor-pointer">
                        <option value="">Awaiting connection...</option>
                    </select>
                </div>
                
                <!-- Date Filters -->
                <div class="flex gap-2">
                    <input type="datetime-local" id="start-date" class="bg-gray-50 px-3 py-2 rounded-xl border border-gray-200 text-xs font-semibold text-gray-800 outline-none w-[160px]">
                    <input type="datetime-local" id="end-date" class="bg-gray-50 px-3 py-2 rounded-xl border border-gray-200 text-xs font-semibold text-gray-800 outline-none w-[160px]">
                    <button id="filter-btn" class="text-xs font-bold bg-blue-600 hover:bg-blue-700 text-white px-3 py-2 rounded-xl transition">Filter</button>
                    <button id="clear-btn" class="text-xs font-bold bg-gray-200 hover:bg-gray-300 text-gray-700 px-3 py-2 rounded-xl transition">Clear</button>
                </div>
            </div>
        </nav>'''

history_html = history_html.replace(history_ui_old, history_ui_new)

js_old_fetch = "const res = await fetch(`/history_detailed?device_id=${encodeURIComponent(activeDevice)}`);"
js_new_fetch = """
                let qs = `?device_id=${encodeURIComponent(activeDevice)}`;
                const startD = document.getElementById('start-date').value;
                const endD = document.getElementById('end-date').value;
                if(startD && endD) {
                    const st = new Date(startD).getTime() / 1000;
                    const et = new Date(endD).getTime() / 1000;
                    qs += `&start_time=${st}&end_time=${et}`;
                }
                const res = await fetch(`/history_detailed${qs}`);"""

history_html = history_html.replace(js_old_fetch, js_new_fetch)

js_old_path = "fetch(`/history?device_id=${encodeURIComponent(activeDevice)}`).then(r => r.json()).then(d => {"
js_new_path = """fetch(`/history${qs}`).then(r => r.json()).then(d => {"""
history_html = history_html.replace(js_old_path, js_new_path)

js_add_filter_btn = """        selectEl.addEventListener('change', fetchHistory);
        document.getElementById('filter-btn').addEventListener('click', fetchHistory);
        document.getElementById('clear-btn').addEventListener('click', () => {
            document.getElementById('start-date').value = '';
            document.getElementById('end-date').value = '';
            fetchHistory();
        });"""
history_html = history_html.replace("selectEl.addEventListener('change', fetchHistory);", js_add_filter_btn)

with open("/home/adrians/tmp_repo/templates/history.html", "w") as f:
    f.write(history_html)

print("HTML Templates Patched")
