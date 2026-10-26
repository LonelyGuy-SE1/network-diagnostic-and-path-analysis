// Per-path RTT, jitter and loss tracking. Pure logic, no sockets, so it can
// be unit tested with synthetic sequences.
#pragma once

#include <cstdint>
#include <deque>
#include <map>

namespace ndpa {

struct PathSnapshot {
    uint64_t sent = 0;
    uint64_t received = 0;    // unique echoes over the whole run
    uint64_t duplicates = 0;
    uint64_t reordered = 0;
    uint64_t late = 0;        // echoes arriving after their timeout
    double rtt_min_ms = 0, rtt_avg_ms = 0, rtt_max_ms = 0;  // over the window
    double jitter_ms = 0;     // RFC 3550 smoothed estimate
    double loss_pct = 0;      // over the window, by missing seq
    uint32_t window_size = 0; // resolved probes counted in the window
};

class PathStats {
public:
    explicit PathStats(uint32_t window = 100, uint64_t timeout_ns = 1'000'000'000)
        : window_(window), timeout_ns_(timeout_ns) {}

    // Record an outgoing probe. Seqs must be increasing.
    void on_send(uint32_t seq, uint64_t send_ns);
    // Record an echo. RTT uses the stored send time, not the echoed one.
    void on_echo(uint32_t seq, uint64_t now_ns);
    // Mark outstanding probes older than the timeout as lost.
    void expire(uint64_t now_ns);

    PathSnapshot snapshot() const;

private:
    struct Result { uint32_t seq; bool lost; double rtt_ms; };
    void push(const Result& r);

    uint32_t window_;
    uint64_t timeout_ns_;
    std::map<uint32_t, uint64_t> outstanding_;  // seq -> send time
    std::deque<Result> results_;                // resolved probes, oldest first
    std::deque<uint32_t> lost_seqs_;            // so late echoes can be told apart
    uint64_t sent_ = 0, received_ = 0, dups_ = 0, reordered_ = 0, late_ = 0;
    uint32_t highest_rx_ = 0;
    bool any_rx_ = false;
    double jitter_ms_ = 0;
    double last_rtt_ms_ = -1;
};

}  // namespace ndpa
