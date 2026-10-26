#include "stats.h"

#include <algorithm>
#include <cmath>

namespace ndpa {

void PathStats::on_send(uint32_t seq, uint64_t send_ns) {
    outstanding_[seq] = send_ns;
    ++sent_;
}

void PathStats::on_echo(uint32_t seq, uint64_t now_ns) {
    auto it = outstanding_.find(seq);
    if (it == outstanding_.end()) {
        // Either already answered, already timed out, or never sent.
        if (std::find(lost_seqs_.begin(), lost_seqs_.end(), seq) != lost_seqs_.end())
            ++late_;
        else
            ++dups_;
        return;
    }
    double rtt = (now_ns - it->second) / 1e6;
    outstanding_.erase(it);
    ++received_;

    if (any_rx_ && seq < highest_rx_) ++reordered_;
    if (!any_rx_ || seq > highest_rx_) highest_rx_ = seq;
    any_rx_ = true;

    // RFC 3550 6.4.1: J += (|D| - J) / 16, with D the change in transit time.
    // For a round trip the transit difference is just the RTT difference.
    if (last_rtt_ms_ >= 0) jitter_ms_ += (std::fabs(rtt - last_rtt_ms_) - jitter_ms_) / 16.0;
    last_rtt_ms_ = rtt;
    push({seq, false, rtt});
}

void PathStats::expire(uint64_t now_ns) {
    for (auto it = outstanding_.begin(); it != outstanding_.end();) {
        if (now_ns >= it->second && now_ns - it->second >= timeout_ns_) {
            push({it->first, true, 0});
            lost_seqs_.push_back(it->first);
            if (lost_seqs_.size() > window_) lost_seqs_.pop_front();
            it = outstanding_.erase(it);
        } else {
            ++it;
        }
    }
}

void PathStats::push(const Result& r) {
    results_.push_back(r);
    if (results_.size() > window_) results_.pop_front();
}

PathSnapshot PathStats::snapshot() const {
    PathSnapshot s;
    s.sent = sent_;
    s.received = received_;
    s.duplicates = dups_;
    s.reordered = reordered_;
    s.late = late_;
    s.jitter_ms = jitter_ms_;
    s.window_size = static_cast<uint32_t>(results_.size());

    uint32_t lost = 0, ok = 0;
    double sum = 0;
    for (const auto& r : results_) {
        if (r.lost) { ++lost; continue; }
        if (ok == 0 || r.rtt_ms < s.rtt_min_ms) s.rtt_min_ms = r.rtt_ms;
        if (ok == 0 || r.rtt_ms > s.rtt_max_ms) s.rtt_max_ms = r.rtt_ms;
        sum += r.rtt_ms;
        ++ok;
    }
    if (ok) s.rtt_avg_ms = sum / ok;
    if (!results_.empty()) s.loss_pct = 100.0 * lost / results_.size();
    return s;
}

}  // namespace ndpa
