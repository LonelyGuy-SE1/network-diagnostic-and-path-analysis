// Probe client: sends one probe per path every interval, matches echoes to
// outstanding probes and prints per-path stats once a second.
// Path id is the index of the port in the -p list (0 = path A, 1 = path B).

#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>
#include <vector>

#include "../protocol.h"
#include "common.h"
#include "stats.h"

static volatile sig_atomic_t g_stop = 0;
static void on_signal(int) { g_stop = 1; }

struct Path {
    uint16_t id;
    uint16_t port;
    int fd;
    sockaddr_in dst;
    uint32_t next_seq = 0;
    ndpa::PathStats stats;
};

static void usage(const char* prog) {
    std::fprintf(stderr,
        "usage: %s [-s server_ip] [-p ports] [-i interval_ms] [-t timeout_ms] [-w window] [-d seconds]\n"
        "  defaults: 127.0.0.1, 5001,5002, 100 ms, 1000 ms, 100 probes, run until SIGINT\n",
        prog);
}

int main(int argc, char** argv) {
    const char* server = "127.0.0.1";
    std::vector<uint16_t> ports = {5001, 5002};  // placeholder until issue #3 is settled
    long interval_ms = 100, timeout_ms = 1000, window = 100, duration_s = 0;
    int opt;
    while ((opt = getopt(argc, argv, "s:p:i:t:w:d:h")) != -1) {
        switch (opt) {
        case 's': server = optarg; break;
        case 'p': ports = ndpa::parse_ports(optarg); break;
        case 'i': interval_ms = std::atol(optarg); break;
        case 't': timeout_ms = std::atol(optarg); break;
        case 'w': window = std::atol(optarg); break;
        case 'd': duration_s = std::atol(optarg); break;
        default: usage(argv[0]); return opt == 'h' ? 0 : 2;
        }
    }
    if (ports.empty() || interval_ms <= 0 || timeout_ms <= 0 || window <= 0) { usage(argv[0]); return 2; }

    in_addr server_addr {};
    if (inet_pton(AF_INET, server, &server_addr) != 1) {
        std::fprintf(stderr, "bad server address: %s\n", server);
        return 2;
    }

    struct sigaction sa {};
    sa.sa_handler = on_signal;
    sigaction(SIGINT, &sa, nullptr);
    sigaction(SIGTERM, &sa, nullptr);

    // One connected socket per path keeps echoes from different paths apart
    // and lets the kernel filter out traffic from anyone but the reflector.
    std::vector<Path> paths;
    std::vector<pollfd> fds;
    for (size_t i = 0; i < ports.size(); ++i) {
        int fd = socket(AF_INET, SOCK_DGRAM, 0);
        if (fd < 0) { std::perror("socket"); return 1; }
        sockaddr_in dst {};
        dst.sin_family = AF_INET;
        dst.sin_addr = server_addr;
        dst.sin_port = htons(ports[i]);
        if (connect(fd, reinterpret_cast<sockaddr*>(&dst), sizeof dst) < 0) { std::perror("connect"); return 1; }
        paths.push_back({static_cast<uint16_t>(i), ports[i], fd, dst, 0,
                         ndpa::PathStats(static_cast<uint32_t>(window), timeout_ms * 1'000'000ull)});
        fds.push_back({fd, POLLIN, 0});
    }

    const uint64_t interval_ns = interval_ms * 1'000'000ull;
    const uint64_t start = ndpa::mono_ns();
    uint64_t next_probe = start, next_report = start + 1'000'000'000ull;
    unsigned char buf[2048];

    while (!g_stop) {
        uint64_t now = ndpa::mono_ns();
        if (duration_s > 0 && now - start >= static_cast<uint64_t>(duration_s) * 1'000'000'000ull) break;

        if (now >= next_probe) {
            for (auto& p : paths) {
                ndpa::ProbePacket pkt {ndpa::kMagic, ndpa::kVersion, ndpa::kProbe, p.id, p.next_seq, 0, ndpa::mono_ns()};
                unsigned char out[sizeof pkt];
                ndpa::encode(pkt, out);
                // ECONNREFUSED here just means an earlier probe hit a closed
                // port; the probe still counts as sent and will time out.
                send(p.fd, out, sizeof out, 0);
                p.stats.on_send(pkt.seq, pkt.send_ns);
                ++p.next_seq;
            }
            next_probe += interval_ns;
            if (next_probe < now) next_probe = now + interval_ns;  // fell behind, don't burst
        }

        if (now >= next_report) {
            for (auto& p : paths) {
                p.stats.expire(now);
                auto s = p.stats.snapshot();
                std::printf("path=%u port=%u sent=%llu recv=%llu loss=%.1f%% rtt_ms min=%.3f avg=%.3f max=%.3f jitter=%.3f dup=%llu reord=%llu late=%llu\n",
                            p.id, p.port, (unsigned long long)s.sent, (unsigned long long)s.received, s.loss_pct,
                            s.rtt_min_ms, s.rtt_avg_ms, s.rtt_max_ms, s.jitter_ms,
                            (unsigned long long)s.duplicates, (unsigned long long)s.reordered, (unsigned long long)s.late);
            }
            std::fflush(stdout);
            next_report += 1'000'000'000ull;
        }

        uint64_t wake = next_probe < next_report ? next_probe : next_report;
        now = ndpa::mono_ns();
        int wait_ms = wake > now ? static_cast<int>((wake - now + 999'999) / 1'000'000) : 0;
        int n = poll(fds.data(), fds.size(), wait_ms);
        if (n < 0) {
            if (errno == EINTR) continue;
            std::perror("poll");
            break;
        }
        for (size_t i = 0; i < fds.size(); ++i) {
            if (!(fds[i].revents & POLLIN)) continue;
            ssize_t len = recv(fds[i].fd, buf, sizeof buf, 0);
            uint64_t rx = ndpa::mono_ns();
            ndpa::ProbePacket pkt;
            if (len < 0 || !ndpa::decode(buf, static_cast<size_t>(len), pkt)) continue;
            if (pkt.path_id != paths[i].id) continue;
            paths[i].stats.on_echo(pkt.seq, pkt.send_ns, rx);
        }
    }

    for (auto& p : paths) close(p.fd);
    return 0;
}
