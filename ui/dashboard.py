#!/usr/bin/env python3
"""
Live READ-ONLY dashboard for the SDN content delivery system.

It asks the controller (TCP 127.0.0.1:9000) for {"type": "state"} once a second and shows:
  * every content server: ACTIVE / DOWN / ..., active connections (load), heartbeat age
  * the controller's selection policy
  * the switch topology (spines on top, leaves below, servers under their leaf); a failed link turns red
  * an event log (server state changes, link failures, policy changes)

It never sends commands, so it cannot break the demo. You keep typing everything in the terminals.

Run (after ./run.sh controller is up):
    python3 ui/dashboard.py              # then open http://localhost:8080
    python3 ui/dashboard.py --port 9090 --controller 127.0.0.1:9000
Standard library only, nothing to install.
"""
import argparse
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HISTORY_LEN = 60
EVENTS_KEPT = 60


class Poller(threading.Thread):
    """Polls the controller and keeps the latest snapshot plus a little history."""

    def __init__(self, host, port, interval=1.0):
        super().__init__(daemon=True)
        self.host, self.port, self.interval = host, port, interval
        self.lock = threading.Lock()
        self.ok = False
        self.error = "waiting for the controller..."
        self.state = {}
        self.events = []
        self.history = {}          # server name -> recent load values
        self.known_links = set()   # every link ever seen
        self.down_links = set()    # links seen before but missing now
        self.prev_servers = {}
        self.prev_policy = None
        self.prev_ok = None

    # ---------------------------------------------------------------- helpers
    def log(self, msg, kind="info"):
        self.events.append({"t": time.strftime("%H:%M:%S"), "msg": msg, "kind": kind})
        del self.events[:-EVENTS_KEPT]

    def fetch(self):
        with socket.create_connection((self.host, self.port), timeout=2) as s:
            s.settimeout(2)
            s.sendall((json.dumps({"type": "state"}) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.decode())

    # ---------------------------------------------------------------- main loop
    def run(self):
        while True:
            try:
                st = self.fetch()
                with self.lock:
                    self.ok, self.error = True, ""
                    if self.prev_ok is False:
                        self.log("controller reachable again", "good")
                    self.prev_ok = True
                    self.update(st)
            except Exception as e:
                with self.lock:
                    self.ok, self.error = False, "controller not reachable (%s)" % e
                    if self.prev_ok is not False:
                        self.log("controller not reachable", "bad")
                    self.prev_ok = False
            time.sleep(self.interval)

    def update(self, st):
        self.state = st
        servers = st.get("servers", {})

        for name, s in servers.items():
            old = self.prev_servers.get(name)
            if old is None:
                self.log("%s registered (%s)" % (name, s.get("state")), "info")
            elif old != s.get("state"):
                kind = "bad" if s.get("state") in ("DOWN", "REMOVED") else \
                       "good" if s.get("state") == "ACTIVE" else "warn"
                self.log("%s: %s -> %s" % (name, old, s.get("state")), kind)
            h = self.history.setdefault(name, [])
            h.append(s.get("load", 0))
            del h[:-HISTORY_LEN]
        for name in set(self.prev_servers) - set(servers):
            self.log("%s removed from the controller" % name, "warn")
        self.prev_servers = {n: s.get("state") for n, s in servers.items()}

        policy = st.get("policy")
        if self.prev_policy is not None and policy != self.prev_policy:
            self.log("policy changed: %s -> %s" % (self.prev_policy, policy), "info")
        self.prev_policy = policy

        cur = set()
        for l in st.get("links", []):
            a, b = l["switch1"], l["switch2"]
            cur.add(tuple(sorted((a, b), key=str)))
        for l in (self.known_links - cur) - self.down_links:
            self.log("link s%s - s%s DOWN" % l, "bad")
            self.down_links.add(l)
        for l in cur & self.down_links:
            self.log("link s%s - s%s restored" % l, "good")
            self.down_links.discard(l)
        self.known_links |= cur

    # ---------------------------------------------------------------- JSON for the page
    def view(self):
        with self.lock:
            now = time.time()
            st = self.state
            servers = {}
            for name, s in st.get("servers", {}).items():
                hb = s.get("last_heartbeat") or 0
                servers[name] = {
                    "ip": s.get("ip"), "state": s.get("state"), "load": s.get("load", 0),
                    "tx_mbps": s.get("tx_mbps"), "switch": s.get("switch"),
                    "age": round(now - hb, 1) if hb else None,
                    "history": list(self.history.get(name, [])),
                }
            util = {}
            for l in st.get("links", []):
                key = tuple(sorted((l["switch1"], l["switch2"]), key=str))
                util[key] = l.get("utilization", 0)
            links = []
            for key in sorted(self.known_links, key=str):
                links.append({"a": key[0], "b": key[1], "up": key not in self.down_links,
                              "util": util.get(key, 0)})
            return {
                "ok": self.ok, "error": self.error, "policy": st.get("policy"),
                "servers": servers, "links": links,
                "hosts": len(st.get("hosts", {})),
                "routes": len(st.get("forwarding_table", {})),
                "events": list(reversed(self.events)),
            }


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SDN Content Delivery - live dashboard</title>
<style>
  :root { --bg:#10141c; --panel:#1a2130; --line:#2c3650; --text:#e6ebf5; --mute:#8d99b3;
          --good:#34c27a; --bad:#ef5b5b; --warn:#e8b23a; --info:#5aa2ff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
  header { display:flex; flex-wrap:wrap; gap:12px 24px; align-items:center;
           padding:14px 22px; border-bottom:1px solid var(--line); background:var(--panel); }
  h1 { font-size:19px; margin:0; font-weight:650; }
  .pill { padding:3px 11px; border-radius:99px; font-size:13px; border:1px solid var(--line); }
  .dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:7px;
         background:var(--bad); vertical-align:middle; }
  .dot.ok { background:var(--good); }
  main { padding:18px 22px; display:grid; gap:18px; max-width:1200px; margin:0 auto; }
  h2 { font-size:13px; letter-spacing:.08em; text-transform:uppercase; color:var(--mute);
       margin:0 0 10px; font-weight:600; }
  .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:12px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:13px 15px;
          border-left:5px solid var(--mute); transition:border-color .25s; }
  .card.ACTIVE { border-left-color:var(--good); }
  .card.DOWN, .card.REMOVED { border-left-color:var(--bad); }
  .card.JOINING, .card.DRAINING { border-left-color:var(--warn); }
  .card .name { font-size:18px; font-weight:650; display:flex; justify-content:space-between; align-items:center; }
  .badge { font-size:12px; padding:2px 9px; border-radius:99px; font-weight:650; color:#0b0f17; background:var(--mute); }
  .ACTIVE .badge { background:var(--good); } .DOWN .badge, .REMOVED .badge { background:var(--bad); }
  .JOINING .badge, .DRAINING .badge { background:var(--warn); }
  .kv { color:var(--mute); font-size:13px; margin-top:3px; }
  .kv b { color:var(--text); font-weight:600; }
  .load { font-size:26px; font-weight:700; margin-top:6px; }
  .load small { font-size:13px; color:var(--mute); font-weight:500; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
  .two { display:grid; grid-template-columns:3fr 2fr; gap:18px; }
  @media (max-width:900px){ .two { grid-template-columns:1fr; } }
  svg text { fill:var(--text); font-size:13px; }
  .legend { color:var(--mute); font-size:12.5px; margin-top:6px; }
  #events { max-height:310px; overflow:auto; font-family:ui-monospace,Consolas,monospace; font-size:13px; }
  .ev { padding:3px 0; border-bottom:1px solid #222b3f; }
  .ev .t { color:var(--mute); margin-right:10px; }
  .ev.good .m { color:var(--good); } .ev.bad .m { color:var(--bad); } .ev.warn .m { color:var(--warn); }
  .empty { color:var(--mute); padding:8px 0; }
  .ubar { display:grid; grid-template-columns:70px 1fr 48px; gap:8px; align-items:center; margin:4px 0; font-size:13px; }
  .ubar .track { height:10px; background:#243049; border-radius:6px; overflow:hidden; }
  .ubar .fill { height:100%; background:var(--info); transition:width .4s, background .4s; }
  .ubar .fill.mid { background:var(--warn); } .ubar .fill.high { background:var(--bad); }
  .ubar .pct { text-align:right; color:var(--mute); }
  footer { color:var(--mute); font-size:12.5px; text-align:center; padding:8px 0 22px; }
</style></head>
<body>
<header>
  <h1>SDN Content Delivery System</h1>
  <span class="pill"><span class="dot" id="dot"></span><span id="conn">connecting...</span></span>
  <span class="pill">policy: <b id="policy">-</b></span>
  <span class="pill">servers: <b id="nsrv">0</b></span>
  <span class="pill">links up: <b id="nlinks">0</b></span>
  <span class="pill">route entries: <b id="nroutes">0</b></span>
</header>
<main>
  <section><h2>Content servers</h2><div class="cards" id="cards"></div></section>
  <div class="two">
    <section class="panel"><h2>Topology</h2><div id="topo"></div>
      <div class="legend">Spine switches on top, leaf switches below, servers under their leaf.
        Red dashed line = failed link.</div>
      <h2 style="margin-top:14px">Link utilization (measured automatically)</h2><div id="util"></div></section>
    <section class="panel"><h2>Event log</h2><div id="events"></div></section>
  </div>
</main>
<footer>Read-only view. The page only asks the controller for its state and never sends commands.</footer>

<script>
const SVGNS = "http://www.w3.org/2000/svg";
function el(tag, attrs, text) {
  const e = document.createElement(tag);
  for (const k in (attrs || {})) e.setAttribute(k, attrs[k]);
  if (text !== undefined) e.textContent = text;
  return e;
}
function sv(tag, attrs, text) {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in (attrs || {})) e.setAttribute(k, attrs[k]);
  if (text !== undefined) e.textContent = text;
  return e;
}
const COLORS = {ACTIVE:"#34c27a", DOWN:"#ef5b5b", REMOVED:"#ef5b5b", JOINING:"#e8b23a", DRAINING:"#e8b23a"};

function spark(vals) {
  const w = 190, h = 26, svg = sv("svg", {viewBox:`0 0 ${w} ${h}`, width:"100%", height:h});
  if (vals.length < 2) return svg;
  const max = Math.max(2, ...vals);
  const pts = vals.map((v, i) => `${(i/(vals.length-1))*w},${h-2-(v/max)*(h-4)}`).join(" ");
  svg.appendChild(sv("polyline", {points:pts, fill:"none", stroke:"#5aa2ff", "stroke-width":"1.8"}));
  return svg;
}

function renderCards(servers) {
  const box = document.getElementById("cards"); box.replaceChildren();
  const names = Object.keys(servers).sort();
  if (!names.length) { box.appendChild(el("div", {class:"empty"}, "No servers registered yet.")); return; }
  for (const n of names) {
    const s = servers[n], c = el("div", {class:"card " + s.state});
    const top = el("div", {class:"name"}); top.appendChild(el("span", {}, n));
    top.appendChild(el("span", {class:"badge"}, s.state)); c.appendChild(top);
    c.appendChild(el("div", {class:"kv"}, s.ip + (s.switch != null ? "  on s" + s.switch : "")));
    const ld = el("div", {class:"load"}, String(s.load)); ld.appendChild(el("small", {}, " active connections"));
    c.appendChild(ld);
    c.appendChild(spark(s.history));
    const hb = s.age == null ? "no heartbeat yet" : s.age + " s ago";
    const k = el("div", {class:"kv"}); k.append("heartbeat: "); k.appendChild(el("b", {}, hb)); c.appendChild(k);
    if (s.tx_mbps != null) {
      const t = el("div", {class:"kv"}); t.append("sending: "); t.appendChild(el("b", {}, s.tx_mbps + " Mbps")); c.appendChild(t);
    }
    box.appendChild(c);
  }
}

function renderTopo(links, servers) {
  const box = document.getElementById("topo"); box.replaceChildren();
  const nodes = new Set(), deg = {};
  for (const l of links) for (const x of [l.a, l.b]) { nodes.add(String(x)); deg[x] = (deg[x] || 0) + 1; }
  if (!nodes.size) { box.appendChild(el("div", {class:"empty"}, "Waiting for switches...")); return; }
  const list = [...nodes].sort((a, b) => Number(a) - Number(b));
  const maxd = Math.max(...list.map(x => deg[x]));
  let top = list.filter(x => deg[x] === maxd), low = list.filter(x => deg[x] !== maxd);
  if (!low.length) { low = top; top = []; }
  const W = 640, H = 300, pos = {};
  const place = (arr, y) => arr.forEach((n, i) => { pos[n] = {x: W*(i+1)/(arr.length+1), y}; });
  place(top, 50); place(low, top.length ? 170 : 90);

  const svg = sv("svg", {viewBox:`0 0 ${W} ${H}`, width:"100%"});
  for (const l of links) {
    const a = pos[String(l.a)], b = pos[String(l.b)]; if (!a || !b) continue;
    const attrs = {x1:a.x, y1:a.y, x2:b.x, y2:b.y};
    if (l.up) { attrs.stroke = "#3f6a8f"; attrs["stroke-width"] = 2 + Math.min(4, l.util / 25); }
    else { attrs.stroke = "#ef5b5b"; attrs["stroke-width"] = 3; attrs["stroke-dasharray"] = "7 5"; }
    svg.appendChild(sv("line", attrs));
  }
  for (const n of list) {
    const p = pos[n], g = sv("g");
    g.appendChild(sv("rect", {x:p.x-26, y:p.y-15, width:52, height:30, rx:7, fill:"#243049", stroke:"#5aa2ff"}));
    g.appendChild(sv("text", {x:p.x, y:p.y+5, "text-anchor":"middle"}, "s" + n));
    svg.appendChild(g);
  }
  // servers hang under their switch
  const byLeaf = {};
  for (const name of Object.keys(servers).sort()) {
    const s = servers[name]; if (s.switch == null) continue;
    (byLeaf[String(s.switch)] = byLeaf[String(s.switch)] || []).push([name, s]);
  }
  for (const leaf in byLeaf) {
    const p = pos[leaf]; if (!p) continue;
    byLeaf[leaf].forEach(([name, s], i) => {
      const y = p.y + 55 + i * 34;
      svg.appendChild(sv("line", {x1:p.x, y1:p.y+15, x2:p.x, y2:y-12, stroke:"#2c3650", "stroke-width":2}));
      svg.appendChild(sv("rect", {x:p.x-34, y:y-13, width:68, height:26, rx:6, fill:COLORS[s.state] || "#8d99b3"}));
      svg.appendChild(sv("text", {x:p.x, y:y+5, "text-anchor":"middle", style:"fill:#0b0f17;font-weight:650"}, name));
    });
  }
  box.appendChild(svg);
}

function renderUtil(links) {
  const box = document.getElementById("util"); box.replaceChildren();
  const up = links.filter(l => l.up).sort((a, b) => b.util - a.util);
  if (!up.length) { box.appendChild(el("div", {class:"empty"}, "No links yet.")); return; }
  const busy = up.filter(l => l.util >= 1);
  if (!busy.length) { box.appendChild(el("div", {class:"empty"}, "All links idle (below 1%). Run `monitor on` at nm> to start measuring.")); return; }
  for (const l of busy.slice(0, 8)) {
    const row = el("div", {class:"ubar"});
    row.appendChild(el("span", {}, "s" + l.a + " - s" + l.b));
    const track = el("div", {class:"track"});
    track.appendChild(el("div", {class:"fill" + (l.util >= 70 ? " high" : l.util >= 40 ? " mid" : ""), style:"width:" + Math.min(100, l.util) + "%"}));
    row.appendChild(track); row.appendChild(el("span", {class:"pct"}, Math.round(l.util) + "%"));
    box.appendChild(row);
  }
}

function renderEvents(events) {
  const box = document.getElementById("events"); box.replaceChildren();
  if (!events.length) { box.appendChild(el("div", {class:"empty"}, "Nothing has happened yet.")); return; }
  for (const e of events) {
    const row = el("div", {class:"ev " + e.kind});
    row.appendChild(el("span", {class:"t"}, e.t)); row.appendChild(el("span", {class:"m"}, e.msg));
    box.appendChild(row);
  }
}

async function tick() {
  try {
    const r = await fetch("/api/state", {cache:"no-store"});
    const d = await r.json();
    document.getElementById("dot").className = "dot" + (d.ok ? " ok" : "");
    document.getElementById("conn").textContent = d.ok ? "controller connected" : (d.error || "controller not reachable");
    document.getElementById("policy").textContent = d.policy || "-";
    document.getElementById("nsrv").textContent = Object.keys(d.servers).length;
    document.getElementById("nlinks").textContent = d.links.filter(l => l.up).length + " / " + d.links.length;
    document.getElementById("nroutes").textContent = d.routes;
    renderCards(d.servers); renderTopo(d.links, d.servers); renderUtil(d.links); renderEvents(d.events);
  } catch (e) {
    document.getElementById("dot").className = "dot";
    document.getElementById("conn").textContent = "dashboard lost its own server";
  }
}
tick(); setInterval(tick, 1000);
</script></body></html>
"""


def make_handler(poller):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                body, ctype = json.dumps(poller.view()).encode(), "application/json"
            elif path in ("/", "/index.html"):
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):   # keep the terminal quiet
            pass
    return Handler


def main():
    ap = argparse.ArgumentParser(description="Read-only live dashboard for the SDN content delivery system")
    ap.add_argument("--port", type=int, default=8080, help="web port (default 8080)")
    ap.add_argument("--controller", default="127.0.0.1:9000", help="controller host:port (default 127.0.0.1:9000)")
    args = ap.parse_args()
    host, _, port = args.controller.partition(":")
    poller = Poller(host, int(port or 9000))
    poller.start()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(poller))
    print("Dashboard running: open http://localhost:%d   (controller %s:%s)" % (args.port, host, port or 9000))
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
