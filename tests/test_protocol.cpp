#include "protocol.h"
#include "check.h"

using namespace ndpa;

int main() {
    CHECK(ntoh64(hton64(0x0102030405060708ull)) == 0x0102030405060708ull);

    ProbePacket in {kMagic, kVersion, kProbe, 0xBEEF, 0xDEADBEEF, 0x11223344, 0x0102030405060708ull};
    unsigned char buf[sizeof(ProbePacket)];
    encode(in, buf);

    // Bytes on the wire are big-endian regardless of host order.
    const unsigned char magic[] = {'N', 'D', 'P', 'A'};
    for (int i = 0; i < 4; ++i) CHECK(buf[i] == magic[i]);
    CHECK(buf[4] == kVersion && buf[5] == kProbe);
    CHECK(buf[6] == 0xBE && buf[7] == 0xEF);
    CHECK(buf[8] == 0xDE && buf[11] == 0xEF);
    for (int i = 0; i < 8; ++i) CHECK(buf[16 + i] == i + 1);

    ProbePacket out {};
    CHECK(decode(buf, sizeof buf, out));
    CHECK(out.path_id == in.path_id && out.seq == in.seq && out.flags == in.flags && out.send_ns == in.send_ns);

    CHECK(!decode(buf, sizeof buf - 1, out));  // short
    buf[0] ^= 0xFF;
    CHECK(!decode(buf, sizeof buf, out));      // bad magic
    buf[0] ^= 0xFF;
    buf[4] = kVersion + 1;
    CHECK(!decode(buf, sizeof buf, out));      // bad version

    std::printf("test_protocol: %s\n", g_failures ? "FAIL" : "ok");
    return g_failures != 0;
}
