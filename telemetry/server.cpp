// UDP reflector: listens on every probe port and echoes valid probes back to
// the sender from the same socket, so the source port of the echo matches the
// port the probe was sent to (the controller pins echo paths on it).

#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>
#include <vector>

#include "../protocol.h"
#include "common.h"

static volatile sig_atomic_t g_stop = 0;
static void on_signal(int) { g_stop = 1; }

static void usage(const char* prog) {
    std::fprintf(stderr, "usage: %s [-p ports]\n  -p  comma-separated UDP ports (default 5001,5002)\n", prog);
}

int main(int argc, char** argv) {
    std::vector<uint16_t> ports = {5001, 5002};  // placeholder until issue #3 is settled
    int opt;
    while ((opt = getopt(argc, argv, "p:h")) != -1) {
        if (opt == 'p') {
            ports = ndpa::parse_ports(optarg);
            if (ports.empty()) { usage(argv[0]); return 2; }
        } else {
            usage(argv[0]);
            return opt == 'h' ? 0 : 2;
        }
    }

    struct sigaction sa {};
    sa.sa_handler = on_signal;  // no SA_RESTART, so poll() returns EINTR
    sigaction(SIGINT, &sa, nullptr);
    sigaction(SIGTERM, &sa, nullptr);

    std::vector<pollfd> fds;
    for (uint16_t port : ports) {
        int fd = socket(AF_INET, SOCK_DGRAM, 0);
        if (fd < 0) { std::perror("socket"); return 1; }
        sockaddr_in addr {};
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_ANY);
        addr.sin_port = htons(port);
        if (bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof addr) < 0) {
            std::fprintf(stderr, "bind %u: %s\n", port, std::strerror(errno));
            return 1;
        }
        fds.push_back({fd, POLLIN, 0});
        std::printf("listening on udp/%u\n", port);
    }
    std::fflush(stdout);

    unsigned long echoed = 0, dropped = 0;
    unsigned char buf[2048];
    while (!g_stop) {
        int n = poll(fds.data(), fds.size(), 500);
        if (n < 0) {
            if (errno == EINTR) continue;
            std::perror("poll");
            break;
        }
        for (auto& p : fds) {
            if (!(p.revents & POLLIN)) continue;
            sockaddr_in from {};
            socklen_t fromlen = sizeof from;
            ssize_t len = recvfrom(p.fd, buf, sizeof buf, 0, reinterpret_cast<sockaddr*>(&from), &fromlen);
            if (len < 0) continue;
            ndpa::ProbePacket pkt;
            if (!ndpa::decode(buf, static_cast<size_t>(len), pkt) || pkt.type != ndpa::kProbe) {
                ++dropped;
                continue;
            }
            // Echo the bytes exactly as received.
            if (sendto(p.fd, buf, static_cast<size_t>(len), 0, reinterpret_cast<sockaddr*>(&from), fromlen) < 0)
                ++dropped;
            else
                ++echoed;
        }
    }

    for (auto& p : fds) close(p.fd);
    std::printf("shutdown: echoed=%lu dropped=%lu\n", echoed, dropped);
    return 0;
}
