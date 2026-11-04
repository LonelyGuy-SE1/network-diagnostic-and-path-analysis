"""NDPA Ryu app: topology, per-path probes, classification, reroute.

    ryu-manager --observe-links controller/ndpa_controller.py
"""
import collections
import json
import mimetypes
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(_ROOT, 'frontend')
TOPO_JSON = os.path.join(_ROOT, 'topology', 'topo.json')
AGENT_DIR = os.environ.get('NDPA_GUI_AGENT', '/tmp/ndpa_gui_agent')
AGENT_HEART = os.path.join(AGENT_DIR, 'heartbeat')
AGENT_REQ = os.path.join(AGENT_DIR, 'request.json')
AGENT_REPLY = os.path.join(AGENT_DIR, 'reply.json')
AGENT_VER_PATH = os.path.join(AGENT_DIR, 'version')
AGENT_VERSION = '2'
AGENT_HINT = 'stack not armed. run once: ./demo/gui.sh'
AGENT_STALE = (
    'Sudo helper is stale. In the ./demo/gui.sh terminal: Ctrl-C, '
    'then run ./demo/gui.sh again. Then press Start.'
)


def _agent_ok():
    try:
        with open(AGENT_HEART) as f:
            age = time.time() - float(f.read().strip())
        return age < 5.0
    except (OSError, ValueError):
        return False


def _agent_version():
    try:
        with open(AGENT_VER_PATH) as f:
            return f.read().strip()
    except OSError:
        return ''


def _agent_stale():
    return _agent_ok() and _agent_version() != AGENT_VERSION


def _stale_start_detail(detail):
    s = (detail or '').lower()
    return ('gui_run.sh' in s or 'gui_demo.sh' in s or 'unknown op' in s)


def _agent_reply():
    try:
        with open(AGENT_REPLY) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _enqueue_gui(payload):
    """Ask the root gui agent to do work. Returns (ok, message)."""
    if not isinstance(payload, dict) or not payload.get('op'):
        return False, 'bad request'
    if not _agent_ok():
        return False, AGENT_HINT
    os.makedirs(AGENT_DIR, exist_ok=True)
    body = dict(payload)
    body['id'] = '%.6f' % time.time()
    tmp = AGENT_REQ + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(body, f)
            f.write('\n')
        os.rename(tmp, AGENT_REQ)
    except OSError as e:
        return False, str(e)
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            with open(AGENT_REPLY) as f:
                reply = json.load(f)
            if reply.get('id') == body['id']:
                if reply.get('ok'):
                    return True, reply.get('detail') or ('queued %s' % body.get('op'))
                return False, reply.get('detail') or reply.get('error') or 'agent refused'
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    return True, 'queued %s' % body.get('op')


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

SRC_IP = os.environ.get('NDPA_SRC_IP', '10.0.0.1')  # probe client
DST_IP = os.environ.get('NDPA_DST_IP', '10.0.0.2')  # reflector
LIVE_PORT = int(os.environ.get('NDPA_LIVE_PORT', '5003'))  # live traffic, not a probe
REPORT_PORT = int(os.environ.get('NDPA_REPORT_PORT', '9100'))


def _probe_ports():
    raw = os.environ.get('NDPA_PROBE_PORTS')
    if raw:
        return [int(p) for p in raw.split(',') if p.strip()]
    out, p = [], 5001
    while len(out) < 8:
        if p != LIVE_PORT:
            out.append(p)
        p += 1
    return out


PROBE_PORTS = _probe_ports()
PORT_STATS_PERIOD_S = 2.0

PRIO_PROBE, PRIO_LIVE, PRIO_MISS = 200, 100, 0
COOKIE_PROBE, COOKIE_LIVE = 0x10, 0x20  # probe cookie is 0x10 + path index
ETH_IP, ETH_ARP, PROTO_UDP = 0x0800, 0x0806, 17


def sw(dpid):
    return 's%d' % dpid


def now_str(ts):
    return time.strftime('%H:%M:%S', time.localtime(ts)) + '.%03d' % int((ts % 1) * 1000)


