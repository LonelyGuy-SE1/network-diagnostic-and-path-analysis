"""NDPA Ryu app: topology discovery, per-path flows, telemetry ingest,
path classification and rerouting (issues #9 to #14, minimal version).

    ryu-manager --observe-links controller/ndpa_controller.py

What it does:
  * Learns switches and links from LLDP (ryu.topology, --observe-links).
  * Learns where the two probe hosts attach from their first packet
    (packet-in on a port that is not an inter-switch link).
  * Enumerates loop-free paths between them. Path i is pinned by UDP port
    PROBE_PORTS[i]: requests match udp_dst, echoes match udp_src. These
    probe flows never move, so each path keeps being measured.
  * All other IPv4 and ARP traffic between the two hosts ("live traffic")
    follows the active path. It starts on path A.
  * The C++ client reports per-path stats as JSON lines over TCP (REPORT_PORT).
  * analysis.py classifies each path healthy / degraded / failed from those
    reports and from OpenFlow port status, and decides when to move live
    traffic. Moving rewrites the live flows on the switches.
  * REST view on the Ryu wsgi port (default 8080):
      GET /ndpa/topology   switches, links, hosts
      GET /ndpa/paths      path table, states, last reports, active path
      GET /ndpa/events     recent decisions with timestamps
"""
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analysis  # noqa: E402

from ryu.app.wsgi import ControllerBase, WSGIApplication, route  # noqa: E402
from ryu.base import app_manager  # noqa: E402
from ryu.controller import ofp_event  # noqa: E402
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,  # noqa: E402
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub  # noqa: E402
from ryu.lib.packet import arp, ether_types, ethernet, ipv4, packet  # noqa: E402
from ryu.ofproto import ofproto_v1_3  # noqa: E402
from ryu.topology import event as topo_event  # noqa: E402
from webob import Response  # noqa: E402

SRC_IP = os.environ.get('NDPA_SRC_IP', '10.0.0.1')    # probe client host
DST_IP = os.environ.get('NDPA_DST_IP', '10.0.0.2')    # reflector host
PROBE_PORTS = [int(p) for p in os.environ.get('NDPA_PROBE_PORTS', '5001,5002').split(',')]
REPORT_PORT = int(os.environ.get('NDPA_REPORT_PORT', '9100'))
PORT_STATS_PERIOD_S = 2.0

PRIO_PROBE, PRIO_LIVE, PRIO_MISS = 200, 100, 0
COOKIE_PROBE, COOKIE_LIVE = 0x10, 0x20          # probe cookie is 0x10 + path index
ETH_IP, ETH_ARP, PROTO_UDP = 0x0800, 0x0806, 17


def sw(dpid):
    return 's%d' % dpid


def now_str(ts=None):
    ts = time.time() if ts is None else ts
    return time.strftime('%H:%M:%S', time.localtime(ts)) + '.%03d' % int((ts % 1) * 1000)


