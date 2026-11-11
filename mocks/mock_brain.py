"""
MOCK of Member C (controller brain). TESTING ONLY -- not part of Member B's submission.

Implements the contract in CONTRACT.md in the dumbest possible way:
  * topology graph from Ryu LLDP events (hop-count shortest paths)
  * server registry fed by JSON-lines over TCP :9000 (register / heartbeat / deregister)
  * heartbeat timeout (3 s) -> server DOWN -> Forwarding.on_server_down(name)
  * round-robin server selection
"""
import importlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import config as cfg  # noqa: E402
import networkx as nx  # noqa: E402

try:
    importlib.import_module("ryu")
    _PKG = "ryu"
except ImportError:  # pragma: no cover
    _PKG = "os_ken"


def _imp(n):
    return importlib.import_module(_PKG + "." + n)


app_manager = _imp("base.app_manager")
_handler = _imp("controller.handler")
set_ev_cls = _handler.set_ev_cls
ofproto_v1_3 = _imp("ofproto.ofproto_v1_3")
hub = _imp("lib.hub")
topo_event = _imp("topology.event")

HB_TIMEOUT = 3.0


class Brain(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(Brain, self).__init__(*args, **kwargs)
        self.G = nx.Graph()
        self.hosts = {}          # ip -> (mac, dpid, port)
        self.servers = {}        # name -> dict
        self.rr = 0
        hub.spawn(self._listen)
        hub.spawn(self._reaper)
        self.logger.info("MOCK brain up (control port %s)", cfg.CTRL_PORT)

    def _fwd(self):
        return app_manager.lookup_service_brick(cfg.FWD_APP)

    def _changed(self):
        f = self._fwd()
        if f is not None:
            f.on_routes_changed()

    # ------------------------------------------------ topology (LLDP)
    @set_ev_cls(topo_event.EventSwitchEnter)
    def _sw_enter(self, ev):
        self.G.add_node(ev.switch.dp.id)

    @set_ev_cls(topo_event.EventSwitchLeave)
    def _sw_leave(self, ev):
        if ev.switch.dp.id in self.G:
            self.G.remove_node(ev.switch.dp.id)
        self._changed()

    @set_ev_cls(topo_event.EventLinkAdd)
    def _link_add(self, ev):
        l = ev.link
        a, b = l.src.dpid, l.dst.dpid
        self.G.add_edge(a, b)
        ports = self.G[a][b].setdefault("ports", {})
        ports[a], ports[b] = l.src.port_no, l.dst.port_no
        self._changed()

    @set_ev_cls(topo_event.EventLinkDelete)
    def _link_del(self, ev):
        l = ev.link
        if self.G.has_edge(l.src.dpid, l.dst.dpid):
            self.G.remove_edge(l.src.dpid, l.dst.dpid)
            self._changed()

    # ------------------------------------------------ contract: B -> C
    def on_host_learned(self, mac, ip, dpid, port):
        self.hosts[ip] = (mac.lower(), dpid, port)
        self.G.add_node(dpid)

    def on_host_removed(self, mac, ip):
        self.hosts.pop(ip, None)

    def on_connection_assigned(self, client_ip, client_port, server_name):
        s = self.servers.get(server_name)
        if s:
            s["assigned"] += 1

    # ------------------------------------------------ contract: C -> B
    def select_server(self, client_ip):
        fwd = self._fwd()
        located = fwd.hosts if fwd else {}
        act = sorted(n for n, s in self.servers.items() if s["state"] == "ACTIVE" and s["ip"] in located)
        if not act:
            return None
        name = act[self.rr % len(act)]
        self.rr += 1
        return {"name": name, "ip": self.servers[name]["ip"]}

    def next_hop_table(self):
        table = {}
        by_edge = {}
        for ip, (mac, dpid, port) in self.hosts.items():
            table[(dpid, mac)] = port
            by_edge.setdefault(dpid, []).append(mac)
        for edge, macs in by_edge.items():
            if edge not in self.G:
                continue
            for u, path in nx.single_source_shortest_path(self.G, edge).items():
                if u == edge:
                    continue
                nh = path[-2]                       # next switch from u toward the edge switch
                port = self.G[u][nh]["ports"][u]
                for mac in macs:
                    table[(u, mac)] = port
        return table

    # ------------------------------------------------ server registry (TCP, JSON lines)
    def _listen(self):
        srv = hub.StreamServer(("0.0.0.0", cfg.CTRL_PORT), self._handle)
        srv.serve_forever()

    def _handle(self, sock, addr):
        buf = b""
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._msg(json.loads(line.decode()), sock)
        except Exception as e:
            self.logger.debug("control conn %s ended: %s", addr, e)
        finally:
            sock.close()

    def _msg(self, m, sock):
        t, name = m.get("type"), m.get("name")
        if t == "register":
            self.servers[name] = {"ip": m["ip"], "last": time.time(), "state": "ACTIVE",
                                  "assigned": 0, "active_conns": 0}
            self.logger.info("server %s registered (%s)", name, m["ip"])
            self._changed()
        elif t == "heartbeat" and name in self.servers:
            s = self.servers[name]
            s["last"] = time.time()
            s["active_conns"] = m.get("active_conns", 0)
            if s["state"] == "DOWN":
                s["state"] = "ACTIVE"
                self.logger.info("server %s is back", name)
        elif t == "deregister" and name in self.servers:
            self.servers[name]["state"] = "DOWN"
            f = self._fwd()
            if f:
                f.on_server_down(name)
        elif t == "cmd" and m.get("action") == "get_status":
            sock.sendall((json.dumps({"servers": self.servers}) + "\n").encode())

    def _reaper(self):
        while True:
            hub.sleep(1)
            now = time.time()
            for name, s in self.servers.items():
                if s["state"] == "ACTIVE" and now - s["last"] > HB_TIMEOUT:
                    s["state"] = "DOWN"
                    self.logger.info("server %s heartbeat timeout -> DOWN", name)
                    f = self._fwd()
                    if f:
                        f.on_server_down(name)
