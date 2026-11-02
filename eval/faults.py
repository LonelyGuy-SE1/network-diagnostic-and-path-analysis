"""Fault injection on the Mininet diamond (issue #17, reusable for #16).

All faults hit path A (s1-s2-s4), the path live traffic starts on:

  soft       netem on s2-eth2 (s2 towards s4): 60 ms delay and 20 % loss. The link
             stays up, so only the probe reports can see it. Both numbers sit above
             the controller's "degraded" thresholds (5 % loss, 50 ms RTT) and below
             "failed" (50 % loss).
  blackhole  netem on s2-eth2: 100 % loss. The link stays up, so again only the
             probes can see it (this is the demo's scenario 2).
  hard       link s2-s4 down. Open vSwitch reports the port change to the controller.
  none       nothing (healthy baseline).
"""

NETEM_INTF = 's2-eth2'
LINK = ('s2', 's4')
NETEM = {'soft': 'delay 60ms loss 20%', 'blackhole': 'loss 100%'}
KINDS = ('none', 'soft', 'blackhole', 'hard')


def inject(net, kind):
    if kind == 'none':
        return
    if kind in NETEM:
        net.get('s2').cmd('tc qdisc replace dev', NETEM_INTF, 'root netem', NETEM[kind])
    elif kind == 'hard':
        net.configLinkStatus(LINK[0], LINK[1], 'down')
    else:
        raise ValueError('unknown fault %r, want one of %s' % (kind, ', '.join(KINDS)))


def clear(net, kind):
    if kind == 'none':
        return
    if kind in NETEM:
        net.get('s2').cmd('tc qdisc del dev', NETEM_INTF, 'root')
    elif kind == 'hard':
        net.configLinkStatus(LINK[0], LINK[1], 'up')
    else:
        raise ValueError('unknown fault %r' % kind)
