// Per-path stats reporter to the controller (issue #8, minimal version).
// One JSON object per line over TCP. The socket is non-blocking and the
// reporter reconnects on its own, so a missing controller never stalls probing.
#pragma once

#include <cstdint>
#include <netinet/in.h>
#include <string>
#include <vector>

#include "stats.h"

namespace ndpa {

struct PathReport {
    uint16_t id;
    uint16_t port;
    PathSnapshot s;
};

// {"v":1,"ts_ms":...,"server":"10.0.0.2","paths":[{"path":0,"port":5001,...}]}
std::string format_report(uint64_t ts_ms, const char* server, const std::vector<PathReport>& paths);

// "ip:port" -> sockaddr. False on a bad string.
bool parse_endpoint(const std::string& s, sockaddr_in& out);

class Reporter {
public:
    explicit Reporter(const sockaddr_in& dst) : dst_(dst) {}
    ~Reporter();
    Reporter(const Reporter&) = delete;
    Reporter& operator=(const Reporter&) = delete;

    // Send one line if connected, otherwise move the connection along and drop
    // the line. Never blocks.
    void send_line(const std::string& line, uint64_t now_ns);

private:
    enum State { kIdle, kConnecting, kConnected };
    void reset(uint64_t now_ns, const char* why);

    sockaddr_in dst_;
    int fd_ = -1;
    State state_ = kIdle;
    uint64_t retry_at_ = 0, connect_started_ = 0;
};

}  // namespace ndpa
