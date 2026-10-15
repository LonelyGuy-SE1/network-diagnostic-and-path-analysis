#include "stats.h"
#include "check.h"

using namespace ndpa;

static const uint64_t MS = 1'000'000;

static void clean_path() {
    PathStats s(10, 1000 * MS);
    for (uint32_t i = 0; i < 5; ++i) {
        s.on_send(i, i * 100 * MS);
        s.on_echo(i, i * 100 * MS, i * 100 * MS + (10 + i) * MS);  // RTT 10..14 ms
    }
    auto r = s.snapshot();
    CHECK(r.sent == 5 && r.received == 5);
    CHECK_NEAR(r.rtt_min_ms, 10, 1e-9);
    CHECK_NEAR(r.rtt_max_ms, 14, 1e-9);
    CHECK_NEAR(r.rtt_avg_ms, 12, 1e-9);
    CHECK_NEAR(r.loss_pct, 0, 1e-9);
    // Constant 1 ms steps: J_n = J_{n-1} + (1 - J_{n-1}) / 16, four times.
    double j = 0;
    for (int i = 0; i < 4; ++i) j += (1 - j) / 16;
    CHECK_NEAR(r.jitter_ms, j, 1e-9);
}

static void loss_by_missing_seq() {
    PathStats s(10, 500 * MS);
    for (uint32_t i = 0; i < 10; ++i) s.on_send(i, i * MS);
    for (uint32_t i = 0; i < 10; ++i)
        if (i % 3 != 0) s.on_echo(i, i * MS, i * MS + 5 * MS);  // 0,3,6,9 missing
    s.expire(100 * MS);
    CHECK(s.snapshot().window_size == 6);  // not timed out yet
    s.expire(1000 * MS);
    auto r = s.snapshot();
    CHECK(r.window_size == 10);
    CHECK_NEAR(r.loss_pct, 40, 1e-9);
}

static void duplicates_reorder_late() {
    PathStats s(10, 100 * MS);
    for (uint32_t i = 0; i < 4; ++i) s.on_send(i, 0);
    s.on_echo(1, 0, 5 * MS);
    s.on_echo(0, 0, 6 * MS);  // out of order
    s.on_echo(1, 0, 7 * MS);  // duplicate
    s.on_echo(2, 0, 8 * MS);
    s.expire(200 * MS);       // seq 3 lost
    s.on_echo(3, 0, 300 * MS);  // arrives after timeout
    auto r = s.snapshot();
    CHECK(r.received == 3);
    CHECK(r.duplicates == 1);
    CHECK(r.reordered == 1);
    CHECK(r.late == 1);
    CHECK_NEAR(r.loss_pct, 25, 1e-9);
}

static void window_slides() {
    PathStats s(4, 10 * MS);
    for (uint32_t i = 0; i < 4; ++i) s.on_send(i, 0);
    s.expire(20 * MS);  // 4 losses
    CHECK_NEAR(s.snapshot().loss_pct, 100, 1e-9);
    for (uint32_t i = 4; i < 8; ++i) {
        s.on_send(i, 100 * MS);
        s.on_echo(i, 100 * MS, 102 * MS);
    }
    auto r = s.snapshot();
    CHECK_NEAR(r.loss_pct, 0, 1e-9);  // old losses slid out
    CHECK_NEAR(r.rtt_avg_ms, 2, 1e-9);
}

static void send_after_now_not_expired() {
    // A probe stamped after the caller read the clock must not underflow.
    PathStats s(10, 100 * MS);
    s.on_send(0, 50 * MS);
    s.expire(49 * MS);
    CHECK(s.snapshot().window_size == 0);
}

int main() {
    send_after_now_not_expired();
    clean_path();
    loss_by_missing_seq();
    duplicates_reorder_late();
    window_slides();
    std::printf("test_stats: %s\n", g_failures ? "FAIL" : "ok");
    return g_failures != 0;
}
