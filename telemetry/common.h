#pragma once

#include <cstdint>
#include <cstdlib>
#include <ctime>
#include <sstream>
#include <string>
#include <vector>

namespace ndpa {

inline uint64_t mono_ns() {
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<uint64_t>(ts.tv_sec) * 1'000'000'000ull + ts.tv_nsec;
}

// "5001,5002" -> {5001, 5002}. Returns empty on any bad entry.
inline std::vector<uint16_t> parse_ports(const std::string& s) {
    std::vector<uint16_t> out;
    std::stringstream ss(s);
    std::string tok;
    while (std::getline(ss, tok, ',')) {
        char* end = nullptr;
        long v = std::strtol(tok.c_str(), &end, 10);
        if (tok.empty() || *end || v < 1 || v > 65535) return {};
        out.push_back(static_cast<uint16_t>(v));
    }
    return out;
}

}  // namespace ndpa