class NdpaController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'wsgi': WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dps = {}
        self.sw_ports = {}
        self.links = {}
        self.adj = {}  # kept when a link drops so failed paths stay enumerable
        self.link_ports = set()
        self.down_ports = set()
        self.hosts = {}
        self.paths = []
        self.active = None
        self.src_ip = SRC_IP
        self.dst_ip = DST_IP
        self.last_reroute = 0.0
        self.reroutes = []
        self.live = {}
        self.port_counters = {}
        self.reports = 0
        self.bad_reports = 0
        self.starting_until = 0.0
        self.decisions = collections.deque(maxlen=200)  # not "events": RyuApp owns that name
        kwargs['wsgi'].register(NdpaRest, {'ndpa': self})
        hub.spawn(self._serve_reports)
        hub.spawn(self._poll_port_stats)

    def note(self, kind, msg, ts=None):
        ts = time.time() if ts is None else ts
        self.decisions.append({'t': now_str(ts), 'kind': kind, 'msg': msg})
        self.logger.info('%s %-8s %s', now_str(ts), kind, msg)

    def reset_analysis(self, why='reset'):
        """Drop path table and learned topology so a new Mininet run starts clean."""
        self.paths = []
        self.active = None
        self.down_ports = set()
        self.hosts = {}
        self.live = {}
        self.reroutes = []
        self.last_reroute = 0.0
        self.links = {}
        self.adj = {}
        self.link_ports = set()
        self.note('PATHS', 'cleared path table (%s)' % why)

    def _drop_switch_topo(self, dpid):
        """Forget LLDP/port roles for one dpid. Same id comes back on the next
        Mininet with different port numbers (host vs core), so sticky link_ports
        would hide h2 behind an old inter-switch port."""
        self.adj.pop(dpid, None)
        for nbr in list(self.adj):
            self.adj[nbr].pop(dpid, None)
            if not self.adj[nbr]:
                del self.adj[nbr]
        self.links = {k: v for k, v in self.links.items()
                      if k[0] != dpid and v[0] != dpid}
        self.link_ports = {k for k in self.link_ports if k[0] != dpid}
        self.down_ports = {k for k in self.down_ports if k[0] != dpid}

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self._drop_switch_topo(dp.id)
            self.dps[dp.id] = dp
        elif dp.id in self.dps:
            del self.dps[dp.id]
            self.sw_ports.pop(dp.id, None)
            self._drop_switch_topo(dp.id)
            if not self.dps:
                self.reset_analysis('dataplane gone')

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
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
        self._maybe_build_paths()

    @set_ev_cls(topo_event.EventLinkDelete)
    def _link_delete(self, ev):
        src, dst = ev.link.src, ev.link.dst
        if self.links.pop((src.dpid, src.port_no), None):
            self.note('TOPO', 'link lost %s:%d -> %s:%d' % (sw(src.dpid), src.port_no,
                                                            sw(dst.dpid), dst.port_no))

    def _is_edge(self, dpid, port):
        return bool(self.link_ports) and (dpid, port) not in self.link_ports

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
            return
        if not self._is_edge(dp.id, in_port):
            return
        if src_ip not in self.hosts:
            self.hosts[src_ip] = (dp.id, in_port, eth.src)
            self.note('HOST', '%s (%s) at %s port %d' % (src_ip, eth.src, sw(dp.id), in_port))
            self._maybe_build_paths()
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
        s_dp, s_port, _ = self.hosts[self.src_ip]
        d_dp, d_port, _ = self.hosts[self.dst_ip]
        fwd = [(route[i], self.adj[route[i]][route[i + 1]]) for i in range(len(route) - 1)]
        fwd.append((route[-1], d_port))
        rev = [(route[i], self.adj[route[i]][route[i - 1]]) for i in range(len(route) - 1, 0, -1)]
        rev.append((route[0], s_port))
        return fwd, rev

    def _core_ports(self, fwd, rev):
        """Inter-switch ports only (exclude host edges shared by every path)."""
        host_ports = {(h[0], h[1]) for h in self.hosts.values()}
        return [hp for hp in (fwd + rev) if hp not in host_ports]

    def _fault_intf(self, route):
        """Mininet OVS netdev to hit with tc/netem for soft faults (middle hop)."""
        if len(route) < 3:
            return None
        mid, nxt = route[1], route[2]
        port = self.adj.get(mid, {}).get(nxt)
        if port is None:
            return None
        return '%s-eth%d' % (sw(mid), port)

    def _route_ready(self, route):
        """True when both directions of every hop are in adj (LLDP finished)."""
        for i in range(len(route) - 1):
            a, b = route[i], route[i + 1]
            if b not in self.adj.get(a, {}) or a not in self.adj.get(b, {}):
                return False
        return True

    def _maybe_build_paths(self):
        if self.src_ip not in self.hosts or self.dst_ip not in self.hosts:
            return
        routes = [r for r in analysis.simple_paths(
            self.adj, self.hosts[self.src_ip][0], self.hosts[self.dst_ip][0]
        ) if self._route_ready(r)]
        if not routes:
            self.note('PATHS', 'no route %s -> %s yet' % (self.src_ip, self.dst_ip))
            return
        if self.paths:
            have = {tuple(p.route) for p in self.paths}
            extra = [r for r in routes if tuple(r) not in have]
            for r in extra:
                i = len(self.paths)
                if i >= len(PROBE_PORTS):
                    self.note('PATHS', '%d more route(s) without a probe port, ignored'
                              % (len(extra) - extra.index(r)))
                    break
                fwd, rev = self._hops(r)
                p = analysis.PathState(i, r, PROBE_PORTS[i], ports=self._core_ports(fwd, rev))
                self.paths.append(p)
                self.note('PATHS', 'path %s probe udp/%d' % (p.name, p.probe_port))
                self._install_probe(p)
            return
        for i, r in enumerate(routes[:len(PROBE_PORTS)]):
            fwd, rev = self._hops(r)
            self.paths.append(analysis.PathState(
                i, r, PROBE_PORTS[i], ports=self._core_ports(fwd, rev)))
        for p in self.paths:
            self.note('PATHS', 'path %s probe udp/%d' % (p.name, p.probe_port))
            self._install_probe(p)
        if len(routes) > len(PROBE_PORTS):
            self.note('PATHS', '%d more route(s) without a probe port, ignored'
                      % (len(routes) - len(PROBE_PORTS)))
        self.active = 0
        self._install_live(self.paths[0])
        self.note('ACTIVE', 'live traffic %s <-> %s on %s'
                  % (self.src_ip, self.dst_ip, self.paths[0].name))

    def _install_probe(self, p):
        fwd, rev = self._hops(p.route)
        parser = self.dps[p.route[0]].ofproto_parser
        m_fwd = parser.OFPMatch(eth_type=ETH_IP, ipv4_src=self.src_ip, ipv4_dst=self.dst_ip,
                                ip_proto=PROTO_UDP, udp_dst=p.probe_port)
        m_rev = parser.OFPMatch(eth_type=ETH_IP, ipv4_src=self.dst_ip, ipv4_dst=self.src_ip,
                                ip_proto=PROTO_UDP, udp_src=p.probe_port)
        for dpid, port in fwd:
            self._out(dpid, PRIO_PROBE, m_fwd, port, COOKIE_PROBE + p.idx)
        for dpid, port in rev:
            self._out(dpid, PRIO_PROBE, m_rev, port, COOKIE_PROBE + p.idx)

    def _install_live(self, p):
        """Live IPv4 + ARP along p. Downstream switches first so ingress is last."""
        fwd, rev = self._hops(p.route)
        parser = self.dps[p.route[0]].ofproto_parser
        for hops, s, d in ((fwd, self.src_ip, self.dst_ip), (rev, self.dst_ip, self.src_ip)):
            for dpid, port in reversed(hops):
                self._out(dpid, PRIO_LIVE, parser.OFPMatch(eth_type=ETH_IP, ipv4_src=s, ipv4_dst=d),
                          port, COOKIE_LIVE)
                self._out(dpid, PRIO_LIVE, parser.OFPMatch(eth_type=ETH_ARP, arp_tpa=d), port, COOKIE_LIVE)

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
                self.note('CLASSIFY', 'path %s %s -> %s: %s' % (p.name, old, p.state, p.reason), now)
        self.logger.info('%s REPORT   %s', now_str(now), ' | '.join(parts))
        self._evaluate(now)

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
                self.note('CLASSIFY', 'path %s %s -> %s: %s' % (p.name, old, p.state, p.reason), now)
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

    def _evaluate(self, now):
        if self.active is None:
            return
        new, why = analysis.choose_active(self.paths, self.active, now, self.last_reroute)
        if new is not None:
            self._reroute(new, why, now)

    def _reroute(self, new, why, now):
        old_p, new_p = self.paths[self.active], self.paths[new]
        detect_ms = (now - old_p.first_bad_ts) * 1000 if old_p.first_bad_ts else 0.0
        self.note('REROUTE', 'live traffic %s -> %s because %s' % (old_p.name, new_p.name, why), now)
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
                  'flow-mods sent in %.1f ms' % (new_p.name, detect_ms, (done - now) * 1000), done)

    def topology_view(self):
        link_intfs = {}
        for (a, pa), (b, pb) in self.links.items():
            key = '%s-%s' % tuple(sorted((sw(a), sw(b))))
            link_intfs.setdefault(key, []).append('%s-eth%d' % (sw(a), pa))
        return {
            'up': bool(self.dps) and bool(self.links),
            'switches': sorted(sw(d) for d in self.sw_ports),
            'links': sorted('%s:%d->%s:%d' % (sw(a[0]), a[1], sw(b[0]), b[1]) for a, b in self.links.items()),
            'link_intfs': link_intfs,
            'hosts': {ip: {'switch': sw(h[0]), 'port': h[1], 'mac': h[2]} for ip, h in self.hosts.items()},
            'ports_down': sorted('%s:%d' % (sw(k[0]), k[1]) for k in self.down_ports),
        }

    def paths_view(self):
        now = time.time()
        out = []
        for p in self.paths:
            route = [sw(d) for d in p.route]
            out.append({
                'name': p.name, 'route': route, 'probe_port': p.probe_port,
                'state': p.state, 'reason': p.reason, 'active': p.idx == self.active,
                'last_report': p.last, 'report_age_s': round(now - p.last_ts, 1) if p.last_ts else None,
                'ports_down': sorted('%s:%d' % (sw(k[0]), k[1]) for k in p.down),
                'port_counters': {'%s:%d' % (sw(k[0]), k[1]): self.port_counters.get(k)
                                  for k in sorted(p.ports)},
                'fault_intf': self._fault_intf(p.route),
            })
        active = self.paths[self.active] if self.active is not None else None
        return {
            'active': active.name if active else None,
            'active_route': [sw(d) for d in active.route] if active else None,
            'active_state': active.state if active else None,
            'src': self.src_ip, 'dst': self.dst_ip,
            'paths': out, 'live': self.live, 'reroutes': self.reroutes,
            'reports': self.reports, 'bad_reports': self.bad_reports,
            'thresholds': {'degraded_loss_pct': analysis.DEGRADED_LOSS_PCT,
                           'failed_loss_pct': analysis.FAILED_LOSS_PCT,
                           'degraded_rtt_ms': analysis.DEGRADED_RTT_MS,
                           'worse_reports': analysis.WORSE_REPORTS,
                           'better_reports': analysis.BETTER_REPORTS,
                           'hold_time_s': analysis.HOLD_TIME_S},
        }

    def health_view(self):
        now = time.time()
        agent = _agent_ok()
        dataplane = bool(self.dps) and bool(self.links)
        hosts_ok = self.src_ip in self.hosts and self.dst_ip in self.hosts
        paths_ok = len(self.paths) >= 1
        fresh = False
        if self.paths:
            ages = [now - p.last_ts for p in self.paths if p.last_ts]
            fresh = bool(ages) and min(ages) < 8.0
        starting = now < float(self.starting_until or 0)
        reply = _agent_reply() or {}
        reply_age = now - float(reply['ts']) if reply.get('ts') else 1e9
        last_fail = None
        if (reply.get('op') == 'run' and reply.get('ok') is False
                and reply_age < 60):
            last_fail = reply.get('detail') or reply.get('error')
            starting = False
        agent_current = agent and not _agent_stale()
        if last_fail and _stale_start_detail(last_fail):
            agent_current = False
        checks = [
            {'id': 'controller', 'ok': True, 'label': 'controller'},
            {'id': 'agent', 'ok': agent_current,
             'label': 'sudo helper (./demo/gui.sh)'},
            {'id': 'dataplane', 'ok': dataplane, 'label': 'Mininet dataplane'},
            {'id': 'hosts', 'ok': hosts_ok, 'label': 'probe hosts learned'},
            {'id': 'paths', 'ok': paths_ok, 'label': 'paths discovered'},
            {'id': 'probes', 'ok': fresh, 'label': 'probe reports'},
        ]
        if not agent:
            action, message = (
                'gui.sh',
                'Run ./demo/gui.sh in a terminal (asks for sudo once). '
                'Then press Start.',
            )
        elif not agent_current:
            action, message = 'gui.sh', AGENT_STALE
        elif not dataplane:
            if starting:
                action, message = 'wait', 'Start is running. Waiting for Mininet switches to connect.'
            elif last_fail:
                action, message = 'start', (
                    'Last Start failed: %s Press Start to retry.' % last_fail
                )
            else:
                action, message = 'start', 'Press Start to bring up Mininet and the C++ probes.'
        elif not hosts_ok:
            missing = [ip for ip in (self.src_ip, self.dst_ip) if ip not in self.hosts]
            action, message = 'wait', 'Dataplane is up. Waiting to learn host(s) %s.' % ', '.join(missing)
        elif not paths_ok:
            action, message = 'wait', 'Hosts are known. Waiting for a route between them.'
        elif not fresh:
            if starting:
                action, message = 'wait', 'Dataplane is up. Waiting for C++ probe reports.'
            else:
                action, message = 'start', 'No fresh probe reports. Press Start to relaunch the probes.'
        else:
            action, message = 'ready', 'Ready. Path colours and the table are live measurements.'
            self.starting_until = 0.0
        return {
            'ok': action == 'ready',
            'action': action,
            'message': message,
            'agent': agent_current,
            'dataplane': dataplane,
            'hosts': hosts_ok,
            'paths': paths_ok,
            'probes': fresh,
            'checks': checks,
            'switches': len(self.dps),
            'n_paths': len(self.paths),
        }


