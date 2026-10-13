// Wire format shared by the probe client and the reflector.
// All multi-byte fields are big-endian on the wire. Never memcpy a struct
// straight onto the socket; use encode()/decode() below.
#pragma once

#include <arpa/inet.h>
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace ndpa {

constexpr uint32_t kMagic = 0x4E445041;  // "NDPA"
constexpr uint8_t kVersion = 1;

enum MsgType : uint8_t { kProbe = 1, kEcho = 2 };

// The reflector copies the packet back unchanged, so a probe and its echo
// share one layout. kEcho exists for future use (e.g. reflector-stamped echoes).
#pragma pack(push, 1)
struct ProbePacket {
    uint32_t magic;
    uint8_t version;
    uint8_t type;
    uint16_t path_id;
    uint32_t seq;
    uint32_t flags;
    uint64_t send_ns;  // sender's CLOCK_MONOTONIC, only meaningful to the sender
};
#pragma pack(pop)

static_assert(sizeof(ProbePacket) == 24, "ProbePacket wire size changed");
static_assert(offsetof(ProbePacket, send_ns) == 16, "ProbePacket layout changed");

inline uint64_t hton64(uint64_t v) {
    return (static_cast<uint64_t>(htonl(static_cast<uint32_t>(v))) << 32) |
           htonl(static_cast<uint32_t>(v >> 32));
}
inline uint64_t ntoh64(uint64_t v) { return hton64(v); }

inline void encode(const ProbePacket& host, unsigned char* buf) {
    ProbePacket net;
    net.magic = htonl(host.magic);
    net.version = host.version;
    net.type = host.type;
    net.path_id = htons(host.path_id);
    net.seq = htonl(host.seq);
    net.flags = htonl(host.flags);
    net.send_ns = hton64(host.send_ns);
    std::memcpy(buf, &net, sizeof net);
}

// Returns false for short packets or a bad magic/version.
inline bool decode(const unsigned char* buf, size_t len, ProbePacket& out) {
    if (len < sizeof(ProbePacket)) return false;
    ProbePacket net;
    std::memcpy(&net, buf, sizeof net);
    out.magic = ntohl(net.magic);
    out.version = net.version;
    out.type = net.type;
    out.path_id = ntohs(net.path_id);
    out.seq = ntohl(net.seq);
    out.flags = ntohl(net.flags);
    out.send_ns = ntoh64(net.send_ns);
    return out.magic == kMagic && out.version == kVersion;
}

}  // namespace ndpa
