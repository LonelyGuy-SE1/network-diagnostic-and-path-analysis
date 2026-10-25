#include "report.h"

#include <arpa/inet.h>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

namespace ndpa {

std::string format_report(uint64_t ts_ms, const char* server, const std::vector<PathReport>& paths) {
    std::string out;
    char buf[512];
    std::snprintf(buf, sizeof buf, "{\"v\":1,\"ts_ms\":%llu,\"server\":\"%s\",\"paths\":[",
                  (unsigned long long)ts_ms, server);
    out += buf;
    for (size_t i = 0; i < paths.size(); ++i) {
        const auto& p = paths[i];
        const auto& s = p.s;
        std::snprintf(buf, sizeof buf,
                      "%s{\"path\":%u,\"port\":%u,\"sent\":%llu,\"recv\":%llu,\"window\":%u,"
                      "\"loss\":%.2f,\"rtt_min\":%.3f,\"rtt_avg\":%.3f,\"rtt_max\":%.3f,\"jitter\":%.3f}",
                      i ? "," : "", p.id, p.port, (unsigned long long)s.sent, (unsigned long long)s.received,
                      s.window_size, s.loss_pct, s.rtt_min_ms, s.rtt_avg_ms, s.rtt_max_ms, s.jitter_ms);
        out += buf;
    }
    out += "]}\n";
    return out;
}

bool parse_endpoint(const std::string& s, sockaddr_in& out) {
    auto colon = s.rfind(':');
    if (colon == std::string::npos) return false;
    std::string host = s.substr(0, colon), port = s.substr(colon + 1);
    char* end = nullptr;
    long v = std::strtol(port.c_str(), &end, 10);
    if (port.empty() || *end || v < 1 || v > 65535) return false;
    std::memset(&out, 0, sizeof out);
    out.sin_family = AF_INET;
    out.sin_port = htons(static_cast<uint16_t>(v));
    return inet_pton(AF_INET, host.c_str(), &out.sin_addr) == 1;
}

Reporter::~Reporter() {
    if (fd_ >= 0) close(fd_);
}

void Reporter::reset(uint64_t now_ns, const char* why) {
    if (state_ == kConnected) std::fprintf(stderr, "reporter: disconnected (%s), retrying\n", why);
    if (fd_ >= 0) close(fd_);
    fd_ = -1;
    state_ = kIdle;
    retry_at_ = now_ns + 1'000'000'000ull;
}

void Reporter::send_line(const std::string& line, uint64_t now_ns) {
    if (state_ == kIdle) {
        if (now_ns < retry_at_) return;
        fd_ = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, 0);
        if (fd_ < 0) { reset(now_ns, "socket"); return; }
        if (connect(fd_, reinterpret_cast<const sockaddr*>(&dst_), sizeof dst_) == 0) {
            state_ = kConnected;
            std::fprintf(stderr, "reporter: connected\n");
        } else if (errno == EINPROGRESS) {
            state_ = kConnecting;
            connect_started_ = now_ns;
        } else {
            reset(now_ns, "connect");
            return;
        }
    }
    if (state_ == kConnecting) {
        pollfd p {fd_, POLLOUT, 0};
        if (poll(&p, 1, 0) <= 0) {
            if (now_ns - connect_started_ > 3'000'000'000ull) reset(now_ns, "connect timeout");
            return;
        }
        int err = 0;
        socklen_t len = sizeof err;
        if (getsockopt(fd_, SOL_SOCKET, SO_ERROR, &err, &len) < 0 || err) { reset(now_ns, "connect"); return; }
        state_ = kConnected;
        std::fprintf(stderr, "reporter: connected\n");
    }
    if (state_ == kConnected) {
        ssize_t n = send(fd_, line.data(), line.size(), MSG_NOSIGNAL | MSG_DONTWAIT);
        if (n == static_cast<ssize_t>(line.size())) return;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return;  // controller slow, drop this line
        reset(now_ns, n < 0 ? std::strerror(errno) : "short write");     // a partial line would corrupt the stream
    }
}

}  // namespace ndpa
