(function () {
  const RANK = { healthy: 0, degraded: 1, failed: 2, unknown: 3 };
  const NS = 'http://www.w3.org/2000/svg';
  const $ = (id) => document.getElementById(id);
  const W = 720, H = 480;
  const LAYOUT_VER = 4;

  let topo = defaultTopo();
  let paths = null;
  let liveOn = false;
  let liveTopo = null;
  let health = null;
  let holdStatusUntil = 0;
  let drag = null;
  const downIntfs = {};

  function defaultTopo() {
    return {
      layoutVer: LAYOUT_VER,
      switches: ['s1', 's2', 's3', 's4'],
      hosts: [
        { name: 'h1', ip: '10.0.0.1', switch: 's1' },
        { name: 'h2', ip: '10.0.0.2', switch: 's4' }
      ],
      links: [
        ['h1', 's1'], ['s1', 's2'], ['s1', 's3'],
        ['s2', 's4'], ['s3', 's4'], ['s4', 'h2']
      ],
      probe: { src: 'h1', dst: 'h2' },
      pos: {
        h1: { x: 360, y: 48 },
        s1: { x: 360, y: 250 },
        s2: { x: 190, y: 340 },
        s3: { x: 530, y: 340 },
        s4: { x: 360, y: 400 },
        h2: { x: 360, y: 450 }
      }
    };
  }

  function ensureProbe() {
    const hosts = topo.hosts.map((h) => h.name);
    topo.probe = topo.probe || {};
    if (hosts.indexOf(topo.probe.src) < 0) {
      topo.probe.src = hosts.indexOf('h1') >= 0 ? 'h1' : (hosts[0] || null);
    }
    if (hosts.indexOf(topo.probe.dst) < 0 || topo.probe.dst === topo.probe.src) {
      topo.probe.dst = hosts.indexOf('h2') >= 0 && 'h2' !== topo.probe.src
        ? 'h2'
        : (hosts.find((h) => h !== topo.probe.src) || null);
    }
  }

  function ukey(a, b) { return a < b ? a + '|' + b : b + '|' + a; }

  function endpoint(s) {
    const i = String(s).lastIndexOf(':');
    return i < 0 ? s : s.slice(0, i);
  }

  function names() {
    return topo.switches.concat(topo.hosts.map((h) => h.name));
  }

  function build() {
    const nodes = {};
    const edges = {};
    const adj = {};

    topo.switches.forEach((s) => {
      nodes[s] = { id: s, kind: 'sw', label: s };
      adj[s] = {};
    });
    topo.hosts.forEach((h) => {
      nodes[h.name] = { id: h.name, kind: 'host', label: h.name, switch: h.switch };
      adj[h.name] = {};
    });

    function touch(a, b, meta) {
      if (!nodes[a] || !nodes[b]) return;
      const k = ukey(a, b);
      edges[k] = Object.assign({ a, b, down: false }, edges[k] || {}, meta || {});
      adj[a][b] = true;
      adj[b][a] = true;
    }

    topo.links.forEach(([a, b]) => touch(a, b, {}));

    if (liveOn && paths && paths._portsDown) {
      Object.keys(edges).forEach((k) => {
        const e = edges[k];
        if (paths._portsDown[ukey(e.a, e.b)]) e.down = true;
      });
    }
    return { nodes, edges, adj };
  }

  function inView(p) {
    return p && Number.isFinite(p.x) && Number.isFinite(p.y)
      && p.x >= 30 && p.x <= W - 30 && p.y >= 30 && p.y <= H - 30;
  }

  function layoutMissing(g) {
    topo.pos = topo.pos || {};
    const need = Object.keys(g.nodes).filter((id) => !inView(topo.pos[id]));
    if (!need.length) return;

    const padX = 90, topY = 56, botY = H - 56, midY = H / 2;
    const switches = topo.switches.filter((s) => need.indexOf(s) >= 0);
    const hostsNeed = topo.hosts.filter((h) => need.indexOf(h.name) >= 0);

    switches.forEach((id, i) => {
      const n = switches.length;
      topo.pos[id] = {
        x: padX + ((i + 0.5) * (W - 2 * padX)) / Math.max(n, 1),
        y: midY
      };
    });

    let freeI = 0;
    hostsNeed.forEach((h) => {
      if (h.switch && inView(topo.pos[h.switch])) {
        const sw = topo.pos[h.switch];
        const above = sw.y < midY;
        topo.pos[h.name] = { x: sw.x, y: above ? topY : botY };
      } else {
        topo.pos[h.name] = { x: W - 70, y: 90 + freeI * 70 };
        freeI++;
      }
    });
  }

  function applyPos(g) {
    layoutMissing(g);
    Object.keys(g.nodes).forEach((id) => {
      const p = topo.pos[id];
      if (!inView(p)) return;
      g.nodes[id].x = p.x;
      g.nodes[id].y = p.y;
    });
  }

  function edgeClass(a, b, meta) {
    if (meta.down) return 'edge down failed';
    if (!liveOn) return 'edge';

    let best = null, active = false;
    (paths && paths.paths || []).forEach((p) => {
      const r = p.route || [];
      for (let i = 0; i < r.length - 1; i++) {
        if ((r[i] === a && r[i + 1] === b) || (r[i] === b && r[i + 1] === a)) {
          const st = p.state || 'unknown';
          if (!best || RANK[st] > RANK[best]) best = st;
          if (p.active || p.name === paths.active) active = true;
        }
      }
    });

    if (best) {
      return ['edge', best].concat(active ? ['active'] : []).join(' ');
    }
    return 'edge idle';
  }

  function el(name, attrs, text) {
    const n = document.createElementNS(NS, name);
    Object.keys(attrs || {}).forEach((k) => n.setAttribute(k, attrs[k]));
    if (text != null) n.textContent = text;
    return n;
  }

  function svgPoint(evt) {
    const svg = $('svg');
    const pt = svg.createSVGPoint();
    pt.x = evt.clientX;
    pt.y = evt.clientY;
    const m = svg.getScreenCTM().inverse();
    const p = pt.matrixTransform(m);
    return { x: p.x, y: p.y };
  }

  function draw() {
    const g = build();
    applyPos(g);

    const svg = $('svg');
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.innerHTML = '';

    Object.keys(g.edges).forEach((k) => {
      const e = g.edges[k];
      const a = g.nodes[e.a], b = g.nodes[e.b];
      if (!a || !b) return;
      svg.appendChild(el('line', {
        x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: edgeClass(e.a, e.b, e)
      }));
    });

    const order = Object.keys(g.nodes).sort((a, b) => {
      const ka = g.nodes[a].kind === 'host' ? 1 : 0;
      const kb = g.nodes[b].kind === 'host' ? 1 : 0;
      return ka - kb;
    });
    order.forEach((id) => {
      const n = g.nodes[id];
      if (n.x == null || n.y == null) return;
      const group = el('g', { class: 'draggable', 'data-id': id });
      if (n.kind === 'host') {
        group.appendChild(el('circle', { cx: n.x, cy: n.y, r: 20, class: 'node-host' }));
      } else {
        group.appendChild(el('rect', {
          x: n.x - 30, y: n.y - 22, width: 60, height: 44, class: 'node-sw'
        }));
      }
      group.appendChild(el('text', { x: n.x, y: n.y, class: 'node-label' }, n.label));
      svg.appendChild(group);
    });
  }

  function fmt(n, d) {
    if (n == null || Number.isNaN(Number(n))) return '-';
    return Number(n).toFixed(d);
  }

  function metricCells(p) {
    const r = p.last_report || {};
    const hardDown = (p.ports_down && p.ports_down.length) || /port .+ down/.test(p.reason || '');
    const blackholed = Number(r.loss) >= 99.9;
    if (hardDown || blackholed || r.rtt_avg == null) {
      const loss = hardDown ? '100.0' : fmt(r.loss, 1);
      return '<td>-</td><td>-</td><td>-</td><td>-</td><td>' + loss + '%</td>';
    }
    return '<td>' + fmt(r.rtt_min, 3) + '</td>'
      + '<td>' + fmt(r.rtt_avg, 3) + '</td>'
      + '<td>' + fmt(r.rtt_max, 3) + '</td>'
      + '<td>' + fmt(r.jitter, 3) + '</td>'
      + '<td>' + fmt(r.loss, 1) + '%</td>';
  }

  function liveLabel(view) {
    if (!view) return null;
    const route = view.active_route || (view.active ? String(view.active).split('-') : null);
    if (!route || !route.length) return null;
    const joined = Array.isArray(route) ? route.join(' → ') : route;
    const st = view.active_state;
    return st && st !== 'healthy' ? ('live ' + joined + ' [' + st + ']') : ('live ' + joined);
  }

  function fillFaultSelect(view) {
    const sel = $('fault-path');
    if (!sel) return;
    const rows = (view && view.paths) || [];
    const prev = sel.value;
    sel.innerHTML = rows.length
      ? rows.map((p) => {
          const intf = p.fault_intf || '';
          const label = (p.route || []).join('→') + (intf ? (' · ' + intf) : '');
          return '<option value="' + p.name + '" data-intf="' + intf + '">' + label + '</option>';
        }).join('')
      : '<option value="">no paths yet</option>';
    if (prev && rows.some((p) => p.name === prev)) sel.value = prev;
  }

  function renderPaths(view) {
    const rows = (view && view.paths) || [];
    fillFaultSelect(view);
    if (!rows.length) {
      $('paths').innerHTML = '<tr><td colspan="10" class="muted">no paths yet · press Start</td></tr>';
      if ($('path-meta')) $('path-meta').textContent = '';
      return;
    }
    $('paths').innerHTML = rows.map((p) => {
      const live = !!(p.active || (view && p.name === view.active));
      const pill = live
        ? ('<span class="pill">' + (p.state === 'failed' || p.state === 'degraded'
          ? ('live · ' + p.state) : 'live') + '</span>')
        : '';
      return '<tr class="' + (live ? 'live' : '') + '">'
        + '<td>' + (p.name || '-') + pill + '</td>'
        + '<td class="route">' + (p.route || []).join(' → ') + '</td>'
        + '<td>udp/' + (p.probe_port || '-') + '</td>'
        + '<td class="state ' + (p.state || '') + '">' + (p.state || '-') + '</td>'
        + '<td class="reason">' + (p.reason || '-') + '</td>'
        + metricCells(p) + '</tr>';
    }).join('');
    if ($('path-meta')) {
      const th = (view && view.thresholds) || {};
      const bits = [];
      if (view && view.src && view.dst) bits.push('probe ' + view.src + ' → ' + view.dst);
      const live = liveLabel(view);
      if (live) bits.push(live);
      if (th.degraded_loss_pct != null) {
        bits.push('degraded ≥' + th.degraded_loss_pct + '% loss / ≥'
          + th.degraded_rtt_ms + 'ms RTT');
      }
      if (th.failed_loss_pct != null) bits.push('failed ≥' + th.failed_loss_pct + '% loss');
      if (th.hold_time_s != null) bits.push('hold ' + th.hold_time_s + 's');
      $('path-meta').textContent = bits.join(' · ');
    }
  }

  function renderEvents(list) {
    const keep = (list || []).filter((e) =>
      /CLASSIFY|REROUTE|PORT|ACTIVE|PATHS|TOPO|HOST|FAULT/.test(e.kind)
    ).slice(-120).reverse();
    $('events').innerHTML = keep.length
      ? keep.map((e) =>
          '<div class="ev"><span>' + e.t + '</span><span class="kind ' + e.kind + '">'
          + e.kind + '</span><span>' + e.msg + '</span></div>'
        ).join('')
      : '<span class="muted">no events yet · press Start</span>';
  }

  function fillSelects() {
    const n = names();
    ['link-a', 'link-b'].forEach((id) => {
      $(id).innerHTML = n.map((x) => '<option value="' + x + '">' + x + '</option>').join('');
    });
    if (n.length > 1) $('link-b').selectedIndex = 1;

    ensureProbe();
    const hosts = topo.hosts.map((h) => h.name);
    const opts = hosts.map((x) => '<option value="' + x + '">' + x + '</option>').join('');
    $('probe-src').innerHTML = opts;
    $('probe-dst').innerHTML = opts;
    if (topo.probe.src) $('probe-src').value = topo.probe.src;
    if (topo.probe.dst) $('probe-dst').value = topo.probe.dst;
  }

  function renderInventory() {
    const items = [];
    topo.switches.forEach((s) => items.push(['sw', s, s, 'switch']));
    topo.hosts.forEach((h) => items.push([
      'host', h.name, h.name,
      h.switch ? ('@ ' + h.switch + ' · ' + h.ip) : ('unlinked · ' + h.ip)
    ]));
    topo.links.forEach((p, i) => items.push(['link', String(i), p[0] + ' - ' + p[1], 'link']));
    $('inventory').innerHTML = items.map((it) =>
      '<li><div>' + it[2] + ' <em>' + it[3] + '</em></div>'
      + '<button type="button" data-del="' + it[0] + ':' + it[1] + '">del</button></li>'
    ).join('') || '<li class="muted">empty</li>';
    fillSelects();
  }

  function nextName(prefix, used) {
    let n = used.length + 1;
    while (used.indexOf(prefix + n) >= 0) n++;
    return prefix + n;
  }

  function nextIp() {
    const used = {};
    topo.hosts.forEach((h) => { used[h.ip] = true; });
    for (let i = 1; i < 254; i++) {
      const ip = '10.0.0.' + i;
      if (!used[ip]) return ip;
    }
    return '10.0.0.50';
  }

  function refresh() {
    renderInventory();
    if (!liveOn) renderPaths(null);
    draw();
  }

  $('svg').addEventListener('pointerdown', (ev) => {
    const g = ev.target.closest('g.draggable');
    if (!g) return;
    const id = g.getAttribute('data-id');
    const p = svgPoint(ev);
    if (!inView(topo.pos[id])) {
      const built = build();
      applyPos(built);
    }
    const cur = topo.pos[id];
    if (!inView(cur)) return;
    drag = { id, dx: p.x - cur.x, dy: p.y - cur.y };
    g.setPointerCapture(ev.pointerId);
    ev.preventDefault();
  });

  $('svg').addEventListener('pointermove', (ev) => {
    if (!drag) return;
    const p = svgPoint(ev);
    topo.pos[drag.id] = {
      x: Math.max(30, Math.min(W - 30, p.x - drag.dx)),
      y: Math.max(30, Math.min(H - 30, p.y - drag.dy))
    };
    draw();
  });

  $('svg').addEventListener('pointerup', () => { drag = null; });
  $('svg').addEventListener('pointercancel', () => { drag = null; });

  $('add-sw').onclick = () => {
    const s = nextName('s', topo.switches);
    topo.switches.push(s);
    topo.switches.sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
    const n = topo.switches.length;
    topo.pos[s] = { x: 120 + (n % 5) * 90, y: 160 + Math.floor(n / 5) * 90 };
    refresh();
  };

  $('add-host').onclick = () => {
    const name = nextName('h', topo.hosts.map((h) => h.name));
    topo.hosts.push({ name, ip: nextIp(), switch: null });
    const n = topo.hosts.length;
    topo.pos[name] = { x: W - 70, y: 80 + (n - 1) * 70 };
    refresh();
  };

  $('add-link').onclick = () => {
    const a = $('link-a').value, b = $('link-b').value;
    if (!a || !b || a === b) return;
    if (topo.links.some((p) => ukey(p[0], p[1]) === ukey(a, b))) return;
    topo.links.push([a, b]);
    const host = topo.hosts.find((h) => h.name === a || h.name === b);
    const sw = topo.switches.find((s) => s === a || s === b);
    if (host && sw) host.switch = sw;
    refresh();
    if (liveOn) restoreLiveLink(a, b);
  };

  function onProbeChange() {
    const src = $('probe-src').value, dst = $('probe-dst').value;
    if (!src || !dst || src === dst) return;
    topo.probe = { src, dst };
    localStorage.setItem('ndpa-topo', JSON.stringify(topo));
    refresh();
  }
  $('probe-src').onchange = onProbeChange;
  $('probe-dst').onchange = onProbeChange;

  $('inventory').onclick = (ev) => {
    const b = ev.target.closest('button[data-del]');
    if (!b) return;
    const [kind, id] = b.getAttribute('data-del').split(':');
    if (kind === 'sw') {
      topo.switches = topo.switches.filter((s) => s !== id);
      topo.hosts = topo.hosts.filter((h) => h.switch !== id);
      topo.links = topo.links.filter((p) => p[0] !== id && p[1] !== id);
      delete topo.pos[id];
    } else if (kind === 'host') {
      topo.hosts = topo.hosts.filter((h) => h.name !== id);
      topo.links = topo.links.filter((p) => p[0] !== id && p[1] !== id);
      delete topo.pos[id];
    } else if (kind === 'link') {
      const pair = topo.links[Number(id)];
      topo.links.splice(Number(id), 1);
      if (pair) {
        topo.hosts.forEach((h) => {
          if ((pair[0] === h.name || pair[1] === h.name) &&
              (pair[0] === h.switch || pair[1] === h.switch)) {
            const still = topo.links.some((p) =>
              (p[0] === h.name || p[1] === h.name) &&
              (p[0] === h.switch || p[1] === h.switch)
            );
            if (!still) h.switch = null;
          }
        });
        if (liveOn) dropLiveLink(pair[0], pair[1]);
      }
    }
    refresh();
  };

  $('reset').onclick = () => {
    localStorage.removeItem('ndpa-topo');
    topo = defaultTopo();
    liveOn = false;
    paths = null;
    $('status').textContent = 'reset to diamond';
    refresh();
  };

  function intfKey(a, b) {
    return a < b ? a + '-' + b : b + '-' + a;
  }

  function rememberedIntfs(a, b) {
    const key = intfKey(a, b);
    const live = (liveTopo && liveTopo.link_intfs && liveTopo.link_intfs[key]) || [];
    if (live.length) downIntfs[key] = live.slice();
    return downIntfs[key] || live;
  }

  async function setLiveLink(a, b, action) {
    const intfs = rememberedIntfs(a, b);
    if (!intfs.length) {
      $('status').textContent = action === 'link_down'
        ? 'link removed from editor. Start to apply a new graph.'
        : 'no live interface for this pair — del it first while the dataplane is up, then add it back.';
      return;
    }
    let last = '', ok = true;
    for (let i = 0; i < intfs.length; i++) {
      const intf = intfs[i];
      try {
        const r = await fetch('/ndpa/fault', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: action, intf: intf })
        });
        const j = await r.json().catch(() => ({}));
        last = j.message || j.error || (action + ' ' + intf);
        if (!r.ok || j.ok === false) ok = false;
      } catch (err) {
        last = 'could not ' + action + ' on ' + intf;
        ok = false;
      }
    }
    if (action === 'link_up' && ok) delete downIntfs[intfKey(a, b)];
    $('status').className = ok ? '' : 'err';
    $('status').textContent = last;
  }

  function dropLiveLink(a, b) { return setLiveLink(a, b, 'link_down'); }
  function restoreLiveLink(a, b) { return setLiveLink(a, b, 'link_up'); }

  const STALE_HELPER =
    'Sudo helper is stale. In the ./demo/gui.sh terminal: Ctrl-C, then run ./demo/gui.sh again. Then press Start.';

  function restartHint(msg) {
    const s = String(msg || '');
    if (/gui_run\.sh|gui_demo\.sh|unknown op|sudo helper is stale/i.test(s)) {
      return STALE_HELPER;
    }
    return s;
  }

  function applyHealth(h) {
    health = h;
    const list = $('health-list');
    if (list && h && h.checks) {
      list.innerHTML = h.checks.map((c) =>
        '<li class="' + (c.ok ? 'ok' : 'bad') + '">' + (c.ok ? 'ok' : 'need') + '  ' + c.label + '</li>'
      ).join('');
    }
    const startBtn = $('run');
    const bh = $('fault-blackhole');
    const cl = $('fault-clear');
    if (startBtn) startBtn.disabled = !(h && h.agent);
    if (bh) bh.disabled = !(h && h.agent && h.dataplane);
    if (cl) cl.disabled = !(h && h.agent && h.dataplane);
  }

  function showHealthMessage(h) {
    if (!h) return;
    if (Date.now() < holdStatusUntil && h.action === 'start') return;
    const el = $('status');
    el.textContent = restartHint(h.message || '');
    el.className = h.ok ? 'ok' : (h.action === 'wait' ? 'warn' : 'err');
  }

  $('run').onclick = async () => {
    $('status').className = 'warn';
    $('status').textContent = 'starting dataplane…';
    holdStatusUntil = Date.now() + 50000;
    localStorage.setItem('ndpa-topo', JSON.stringify(topo));
    try {
      const r = await fetch('/ndpa/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(topo)
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok || body.ok === false) {
        holdStatusUntil = 0;
        $('status').className = 'err';
        $('status').textContent = restartHint(body.message || body.error
          || 'Start failed. Run ./demo/gui.sh once so the agent can use sudo.');
        liveOn = false;
      } else {
        $('status').className = '';
        $('status').textContent = body.message || 'starting dataplane + probes';
      }
      tick();
    } catch (err) {
      liveOn = false;
      paths = null;
      $('status').className = 'err';
      $('status').textContent = 'controller offline. start ./demo/gui.sh';
      draw();
    }
  };


  async function sendFault(action, lossPct) {
    const sel = $('fault-path');
    const name = sel && sel.value;
    if (!name) {
      $('status').className = 'err';
      $('status').textContent = 'no path selected for fault inject';
      return;
    }
    const opt = sel.options[sel.selectedIndex];
    const intf = opt && opt.getAttribute('data-intf');
    $('status').textContent = 'injecting ' + action + '…';
    try {
      const body = { action: action, path: name };
      if (intf) body.intf = intf;
      if (lossPct != null) body.loss_pct = lossPct;
      const r = await fetch('/ndpa/fault', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) {
        $('status').className = 'err';
        $('status').textContent = restartHint(j.message || j.error
          || 'fault failed. Run ./demo/gui.sh');
        return;
      }
      $('status').className = '';
      $('status').textContent = j.message || (action + ' on ' + (intf || name));
      tick();
    } catch (err) {
      $('status').className = 'err';
      $('status').textContent = 'controller offline · start: ./demo/gui.sh';
    }
  }

  if ($('fault-blackhole')) {
    $('fault-blackhole').onclick = () => sendFault('blackhole', 100);
    $('fault-clear').onclick = () => sendFault('clear');
  }

  async function tick() {
    try {
      const [t, p, e, h] = await Promise.all(
        ['topology', 'paths', 'events', 'health'].map((x) =>
          fetch('/ndpa/' + x).then((r) => {
            if (!r.ok) throw new Error(r.status);
            return r.json();
          })
        )
      );
      liveTopo = t;
      applyHealth(h);
      const up = !!(h && h.dataplane);
      liveOn = up && !!(h && (h.paths || h.ok));
      paths = p;
      paths._portsDown = {};
      (t.links || []).forEach((raw) => {
        const [L, R] = String(raw).split('->');
        if (!L || !R) return;
        if ((t.ports_down || []).indexOf(L) >= 0 || (t.ports_down || []).indexOf(R) >= 0) {
          paths._portsDown[ukey(endpoint(L), endpoint(R))] = true;
        }
      });
      if (h && h.ok) {
        holdStatusUntil = 0;
        const bits = [
          (t.switches || []).length + ' switches',
          ((p.paths || []).length) + ' path(s)'
        ];
        const live = liveLabel(p);
        if (live) bits.push(live);
        $('status').className = 'ok';
        $('status').textContent = bits.join(' · ');
      } else {
        showHealthMessage(h);
      }
      renderPaths(liveOn ? p : { paths: (p && p.paths) || [], active: null });
      renderEvents(e);
      draw();
    } catch (err) {
      liveOn = false;
      applyHealth({
        ok: false, action: 'gui.sh', agent: false, dataplane: false,
        checks: [
          { id: 'controller', ok: false, label: 'controller' },
          { id: 'agent', ok: false, label: 'sudo helper (./demo/gui.sh)' },
          { id: 'dataplane', ok: false, label: 'Mininet dataplane' },
          { id: 'hosts', ok: false, label: 'probe hosts learned' },
          { id: 'paths', ok: false, label: 'paths discovered' },
          { id: 'probes', ok: false, label: 'probe reports' }
        ]
      });
      if (Date.now() >= holdStatusUntil) {
        $('status').className = 'err';
        $('status').textContent = 'Controller is not running. In a terminal: ./demo/gui.sh';
      }
    }
  }

  try {
    const saved = localStorage.getItem('ndpa-topo');
    if (saved) {
      const parsed = JSON.parse(saved);
      const base = defaultTopo();
      const keepPos = parsed.layoutVer === LAYOUT_VER ? (parsed.pos || {}) : {};
      topo = {
        layoutVer: LAYOUT_VER,
        switches: parsed.switches || base.switches,
        hosts: parsed.hosts || base.hosts,
        links: parsed.links || base.links,
        probe: parsed.probe || base.probe,
        pos: Object.assign({}, base.pos, keepPos)
      };
      ensureProbe();
      Object.keys(topo.pos).forEach((id) => {
        if (!inView(topo.pos[id])) delete topo.pos[id];
      });
    }
  } catch (e) {}

  refresh();
  tick();
  setInterval(tick, 1000);
})();