class NdpaRest(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app = data['ndpa']

    @staticmethod
    def _json(obj):
        return Response(content_type='application/json', body=(json.dumps(obj, indent=1) + '\n').encode())

    @staticmethod
    def _file(path):
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except OSError as e:
            return Response(status=404, content_type='text/plain',
                            body=('missing: %s\n' % e).encode())
        ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        return Response(content_type=ctype, body=body)

    def _dashboard(self):
        return self._file(os.path.join(FRONTEND, 'index.html'))

    @route('ndpa', '/ndpa', methods=['GET'])
    def dashboard(self, req, **kwargs):
        return self._dashboard()

    @route('ndpa', '/ndpa/', methods=['GET'])
    def dashboard_slash(self, req, **kwargs):
        return self._dashboard()

    @route('ndpa', '/ndpa/styles.css', methods=['GET'])
    def styles(self, req, **kwargs):
        return self._file(os.path.join(FRONTEND, 'styles.css'))

    @route('ndpa', '/ndpa/app.js', methods=['GET'])
    def app_js(self, req, **kwargs):
        return self._file(os.path.join(FRONTEND, 'app.js'))

    @route('ndpa', '/ndpa/favicon.png', methods=['GET', 'HEAD'])
    def favicon_png(self, req, **kwargs):
        return self._file(os.path.join(FRONTEND, 'favicon.png'))

    @route('ndpa', '/ndpa/favicon.ico', methods=['GET', 'HEAD'])
    def favicon_ico(self, req, **kwargs):
        return self._file(os.path.join(FRONTEND, 'favicon.ico'))

    @route('ndpa', '/ndpa/health', methods=['GET'])
    def health(self, req, **kwargs):
        return self._json(self.app.health_view())

    @route('ndpa', '/ndpa/topology', methods=['GET'])
    def topology(self, req, **kwargs):
        return self._json(self.app.topology_view())

    @route('ndpa', '/ndpa/paths', methods=['GET'])
    def paths(self, req, **kwargs):
        return self._json(self.app.paths_view())

    @route('ndpa', '/ndpa/events', methods=['GET'])
    def events(self, req, **kwargs):
        return self._json(list(self.app.decisions))

    def _apply_probe_pair(self, data):
        probe = (data or {}).get('probe') or {}
        by_name = {h.get('name'): h for h in (data or {}).get('hosts') or [] if h.get('name')}
        src = by_name.get(probe.get('src')) or {}
        dst = by_name.get(probe.get('dst')) or {}
        if src.get('ip'):
            self.app.src_ip = str(src['ip']).split('/')[0]
        if dst.get('ip'):
            self.app.dst_ip = str(dst['ip']).split('/')[0]

    @route('ndpa', '/ndpa/run', methods=['POST'])
    def run_topo(self, req, **kwargs):
        try:
            data = json.loads(req.body.decode() if req.body else '{}')
            with open(TOPO_JSON, 'w') as f:
                json.dump(data, f, indent=2)
                f.write('\n')
        except (ValueError, OSError, UnicodeDecodeError) as e:
            return Response(status=400, content_type='application/json',
                            body=json.dumps({'error': str(e)}).encode())
        self._apply_probe_pair(data)
        ok, detail = _enqueue_gui({'op': 'run'})
        if ok:
            self.app.starting_until = time.time() + 60
            self.app.reset_analysis('Start')
            self.app.note('PATHS', 'Start queued · probe %s -> %s' % (self.app.src_ip, self.app.dst_ip))
            return self._json({'ok': True, 'started': True,
                               'message': 'starting dataplane + probes'})
        self.app.starting_until = 0.0
        hint = AGENT_STALE if (_agent_stale() or _stale_start_detail(detail)) else (
            'topo saved · ' + detail)
        return Response(status=503, content_type='application/json',
                        body=json.dumps({
                            'ok': False, 'started': False,
                            'saved': True, 'error': detail,
                            'message': hint,
                        }).encode())

    @route('ndpa', '/ndpa/fault', methods=['POST'])
    def fault(self, req, **kwargs):
        try:
            data = json.loads(req.body.decode() if req.body else '{}')
        except (ValueError, UnicodeDecodeError) as e:
            return Response(status=400, content_type='application/json',
                            body=json.dumps({'error': str(e)}).encode())
        action = data.get('action') or data.get('op')
        if action not in ('degrade', 'blackhole', 'clear', 'link_down', 'link_up'):
            return Response(status=400, content_type='application/json',
                            body=json.dumps({'error': 'bad action'}).encode())
        intf = data.get('intf')
        path_name = data.get('path')
        if not intf and path_name:
            for p in self.app.paths:
                if p.name == path_name or analysis.route_name(p.route) == path_name:
                    intf = self.app._fault_intf(p.route)
                    break
        if not intf:
            return Response(status=400, content_type='application/json',
                            body=json.dumps({
                                'error': 'need intf or known path',
                                'hint': 'pick a path with fault_intf from /ndpa/paths',
                            }).encode())
        loss = float(data.get('loss_pct', 15 if action == 'degrade' else 100))
        ok, detail = _enqueue_gui({
            'op': 'fault', 'action': action, 'intf': intf,
            'loss_pct': loss, 'path': path_name,
        })
        if not ok:
            return Response(status=503, content_type='application/json',
                            body=json.dumps({'ok': False, 'error': detail,
                                             'message': detail}).encode())
        self.app.note('FAULT', '%s on %s (loss=%s)' % (action, intf, loss))
        return self._json({'ok': True, 'action': action, 'intf': intf,
                           'loss_pct': loss, 'message': detail})
