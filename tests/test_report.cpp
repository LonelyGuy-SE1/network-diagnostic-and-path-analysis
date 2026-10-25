#include <arpa/inet.h>
#include <string>

#include "check.h"
#include "report.h"

int main() {
    ndpa::PathSnapshot a;
    a.sent = 10; a.received = 9; a.window_size = 10; a.loss_pct = 10; a.rtt_avg_ms = 0.25;
    ndpa::PathSnapshot b;
    std::string s = ndpa::format_report(1234, "10.0.0.2", {{0, 5001, a}, {1, 5002, b}});
    CHECK(s.back() == '\n');
    CHECK(s.find('\n') == s.size() - 1);  // exactly one line
    CHECK(s.rfind("{\"v\":1,\"ts_ms\":1234,\"server\":\"10.0.0.2\",\"paths\":[{\"path\":0,\"port\":5001,", 0) == 0);
    CHECK(s.find("\"loss\":10.00,\"rtt_min\":0.000,\"rtt_avg\":0.250") != std::string::npos);
    CHECK(s.find("},{\"path\":1,\"port\":5002,") != std::string::npos);
    CHECK(s.find("]}\n") == s.size() - 3);

    sockaddr_in sa;
    CHECK(ndpa::parse_endpoint("192.168.100.254:9100", sa));
    CHECK(ntohs(sa.sin_port) == 9100);
    CHECK(!ndpa::parse_endpoint("192.168.100.254", sa));
    CHECK(!ndpa::parse_endpoint("192.168.100.254:0", sa));
    CHECK(!ndpa::parse_endpoint("nohost:9100", sa));
    CHECK(!ndpa::parse_endpoint("1.2.3.4:99999", sa));
    std::printf("test_report: %s\n", g_failures ? "FAIL" : "ok");
    return g_failures ? 1 : 0;
}