class NdpaController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dps = {}                  # dpid -> datapath
        self.sw_ports = {}             # dpid -> set(port_no)
        self.links = {}                # (dpid, port) -> (dpid, port), as LLDP sees them now
        self.adj = {}                  # dpid -> {nbr: out_port}; kept when a link drops
        self.link_ports = set()        # every (dpid, port) ever seen on a link
        self.down_ports = set()
        self.hosts = {}                # ip -> (dpid, port, mac)
        self.paths = []                # analysis.PathState
        self.active = None             # index into self.paths for live traffic
        self.last_reroute = 0.0
        self.reroutes = []
        self.live = {}                 # non-probe ports the client reported, port -> sample
        self.port_counters = {}        # (dpid, port) -> counters
        self.reports = 0
        self.bad_reports = 0
        self.decisions = collections.deque(maxlen=200)   # not "events": RyuApp uses that name for its queue
        kwargs['wsgi'].register(NdpaRest, {'ndpa': self})
        hub.spawn(self._serve_reports)
        hub.spawn(self._poll_port_stats)

    def note(self, kind, msg, ts=None):
        ts = time.time() if ts is None else ts
        self.decisions.append({'t': now_str(ts), 'kind': kind, 'msg': msg})
        self.logger.info('%s %-8s %s', now_str(ts), kind, msg)

    # ---- switches and flows -------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.dps[dp.id] = dp
        elif dp.id in self.dps:
            del self.dps[dp.id]

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        # Table-miss to the controller: used to learn where hosts attach.
        self._flow(dp, PRIO_MISS, parser.OFPMatch(),
                   [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)], cookie=0)

    def _flow(self, dp, prio, match, actions, cookie):
        parser = dp.ofproto_parser
        inst = [parser.OFPInstructionActions(dp.ofproto.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=cookie, priority=prio,
                                      match=match, instructions=inst))

    def _out(self, dpid, prio, match, port, cookie):
        dp = self.dps[dpid]
        self._flow(dp, prio, match, [dp.ofproto_parser.OFPActionOutput(port)], cookie)

    def _delete_cookie(self, dpid, cookie):
        dp = self.dps[dpid]
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=cookie, cookie_mask=0xffffffffffffffff,
                                      table_id=ofp.OFPTT_ALL, command=ofp.OFPFC_DELETE,
                                      out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                      match=parser.OFPMatch()))

    # ---- topology discovery (LLDP via ryu.topology) --------------------------

    @set_ev_cls(topo_event.EventSwitchEnter)
    def _switch_enter(self, ev):
        s = ev.switch
        self.sw_ports[s.dp.id] = {p.port_no for p in s.ports}
        self.note('TOPO', 'switch %s up, ports %s' % (sw(s.dp.id), sorted(self.sw_ports[s.dp.id])))

    @set_ev_cls(topo_event.EventLinkAdd)
    def _link_add(self, ev):
        src, dst = ev.link.src, ev.link.dst
        key = (src.dpid, src.port_no)
        if self.links.get(key) == (dst.dpid, dst.port_no):
            return
        self.links[key] = (dst.dpid, dst.port_no)
        self.adj.setdefault(src.dpid, {})[dst.dpid] = src.port_no
        self.link_ports.add(key)
        self.link_ports.add((dst.dpid, dst.port_no))
        self.note('TOPO', 'link %s:%d -> %s:%d' % (sw(src.dpid), src.port_no, sw(dst.dpid), dst.port_no))

    @set_ev_cls(topo_event.EventLinkDelete)
    def _link_delete(self, ev):
        src, dst = ev.link.src, ev.link.dst
        if self.links.pop((src.dpid, src.port_no), None):
            # The path table keeps the link; the port state marks the path failed.
            self.note('TOPO', 'link lost %s:%d -> %s:%d' % (sw(src.dpid), src.port_no,
                                                            sw(dst.dpid), dst.port_no))

    def _is_edge(self, dpid, port):
        return bool(self.link_ports) and (dpid, port) not in self.link_ports

    # ---- host discovery and path setup --------------------------------------

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        ip4, a = pkt.get_protocol(ipv4.ipv4), pkt.get_protocol(arp.arp)
        if ip4 is not None:
            src_ip, dst_ip = ip4.src, ip4.dst
        elif a is not None:
            src_ip, dst_ip = a.src_ip, a.dst_ip
        else:
            return                                   # IPv6 noise etc.
        if not self._is_edge(dp.id, in_port):
            return                                   # topology not known yet, or not from a host
        if src_ip not in self.hosts:
            self.hosts[src_ip] = (dp.id, in_port, eth.src)
            self.note('HOST', '%s (%s) at %s port %d' % (src_ip, eth.src, sw(dp.id), in_port))
            self._maybe_build_paths()
        # Deliver this one packet: straight to the destination if known,
        # otherwise to every host-facing port (never onto inter-switch links,
        # so the loop in the diamond cannot cause a storm).
        if dst_ip in self.hosts:
            ddp, dport, _ = self.hosts[dst_ip]
            self._packet_out(ddp, dport, msg.data)
        else:
            for dpid, ports in self.sw_ports.items():
                for port in ports:
                    if port < 0xff00 and self._is_edge(dpid, port) and (dpid, port) != (dp.id, in_port):
                        self._packet_out(dpid, port, msg.data)

    def _packet_out(self, dpid, port, data):
        dp = self.dps.get(dpid)
        if dp is None:
            return
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                        in_port=ofp.OFPP_CONTROLLER,
                                        actions=[parser.OFPActionOutput(port)], data=data))

    def _hops(self, route):
        """Forward (src->dst) and reverse hop lists of (dpid, out_port)."""
        s_dp, s_port, _ = self.hosts[SRC_IP]
        d_dp, d_port, _ = self.hosts[DST_IP]
        fwd = [(route[i], self.adj[route[i]][route[i + 1]]) for i in range(len(route) - 1)]
        fwd.append((route[-1], d_port))
        rev = [(route[i], self.adj[route[i]][route[i - 1]]) for i in range(len(route) - 1, 0, -1)]
        rev.append((route[0], s_port))
        return fwd, rev

    def _maybe_build_paths(self):
        if self.paths or SRC_IP not in self.hosts or DST_IP not in self.hosts:
            return
        routes = analysis.simple_paths(self.adj, self.hosts[SRC_IP][0], self.hosts[DST_IP][0])
        if not routes:
            self.note('PATHS', 'no route %s -> %s yet' % (SRC_IP, DST_IP))
            return
        for i, r in enumerate(routes[:len(PROBE_PORTS)]):
            fwd, rev = self._hops(r)
            self.paths.append(analysis.PathState(i, r, PROBE_PORTS[i], ports=fwd + rev))
        for p in self.paths:
            self.note('PATHS', 'path %s probe udp/%d' % (p.label(), p.probe_port))
            self._install_probe(p)
        if len(routes) > len(PROBE_PORTS):
            self.note('PATHS', '%d more route(s) without a probe port, ignored'
                      % (len(routes) - len(PROBE_PORTS)))
        self.active = 0
        self._install_live(self.paths[0])
        self.note('ACTIVE', 'live traffic %s <-> %s on path %s' % (SRC_IP, DST_IP, self.paths[0].label()))

    def _install_probe(self, p):
        fwd, rev = self._hops(p.route)
        parser = self.dps[p.route[0]].ofproto_parser
        m_fwd = parser.OFPMatch(eth_type=ETH_IP, ipv4_src=SRC_IP, ipv4_dst=DST_IP,
                                ip_proto=PROTO_UDP, udp_dst=p.probe_port)
        m_rev = parser.OFPMatch(eth_type=ETH_IP, ipv4_src=DST_IP, ipv4_dst=SRC_IP,
                                ip_proto=PROTO_UDP, udp_src=p.probe_port)
        for dpid, port in fwd:
            self._out(dpid, PRIO_PROBE, m_fwd, port, COOKIE_PROBE + p.idx)
        for dpid, port in rev:
            self._out(dpid, PRIO_PROBE, m_rev, port, COOKIE_PROBE + p.idx)

    def _install_live(self, p):
        """Live IPv4 + ARP between the hosts along p. Downstream switches first,
        so the ingress switch only turns traffic over once the rest is ready."""
        fwd, rev = self._hops(p.route)
        parser = self.dps[p.route[0]].ofproto_parser
        for hops, s, d in ((fwd, SRC_IP, DST_IP), (rev, DST_IP, SRC_IP)):
            for dpid, port in reversed(hops):
                self._out(dpid, PRIO_LIVE, parser.OFPMatch(eth_type=ETH_IP, ipv4_src=s, ipv4_dst=d),
                          port, COOKIE_LIVE)
                self._out(dpid, PRIO_LIVE, parser.OFPMatch(eth_type=ETH_ARP, arp_tpa=d), port, COOKIE_LIVE)

    # ---- telemetry ingest (JSON lines over TCP, issue #11) -------------------

    def _serve_reports(self):
        server = hub.StreamServer(('0.0.0.0', REPORT_PORT), self._report_conn)
        self.note('INGEST', 'listening on tcp/%d for probe reports' % REPORT_PORT)
        server.serve_forever()

    def _report_conn(self, sock, addr):
        self.note('INGEST', 'reporter connected from %s:%d' % addr)
        buf = b''
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                buf += data
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    self._on_report(line)
                if len(buf) > 65536:
                    self.note('INGEST', 'dropping 64 KiB without a newline')
                    buf = b''
        except OSError as e:
            self.note('INGEST', 'reporter %s:%d error %s' % (addr[0], addr[1], e))
        finally:
            sock.close()
        self.note('INGEST', 'reporter %s:%d disconnected' % addr)

    def _on_report(self, line):
        now = time.time()
        try:
            rep = json.loads(line.decode())
            entries = rep['paths']
            samples = []
            for e in entries:
                samples.append({k: float(e[k]) for k in ('loss', 'rtt_min', 'rtt_avg', 'rtt_max', 'jitter')})
                samples[-1].update(port=int(e['port']), sent=int(e['sent']), recv=int(e['recv']))
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError) as e:
            self.bad_reports += 1
            if self.bad_reports <= 5 or self.bad_reports % 100 == 0:
                self.note('INGEST', 'bad report #%d (%s): %r' % (self.bad_reports, e, line[:120]))
            return
        self.reports += 1
        by_port = {p.probe_port: p for p in self.paths}
        parts = []
        for s in samples:
            p = by_port.get(s['port'])
            if p is None:
                self.live[s['port']] = dict(s, t=now)
                parts.append('live udp/%d loss %.1f%%' % (s['port'], s['loss']))
                continue
            old = p.update(s['loss'], s['rtt_avg'], now, sample=s)
            parts.append('%s=%s loss %.1f%% rtt %.3f ms' % (p.name, p.state, s['loss'], s['rtt_avg']))
            if old is not None:
                self.note('CLASSIFY', 'path %s %s -> %s: %s' % (p.label(), old, p.state, p.reason), now)
        self.logger.info('%s REPORT   %s', now_str(now), ' | '.join(parts))
        self._evaluate(now)

    # ---- failures: port status and port stats (issue #12) --------------------

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status(self, ev):
        msg, now = ev.msg, time.time()
        dp = msg.datapath
        ofp = dp.ofproto
        d = msg.desc
        key = (dp.id, d.port_no)
        if msg.reason == ofp.OFPPR_ADD:
            self.sw_ports.setdefault(dp.id, set()).add(d.port_no)
        down = (msg.reason == ofp.OFPPR_DELETE or bool(d.state & ofp.OFPPS_LINK_DOWN)
                or bool(d.config & ofp.OFPPC_PORT_DOWN))
        if down == (key in self.down_ports):
            return
        (self.down_ports.add if down else self.down_ports.discard)(key)
        self.note('PORT', '%s:%d %s' % (sw(dp.id), d.port_no, 'down' if down else 'up'), now)
        for p in self.paths:
            old = p.set_port(key, down, now)
            if old is not None:
                self.note('CLASSIFY', 'path %s %s -> %s: %s' % (p.label(), old, p.state, p.reason), now)
        self._evaluate(now)

    def _poll_port_stats(self):
        while True:
            for dp in list(self.dps.values()):
                dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))
            hub.sleep(PORT_STATS_PERIOD_S)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats(self, ev):
        for st in ev.msg.body:
            self.port_counters[(ev.msg.datapath.id, st.port_no)] = {
                'rx_packets': st.rx_packets, 'tx_packets': st.tx_packets,
                'rx_dropped': st.rx_dropped, 'tx_dropped': st.tx_dropped,
                'rx_errors': st.rx_errors, 'tx_errors': st.tx_errors}

    # ---- decisions (issues #13, #14) ----------------------------------------

    def _evaluate(self, now):
        if self.active is None:
            return
        new, why = analysis.choose_active(self.paths, self.active, now, self.last_reroute)
        if new is not None:
            self._reroute(new, why, now)

    def _reroute(self, new, why, now):
        old_p, new_p = self.paths[self.active], self.paths[new]
        detect_ms = (now - old_p.first_bad_ts) * 1000 if old_p.first_bad_ts else 0.0
        self.note('REROUTE', 'live traffic %s -> %s because %s' % (old_p.label(), new_p.label(), why), now)
        self._install_live(new_p)
        for dpid in set(old_p.route) - set(new_p.route):
            self._delete_cookie(dpid, COOKIE_LIVE)
        done = time.time()
        self.active, self.last_reroute = new, done
        rec = {'t': now_str(now), 'from': old_p.name, 'to': new_p.name, 'reason': why,
               'first_bad_to_reroute_ms': round(detect_ms, 1),
               'flowmods_ms': round((done - now) * 1000, 1)}
        self.reroutes.append(rec)
        self.note('REROUTE', 'done: live traffic on %s, %.0f ms after the first bad sign, '
                  'flow-mods sent in %.1f ms' % (new_p.label(), detect_ms, (done - now) * 1000), done)

    # ---- REST views --------------------------------------------------------

    def topology_view(self):
        return {
            'switches': sorted(sw(d) for d in self.sw_ports),
            'links': sorted('%s:%d->%s:%d' % (sw(a[0]), a[1], sw(b[0]), b[1]) for a, b in self.links.items()),
            'hosts': {ip: {'switch': sw(h[0]), 'port': h[1], 'mac': h[2]} for ip, h in self.hosts.items()},
            'ports_down': sorted('%s:%d' % (sw(k[0]), k[1]) for k in self.down_ports),
        }

    def paths_view(self):
        now = time.time()
        out = []
        for p in self.paths:
            out.append({
                'name': p.name, 'route': [sw(d) for d in p.route], 'probe_port': p.probe_port,
                'state': p.state, 'reason': p.reason, 'active': p.idx == self.active,
                'last_report': p.last, 'report_age_s': round(now - p.last_ts, 1) if p.last_ts else None,
                'ports_down': sorted('%s:%d' % (sw(k[0]), k[1]) for k in p.down),
                'port_counters': {'%s:%d' % (sw(k[0]), k[1]): self.port_counters.get(k)
                                  for k in sorted(p.ports)},
            })
        return {'active': self.paths[self.active].name if self.active is not None else None,
                'paths': out, 'live': self.live, 'reroutes': self.reroutes,
                'reports': self.reports, 'bad_reports': self.bad_reports,
                'thresholds': {'degraded_loss_pct': analysis.DEGRADED_LOSS_PCT,
                               'failed_loss_pct': analysis.FAILED_LOSS_PCT,
                               'degraded_rtt_ms': analysis.DEGRADED_RTT_MS,
                               'worse_reports': analysis.WORSE_REPORTS,
                               'better_reports': analysis.BETTER_REPORTS,
                               'hold_time_s': analysis.HOLD_TIME_S}}


class NdpaRest(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app = data['ndpa']

    @staticmethod
    def _json(obj):
        return Response(content_type='application/json', body=(json.dumps(obj, indent=1) + '\n').encode())

    @route('ndpa', '/ndpa/topology', methods=['GET'])
    def topology(self, req, **kwargs):
        return self._json(self.app.topology_view())

    @route('ndpa', '/ndpa/paths', methods=['GET'])
    def paths(self, req, **kwargs):
        return self._json(self.app.paths_view())

    @route('ndpa', '/ndpa/events', methods=['GET'])
    def events(self, req, **kwargs):
        return self._json(list(self.app.decisions))
