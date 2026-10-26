#!/usr/bin/env python3
"""Diamond topology for path analysis (issue #15).

    h1 - s1 --- s2 --- s4 - h2
           \\          /
            `-- s3 --'

Usage:
  sudo python3 topology/diamond.py                      # OVS + RemoteController (Ryu) on 127.0.0.1:6653
  sudo python3 topology/diamond.py --controller-ip 10.0.0.100 --controller-port 6633
  sudo python3 topology/diamond.py --static             # no controller, static per-port path flows
  sudo python3 topology/diamond.py --static --datapath user   # same, without the OVS kernel module
  sudo python3 topology/diamond.py --mgmt               # plus an out-of-band h1 <-> root link
                                                        # so the client can report to Ryu

Hosts get static ARP entries so probes never depend on ARP flooding.
"""
import argparse

from mininet.cli import CLI
from mininet.link import Link
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import Node, OVSSwitch, RemoteController
from mininet.topo import Topo

# Port numbers follow the link order in DiamondTopo.build():
#   s1: 1=h1 2=s2 3=s3    s2, s3: 1=s1 2=s4    s4: 1=s2 2=s3 3=h2
S1_UPLINK = {'s2': 2, 's3': 3}
S4_DOWNLINK = {'s2': 1, 's3': 2}
H1_PORT, H2_PORT = 1, 3

# Probe port -> middle switch. Placeholder until issue #3 settles the ports.
PATH_PORTS = {5001: 's2', 5002: 's3'}

# Out-of-band management link for telemetry reports (README, "Control traffic
# stays off the tested links"). h1-eth1 <-> root-eth0, never touches a switch.
MGMT_HOST_IP = '192.168.100.1'
MGMT_ROOT_IP = '192.168.100.254'


class DiamondTopo(Topo):
    def build(self):
        h1 = self.addHost('h1', ip='10.0.0.1/24', mac='00:00:00:00:00:01')
        h2 = self.addHost('h2', ip='10.0.0.2/24', mac='00:00:00:00:00:02')
        s1, s2, s3, s4 = (self.addSwitch('s%d' % i) for i in range(1, 5))
        self.addLink(h1, s1)
        self.addLink(s1, s2)
        self.addLink(s1, s3)
        self.addLink(s2, s4)
        self.addLink(s3, s4)
        self.addLink(s4, h2)


def build_net(datapath='kernel', controller=True, controller_ip='127.0.0.1', controller_port=6653):
    def ovs(name, **kw):
        return OVSSwitch(name, protocols='OpenFlow13', datapath=datapath,
                         failMode='secure', **kw)

    net = Mininet(topo=DiamondTopo(), switch=ovs, controller=None)
    if controller:
        net.addController('c0', controller=RemoteController,
                          ip=controller_ip, port=controller_port)
    return net


def prepare_hosts(net, datapath='kernel'):
    h1, h2 = net.get('h1', 'h2')
    h1.setARP(h2.IP(), h2.MAC())
    h2.setARP(h1.IP(), h1.MAC())
    if datapath == 'user':
        # The netdev datapath does not finish checksums that veth TX offload
        # leaves partial, so the receiver drops the UDP. Compute them on the host.
        for h in net.hosts:
            h.cmd('ethtool -K', h.defaultIntf(), 'tx off')


def add_mgmt_link(net, host='h1'):
    """Veth from a host straight into the root namespace, where Ryu runs.
    Returns (root_node, link); pass them to remove_mgmt_link() before net.stop()."""
    root = Node('root', inNamespace=False)
    link = Link(net.get(host), root)
    link.intf1.setIP(MGMT_HOST_IP, 24)
    link.intf2.setIP(MGMT_ROOT_IP, 24)
    return root, link


def remove_mgmt_link(mgmt):
    root, link = mgmt
    link.delete()
    root.terminate()


def install_static_flows(net):
    """Controller-less baseline: each probe port takes its own middle switch,
    everything else (ICMP, other ports) goes via s2."""
    def add(sw, flow):
        net.get(sw).cmd('ovs-ofctl -O OpenFlow13 add-flow', sw, "'%s'" % flow)

    for sw in ('s1', 's2', 's3', 's4'):
        net.get(sw).cmd('ovs-ofctl -O OpenFlow13 del-flows', sw)

    for port, via in PATH_PORTS.items():
        # Requests h1 -> h2 match the destination port, replies the source port.
        add('s1', 'priority=100,udp,in_port=%d,tp_dst=%d,actions=output:%d'
            % (H1_PORT, port, S1_UPLINK[via]))
        add('s4', 'priority=100,udp,in_port=%d,tp_src=%d,actions=output:%d'
            % (H2_PORT, port, S4_DOWNLINK[via]))

    add('s1', 'priority=10,in_port=%d,actions=output:%d' % (H1_PORT, S1_UPLINK['s2']))
    add('s4', 'priority=10,in_port=%d,actions=output:%d' % (H2_PORT, S4_DOWNLINK['s2']))
    for uplink in S1_UPLINK.values():
        add('s1', 'priority=10,in_port=%d,actions=output:%d' % (uplink, H1_PORT))
    for downlink in S4_DOWNLINK.values():
        add('s4', 'priority=10,in_port=%d,actions=output:%d' % (downlink, H2_PORT))
    for sw in ('s2', 's3'):
        add(sw, 'priority=10,in_port=1,actions=output:2')
        add(sw, 'priority=10,in_port=2,actions=output:1')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--datapath', choices=['kernel', 'user'], default='kernel',
                    help="'user' runs OVS without its kernel module")
    ap.add_argument('--static', action='store_true',
                    help='no controller, install static per-port path flows')
    ap.add_argument('--controller-ip', default='127.0.0.1')
    ap.add_argument('--controller-port', type=int, default=6653)
    ap.add_argument('--mgmt', action='store_true',
                    help='add h1-eth1 %s <-> root %s for controller reports' % (MGMT_HOST_IP, MGMT_ROOT_IP))
    args = ap.parse_args()

    setLogLevel('info')
    net = build_net(args.datapath, not args.static, args.controller_ip, args.controller_port)
    net.start()
    prepare_hosts(net, args.datapath)
    if args.static:
        install_static_flows(net)
    mgmt = add_mgmt_link(net) if args.mgmt else None
    CLI(net)
    if mgmt:
        remove_mgmt_link(mgmt)
    net.stop()


if __name__ == '__main__':
    main()
