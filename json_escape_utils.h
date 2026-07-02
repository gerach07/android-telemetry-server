#pragma once

#include <cstdio>
#include <string>
#include <string_view>

inline size_t json_escape_extra_bytes(std::string_view input) {
    size_t extra = 0;
    for (unsigned char c : input) {
        switch (c) {
            case '"':
            case '\\':
            case '\b':
            case '\f':
            case '\n':
            case '\r':
            case '\t':
                extra += 1;
                break;
            default:
                if (c < 0x20) {
                    extra += 5;
                }
                break;
        }
    }
    return extra;
}

inline void append_json_escaped(std::string& out, std::string_view input) {
    for (unsigned char c : input) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[7];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out.push_back(static_cast<char>(c));
                }
                break;
        }
    }
}

inline std::string json_escape(std::string_view input) {
    std::string escaped;
    escaped.reserve(input.size() + json_escape_extra_bytes(input));
    append_json_escaped(escaped, input);
    return escaped;
}
