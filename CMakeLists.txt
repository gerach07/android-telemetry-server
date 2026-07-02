cmake_minimum_required(VERSION 3.10.2)
project("Reporter")

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
enable_testing()

# ── Optimizations (Size & Execution Efficiency) ───────────────────────────
set(CMAKE_BUILD_TYPE Release)
# -O2: Full performance optimization
# -flto: Link-time optimization for aggressive cross-translation-unit inlining
# -fno-rtti: Removes Run-Time Type Information to minimize binary size footprint
set(CMAKE_CXX_FLAGS_RELEASE "-O2 -flto -fvisibility=hidden -fno-rtti -ffunction-sections -fdata-sections")
set(CMAKE_C_FLAGS_RELEASE "-O2 -flto -fvisibility=hidden -ffunction-sections -fdata-sections")
set(CMAKE_EXE_LINKER_FLAGS_RELEASE "-Wl,--gc-sections -Wl,-s -Wl,--exclude-libs,ALL -Wl,--build-id=none -flto")

# ── Executable Targets ───────────────────────────────────────────────────────
add_executable(reporter C++/reporter.cpp)
add_executable(updater C++/updater.cpp)
add_executable(json_escape_utils_test C++/json_escape_utils_test.cpp)

add_test(NAME json_escape_utils_test COMMAND json_escape_utils_test)

# ── Static Third-Party Dependencies (arm64-v8a) ──────────────────────────────
add_library(ixwebsocket STATIC IMPORTED)
set_target_properties(ixwebsocket PROPERTIES
    IMPORTED_LOCATION ${CMAKE_SOURCE_DIR}/libs/arm64-v8a/libixwebsocket.a
)

add_library(mbedtls STATIC IMPORTED)
set_target_properties(mbedtls PROPERTIES
    IMPORTED_LOCATION ${CMAKE_SOURCE_DIR}/libs/arm64-v8a/libmbedtls.a
)

add_library(mbedx509 STATIC IMPORTED)
set_target_properties(mbedx509 PROPERTIES
    IMPORTED_LOCATION ${CMAKE_SOURCE_DIR}/libs/arm64-v8a/libmbedx509.a
)

add_library(mbedcrypto STATIC IMPORTED)
set_target_properties(mbedcrypto PROPERTIES
    IMPORTED_LOCATION ${CMAKE_SOURCE_DIR}/libs/arm64-v8a/libmbedcrypto.a
)

# Include directories configuration (Maps project root headers)
include_directories(${CMAKE_SOURCE_DIR})

# ── Link Targets ─────────────────────────────────────────────────────────────
# Link: ixwebsocket + AAudio + Android NDK platform libraries + TLS/Crypto layer
target_link_libraries(reporter
    ixwebsocket
    mbedtls
    mbedx509
    mbedcrypto
    log
    aaudio
    z
)

target_link_libraries(updater
    ixwebsocket
    mbedtls
    mbedx509
    mbedcrypto
    log
    z
)