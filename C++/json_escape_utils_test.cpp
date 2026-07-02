#include <cstdlib>
#include <iostream>
#include <string>

#include "json_escape_utils.h"

static void expect_equal(const std::string& actual, const std::string& expected, const char* label) {
    if (actual != expected) {
        std::cerr << label << " failed\nexpected: " << expected << "\nactual:   " << actual << '\n';
        std::exit(1);
    }
}

static void expect_size(std::size_t actual, std::size_t expected, const char* label) {
    if (actual != expected) {
        std::cerr << label << " failed\nexpected: " << expected << "\nactual:   " << actual << '\n';
        std::exit(1);
    }
}

int main() {
    expect_equal(json_escape("plain text"), "plain text", "plain text");
    expect_equal(json_escape("\"\\\b\f\n\r\t"), "\\\"\\\\\\b\\f\\n\\r\\t", "basic escapes");
    expect_equal(json_escape(std::string("line1\nline2")), "line1\\nline2", "newline escape");

    std::string control_input;
    for (unsigned char c = 0x00; c <= 0x1f; ++c) {
        control_input.push_back(static_cast<char>(c));
    }
    std::string control_output = json_escape(control_input);
    expect_size(control_output.size(), control_input.size() + json_escape_extra_bytes(control_input), "control size");
    expect_equal(json_escape(std::string(1, '\0')), "\\u0000", "nul escape");

    const char embedded_nul_raw[] = {'a', '\0', 'b'};
    std::string embedded_nul(embedded_nul_raw, sizeof(embedded_nul_raw));
    expect_equal(json_escape(embedded_nul), "a\\u0000b", "embedded nul");

    std::string escaped;
    append_json_escaped(escaped, std::string_view(embedded_nul));
    expect_equal(escaped, "a\\u0000b", "append_json_escaped");

    return 0;
}